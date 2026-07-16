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
import re
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


# Karantina eşiği — kullanıcının GERÇEK arşivinde ölçüldü, uydurulmadı:
# Tesseract kelime-güveni çöple sağlamı çift tepeyle ayırıyor (çöp medyan 23,
# sağlam medyan 90-91). 60, o iki tepenin arasındaki vadide duruyor.
# Külliyat büyüdükçe yeniden ölçülmeli; bu yüzden her karenin güveni LOGLANIR
# (ateşlemese bile) — tek seferlik bulgu değil, izlenen değişmez.
OCR_CONF_ESIK = 60.0

# Ölçüm M3: bir sayfa "okunamaz" değildi, yalnızca 180° dönmüştü — düzeltilince
# güven 23'ten 91'e çıktı ve Türkçe kusursuz okundu. Bu yüzden karantina SON
# çare: önce onar, sonra ölç, sonra karantinaya al.
# Ölçüm M4: Tesseract'ın kendi yön tespitine (OSD) GÜVENİLMEZ — güveni 11.24
# çıktı ve Türkçe bir belgeye "Script=Cyrillic" dedi. O yüzden OSD'ye sorulmuyor;
# dört açı ölçülüp en iyi medyan seçiliyor. 4x OCR maliyeti yalnızca düşük
# güvenli karelerde ödeniyor.
ACILAR = (0, 90, 180, 270)

# Sessizlik kuplajı (§3). silencedetect eşikleri: -30dB, en az 2 sn. 2 sn,
# konuşma arasındaki nefesi değil, konuşmacının slaytı okumaya bıraktığı
# duraklamayı yakalar. SILENCE_DENSITY: o pencerede kaç kat sık örneklenir.
SILENCE_NOISE = "-30dB"
SILENCE_MIN = 2.0
SILENCE_DENSITY = 3


@dataclass
class Frame:
    ts: float
    path: Path
    text: str
    # Kelime-güveni medyanı. Defterin kanıtı; None = ölçülemedi (kelime yok).
    conf: float | None = None
    # Metin hangi açıda okundu? 0 dışında bir değer, onarımın işe yaradığını söyler.
    rotation: int = 0
    # True ise METİN LLM'E GİTMEZ. Düşük güvenli çöpü gerçek bilgi gibi vermek,
    # ürünün tam olarak suçladığı şey. Kare yine de saklanır: defter "okuyamadım"
    # derken sayfanın görüntüsünü masaya koymalı, kendi ölçümüne de kefil olmadan.
    quarantined: bool = False
    # Bu kare, sesin sustuğu bir pencereden mi geldi? (§3) Öyleyse transkriptin
    # kapsamadığı — ve Whisper'ın uydurmaya en yatkın olduğu — andan geliyor.
    in_silence: bool = False


async def _silences(video: Path) -> list[tuple[float, float]]:
    """Sesin sustuğu pencereler.

    Neden: sessizlik hem Whisper'ın halüsinasyon tetikleyicisi (boş sesten
    "Thank you." ya da uydurma cümleler üretir), hem konuşmacının slaytı
    okumaya bıraktığı an. AYNI saniye. Yani ses en çok yalan söylediği anda
    ekran en çok öğretiyor — o pencerede kameraya daha çok bakmalıyız.

    Bu geçiş bedava değil ama ucuz: -vn ile video hiç çözülmüyor, kare
    yazılmıyor; yalnız ses taranıyor. Düşerse kuplaj kapanır, boru hattı
    çalışmaya devam eder — sessizlik bir iyileştirme, zorunluluk değil.
    """
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-i", str(video), "-vn",
        "-af", f"silencedetect=noise={SILENCE_NOISE}:d={SILENCE_MIN}",
        "-f", "null", "-",
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        print("[frames] silencedetect basarisiz, kuplaj kapali", flush=True)
        return []

    pencereler: list[tuple[float, float]] = []
    baslangic: float | None = None
    for satir in stderr.decode("utf-8", "replace").splitlines():
        bas = re.search(r"silence_start:\s*(-?[\d.]+)", satir)
        if bas:
            baslangic = max(0.0, float(bas.group(1)))
            continue
        son = re.search(r"silence_end:\s*([\d.]+)", satir)
        if son and baslangic is not None:
            pencereler.append((baslangic, float(son.group(1))))
            baslangic = None
    # Video sessizlikle bitiyorsa silence_end hiç gelmez; o pencere kaybolurdu.
    if baslangic is not None:
        pencereler.append((baslangic, float("inf")))
    return pencereler


def _sessizde(ts: float, pencereler: list[tuple[float, float]]) -> bool:
    return any(bas <= ts <= son for bas, son in pencereler)


