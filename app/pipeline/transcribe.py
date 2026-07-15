"""Groq'un ücretsiz Whisper uç noktasıyla zaman damgalı transkript.

Ses, istek başına boyut limitine takılmamak için parçalara bölünür; her parçanın
gerçek süresi ölçülüp zaman damgaları global zaman eksenine kaydırılır.
"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..config import (
    CHUNK_SECONDS,
    GROQ_API_KEY,
    GROQ_BASE_URL,
    GROQ_TRANSCRIBE_MODEL,
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
    chunk_dir = work / "chunks"
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


async def _transcribe_chunk(client: httpx.AsyncClient, path: Path) -> list[Segment]:
    data = {
        "model": GROQ_TRANSCRIBE_MODEL,
        "response_format": "verbose_json",
        "timestamp_granularities[]": "segment",
    }
    if TRANSCRIBE_LANGUAGE:
        data["language"] = TRANSCRIBE_LANGUAGE

    last_error: Exception | None = None
    for attempt in range(5):
        try:
            with path.open("rb") as fh:
                resp = await client.post(
                    f"{GROQ_BASE_URL}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                    files={"file": (path.name, fh, "audio/flac")},
                    data=data,
                )
            if resp.status_code == 429:
                wait = float(resp.headers.get("retry-after", 2 ** attempt))
                await asyncio.sleep(min(wait, 60))
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
            await asyncio.sleep(2 ** attempt)
    raise RuntimeError(f"Groq transkripsiyonu başarısız ({path.name}): {last_error}")


async def transcribe(audio: Path, work: Path) -> list[Segment]:
    if not GROQ_API_KEY:
        raise RuntimeError("GROQ_API_KEY tanımlı değil.")

    # Zaman damgaları parça-yerel gelir; offset ile global eksene kaydırılır.
    chunks = await _split(audio, work)

    sem = asyncio.Semaphore(TRANSCRIBE_CONCURRENCY)
    limits = httpx.Limits(max_connections=TRANSCRIBE_CONCURRENCY)

    async with httpx.AsyncClient(timeout=300, limits=limits) as client:
        async def one(chunk: Path) -> list[Segment]:
            async with sem:
                return await _transcribe_chunk(client, chunk)

        results = await asyncio.gather(*(one(c) for c, _ in chunks))

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
