"""Groq'un ücretsiz Whisper uç noktasıyla zaman damgalı transkript.

Ses, istek başına boyut limitine takılmamak için parçalara bölünür; her parçanın
gerçek süresi ölçülüp zaman damgaları global zaman eksenine kaydırılır.
"""

import asyncio
import importlib.util
import json
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import (
    CHUNK_SECONDS,
    GROQ_API_KEY,
    GROQ_API_KEYS,
    GROQ_BASE_URL,
    GROQ_TRANSCRIBE_MODEL,
    LOCAL_WHISPER,
    LOCAL_WHISPER_MODEL,
    ON_VERCEL,
    TRANSCRIBE_CONCURRENCY,
    TRANSCRIBE_LANGUAGE,
)


@dataclass
class Segment:
    start: float
    end: float
    text: str


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"{cmd[0]} başarısız:\n{stderr.decode('utf-8', 'replace')[-2000:]}"
        )
    return stdout.decode("utf-8", "replace")


async def _split(audio: Path, work: Path) -> list[tuple[Path, float]]:
    """Sesi parçalara böler ve her parçanın GLOBAL başlangıç zamanını döndürür.

    Başlangıçları ffmpeg'in kendisine yazdırıyoruz (-segment_list). Parçaları
    sonradan ffprobe ile ölçmek çalışmıyor: segment muxer'ın yazdığı FLAC'larda
    format.duration alanı bulunmuyor ve ölçüm KeyError ile çöküyordu — üstelik
    bu yalnızca çok parçalı işlerde, yani 10 dakikadan uzun her videoda oluyordu.
    """
    # Ses dosyasına ÖZEL klasör: uzun videoda parçalar PARALEL transkript edilir ve
    # hepsi aynı "chunks/chunk_0000.flac" adlarına yazınca birbirinin sesini
    # eziyordu — bir parçanın transkriptine başka parçanın konuşması karışabilirdi.
    chunk_dir = work / "chunks" / audio.stem
    chunk_dir.mkdir(parents=True, exist_ok=True)
    listfile = chunk_dir / "segments.csv"

    await _run(
        "ffmpeg", "-nostdin", "-y", "-i", str(audio),
        "-f", "segment", "-segment_time", str(CHUNK_SECONDS),
        "-segment_list", str(listfile), "-segment_list_type", "csv",
        "-c:a", "flac", "-ac", "1", "-ar", "16000",
        str(chunk_dir / "chunk_%04d.flac"),
    )

    chunks = sorted(chunk_dir.glob("chunk_*.flac"))
    if not chunks:
        raise RuntimeError("Ses parçalara bölünemedi.")

    starts = _parse_segment_list(listfile, chunk_dir)
    if len(starts) != len(chunks):
        # Liste okunamadıysa nominal aralığa düş: segment_time yaklaşık tutar.
        print(
            f"[transcribe] segment listesi eşleşmedi "
            f"({len(starts)} kayıt / {len(chunks)} parça), nominal offset kullanılıyor",
            flush=True,
        )
        return [(c, i * CHUNK_SECONDS) for i, c in enumerate(chunks)]
    return starts


def _parse_segment_list(listfile: Path, chunk_dir: Path) -> list[tuple[Path, float]]:
    """CSV satırları: dosyaadi,baslangic,bitis"""
    if not listfile.exists():
        return []
    out: list[tuple[Path, float]] = []
    for line in listfile.read_text(encoding="utf-8").splitlines():
        parts = line.strip().split(",")
        if len(parts) < 3:
            continue
        try:
            out.append((chunk_dir / Path(parts[0]).name, float(parts[1])))
        except ValueError:
            continue
    return out


def _groq_keys() -> list[str]:
    """Kullanılabilir Groq anahtarları (çok-anahtar; tek anahtara geriye uyumlu)."""
    return GROQ_API_KEYS or ([GROQ_API_KEY] if GROQ_API_KEY else [])


class _GroqDustu(RuntimeError):
    """Groq bu parçayı veremedi — yerel yedeğe düşülebilir."""


# --- Yerel yedek (faster-whisper) ------------------------------------------------
# Groq düşünce (kota, 5xx, geçersiz anahtar) iş ÖLMESİN: ev bilgisayarında aynı
# parça yerel Whisper ile yazılır. Yavaş (CPU) ama iş her zaman biter; eskiden
# 10 deneme + dakikalarca bekleme sonrası tüm iş hata veriyordu (israf).
# Vercel'de yok (paket/CPU yok) — orada eski davranış.
_yerel_model = None
_yerel_kilit = threading.Lock()