async def _sample(
    video: Path, raw_dir: Path, silences: list[tuple[float, float]]
) -> list[tuple[float, Path]]:
    """Tek geçişte kare yazar: her yerde SAMPLE_INTERVAL, SESSİZLİKTE daha sık.

    Tek sıralı geçiş, kare başına video içinde atlamaktan çok daha hızlı; bu
    yüzden sık ızgarada örnekleyip sessiz olmayan fazlalıkları ATIYORUZ —
    ikinci bir ffmpeg geçişi videoyu bir kez daha çözerdi.

    Sessizlikte sıklaştırmanın maliyeti düşük: aynı slayt kalmışsa fazladan
    kareler zaten phash dedup'ında düşer. Kazanç, slayt tam o pencerede
    DEĞİŞTİYSE ortaya çıkar — seyrek ızgara onu kaçırırdı.
    """
    raw_dir.mkdir(parents=True, exist_ok=True)
    sik = SAMPLE_INTERVAL / SILENCE_DENSITY if silences else SAMPLE_INTERVAL

    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-nostdin", "-y", "-i", str(video),
        "-vf", f"fps=1/{sik}", "-q:v", "3",
        str(raw_dir / "s_%05d.jpg"),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"ffmpeg kare örnekleme başarısız:\n{stderr.decode('utf-8', 'replace')[-1500:]}"
        )

    # fps filtresi ilk kareyi t=0'da, sonrakileri interval aralıklarla verir.
    tumu = [(i * sik, p) for i, p in enumerate(sorted(raw_dir.glob("s_*.jpg")))]
    if not silences:
        return tumu

    tutulan: list[tuple[float, Path]] = []
    for ts, path in tumu:
        # Ya taban ızgarada ya sessizlik penceresinde; gerisi silinir.
        izgarada = abs(ts % SAMPLE_INTERVAL) < sik / 2 or (
            SAMPLE_INTERVAL - abs(ts % SAMPLE_INTERVAL) < sik / 2
        )
        if izgarada or _sessizde(ts, silences):
            tutulan.append((ts, path))
        else:
            path.unlink(missing_ok=True)

    print(
        f"[frames] {len(silences)} sessiz pencere -> sessizlikte {SILENCE_DENSITY}x "
        f"siklastirildi ({len(tumu)} ornek -> {len(tutulan)} tutuldu)",
        flush=True,
    )
    return tutulan


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


def _oku(img: Image.Image) -> tuple[str, float | None]:
    """Tek okuma: metin + kelime-güveni medyanı.

    image_to_string yerine image_to_data: güven olmadan çöple sağlamı ayıramayız.
    Karakter saymak bunu YAPAMAZ — ölçüldü: çöp bol karakter üretir, uzunluk
    sinyal değil. Güven ise çift tepe veriyor (23 vs 91).
    """
    d = pytesseract.image_to_data(
        img, lang=OCR_LANGS, output_type=pytesseract.Output.DICT
    )
    kelimeler: list[str] = []
    guvenler: list[float] = []
    for kelime, guven in zip(d["text"], d["conf"]):
        g = float(guven)
        # -1 = kelime değil (blok/satır kaydı), atılır.
        if kelime.strip() and g >= 0:
            kelimeler.append(kelime.strip())
            guvenler.append(g)

    if not guvenler:
        return "", None
    guvenler.sort()
    orta = len(guvenler) // 2
    medyan = (
        guvenler[orta]
        if len(guvenler) % 2
        else (guvenler[orta - 1] + guvenler[orta]) / 2
    )
    return " ".join(kelimeler), medyan


def _en_iyi_aci(img: Image.Image) -> tuple[str, float | None, int]:
    """Düşük güvenli kareyi karantinaya atmadan ÖNCE döndürmeyi dener.

    Ölçüm: ters duran bir sayfa 23 → 91 güvene çıktı, metin kusursuzdu. OSD'ye
    sormuyoruz (M4: güven 11.24, Türkçe'ye "Cyrillic" dedi); dört açıyı ölçüp en
    iyi medyanı seçiyoruz — onarımın başarısının ÖLÇÜLEBİLİR sinyali bu.

    Denoise/upscale/PSM taraması bilerek YOK: hangi çıktının daha doğru olduğunu
    söyleyen ground-truth'suz bir sinyal olmadığı için durma kuralı yazılamıyor.
    """
    en_iyi = ("", None, 0)
    for aci in ACILAR:
        dondurulmus = img if aci == 0 else img.rotate(-aci, expand=True)
        metin, medyan = _oku(dondurulmus)
        if medyan is not None and (en_iyi[1] is None or medyan > en_iyi[1]):
            en_iyi = (metin, medyan, aci)
    return en_iyi


