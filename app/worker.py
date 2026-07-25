"""Tek tüketicili iş kuyruğu.

Ağır iş (ffmpeg) CPU-bağlı olduğu için işler sırayla koşar; paralellik iş
İÇİNDE (transkript parçaları, bölüm özetleri) zaten var.
"""

import asyncio
import shutil
import time
import traceback

from . import db, llm, notify
from .config import (
    DELETE_SOURCE_AFTER_DONE,
    OUT_DIR,
    PART_CONCURRENCY,
    PART_SECONDS,
    UPLOAD_DIR,
    WORK_DIR,
)
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

# İptal altyapısı: o an İŞLENEN işin görevi (running işi durdurmak için cancel
# edilir) ve kullanıcının iptal ettiği iş id'leri (queued işi dequeue'da atlamak
# + running iptalini döngü kapanışından ayırmak için).
_current_id: str | None = None
_current_task: "asyncio.Task | None" = None
_user_cancel: set[str] = set()


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


async def _split_audio(wav: Path, part_len: float, work: Path) -> list[Path]:
    """16kHz wav'ı ~part_len'lik parçalara böl (ffmpeg segment, pcm copy — hızlı,
    yeniden-kodlama yok). Her parça ayrı transkript edilecek."""
    outdir = work / "parts"
    outdir.mkdir(parents=True, exist_ok=True)
    await fetch._run(
        "ffmpeg", "-nostdin", "-y", "-i", str(wav),
        "-f", "segment", "-segment_time", f"{part_len:.3f}",
        "-c", "copy", str(outdir / "part_%03d.wav"),
    )
    return sorted(outdir.glob("part_*.wav"))


def _part_meta(source, part_len, job_id, digest, p_shots, assets_rel):
    return {
        **source.meta,
        "duration": part_len,
        "part_of": job_id,
        "learning_type": digest.learning_type,
        "tur": digest.tur,
        "topics": digest.topics,
        "sections": len(digest.sections),
        "critic_added": digest.added_by_critic,
        "critic_types": digest.critic_types,
        "critic_from_screen": digest.critic_from_screen,
        "compression": digest.compression,
        "frames_used": sum(1 for f in p_shots if not f.quarantined),
        "frames_from_silence": sum(
            1 for f in p_shots if f.in_silence and not f.quarantined
        ),
        "quarantined": [
            {"ts": f.ts, "conf": f.conf, "src": f"{assets_rel}/{f.path.name}"}
            for f in p_shots if f.quarantined
        ],
    }


def _birlestir(base_title: str, ozetler: list, n: int, hatalar: list) -> str:
    """'Tüm hali': her part'ın TL;DR'ı + bölüm başlıkları tek belgede (genel bakış).
    Detay her part'ın kendi girdisinde. Başaramayan part varsa dürüstçe söyler."""
    out = [f"# {base_title} — Tüm hali", ""]
    out += [f"{n} parça · her parçanın kendi ayrıntılı özeti ayrı girdide.", ""]
    for p_title, d in ozetler:
        out += [f"## {p_title}", ""]
        out += [f"- {x}" for x in d.tldr]
        basliklar = " · ".join(s.section.title for s in d.sections)
        if basliklar:
            out += ["", f"_Bölümler:_ {basliklar}"]
        out += [""]
    if hatalar:
        out += ["## İşlenemeyen parçalar", ""]
        out += ["Bu parçalar tamamlanamadı (sebebi yanlarında); tek tek 'tekrar dene' ile denenebilir.", ""]
        out += [f"- **{t}** — {e}" for t, e in hatalar]
        out += [""]
    return "\n".join(out)


