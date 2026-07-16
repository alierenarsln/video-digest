"""Tek tüketicili iş kuyruğu.

Ağır iş (ffmpeg) CPU-bağlı olduğu için işler sırayla koşar; paralellik iş
İÇİNDE (transkript parçaları, bölüm özetleri) zaten var.
"""

import asyncio
import shutil
import traceback

from . import db, llm, notify
from .config import OUT_DIR, WORK_DIR
from .pipeline import fetch, frames, render, repair, segment, summarize, transcribe

_queue: asyncio.Queue[str] = asyncio.Queue()
# Kuyrukta veya işlenmekte olan işler. Kurtarıcının aynı işi ikinci kez kuyruğa
# koymasını engelliyor.
_ucusta: set[str] = set()


async def enqueue(job_id: str) -> None:
    _ucusta.add(job_id)
    await _queue.put(job_id)


async def kurtarici(aralik: int = 60) -> None:
    """Kuyruğa girmiş ama işlenmemiş işleri bulup geri koyar.

    Gerçek bir koşuda bir iş 'queued' durumunda asılı kaldı ve yalnızca sunucu
    yeniden başlatılınca işlendi; kök neden tekrar üretilemedi. Sebebi ne olursa
    olsun (kaybolan kuyruk girdisi, ölen görev) sonuç kabul edilemez: iş sessizce
    kaybolur ve kimse fark etmez. Bu döngü onu kendiliğinden toparlar.
    """
    while True:
        await asyncio.sleep(aralik)
        try:
            for job_id in db.pending_ids():
                if job_id not in _ucusta:
                    print(
                        f"[kurtarici] {job_id} kuyrukta unutulmus, geri konuyor",
                        flush=True,
                    )
                    await enqueue(job_id)
        except Exception as exc:
            print(f"[kurtarici] hata: {exc}", flush=True)


async def _process(job_id: str) -> None:
    job = db.get(job_id)
    if job is None:
        return

    # Sağlayıcı iş başına seçiliyor (arayüzden). Pencere boyutları buna bağlı
    # olduğu için boru hattı başlamadan ÖNCE ayarlanmalı.
    llm.set_provider(job.get("provider") or llm.provider())

    work = WORK_DIR / job_id
    work.mkdir(parents=True, exist_ok=True)

    db.update(job_id, status="running", stage="fetch")
    source = await fetch.fetch(job["source"], work)

    # Agent bir linki indirip yüklediyse dosya adı iş numarasıdır ve link
    # kaybolmuştur. Orijinali geri koyuyoruz: başlık anlamlı olsun ve özetteki
    # zaman damgaları videoya tıklanabilsin.
    if job.get("origin_url"):
        source.meta["url"] = job["origin_url"]
        if job.get("title"):
            source.title = job["title"]

    db.update(job_id, title=source.title)

    if source.subtitles is not None:
        # Hazır altyazı bulundu (fetch aşamasında) — Whisper'a hiç gitmiyoruz.
        db.update(job_id, stage="subtitles")
        segments = source.subtitles
    else:
        db.update(job_id, stage="transcribe")
        segments = await transcribe.transcribe(source.audio_path, work)

    # Görsel katman: sesi olan ama videosu olmayan kaynaklarda (meeting kaydı,
    # podcast) kendiliğinden atlanır. Onarımdan ÖNCE koşar: ekran metni, ASR'ın
    # bozduğu terim ve özel isimleri düzeltmekte kullanılıyor.
    assets_rel = f"{job_id}_frames"
    shots: list[frames.Frame] = []
    if source.video_path is not None:
        db.update(job_id, stage="frames")
        shots = await frames.extract(
            source.video_path, source.duration, OUT_DIR / assets_rel
        )

    raw_transcript = transcribe.to_timestamped_text(segments)
    punct = repair.punct_density(segments)
    caps = repair.caps_ratio(segments)
    repaired = repair.needs_repair(segments)
    print(
        f"[repair] noktalama {punct:.1f} (esik {repair.REPAIR_MIN_PUNCT}) | "
        f"buyuk-harf {caps:.2f} (esik {repair.REPAIR_MIN_CAPS}) -> "
        f"{'ONARILIYOR' if repaired else 'onarim gerekmiyor'}",
        flush=True,
    )
    if repaired:
        db.update(job_id, stage="repair")
        segments = await repair.repair(segments, shots)

    transcript = transcribe.to_timestamped_text(segments)
    transcript_path = OUT_DIR / f"{job_id}.transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")
    if repaired:
        # Ham hali de kalsın: onarımın bir şeyi bozup bozmadığı ancak böyle görülür.
        (OUT_DIR / f"{job_id}.transcript.raw.txt").write_text(
            raw_transcript, encoding="utf-8"
        )

    db.update(job_id, stage="segment")
    sections = await segment.split_into_sections(segments, source.title, transcript)

    db.update(job_id, stage="summarize")
    digest = await summarize.summarize(sections, transcript, shots)

    db.update(job_id, stage="render")
    markdown = render.render(
        digest,
        source.title,
        source.duration,
        source.meta,
        assets_rel=assets_rel if shots else None,
    )
    out_path = OUT_DIR / f"{job_id}.md"
    out_path.write_text(markdown, encoding="utf-8")

    db.update(
        job_id,
        status="done",
        stage="done",
        result_path=str(out_path),
        meta={
            **source.meta,
            "duration": source.duration,
            "sections": len(digest.sections),
            "critic_added": digest.added_by_critic,
            # Defter "6 madde" değil "3 sayı · 2 tanım" der; sayı tek başına
            # neyin riskte olduğunu söylemiyor.
            "critic_types": digest.critic_types,
            # Konuşmacının söylemediği, yalnızca ekranda olan bilgi: ürünün tek
            # farkının ölçülebilir hâli. Tahmin değil, eleştirmenin etiketi.
            "critic_from_screen": digest.critic_from_screen,
            # Bölüm başına kaç kelime girip kaç kelime çıktı. Defterin
            # ölçemediği boşluk için dürüst vekil: iddia değil, davet.
            "compression": digest.compression,
            "frames_used": digest.frames_used,
            "transcript_punct": round(punct, 1),
            "transcript_caps": round(caps, 2),
            "transcript_repaired": repaired,
            "transcript_path": str(transcript_path),
        },
    )
    shutil.rmtree(work, ignore_errors=True)


async def _run_one(job_id: str) -> None:
    try:
        await _process(job_id)
        job = db.get(job_id) or {}
        payload = {
            "job_id": job_id,
            "status": "done",
            "title": job.get("title"),
            "markdown": (job.get("result_path") and
                         open(job["result_path"], encoding="utf-8").read()),
            "meta": job.get("meta"),
        }
    except Exception as exc:
        traceback.print_exc()
        db.update(job_id, status="error", stage="error", error=str(exc))
        shutil.rmtree(WORK_DIR / job_id, ignore_errors=True)
        payload = {"job_id": job_id, "status": "error", "error": str(exc)}

    job = db.get(job_id) or {}
    if job.get("callback_url"):
        await notify.callback(job["callback_url"], payload)


async def loop() -> None:
    while True:
        job_id = await _queue.get()
        try:
            await _run_one(job_id)
        except BaseException as exc:
            # except Exception yetmez: CancelledError gibi bir BaseException
            # döngüyü sessizce öldürür ve o andan sonra HİÇBİR iş işlenmez.
            print(f"[worker] {job_id} beklenmedik sekilde dustu: {exc!r}", flush=True)
            if isinstance(exc, asyncio.CancelledError):
                raise
        finally:
            _ucusta.discard(job_id)
            _queue.task_done()