def _ocr(
    samples: list[tuple[float, Path]],
    out_dir: Path,
    silences: list[tuple[float, float]] | None = None,
) -> list[Frame]:
    out_dir.mkdir(parents=True, exist_ok=True)
    silences = silences or []
    frames: list[Frame] = []

    for index, (ts, src) in enumerate(samples):
        try:
            with Image.open(src) as img:
                text, conf = _oku(img)
                rotation = 0
                # ÖNCE ONAR, sonra ölç, sonra karantina. Karantina ilk refleks
                # olsaydı yalnızca ters duran sağlam bir sayfayı atardık.
                if conf is not None and conf < OCR_CONF_ESIK:
                    text, conf, rotation = _en_iyi_aci(img)
        except Exception as exc:
            print(f"[frames] OCR başarısız ({src.name}): {exc}", flush=True)
            continue

        cleaned = "\n".join(line.strip() for line in text.splitlines() if line.strip())

        # Metinsiz kare = slayt değil, kamera görüntüsü. Özete katkısı yok ve
        # okunamamış da değil — karantina değil, eleme.
        if conf is None or len(cleaned) < MIN_OCR_CHARS:
            continue

        # Onarım da kurtaramadıysa: metin LLM'e GİTMEZ ama kare saklanır.
        # Sessizce atmak, defterin "okuyamadığını" gizlemesi olurdu.
        karantina = conf < OCR_CONF_ESIK

        # Ateşlemese bile her karenin güveni loglanır: eşik tek seferlik bir
        # bulgudan değil, biriken ölçümden gelmeli.
        print(
            f"[frames] {int(ts):>5}s guven={conf:5.1f} aci={rotation:>3} "
            f"{'KARANTINA' if karantina else 'ok'} ({len(cleaned)} karakter)",
            flush=True,
        )

        # index olmadan, aynı saniyeye düşen iki kare birbirinin üstüne yazardı.
        dst = out_dir / f"frame_{index:04d}_{int(ts):06d}.jpg"
        shutil.copy2(src, dst)
        frames.append(
            Frame(
                ts=ts,
                path=dst,
                text="" if karantina else cleaned,
                conf=round(conf, 1),
                rotation=rotation,
                quarantined=karantina,
                in_silence=_sessizde(ts, silences),
            )
        )
    return frames


def _cap(frames: list[Frame]) -> list[Frame]:
    """Çok fazla slayt varsa eşit aralıklarla seyrelt — ilk N'i almak videonun
    sonunu tamamen kör bırakırdı.

    Sessizlik kuplajı (§3): eşit aralık ızgarası sessiz bir kareye denk
    gelmiyorsa, komşusu yerine SESSİZ olanı al. Sesin sustuğu pencere,
    transkriptin kapsamadığı tek yer — seyreltmede en son atılacak şey o.
    """
    if len(frames) <= MAX_FRAMES:
        return frames

    step = len(frames) / MAX_FRAMES
    kept: list[Frame] = []
    secilen: set[int] = set()
    for i in range(MAX_FRAMES):
        idx = int(i * step)
        # Bu adımın penceresi içinde sessiz bir kare varsa onu tercih et.
        pencere = range(idx, min(len(frames), int((i + 1) * step)))
        sessiz = [k for k in pencere if frames[k].in_silence and k not in secilen]
        secim = sessiz[0] if sessiz else idx
        if secim in secilen:
            continue
        secilen.add(secim)
        kept.append(frames[secim])

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


def _process_sync(
    samples: list[tuple[float, Path]],
    out_dir: Path,
    silences: list[tuple[float, float]],
) -> list[Frame]:
    unique = _dedupe(samples)
    frames = _cap(_ocr(unique, out_dir, silences))
    # Log'da ASCII kalın: Windows konsolu cp1254 ve "→" gibi karakterlerde
    # UnicodeEncodeError atıp worker'ı düşürüyor.
    print(
        f"[frames] {len(samples)} ornek -> {len(unique)} benzersiz ekran -> "
        f"{len(frames)} slayt ({sum(1 for f in frames if f.in_silence)} sessizlikten, "
        f"{sum(1 for f in frames if f.quarantined)} karantinada)",
        flush=True,
    )
    return frames


async def extract(video: Path, duration: float, out_dir: Path) -> list[Frame]:
    raw_dir = out_dir / "_raw"
    try:
        # Sessizlik ÖNCE ölçülür: örnekleme sıklığı ona göre ayarlanacak (§3).
        # Düşerse boş liste döner ve kuplaj sessizce kapanır — sessizlik bir
        # iyileştirme, boru hattının koşulu değil.
        silences = await _silences(video)
        samples = await _sample(video, raw_dir, silences)
        if not samples:
            return []
        # phash + OCR bloklayıcı ve CPU-bağlı — event loop'u tıkamasın.
        return await asyncio.to_thread(_process_sync, samples, out_dir, silences)
    finally:
        shutil.rmtree(raw_dir, ignore_errors=True)


def for_range(frames: list[Frame], start: float, end: float) -> list[Frame]:
    return [f for f in frames if start <= f.ts < end]


def as_prompt_text(frames: list[Frame]) -> str:
    """Yalnızca GÜVENİLİR ekran metni prompt'a girer.

    Karantinalı kare buradan geçmez: düşük güvenli çöpü gerçek bilgi gibi
    vermek, ürünün suçladığı arızanın ta kendisi olurdu. Kare kaybolmuyor —
    defter onu kanıtıyla gösteriyor (bkz. Frame.quarantined).
    """
    from .transcribe import fmt_ts

    return "\n\n".join(
        f"[{fmt_ts(f.ts)} ekran]\n{f.text}"
        for f in frames
        if not f.quarantined and f.text
    )