async def _process_long(job_id, job, source, shots, assets_rel, work, n) -> None:
    """Uzun video: sesi part'lara böl, HER PART'I BAĞIMSIZ transkript+özetle (ayrı
    kütüphane girdisi), tüm video 'Tüm hali' birleşik belge olur. Bir part'ın
    transkript hatası (Groq 502 gibi) diğerlerini ÖLDÜRMEZ — o part hata alır, gerisi
    tamamlanır. Part segmentleri mutlak zamana kaydırılır (tam videoya tıklanabilir)."""
    base_title = job.get("title") or source.title
    provider = job.get("provider") or llm.provider()
    origin_url = job.get("origin_url") or source.meta.get("url")
    part_len = source.duration / n
    db.update(job_id, title=f"{base_title} — Tüm hali", stage=f"0/{n} parça bitti")

    part_files = await _split_audio(source.audio_path, part_len, work)
    n = len(part_files) or n
    # Koleksiyonu başlıktan BİR KEZ belirle (paralel part'larda yarış olmasın).
    koleksiyon = await summarize.classify_collection(base_title, [], db.distinct_collections())

    # Part'lar PARALEL işlenir (PART_CONCURRENCY kadar aynı anda). Groq transkripti
    # kendi semaforuyla sıralanır (kazanç sınırlı); yerel transkriptte tam paralel.
    sem = asyncio.Semaphore(PART_CONCURRENCY)
    biten = [0]

    async def _bir_part(i, pf):
        t0 = i * part_len
        aralik = f"{int(t0 // 60)}-{int((t0 + part_len) // 60)}dk"
        p_title = f"{base_title} — Part {i + 1} ({aralik})"
        async with sem:
            try:
                p_segs = await transcribe.transcribe(pf, work)
            except Exception as exc:
                print(f"[part] {i + 1}/{n} transkript HATA: {exc}", flush=True)
                return ("hata", i, p_title, _hata_acikla(exc))
            # 0-tabanlı part zamanlarını mutlak zamana kaydır (tam videoya tıklanabilsin).
            p_segs = [transcribe.Segment(s.start + t0, s.end + t0, s.text) for s in p_segs]
            p_shots = [f for f in shots if t0 <= f.ts < t0 + part_len]
            p_transcript = transcribe.to_timestamped_text(p_segs)
            p_sections = await segment.split_into_sections(p_segs, p_title, p_transcript)
            p_digest = await summarize.summarize(p_sections, p_transcript, p_shots)
            p_md = render.render(
                p_digest, p_title, part_len, source.meta,
                assets_rel=assets_rel if p_shots else None,
            )
            cid = f"{job_id}p{i + 1}"
            db.create_job(cid, job["source"], None, provider)
            out = OUT_DIR / f"{cid}.md"
            out.write_text(p_md, encoding="utf-8")
            db.update(
                cid, status="done", stage="done", title=p_title, collection=koleksiyon,
                origin_url=origin_url, result_path=str(out),
                meta=_part_meta(source, part_len, job_id, p_digest, p_shots, assets_rel),
            )
            biten[0] += 1
            db.update(job_id, stage=f"{biten[0]}/{n} parça bitti")
            print(f"[part] {cid} bitti: {p_title}", flush=True)
            return ("ok", i, p_title, p_digest)

    sonuclar = await asyncio.gather(*[_bir_part(i, pf) for i, pf in enumerate(part_files)])
    sonuclar.sort(key=lambda x: x[1])  # part sırasına geri diz
    ozetler = [(t, d) for (s, _i, t, d) in sonuclar if s == "ok"]
    hatalar = [(t, e) for (s, _i, t, e) in sonuclar if s == "hata"]

    # Birleşik "Tüm hali" = bu (parent) iş.
    combined = _birlestir(base_title, ozetler, n, hatalar)
    out = OUT_DIR / f"{job_id}.md"
    out.write_text(combined, encoding="utf-8")
    ilk = ozetler[0][1] if ozetler else None
    db.update(
        job_id, status="done", stage="done", collection=koleksiyon,
        result_path=str(out),
        meta={
            **source.meta, "duration": source.duration, "parts": n,
            "parts_failed": len(hatalar), "is_combined": True,
            "topics": ilk.topics if ilk else [],
            "learning_type": ilk.learning_type if ilk else "genel",
            "tur": ilk.tur if ilk else "genel",
        },
    )
    print(f"[part] {job_id} tum hali: {len(ozetler)} basarili, {len(hatalar)} hata", flush=True)


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
    # Markdown/metin: en basit hat — transkript/OCR yok, metin zaten okunabilir.
    kaynak = job["source"]
    if not kaynak.startswith(("http://", "https://")):
        suffix = Path(kaynak).suffix.lower()
        if suffix == ".pdf":
            await _process_document(job_id, Path(kaynak), work)
            return
        if suffix in (".md", ".markdown", ".txt"):
            await _process_text(job_id, Path(kaynak), work)
            return

    db.update(job_id, status="running", stage="fetch")
    source = await fetch.fetch(
        job["source"], work, job.get("referer"), bool(job.get("audio_only"))
    )

    # Agent bir linki indirip yüklediyse dosya adı iş numarasıdır ve link
    # kaybolmuştur. Orijinali geri koyuyoruz: başlık anlamlı olsun ve özetteki
    # zaman damgaları videoya tıklanabilsin.
    if job.get("origin_url"):
        source.meta["url"] = job["origin_url"]
        if job.get("title"):
            source.title = job["title"]

    # Yüklemede başlık zaten orijinal dosya adına ayarlanmış olabilir (upload_job)
    # — job_id'yle ezmeyelim; yoksa source.title (link'te yt-dlp başlığı) kullanılır.
    db.update(job_id, title=job.get("title") or source.title)

    # Görsel katman ÖNCE: kareler transkriptten bağımsız (video'dan çıkar). Hem
    # kısa yolda onarımda hem uzun yolda part'lara zaman-damgasıyla dağıtımda lazım.
    assets_rel = f"{job_id}_frames"
    shots: list[frames.Frame] = []
    if source.video_path is not None:
        db.update(job_id, stage="frames")
        shots = await frames.extract(
            source.video_path, source.duration, OUT_DIR / assets_rel
        )

    # UZUN VIDEO: transkriptten ÖNCE part'lara böl. Her part BAĞIMSIZ transkript →
    # bir part'ın Groq 502'si tüm işi öldürmez; tek dev transkript (chunk 25'te
    # patlayan) yok. Altyazı varsa (tüm video için hazır) bölme yok — o yol zaten hızlı.
    n_part = round(source.duration / PART_SECONDS) if source.duration else 0
    if n_part >= 2 and source.subtitles is None:
        await _process_long(job_id, job, source, shots, assets_rel, work, n_part)
        return

    if source.subtitles is not None:
        # Hazır altyazı bulundu (fetch aşamasında) — Whisper'a hiç gitmiyoruz.
        db.update(job_id, stage="subtitles")
        segments = source.subtitles
    else:
        db.update(job_id, stage="transcribe")
        segments = await transcribe.transcribe(source.audio_path, work)

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
            "tur": digest.tur,
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

    # Yükleme orijinal dosya adını başlık yaptıysa koru (yoksa dosya adı = job_id).
    title = (db.get(job_id) or {}).get("title") or pdf.stem
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
            "tur": digest.tur,
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


