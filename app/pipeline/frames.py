"""Görsel katman: slayt yakalama + OCR.

Neden gerekli: konuşmacı slaytta yazan her şeyi söylemez. Sadece sesten üretilen
özet, ekranda 20 saniye duran bir tanımı ya da komut satırını tamamen kaçırır.
Bu modül ekranı okur ve metnini özetleyiciye verir.

Akış:
  sabit aralıkla örnekle (tek ffmpeg geçişi)
  → algısal hash ile tekrar eleme (aynı slaytın kareleri + konuşan kafa burada düşer)
  → OCR → metinsiz kareleri at (slayt değil, kamera görüntüsü)

Neden sahne tespiti değil: PySceneDetect'in tüm dedektörleri (Content, Adaptive,
Histogram, Hash) bir slayt destesinde ÖLÇÜLDÜ ve sıfır kesme buldu — slaytta ekranın
büyük kısmı sabit kalıp yalnızca metin değişiyor, fark eşiğin altında kalıyor.
Kontrol deneyi (siyah→beyaz) düzgün çalıştı, yani araç değil kullanım alanı yanlıştı:
sahne dedektörleri sinema kurgusu için tasarlanmış. Aynı slaytlarda phash ölçümü
temiz ayrım verdi (aynı slayt = 0, farklı slayt = 6-8), bu yüzden ayrım tamamen
phash'e bırakıldı.
"""

import asyncio
import shutil
from dataclasses import dataclass
from pathlib import Path

import imagehash
import pytesseract
from PIL import Image

from ..config import (
    MAX_FRAMES,
    MIN_OCR_CHARS,
    OCR_LANGS,
    PHASH_DISTANCE,
    SAMPLE_INTERVAL,
    TESSERACT_CMD,
)

if TESSERACT_CMD:
    pytesseract.pytesseract.tesseract_cmd = TESSERACT_CMD


def check_ocr_langs() -> tuple[bool, str]:
    """OCR_LANGS'teki her dilin gerçekten kurulu olduğunu doğrular.

    Neden gerekli: dil paketi eksikse Tesseract SESSİZCE İngilizce'ye düşüyor —
    hata yok, log yok. Türkçe slaytlar bozuk okunur, o bozuk metin özete ve
    eleştirmene akar, kimse fark etmez. Sessiz bozulma yerine yüksek sesle söyle.
    """
    wanted = [p for p in OCR_LANGS.split("+") if p]
    try:
        have = set(pytesseract.get_languages(config=""))
    except Exception as exc:
        return False, f"tesseract calistirilamadi: {exc}"

    missing = [lang for lang in wanted if lang not in have]
    if missing:
        return False, (
            f"OCR dil paketi EKSIK: {', '.join(missing)} "
            f"(kurulu: {', '.join(sorted(have))}). Tesseract sessizce Ingilizce'ye "
            f"duser ve Turkce slaytlar bozuk okunur. TESSDATA_PREFIX'i kontrol edin."
        )
    return True, f"OCR dilleri hazir: {', '.join(wanted)}"


@dataclass
class Frame:
    ts: float
    path: Path
    text: str


