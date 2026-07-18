"""Tek tüketicili iş kuyruğu.

Ağır iş (ffmpeg) CPU-bağlı olduğu için işler sırayla koşar; paralellik iş
İÇİNDE (transkript parçaları, bölüm özetleri) zaten var.
"""

import asyncio
import shutil
import traceback

from . import db, llm, notify
from .config import OUT_DIR, WORK_DIR
from .pipeline import (
    document,
    fetch,
    frames,
    render,
    repair,
    segment,
    summarize,
    transcribe,
)
from pathlib import Path

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

    # PDF gezilemeyen bir kaynak (SPEC §0): ayrı hat — ses/kare yok, sayfa var.
    # Ama AYNI defter: segment + summarize + karantina değişmeden koşar.
    kaynak = job["source"]
    if not kaynak.startswith(("http://", "https://")) and \
            Path(kaynak).suffix.lower() == ".pdf":
        await _process_document(job_id, Path(kaynak), work)
        return

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

    # LLM içeriği bir çalışmaya (koleksiyona) otomatik atar — konuya göre.
    koleksiyon = await summarize.classify_collection(
        source.title, digest.topics, db.distinct_collections()
    )
    db.update(job_id, collection=koleksiyon)

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
            # Öğrenme: tür + konu etiketleri (arayüz rozeti / gruplama).
            "learning_type": digest.learning_type,
            "topics": digest.topics,
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
            # frames_used = OKUNAN ekran. Karantinadakiler buraya girmez:
            # "31 slayt okundu" derken okuyamadığımızı saymak yalan olurdu.
            "frames_used": sum(1 for f in shots if not f.quarantined),
            # Sessizlik kuplajının (§3) ölçülebilir karşılığı: bu ekranlar,
            # sesin sustuğu — yani Whisper'ın uydurmaya en yatkın olduğu —
            # pencerelerden geldi. Transkriptin kapsamadığı tek yer.
            "frames_from_silence": sum(
                1 for f in shots if f.in_silence and not f.quarantined
            ),
            # Okuyamadıklarımız kaybolmuyor; defter kanıtıyla gösteriyor.
            "quarantined": [
                {
                    "ts": f.ts,
                    "conf": f.conf,
                    "src": f"{assets_rel}/{f.path.name}",
                }
                for f in shots
                if f.quarantined
            ],
            "transcript_punct": round(punct, 1),
            "transcript_caps": round(caps, 2),
            "transcript_repaired": repaired,
            "transcript_path": str(transcript_path),
        },
    )
    shutil.rmtree(work, ignore_errors=True)


async def _process_document(job_id: str, pdf: Path, work: Path) -> None:
    """PDF hattı: sayfa metni (katman ya da OCR) → aynı segment/summarize/defter.

    Ses/kare yok; sayfa numarası 'zaman' olarak kodlanıyor (bkz. document.py).
    Kurtarılan görsel/sessizlik kavramları PDF'e uymaz (birincil kanal sayfanın
    kendisi) — o alanlar boş kalır, arayüz buna göre uyarlanır.
    """
    if not pdf.exists():
        raise RuntimeError(f"PDF bulunamadı: {pdf}")

    title = pdf.stem
    db.update(job_id, status="running", stage="pages", title=title)

    assets_rel = f"{job_id}_pages"
    pages = await asyncio.to_thread(
        document.extract, pdf, OUT_DIR / assets_rel, assets_rel
    )
    segments = document.to_segments(pages)
    if not segments:
        raise RuntimeError(
            "Hiçbir sayfadan güvenilir metin çıkmadı — belge tümüyle taranmış "
            "ve okunamadı olabilir. Karantina kanıtları defterde."
        )

    transcript = transcribe.to_timestamped_text(segments)
    transcript_path = OUT_DIR / f"{job_id}.transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")

    db.update(job_id, stage="segment")
    sections = await segment.split_into_sections(segments, title, transcript)

    db.update(job_id, stage="summarize")
    digest = await summarize.summarize(sections, transcript, [])

    # LLM belgeyi de bir çalışmaya (koleksiyona) otomatik atar — konuya göre.
    koleksiyon = await summarize.classify_collection(
        title, digest.topics, db.distinct_collections()
    )
    db.update(job_id, collection=koleksiyon)

    db.update(job_id, stage="render")
    markdown = render.render_document(digest, title, pages, assets_rel)
    out_path = OUT_DIR / f"{job_id}.md"
    out_path.write_text(markdown, encoding="utf-8")

    okunan = sum(1 for p in pages if not p.quarantined and p.text.strip())
    oranlar = [p.word_ratio for p in pages if p.word_ratio is not None]

    db.update(
        job_id,
        status="done",
        stage="done",
        result_path=str(out_path),
        meta={
            "kind": "document",
            "learning_type": digest.learning_type,
            "topics": digest.topics,
            "pages": len(pages),
            "pages_read": okunan,
            "pages_text_layer": sum(1 for p in pages if p.source == "metin-katmani"),
            "pages_ocr": sum(1 for p in pages if p.source == "ocr" and not p.quarantined),
            "sections": len(digest.sections),
            "critic_added": digest.added_by_critic,
            "critic_types": digest.critic_types,
            "compression": digest.compression,
            # Okunamayan sayfalar kaybolmuyor; defter kanıtıyla gösteriyor.
            "quarantined": [
                {"ts": p.number, "conf": p.conf, "src": p.img_rel, "page": p.number}
                for p in pages
                if p.quarantined
            ],
            # SPEC §2.2 loglanmış sigorta: en düşük gerçek-kelime oranı. Kapı
            # ateşlemese bile yazılır — külliyat bimodal olursa veri söyler.
            "min_word_ratio": round(min(oranlar), 3) if oranlar else None,
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