async def _process_text(job_id: str, path: Path, work: Path) -> None:
    """Markdown/metin hattı: en basit — transkript/OCR/karantina yok, metin zaten
    okunabilir. AYNI defter (segment/summarize/eleştirmen) işler; render 'sayfa'
    değil 'bölüm' der. Blok numarası 'zaman' olarak kodlanır (document.py deseni).
    """
    if not path.exists():
        raise RuntimeError(f"Dosya bulunamadı: {path}")

    # Yükleme orijinal dosya adını başlık yaptıysa koru (yoksa dosya adı = job_id).
    title = (db.get(job_id) or {}).get("title") or path.stem
    db.update(job_id, status="running", stage="pages", title=title)

    pages = await asyncio.to_thread(document.extract_markdown, path)
    segments = document.to_segments(pages)
    if not segments:
        raise RuntimeError("Dosyada özetlenecek metin yok (boş ya da yalnızca başlık).")

    transcript = transcribe.to_timestamped_text(segments)
    transcript_path = OUT_DIR / f"{job_id}.transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")

    db.update(job_id, stage="segment")
    sections = await segment.split_into_sections(segments, title, transcript)

    db.update(job_id, stage="summarize")
    digest = await summarize.summarize(sections, transcript, [])

    koleksiyon = await summarize.classify_collection(
        title, digest.topics, db.distinct_collections()
    )
    db.update(job_id, collection=koleksiyon)

    db.update(job_id, stage="render")
    markdown = render.render_document(
        digest, title, pages, assets_rel="", birim="bölüm", kisa="b."
    )
    out_path = OUT_DIR / f"{job_id}.md"
    out_path.write_text(markdown, encoding="utf-8")

    kelime = sum(len(p.text.split()) for p in pages)
    db.update(
        job_id,
        status="done",
        stage="done",
        result_path=str(out_path),
        meta={
            "kind": "markdown",
            "learning_type": digest.learning_type,
            "tur": digest.tur,
            "topics": digest.topics,
            "blocks": len(pages),
            "words": kelime,
            "sections": len(digest.sections),
            "critic_added": digest.added_by_critic,
            "critic_types": digest.critic_types,
            "compression": digest.compression,
            "transcript_path": str(transcript_path),
        },
    )
    shutil.rmtree(work, ignore_errors=True)


