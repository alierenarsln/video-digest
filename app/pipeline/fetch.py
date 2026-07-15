"""Kaynak ne olursa olsun (YouTube linki / yerel dosya) tek bir sözleşmeye indirger:
16 kHz mono WAV + (varsa) video + metadata. Boru hattının geri kalanı kaynağı bilmez.

Video, görsel katman (slayt/OCR) için gerekir. Yalnızca sesten oluşan kaynaklarda
(meeting kaydı, podcast) video_path None olur ve görsel katman kendiliğinden atlanır.
"""

import asyncio
import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path

from ..config import ENABLE_FRAMES, VIDEO_MAX_HEIGHT
from . import subtitles
from .transcribe import Segment


@dataclass
class Source:
    audio_path: Path
    title: str
    duration: float
    video_path: Path | None = None
    # Hazır altyazı bulunduysa transkript adımı tamamen atlanır.
    subtitles: list[Segment] | None = None
    meta: dict = field(default_factory=dict)


async def _run(*cmd: str) -> str:
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = stderr.decode("utf-8", "replace")[-2000:]
        raise RuntimeError(f"{cmd[0]} başarısız (kod {proc.returncode}):\n{tail}")
    return stdout.decode("utf-8", "replace")


async def _to_wav(src: Path, dst: Path) -> None:
    # 16 kHz mono: Whisper'ın beklediği format; dosyayı da küçültür.
    await _run(
        "ffmpeg", "-nostdin", "-y", "-i", str(src),
        "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
        str(dst),
    )


async def _probe_duration(path: Path) -> float:
    out = await _run(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "json", str(path),
    )
    return float(json.loads(out)["format"]["duration"])


async def _has_stream(path: Path, kind: str) -> bool:
    out = await _run(
        "ffprobe", "-v", "error", "-select_streams", f"{kind}:0",
        "-show_entries", "stream=codec_type", "-of", "json", str(path),
    )
    return bool(json.loads(out).get("streams"))


async def _require_audio(path: Path) -> None:
    """Sessiz videoda ffmpeg 40 satırlık anlaşılmaz bir hata veriyor; kullanıcıya
    ne olduğunu söyleyelim."""
    if not await _has_stream(path, "a"):
        raise RuntimeError(
            "Kaynakta ses akışı yok — transkript çıkarılamaz. "
            "(Sessiz video ya da yalnızca görüntü içeren dosya.)"
        )


def _find_download(work: Path) -> Path:
    candidates = [
        p for p in work.glob("download.*")
        if p.suffix.lower() not in {".json", ".part", ".ytdl"}
    ]
    if not candidates:
        raise RuntimeError("yt-dlp bir medya dosyası üretmedi.")
    return max(candidates, key=lambda p: p.stat().st_size)


async def from_url(url: str, work: Path) -> Source:
    info = json.loads(
        await _run("yt-dlp", "--dump-single-json", "--no-playlist", "--no-warnings", url)
    )

    if ENABLE_FRAMES:
        # Slayt OCR'ı için görüntü lazım; çözünürlüğü sınırlıyoruz — 720p
        # slayt metnini okumaya fazlasıyla yeter, indirme süresini kısaltır.
        fmt = (
            f"bestvideo[height<={VIDEO_MAX_HEIGHT}]+bestaudio/"
            f"best[height<={VIDEO_MAX_HEIGHT}]/best"
        )
        await _run(
            "yt-dlp", "--no-playlist", "--no-warnings",
            "-f", fmt, "--merge-output-format", "mp4",
            "-o", str(work / "download.%(ext)s"), url,
        )
    else:
        await _run(
            "yt-dlp", "--no-playlist", "--no-warnings",
            "-f", "bestaudio/best", "-x", "--audio-format", "m4a",
            "-o", str(work / "download.%(ext)s"), url,
        )

    media = _find_download(work)
    await _require_audio(media)
    audio = work / "audio.wav"
    await _to_wav(media, audio)

    video = media if (ENABLE_FRAMES and await _has_stream(media, "v")) else None

    # Hazır altyazı varsa Whisper'a hiç gitmeyiz: bedava, anlık ve elle yazılmışsa
    # daha doğru. Başarısız olursa iş düşmez, Whisper'a döneriz.
    subs: list[Segment] | None = None
    sub_meta: dict = {}
    choice = subtitles.pick(info)
    if choice is not None:
        lang, is_auto = choice
        kind = "otomatik" if is_auto else "elle yazılmış"
        try:
            subs = await subtitles.download(url, lang, is_auto, work)
            sub_meta = {"subtitle_lang": lang, "subtitle_auto": is_auto}
            print(
                f"[fetch] {kind} altyazi kullaniliyor ({lang}, {len(subs)} satir) "
                f"- transkript adimi atlanacak",
                flush=True,
            )
        except Exception as exc:
            print(f"[fetch] altyazi alinamadi, Whisper'a donuluyor: {exc}", flush=True)

    return Source(
        audio_path=audio,
        video_path=video,
        subtitles=subs,
        title=info.get("title") or url,
        duration=float(info.get("duration") or await _probe_duration(audio)),
        meta={
            "url": info.get("webpage_url") or url,
            "uploader": info.get("uploader"),
            "upload_date": info.get("upload_date"),
            "language": info.get("language"),
            **sub_meta,
        },
    )


async def from_file(path: Path, work: Path) -> Source:
    if not path.exists():
        raise RuntimeError(f"Dosya bulunamadı: {path}")
    local = work / path.name
    if local != path:
        shutil.copy2(path, local)

    await _require_audio(local)
    audio = work / "audio.wav"
    await _to_wav(local, audio)
    video = local if (ENABLE_FRAMES and await _has_stream(local, "v")) else None

    return Source(
        audio_path=audio,
        video_path=video,
        title=path.stem,
        duration=await _probe_duration(audio),
        meta={"local_path": str(path)},
    )


async def fetch(source: str, work: Path) -> Source:
    if source.startswith(("http://", "https://")):
        return await from_url(source, work)
    return await from_file(Path(source), work)