async def _sample(video: Path, raw_dir: Path) -> list[tuple[float, Path]]:
    """Videoyu tek geçişte tarayıp SAMPLE_INTERVAL saniyede bir kare yazar.

    Tek sıralı geçiş, kare başına video içinde atlamaktan çok daha hızlı.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-y", "-i", str(video),
        "-vf", f"fps=1/{SAMPLE_INTERVAL}", "-q:v", "3",
        str(raw_dir / "s_%05d.jpg"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg kare örnekleme başarısız:\n{stderr.decode('utf-8', 'replace')[-1500:]}"
        )

    files = sorted(raw_dir.glob("s_*.jpg"))
    # fps filtresi ilk kareyi t=0'da, sonrakileri interval aralıklarla verir.
    return [(i * SAMPLE_INTERVAL, p) for i, p in enumerate(files)]


def _dedupe(samples: list[tuple[float, Path]]) -> list[tuple[float, Path]]:
    """Algısal hash ile tekrarı ele.

    Aynı slayt ekranda 30 sn durduysa 6 örnek alınır ama hash'leri aynıdır → biri
    kalır. Konuşan kafa çekimleri de kareden kareye çok az değişir → elenir.
    Slayt değişimi hash'i belirgin değiştirir → yeni kare olarak kalır.
    """
    kept: list[tuple[float, Path]] = []
    hashes: list[imagehash.ImageHash] = []

    for ts, path in samples:
        with Image.open(path) as img:
            h = imagehash.phash(img)
        if any(h - prev <= PHASH_DISTANCE for prev in hashes):
            continue
        hashes.append(h)
        kept.append((ts, path))
    return kept


def _ocr(samples: list[tuple[float, Path]], out_dir: Path) -> list[Frame]:
    out_dir.mkdir(parents=True, exist_ok=True)
    frames: list[Frame] = []

    for index, (ts, src) in enumerate(samples):
        try:
            with Image.open(src) as img:
                text = pytesseract.image_to_string(img, lang=OCR_LANGS)
        except Exception as exc:
            print(f"[frames] OCR başarısız ({src.name}): {exc}", flush=True)
            continue

        cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())
        # Metni olmayan kare = slayt değil, kamera görüntüsü. Özete katkısı yok.
        if len(cleaned) < MIN_OCR_CHARS:
            continue

        # index olmadan, aynı saniyeye düşen iki kare birbirinin üstüne yazardı.
        dst = out_dir / f"frame_{index:04d}_{int(ts):06d}.jpg"
        shutil.copy2(src, dst)
        frames.append(Frame(ts=ts, path=dst, text=cleaned))
    return frames


def _cap(frames: list[Frame]) -> list[Frame]:
    """Çok fazla slayt varsa eşit aralıklarla seyrelt — ilk N'i almak videonun
    sonunu tamamen kör bırakırdı."""
    if len(frames) <= MAX_FRAMES:
        return frames

    step = len(frames) / MAX_FRAMES
    kept = [frames[int(i * step)] for i in range(MAX_FRAMES)]

    keep_paths = {f.path for f in kept}
    for frame in frames:
        if frame.path not in keep_paths:
            frame.path.unlink(missing_ok=True)

    print(
        f"[frames] {len(frames)} slayt bulundu, MAX_FRAMES={MAX_FRAMES} sınırına "
        f"seyreltildi ({len(frames) - len(kept)} kare atıldı)",
        flush=True,
    )
    return kept


def _process_sync(samples: list[tuple[float, Path]], out_dir: Path) -> list[Frame]:
    unique = _dedupe(samples)
    frames = _cap(_ocr(unique, out_dir))
    # Log'da ASCII kalın: Windows konsolu cp1254 ve "→" gibi karakterlerde
    # UnicodeEncodeError atıp worker'ı düşürüyor.
    print(
        f"[frames] {len(samples)} ornek -> {len(unique)} benzersiz ekran -> "
        f"{len(frames)} slayt (metinsizler elendi)",
        flush=True,
    )
    return frames


async def extract(video: Path, duration: float, out_dir: Path) -> list[Frame]:
    raw_dir = out_dir / "_raw"
    try:
        samples = await _sample(video, raw_dir)
        if not samples:
            return []
        # phash + OCR bloklayıcı ve CPU-bağlı — event loop'u tıkamasın.
        return await asyncio.to_thread(_process_sync, samples, out_dir)
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)


def for_range(frames: list[Frame], start: float, end: float) -> list[Frame]:
    return [f for f in frames if start <= f.ts < end]


def as_prompt_text(frames: list[Frame]) -> str:
    from .transcribe import fmt_ts

    return "\n\n".join(f"[{fmt_ts(f.ts)} ekran]\n{f.text}" for f in frames)