def _saklama_temizle(job_id: str) -> None:
    """İş bittikten sonra yüklenen kaynağı (en büyük dosya) sil — özet/transkript/
    slaytlar kalır. delete_job ile aynı desen (UPLOAD_DIR/<id>.*); origin_url DB'de
    durduğu için zaman damgaları hâlâ tıklanabilir.
    """
    if not DELETE_SOURCE_AFTER_DONE:
        return
    bayt = 0
    for p in UPLOAD_DIR.glob(f"{job_id}.*"):
        try:
            bayt += p.stat().st_size
            p.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[saklama] {p.name} silinemedi: {exc}", flush=True)
    if bayt:
        print(f"[saklama] {job_id} kaynagi silindi ({bayt // 1024} KB bosaldi)", flush=True)


def _hata_acikla(exc: Exception) -> str:
    """Ham hatayı LLM'le paylaşılabilir bir AÇIKLAMAYLA zenginleştir: 'ne oldu' +
    'ne demek' + 'öneri'. Bilinen imzaları eşler; bilinmeyende ham hata döner."""
    ham = str(exc)
    dl = ham.lower()
    a = ""
    if "groq" in dl and ("502" in dl or "503" in dl or "500" in dl or "transkrip" in dl):
        a = ("Groq'un transkripsiyon (Whisper) sunucusu geçici yanıt vermedi "
             "(5xx = sunucu yükü/kesintisi, senin dosyanda sorun değil). Uzun "
             "videolarda daha olası. Öneri: 'tekrar dene' (çoğu zaman geçer); video "
             "30dk+ ise part'lara bölünür, yalnız hatalı part'ı yeniden dene; ya da "
             "yerel transkript (yerel-transkript.py) ile Groq'u tümden atla.")
    elif "429" in dl or "rate limit" in dl or "kota" in dl or "quota" in dl or "too many" in dl:
        a = ("Sağlayıcı kotası doldu (dakikalık/günlük istek ya da token sınırı). "
             "Öneri: birkaç dakika bekleyip tekrar dene; ya da arayüzden farklı "
             "sağlayıcı seç (OpenRouter/Gemini/Groq).")
    elif "sign in to confirm" in dl or ("youtube" in dl and "bot" in dl):
        a = ("YouTube bu IP'yi bot sanıp engelledi (veri-merkezi IP'si). Sunucu "
             "YouTube indiremez; ev-agent'ı (agent.ps1) gerekir — link işleri onu bekler.")
    elif "yt-dlp" in dl or "unable to download" in dl or "http error 4" in dl or "forbidden" in dl:
        a = ("Kaynak indirilemedi. Link erişilebilir mi? Korumalı bir .m3u8 ise "
             "'gelişmiş' altında doğru referer (örn. coderspace.io) gerekebilir.")
    elif "ses akışı yok" in dl or "no audio" in dl:
        a = ("Kaynakta ses akışı yok — transkript çıkarılamaz (sessiz video ya da "
             "yalnızca görüntü içeren dosya).")
    elif "ffmpeg" in dl or "ffprobe" in dl:
        a = ("Medya işleme hatası (ffmpeg). Dosya bozuk ya da desteklenmeyen bir "
             "format olabilir; farklı bir kaynak/format dene.")
    elif "güvenilir metin çıkmadı" in dl or "yalnızca başlık" in dl:
        a = ("Belgeden okunabilir metin çıkmadı — tümüyle taranmış/görüntü olabilir. "
             "Karantina kanıtları defterde; daha net bir tarama gerekebilir.")
    return f"{ham}\n\nBu ne demek: {a}" if a else ham