def yerel_var() -> bool:
    if ON_VERCEL or not LOCAL_WHISPER:
        return False
    return importlib.util.find_spec("faster_whisper") is not None


def _yerel_sync(path: Path) -> list[Segment]:
    global _yerel_model
    from faster_whisper import BatchedInferencePipeline, WhisperModel

    # Tek model, aynı anda tek transkript: batched çıkarım zaten tüm çekirdekleri
    # kullanıyor; paralel ikinci geçiş yalnız yavaşlatır.
    with _yerel_kilit:
        if _yerel_model is None:
            print(f"[transcribe] yerel model yukleniyor ({LOCAL_WHISPER_MODEL})", flush=True)
            _yerel_model = WhisperModel(LOCAL_WHISPER_MODEL, device="cpu", compute_type="int8")
        segs, _info = BatchedInferencePipeline(model=_yerel_model).transcribe(
            str(path), batch_size=8, language=TRANSCRIBE_LANGUAGE, vad_filter=True
        )
        return [Segment(float(s.start), float(s.end), s.text.strip())
                for s in segs if s.text.strip()]


async def _yerel(path: Path) -> list[Segment]:
    return await asyncio.to_thread(_yerel_sync, path)


async def _transcribe_chunk(
    client: httpx.AsyncClient, path: Path, idx: int
) -> list[Segment]:
    """Groq ile yaz; olmazsa (ve ev bilgisayarındaysak) yerel yedeğe düş."""
    if not _groq_keys():
        return await _yerel(path)
    try:
        return await _groq_chunk(client, path, idx)
    except _GroqDustu as exc:
        if not yerel_var():
            raise RuntimeError(str(exc)) from None
        print(f"[transcribe] {path.name}: {exc} -> YEREL yedek", flush=True)
        return await _yerel(path)


async def _groq_chunk(
    client: httpx.AsyncClient, path: Path, idx: int
) -> list[Segment]:
    data = {
        "model": GROQ_TRANSCRIBE_MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }
    if TRANSCRIBE_LANGUAGE:
        data["language"] = TRANSCRIBE_LANGUAGE

    # ÇOK ANAHTAR: parçalar farklı anahtarlara dağılır (idx offset'i), bir parça
    # 429/5xx alınca SIRADAKI anahtara döner — böylece tek anahtar rate-limit'e
    # takılınca koca iş retry'de dakikalarca asılmaz (büyük yüklemede yaşandı).
    # Bekleme yalnızca TÜM anahtarları bir tur denedikten sonra yapılır; tek
    # anahtarda davranış eskisiyle aynı (her denemede backoff).
    keys = _groq_keys()
    nkey = max(1, len(keys))
    yedek = yerel_var()
    # Yerel yedek varsa Groq'u uzun uzun bekleme: 4 denemede olmadıysa yerele geç.
    deneme = 4 if yedek else 10
    last_error: Exception | None = None
    for attempt in range(deneme):
        key = keys[(idx + attempt) % nkey]
        cycled = (attempt + 1) % nkey == 0  # tüm anahtarlar bir tur denendi mi
        try:
            with path.open("rb") as fh:
                resp = await client.post(
                    f"{GROQ_BASE_URL}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {key}"},
                    files={"file": (path.name, fh, "audio/flac")},
                    data=data,
                )
            if resp.status_code in (401, 403):
                raise _GroqDustu(f"Groq anahtarı geçersiz (HTTP {resp.status_code})")
            if resp.status_code == 429:
                last_error = RuntimeError("Groq 429 (rate limit)")
                if cycled:
                    wait = float(resp.headers.get("retry-after", 2 ** (attempt // nkey)))
                    if wait > 60 and yedek:
                        # Saatlik/günlük ses kotası doldu: beklemek dakikalar sürer.
                        raise _GroqDustu(f"Groq ses kotası doldu ({int(wait)} sn bekleme)")
                    await asyncio.sleep(min(wait, 60))
                continue
            if 400 <= resp.status_code < 500:
                raise _GroqDustu(f"Groq {resp.status_code}: {resp.text[:160]}")
            if resp.status_code >= 500:
                # Groq'un kendi sorunu; bizim istekte düzeltilecek bir şey yok.
                last_error = RuntimeError(f"Groq {resp.status_code}")
                if cycled:
                    bekle = min(2 ** (attempt // nkey), 60)
                    print(
                        f"[transcribe] {path.name}: Groq {resp.status_code} (gecici), "
                        f"{bekle} sn sonra yeniden ({attempt + 1}/{deneme})",
                        flush=True,
                    )
                    await asyncio.sleep(bekle)
                continue
            resp.raise_for_status()
            payload = resp.json()
            return [
                Segment(float(s["start"]), float(s["end"]), s["text"].strip())
                for s in payload.get("segments", [])
                if s.get("text", "").strip()
            ]
        except httpx.HTTPError as exc:
            last_error = exc
            if cycled:
                await asyncio.sleep(min(2 ** (attempt // nkey), 60))
    raise _GroqDustu(
        f"Groq transkripsiyonu başarısız ({path.name}), {deneme} deneme: {last_error}"
    )


def _onbellek_yolu(audio: Path) -> Path:
    return audio.with_name(audio.name + ".segs.json")


def _onbellekten(audio: Path) -> list[Segment] | None:
    """Aynı ses için daha önce çıkarılmış transkript (tekrar denemede yeniden kullan:
    iş özet adımında düşse bile transkript yeniden ÜCRET/SÜRE harcamasın)."""
    yol = _onbellek_yolu(audio)
    try:
        veri = json.loads(yol.read_text(encoding="utf-8"))
        if veri.get("boyut") != audio.stat().st_size:
            return None
        return [Segment(s["start"], s["end"], s["text"]) for s in veri["segs"]]
    except (OSError, ValueError, KeyError):
        return None


def kullanilabilir() -> bool:
    """Bu makinede transkript üretilebilir mi (Groq anahtarı ya da yerel Whisper)?
    Ön kontrol bunu indirmeden ÖNCE sorar."""
    return bool(_groq_keys()) or yerel_var()


async def transcribe(
    audio: Path, work: Path, ilerleme: Callable[[int, int], None] | None = None
) -> list[Segment]:
    """ilerleme(biten, toplam): her ses parçası yazıldıkça (arayüz 'transkript 3/7')."""
    hazir = _onbellekten(audio)
    if hazir:
        print(f"[transcribe] {audio.name}: onbellekten ({len(hazir)} segment)", flush=True)
        return hazir
    segs = await _transcribe(audio, work, ilerleme)
    try:
        _onbellek_yolu(audio).write_text(json.dumps({
            "boyut": audio.stat().st_size,
            "segs": [{"start": s.start, "end": s.end, "text": s.text} for s in segs],
        }, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass
    return segs


async def _transcribe(
    audio: Path, work: Path, ilerleme: Callable[[int, int], None] | None = None
) -> list[Segment]:
    if not kullanilabilir():
        raise RuntimeError("GROQ_API_KEY tanımlı değil (ve yerel Whisper yok).")

    # Zaman damgaları parça-yerel gelir; offset ile global eksene kaydırılır.
    chunks = await _split(audio, work)

    # Anahtar sayısı kadar (en az TRANSCRIBE_CONCURRENCY) eşzamanlılık: iki anahtar
    # varsa iki parça aynı anda ayrı anahtarlarda işlensin, cascade gibi.
    esz = max(TRANSCRIBE_CONCURRENCY, len(_groq_keys()))
    sem = asyncio.Semaphore(esz)
    limits = httpx.Limits(max_connections=esz)

    biten = [0]

    async with httpx.AsyncClient(timeout=300, limits=limits) as client:
        async def one(idx: int, chunk: Path) -> list[Segment]:
            async with sem:
                segs = await _transcribe_chunk(client, chunk, idx)
            biten[0] += 1
            if ilerleme:
                try:
                    ilerleme(biten[0], len(chunks))
                except Exception:
                    pass
            return segs

        results = await asyncio.gather(
            *(one(i, c) for i, (c, _) in enumerate(chunks))
        )

    segments: list[Segment] = []
    for (_, offset), chunk_segments in zip(chunks, results):
        for seg in chunk_segments:
            segments.append(
                Segment(seg.start + offset, seg.end + offset, seg.text)
            )
    if not segments:
        raise RuntimeError("Transkript boş döndü — ses kanalı sessiz olabilir.")
    return segments


def to_timestamped_text(segments: list[Segment]) -> str:
    return "\n".join(f"[{fmt_ts(s.start)}] {s.text}" for s in segments)


def fmt_ts(seconds: float) -> str:
    total = int(seconds)
    h, m, s = total // 3600, (total % 3600) // 60, total % 60
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