async def _run_one(job_id: str) -> None:
    t0 = time.time()  # işlenme süresi: kuyruk beklemesi HARİÇ, running→done
    try:
        await _process(job_id)
        # Yalnızca BAŞARILI işte kaynağı at; hata olursa dosya kalsın ki
        # kullanıcı sebebi araştırabilsin / yeniden denenebilsin.
        _saklama_temizle(job_id)
        # İşlenme süresini meta'ya ekle (arayüz "N dk'da işlendi" gösterir).
        job = db.get(job_id) or {}
        if job.get("meta") is not None:
            db.update(job_id, meta={**job["meta"], "islenme_sn": round(time.time() - t0)})
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
        # Hatayı LLM'le paylaşılabilir açıklamayla zenginleştir (ne demek + öneri).
        aciklama = _hata_acikla(exc)
        db.update(job_id, status="error", stage="error", error=aciklama)
        shutil.rmtree(WORK_DIR / job_id, ignore_errors=True)
        payload = {"job_id": job_id, "status": "error", "error": aciklama}

    job = db.get(job_id) or {}
    if job.get("callback_url"):
        await notify.callback(job["callback_url"], payload)


def cancel(job_id: str) -> str | None:
    """Queued ya da running işi iptal et. running ise o an işleyen görevi cancel
    eder (bir sonraki await'te CancelledError ile durur); queued ise dequeue'da
    atlanır. Dönen: 'cancelled' | 'not-active' | None (iş yok).
    """
    job = db.get(job_id)
    if job is None:
        return None
    # waiting = link işi agent'ın indirmesini bekliyor; o da iptal edilebilmeli.
    if job["status"] not in ("queued", "running", "waiting"):
        return "not-active"
    _user_cancel.add(job_id)
    db.update(job_id, status="cancelled", stage="cancelled", error="Kullanıcı iptal etti")
    if job_id == _current_id and _current_task is not None and not _current_task.done():
        _current_task.cancel()
    return "cancelled"


async def retry(job_id: str) -> str | None:
    """Hata almış / iptal edilmiş işi yeniden kuyruğa koy. Kaynak dosya duruyor
    (saklama yalnızca 'done'da siler), o yüzden baştan koşabilir.
    """
    job = db.get(job_id)
    if job is None:
        return None
    if job["status"] not in ("error", "cancelled"):
        return "not-retryable"
    _user_cancel.discard(job_id)
    db.update(job_id, status="queued", stage="queued", error=None)
    await enqueue(job_id)
    return "requeued"


async def loop() -> None:
    global _current_id, _current_task
    while True:
        job_id = await _queue.get()
        # Kuyruğa girdikten sonra iptal edildiyse hiç işleme.
        if job_id in _user_cancel:
            _user_cancel.discard(job_id)
            db.update(job_id, status="cancelled", stage="cancelled")
            _ucusta.discard(job_id)
            _queue.task_done()
            continue
        _current_id = job_id
        _current_task = asyncio.create_task(_run_one(job_id))
        try:
            await _current_task
        except asyncio.CancelledError:
            # İki olası kaynak: (a) kullanıcı bu işi iptal etti → işi durdur,
            # döngü YAŞASIN; (b) döngünün kendisi kapatılıyor (lifespan) → yay.
            if job_id in _user_cancel:
                _user_cancel.discard(job_id)
                db.update(job_id, status="cancelled", stage="cancelled",
                          error="Kullanıcı iptal etti")
                shutil.rmtree(WORK_DIR / job_id, ignore_errors=True)
                print(f"[worker] {job_id} kullanici tarafindan iptal edildi", flush=True)
            else:
                raise
        except BaseException as exc:
            # except Exception yetmez: beklenmedik bir BaseException döngüyü
            # sessizce öldürür ve o andan sonra HİÇBİR iş işlenmez.
            print(f"[worker] {job_id} beklenmedik sekilde dustu: {exc!r}", flush=True)
        finally:
            _current_id = None
            _current_task = None
            _ucusta.discard(job_id)
            _queue.task_done()
