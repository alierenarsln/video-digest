"""Tek tüketicili iş kuyruğu.

Ağır iş (ffmpeg) CPU-bağlı olduğu için işler sırayla koşar; paralellik iş
İÇİNDE (transkript parçaları, bölüm özetleri) zaten var.
"""

import asyncio
import json
import pickle
import shutil
import time
import traceback

from . import db, llm, notify, storage
from .config import (
    DELETE_SOURCE_AFTER_DONE,
    MAX_PDF_PAGES,
    OUT_DIR,
    PART_CONCURRENCY,
    UZUN_BELGE_BIRIM,
    PART_SECONDS,
    UPLOAD_DIR,
    WORK_DIR,
)
from .pipeline import (
    document,
    fetch,
    frames,
    gemini_video,
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


# --- Kaldığı yerden devam -------------------------------------------------------
# Her pahalı aşamanın sonucu çalışma klasörüne yazılır (work/asama.<ad>.pkl). İş
# bir adımda düşerse "tekrar dene" biten aşamaları YENİDEN YAPMAZ: indirme, slayt
# OCR'ı, transkript, bölümleme, özet — hangisi bittiyse diskten gelir, yalnız
# düşen adım koşar. Eskiden hata = her şey baştan (israf). LLM aşamaları
# sağlayıcıya göre ayrı anahtarlanır: başka sağlayıcıyla tekrar denemek (pencere
# boyutları farklı) eski sağlayıcının sonucunu kullanmasın. Klasör iş bitince
# silinir; hatalı işinki 3 gün kalır (_eski_calismalari_temizle).
def _kayit_yolu(work: Path, ad: str) -> Path:
    return work / f"asama.{ad}.pkl"


def _kayit_oku(work: Path, ad: str, gecerli=None):
    """Önceki denemenin kaydı (yoksa/bozuksa/geçersizse None)."""
    yol = _kayit_yolu(work, ad)
    if not yol.exists():
        return None
    try:
        deger = pickle.loads(yol.read_bytes())
    except Exception as exc:
        print(f"[devam] {ad} kaydi okunamadi, yeniden uretilecek: {exc!r}", flush=True)
        return None
    if gecerli is not None and not gecerli(deger):
        return None
    print(f"[devam] {work.name}: '{ad}' onceki denemeden alindi", flush=True)
    return deger


async def _asama(work: Path, ad: str, uret, gecerli=None):
    deger = _kayit_oku(work, ad, gecerli)
    if deger is not None:
        return deger
    yol = _kayit_yolu(work, ad)
    deger = await uret()
    try:
        gecici = yol.with_suffix(".tmp")
        gecici.write_bytes(pickle.dumps(deger))
        gecici.replace(yol)
    except Exception as exc:  # kayıt yazılamasa da iş sürsün
        print(f"[devam] {ad} kaydedilemedi: {exc!r}", flush=True)
    return deger


async def _hizli_yol(job_id: str, url: str, work: Path):
    """(Source, ekran kareleri) ya da None (klasik yola dön). Sonuç 'kaynak' ve
    'hizli_ekran' kaydına yazılır: tekrar denemede Gemini'ye yeniden gidilmez."""
    t0 = time.time()
    try:
        # Süre/başlık için yt-dlp -J (indirme YOK, birkaç saniye). YouTube bot
        # kontrolüne takılırsa süresiz devam: tek pencerede istenir.
        try:
            info = json.loads(await fetch._ytdlp(
                "--dump-single-json", "--no-playlist", "--no-warnings", url))
        except Exception as exc:
            print(f"[hizli] {job_id}: video bilgisi alinamadi ({str(exc)[:120]}), suresiz", flush=True)
            info = {}
        sure = float(info.get("duration") or 0)
        if info:
            fetch.onkontrol_canli(info)
        segs, ekran = await gemini_video.oku(url, sure)
    except gemini_video.HizliYolYok as exc:
        print(f"[hizli] {job_id}: hizli yol olmadi -> klasik yol ({str(exc)[:200]})", flush=True)
        return None
    except RuntimeError as exc:
        if "Ön kontrol" in str(exc):
            raise
        print(f"[hizli] {job_id}: hizli yol hatasi -> klasik yol ({exc!r})", flush=True)
        return None
    source = fetch.Source(
        audio_path=Path(""),
        title=info.get("title") or url,
        duration=sure or max(s.end for s in segs),
        video_path=None,
        subtitles=segs,
        meta={
            "url": info.get("webpage_url") or url,
            "uploader": info.get("uploader"),
            "upload_date": info.get("upload_date"),
            "language": info.get("language"),
            "hizli_yol": True,
        },
    )
    print(f"[hizli] {job_id}: {len(segs)} konusma, {len(ekran)} ekran "
          f"({round(time.time() - t0)} sn)", flush=True)
    for ad, deger in (("kaynak", source), ("hizli_ekran", ekran)):
        _kayit_yolu(work, ad).write_bytes(pickle.dumps(deger))
    return source, ekran


def _kaynak_gecerli(s) -> bool:
    ses_var = s.subtitles is not None or Path(s.audio_path).exists()
    return ses_var and (s.video_path is None or Path(s.video_path).exists())


def _kareler_gecerli(shots) -> bool:
    return all(Path(f.path).exists() for f in shots)


# --- Canlı önizleme -------------------------------------------------------------
# İş sürerken hazır olan ara sonuçlar (transkript, biten bölüm özetleri) DB'ye
# (jobs.canli) yazılır; arayüz özetin tamamını beklemeden gösterir. Özet adımı
# düşse bile transkript elde kalır. İş 'done' olunca temizlenir (_bitir).
_CANLI_TRANSKRIPT_SINIR = 150_000  # karakter; uzun videoda satır yükü sınırlı


class _Canli:
    def __init__(self, job_id: str) -> None:
        self.job_id = job_id
        self.veri: dict = {}

    def _yaz(self) -> None:
        try:
            db.canli_yaz(self.job_id, self.veri)
        except Exception as exc:
            print(f"[canli] {self.job_id} yazilamadi: {exc!r}", flush=True)

    def transkript(self, metin: str) -> None:
        self.veri["transkript"] = metin[:_CANLI_TRANSKRIPT_SINIR]
        self._yaz()

    def bolum_ilerleme(self, onek: str = ""):
        """summarize(ilerleme=...) için geri çağırım: biten bölümü ekle + aşama i/n."""
        def _cb(biten: int, toplam: int, s) -> None:
            self.veri.setdefault("bolumler", []).append({
                "baslik": (onek + s.section.title) if onek else s.section.title,
                "start": s.section.start,
                "ozet": s.summary,
                "maddeler": [t for _ts, t in s.points][:8],
            })
            self.veri["bolumler"].sort(key=lambda b: b["start"])
            self._yaz()
            if not onek:
                db.update(self.job_id, stage=f"summarize:{biten}/{toplam}")
        return _cb


def _asama_ilerleme(job_id: str, asama: str):
    def _cb(biten: int, toplam: int) -> None:
        db.update(job_id, stage=f"{asama}:{biten}/{toplam}")
    return _cb


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
    base_title = (job.get("title") or source.title).removesuffix(" — Tüm hali")
    provider = job.get("provider") or llm.provider()
    origin_url = job.get("origin_url") or source.meta.get("url")
    part_len = source.duration / n
    db.update(job_id, title=f"{base_title} — Tüm hali", stage=f"0/{n} parça bitti")

    # Altyazı hazırsa (yerel-transkript json3 / YouTube) parçayı yeniden transkript
    # ETME — hazır segmentleri zamana göre DİLİMLE (split-after). Yoksa (sunucu Groq)
    # parça sesini böl + ayrı transkript et (split-before, 502 izolasyonu). İkisinde de
    # her parça AYRI özetlenir → 6 saatlik metin tek LLM çağrısında max_tokens'ı taşırmaz.
    hazir = source.subtitles is not None
    if hazir:
        part_files = [None] * n
    else:
        async def _bol():
            return await _split_audio(source.audio_path, part_len, work)
        part_files = await _asama(
            work, "ses_parcalari", _bol, lambda ps: bool(ps) and all(p.exists() for p in ps)
        )
        n = len(part_files) or n
    # Koleksiyonu başlıktan BİR KEZ belirle (paralel part'larda yarış olmasın).
    koleksiyon = await summarize.classify_collection(base_title, [], db.distinct_collections())

    # Part'lar PARALEL işlenir (PART_CONCURRENCY kadar aynı anda). Groq transkripti
    # kendi semaforuyla sıralanır (kazanç sınırlı); yerel transkriptte tam paralel.
    sem = asyncio.Semaphore(PART_CONCURRENCY)
    prov = llm.provider()
    canli = _Canli(job_id)
    # Tekrar denemede önceki koşuda BİTMİŞ parçalar yeniden işlenmez (sayaç da
    # onlardan başlar); yalnız düşen parçalar koşar.
    biten = [sum(1 for i in range(n) if (db.get(f"{job_id}p{i + 1}") or {}).get("status") == "done")]
    db.update(job_id, stage=f"{biten[0]}/{n} parça bitti")

    async def _bir_part(i, pf):
        t0 = i * part_len
        aralik = f"{int(t0 // 60)}-{int((t0 + part_len) // 60)}dk"
        p_title = f"{base_title} — Part {i + 1} ({aralik})"
        cid = f"{job_id}p{i + 1}"
        ozet_kaydi = f"part{i + 1}.ozet.{prov}"
        onceki = db.get(cid)
        if onceki and onceki.get("status") == "done":
            p_digest = _kayit_oku(work, ozet_kaydi)
            if p_digest is not None:
                return ("ok", i, p_title, p_digest)
        async with sem:
            # Bir parçanın HERHANGİ bir adımı (transkript, bölümleme, özet) düşerse
            # yalnız o parça kaybedilir; diğerleri tamamlanır. Eskiden transkript
            # dışındaki bir hata gather'ı ve tüm uzun videoyu öldürüyordu.
            try:
                if hazir:
                    # Hazır altyazıyı bu parçanın aralığına göre dilimle (zaman zaten mutlak).
                    p_segs = [s for s in source.subtitles if t0 <= s.start < t0 + part_len]
                    if not p_segs:
                        return ("hata", i, p_title, "Bu parçada altyazı segmenti yok (boş aralık).")
                else:
                    p_raw = await transcribe.transcribe(pf, work)
                    # 0-tabanlı part zamanlarını mutlak zamana kaydır (tam videoya tıklanabilsin).
                    p_segs = [transcribe.Segment(s.start + t0, s.end + t0, s.text) for s in p_raw]
                p_shots = [f for f in shots if t0 <= f.ts < t0 + part_len]
                p_transcript = transcribe.to_timestamped_text(p_segs)
                p_sections = await _asama(
                    work, f"part{i + 1}.bolumler.{prov}",
                    lambda: segment.split_into_sections(p_segs, p_title, p_transcript),
                )
                p_digest = await _asama(
                    work, ozet_kaydi,
                    lambda: summarize.summarize(
                        p_sections, p_transcript, p_shots,
                        canli.bolum_ilerleme(onek=f"Part {i + 1} · "),
                    ),
                )
                p_md = render.render(
                    p_digest, p_title, part_len, source.meta,
                    assets_rel=assets_rel if p_shots else None,
                )
                if onceki is None:
                    db.create_job(cid, job["source"], None, provider)
                out = OUT_DIR / f"{cid}.md"
                out.write_text(p_md, encoding="utf-8")
                await _bitir(
                    cid, title=p_title, collection=koleksiyon,
                    origin_url=origin_url, result_path=str(out),
                    meta=_part_meta(source, part_len, job_id, p_digest, p_shots, assets_rel),
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[part] {i + 1}/{n} HATA: {exc}", flush=True)
                return ("hata", i, p_title, _hata_acikla(exc))
            biten[0] += 1
            db.update(job_id, stage=f"{biten[0]}/{n} parça bitti")
            print(f"[part] {cid} bitti: {p_title}", flush=True)
            return ("ok", i, p_title, p_digest)

    sonuclar = await asyncio.gather(*[_bir_part(i, pf) for i, pf in enumerate(part_files)])
    sonuclar.sort(key=lambda x: x[1])  # part sırasına geri diz
    ozetler = [(t, d) for (s, _i, t, d) in sonuclar if s == "ok"]
    hatalar = [(t, e) for (s, _i, t, e) in sonuclar if s == "hata"]

    # Kaynak başlığı anlamsızsa (m3u8 → "playlist" gibi) içerikten BAŞLIK üret ve hem
    # parent'a hem TÜM parçalara uygula — kütüphanede hangi videoya ait olduğu görünsün
    # ("playlist — Part 1" değil "Veri Bilimi Kursu — Part 1"). Koleksiyonu da iyi
    # başlık + gerçek konularla yeniden sınıfla (ilk sınıflama "playlist" ile kördü).
    KOTU_BASLIK = {"", "playlist", "index", "master", "chunklist", "media", "video", "stream"}
    if base_title.strip().lower() in KOTU_BASLIK and ozetler:
        tum_bolumler = [s.section.title for _t, d in ozetler for s in d.sections]
        tum_konular = [t for _t, d in ozetler for t in d.topics]
        uretilen = await summarize.generate_title(tum_bolumler, tum_konular)
        if uretilen:
            koleksiyon = await summarize.classify_collection(
                uretilen, tum_konular, db.distinct_collections()
            )
            print(f"[title] '{base_title}' -> '{uretilen}' | koleksiyon '{koleksiyon}'", flush=True)
            base_title = uretilen
            yeni_ozetler = []
            for (st, i, _pt, d) in sonuclar:
                if st != "ok":
                    continue
                t0 = i * part_len
                aralik = f"{int(t0 // 60)}-{int((t0 + part_len) // 60)}dk"
                yeni_baslik = f"{base_title} — Part {i + 1} ({aralik})"
                db.update(f"{job_id}p{i + 1}", title=yeni_baslik, collection=koleksiyon)
                yeni_ozetler.append((yeni_baslik, d))
            ozetler = yeni_ozetler  # "Tüm hali"ndeki parça başlıkları da yeni ada geçsin

    # Birleşik "Tüm hali" = bu (parent) iş.
    combined = _birlestir(base_title, ozetler, n, hatalar)
    out = OUT_DIR / f"{job_id}.md"
    out.write_text(combined, encoding="utf-8")
    ilk = ozetler[0][1] if ozetler else None
    await _bitir(
        job_id, collection=koleksiyon,
        title=f"{base_title} — Tüm hali",
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
    # Özet sağlayıcısı pahalı adımlardan ÖNCE doğrulansın (anahtar + OpenRouter
    # bakiyesi). Sorunluysa çalışan bir yedeğe geçilir; hiçbiri yoksa iş saniyeler
    # içinde, hiçbir şey indirilmeden/harcanmadan durur.
    secilen, gecis = await llm.saglayici_sec(job.get("provider") or llm.provider())
    llm.set_provider(secilen)
    if gecis:
        print(f"[llm] {job_id}: {gecis}", flush=True)
        db.update(job_id, provider=secilen)
        job["provider"] = secilen

    work = WORK_DIR / job_id
    work.mkdir(parents=True, exist_ok=True)

    # PDF gezilemeyen bir kaynak (SPEC §0): ayrı hat — ses/kare yok, sayfa var.
    # Ama AYNI defter: segment + summarize + karantina değişmeden koşar.
    # Markdown/metin: en basit hat — transkript/OCR yok, metin zaten okunabilir.
    # Vercel'den tarayıcıyla yüklenen dosya: kaynak "blob:in/...". Önce yerel diske
    # indir; hat (PDF/metin/video ayrımı dahil) oradan hiç değişmeden devam eder.
    if job["source"].startswith(storage.BLOB_ONEK):
        db.update(job_id, status="running", stage="fetch")
        uzanti = Path(job["source"]).suffix.lower()[:10] or ".bin"
        if (_kayit_oku(work, "kaynak", _kaynak_gecerli) is not None
                or _kayit_yolu(work, "sayfalar").exists()):
            # Önceki denemede zaten indirilip işlenmiş: Blob'dan tekrar indirme
            # (başarılı işte Blob kaynağı silinmiş de olabilir). Uzantı yeter —
            # hat seçimi (PDF/metin/video) ona bakıyor.
            job["source"] = str(UPLOAD_DIR / f"{job_id}{uzanti}")
        else:
            job["source"] = str(await asyncio.to_thread(
                storage.indir_kaynak, job_id, job["source"], UPLOAD_DIR
            ))
    kaynak = job["source"]
    if not kaynak.startswith(("http://", "https://")):
        suffix = Path(kaynak).suffix.lower()
        if suffix == ".pdf":
            await _process_document(job_id, Path(kaynak), work)
            return
        if suffix in (".md", ".markdown", ".txt"):
            await _process_text(job_id, Path(kaynak), work)
            return

    # Hızlı yol (YouTube → Gemini videoyu linkiyle izler): indirme/OCR/Whisper yok.
    # Olmazsa sessizce klasik yola dönülür; iş bu yüzden asla düşmez.
    hizli = None
    onceki = _kayit_oku(work, "kaynak", _kaynak_gecerli)
    if onceki is not None and onceki.meta.get("hizli_yol"):
        hizli = (onceki, _kayit_oku(work, "hizli_ekran") or [])
    elif onceki is None and gemini_video.uygun_mu(kaynak):
        db.update(job_id, status="running", stage="hizli")
        hizli = await _hizli_yol(job_id, kaynak, work)

    db.update(job_id, status="running", stage="fetch")
    if hizli is not None:
        source, hizli_ekran = hizli
    else:
        source = await _asama(
            work, "kaynak",
            lambda: fetch.fetch(job["source"], work, job.get("referer"), bool(job.get("audio_only"))),
            _kaynak_gecerli,
        )

    # Agent bir linki indirip yüklediyse dosya adı iş numarasıdır ve link
    # kaybolmuştur. Orijinali geri koyuyoruz: özetteki zaman damgaları videoya
    # tıklanabilsin.
    if job.get("origin_url"):
        source.meta["url"] = job["origin_url"]
    # İşin başlığı (yüklemede orijinal dosya adı, YouTube'da oEmbed başlığı, yer
    # iminde sayfa başlığı) kaynağın adından iyidir: m3u8'in adı "playlist",
    # yüklemenin adı iş numarası. Bölümleme/özet de bu başlığı görür.
    if job.get("title"):
        source.title = job["title"].removesuffix(" — Tüm hali")
    db.update(job_id, title=job.get("title") or source.title)

    # Görsel katman ÖNCE: kareler transkriptten bağımsız (video'dan çıkar). Hem
    # kısa yolda onarımda hem uzun yolda part'lara zaman-damgasıyla dağıtımda lazım.
    assets_rel = f"{job_id}_frames"
    shots: list[frames.Frame] = []
    if hizli is not None:
        # Ekran metni Gemini'den; görüntü dosyası yok → özette resim bağlantısı olmasın.
        shots = [] if job.get("audio_only") else hizli_ekran
        assets_rel = None
    elif source.video_path is not None:
        db.update(job_id, stage="frames")
        shots = await _asama(
            work, "kareler",
            lambda: frames.extract(source.video_path, source.duration, OUT_DIR / assets_rel),
            _kareler_gecerli,
        )

    # UZUN VIDEO: transkriptten ÖNCE part'lara böl. Her part BAĞIMSIZ transkript →
    # bir part'ın Groq 502'si tüm işi öldürmez; tek dev transkript (chunk 25'te
    # patlayan) yok. Altyazı varsa (tüm video için hazır) bölme yok — o yol zaten hızlı.
    n_part = round(source.duration / PART_SECONDS) if source.duration else 0
    if n_part >= 2:
        # Uzun video → HER ZAMAN parça parça (altyazı hazır olsa bile). Tek dev
        # özet 6 saatlik metinde max_tokens'ı taşırıyordu ("Yanıt kesildi"); parça
        # başına özet her LLM çağrısını ~30dk'ya sınırlar. _process_long altyazı
        # hazırsa sesi yeniden transkript etmez, hazır segmentleri zamana böler.
        await _process_long(job_id, job, source, shots, assets_rel, work, n_part)
        # Düşen parça varsa kayıtlar (indirilen video, transkriptler) kalsın:
        # "tekrar dene" yalnız onları işler. Hepsi bittiyse disk boşalsın.
        if not ((db.get(job_id) or {}).get("meta") or {}).get("parts_failed"):
            shutil.rmtree(work, ignore_errors=True)
        return

    if source.subtitles is not None:
        # Hazır altyazı bulundu (fetch aşamasında) — Whisper'a hiç gitmiyoruz.
        db.update(job_id, stage="subtitles")
        segments = source.subtitles
    else:
        db.update(job_id, stage="transcribe")
        segments = await transcribe.transcribe(
            source.audio_path, work, _asama_ilerleme(job_id, "transcribe")
        )

    prov = llm.provider()
    canli = _Canli(job_id)
    raw_transcript = transcribe.to_timestamped_text(segments)
    # Ham transkript HEMEN görünsün: onarım/özet dakikalar sürebilir, düşebilir.
    canli.transkript(raw_transcript)
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
        segments = await _asama(
            work, f"onarim.{prov}", lambda: repair.repair(segments, shots)
        )

    transcript = transcribe.to_timestamped_text(segments)
    if repaired:
        canli.transkript(transcript)
    transcript_path = OUT_DIR / f"{job_id}.transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")
    if repaired:
        # Ham hali de kalsın: onarımın bir şeyi bozup bozmadığı ancak böyle görülür.
        (OUT_DIR / f"{job_id}.transcript.raw.txt").write_text(
            raw_transcript, encoding="utf-8"
        )

    db.update(job_id, stage="segment")
    sections = await _asama(
        work, f"bolumler.{prov}",
        lambda: segment.split_into_sections(segments, source.title, transcript),
    )

    db.update(job_id, stage="summarize")
    digest = await _asama(
        work, f"ozet.{prov}",
        lambda: summarize.summarize(sections, transcript, shots, canli.bolum_ilerleme()),
    )

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

    await _bitir(
        job_id,
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


async def _process_document_long(
    job_id, job, pages, base_title, assets_rel, basliklar, birim="sayfa", kisa="s.",
    kind="document", uyari_md="", ek_meta=None,
) -> None:
    """Uzun belge (kitap): BÖLÜM BÖLÜM özet — her bölüm ayrı kütüphane girdisi, tümü
    'Tüm hali' birleşik. Bölüm sınırı kitabın kendisinden (yer imi / 'Chapter 3' /
    markdown başlığı), yoksa ~35 birimlik eşit parçalar (bkz. document.bolum_araliklari).
    İçindekiler/dizin/künye özetlenmez (metni 'kaynağa sor'da yine var).
    PDF, TXT ve MD aynı yoldan geçer; yalnız birim adı farklı."""
    provider = job.get("provider") or llm.provider()
    base_title = base_title.removesuffix(" — Tüm hali")  # tekrar-işlemede çift eki temizle
    araliklar = [a for a in document.bolum_araliklari(basliklar, len(pages))
                 if not document.gereksiz_mi(a[0])]
    atlanan = len(document.bolum_araliklari(basliklar, len(pages))) - len(araliklar)
    n = len(araliklar)
    db.update(job_id, title=f"{base_title} — Tüm hali", stage=f"0/{n} parça bitti")
    koleksiyon = await summarize.classify_collection(base_title, [], db.distinct_collections())

    # Tüm belgenin metni EN BAŞTA yazılır: parçalar ve 'kaynağa sor' bunu kullanır
    # (parça başına ayrı metin dosyası yok — Blob'un aylık yazma kotası küçük).
    tpath = OUT_DIR / f"{job_id}.transcript.txt"
    tpath.write_text(transcribe.to_timestamped_text(document.to_segments(pages)), encoding="utf-8")

    sem = asyncio.Semaphore(PART_CONCURRENCY)
    work = WORK_DIR / job_id
    prov = llm.provider()
    canli = _Canli(job_id)
    # Tekrar denemede biten parçalar atlanır (bkz. _process_long).
    biten = [sum(1 for i in range(n) if (db.get(f"{job_id}p{i + 1}") or {}).get("status") == "done")]
    db.update(job_id, stage=f"{biten[0]}/{n} parça bitti")

    async def _bir(i):
        ad, a, b = araliklar[i]
        p_pages = pages[a:b]
        if not p_pages:
            return None
        aralik = f"{kisa}{p_pages[0].number}-{p_pages[-1].number}"
        p_title = (f"{base_title} — Part {i + 1}: {ad} ({aralik})" if ad
                   else f"{base_title} — Part {i + 1} ({aralik})")
        cid = f"{job_id}p{i + 1}"
        ozet_kaydi = f"part{i + 1}.ozet.{prov}"
        onceki = db.get(cid)
        if onceki and onceki.get("status") == "done":
            p_dig = _kayit_oku(work, ozet_kaydi)
            if p_dig is not None:
                return ("ok", i, p_title, p_dig)
        async with sem:
            try:
                p_segs = document.to_segments(p_pages)
                if not p_segs:
                    return ("hata", i, p_title, f"Bu parçada okunabilir {birim} yok (taranmış/karantina).")
                p_tr = transcribe.to_timestamped_text(p_segs)
                p_sec = await _asama(
                    work, f"part{i + 1}.bolumler.{prov}",
                    lambda: segment.split_into_sections(p_segs, ad or p_title, p_tr, belge=True),
                )
                p_dig = await _asama(
                    work, ozet_kaydi,
                    lambda: summarize.summarize(
                        p_sec, p_tr, [], canli.bolum_ilerleme(onek=f"Part {i + 1} · ")
                    ),
                )
                p_md = render.render_document(p_dig, p_title, p_pages, assets_rel, birim=birim, kisa=kisa)
                if onceki is None:
                    db.create_job(cid, job["source"], None, provider)
                (OUT_DIR / f"{cid}.md").write_text(p_md, encoding="utf-8")
                await _bitir(
                    cid, title=p_title, collection=koleksiyon,
                    result_path=str(OUT_DIR / f"{cid}.md"),
                    meta={
                        "kind": kind, "part_of": job_id, "bolum": ad or None,
                        "learning_type": p_dig.learning_type, "tur": p_dig.tur,
                        "topics": p_dig.topics, "pages": len(p_pages),
                        "sections": len(p_dig.sections), "critic_added": p_dig.added_by_critic,
                        "critic_types": p_dig.critic_types, "compression": p_dig.compression,
                        "transcript_path": str(tpath),
                    },
                )
                biten[0] += 1
                db.update(job_id, stage=f"{biten[0]}/{n} parça bitti")
                print(f"[belge-part] {cid} bitti: {p_title}", flush=True)
                return ("ok", i, p_title, p_dig)
            except Exception as exc:
                # Bir parçanın LLM çağrısı patlarsa (kesilme, kota, flake) SADECE
                # o parça kaybedilir; diğerleri tamamlanır.
                print(f"[belge-part] {job_id}p{i + 1} HATA: {exc}", flush=True)
                return ("hata", i, p_title, _hata_acikla(exc)[:300])

    sonuclar = [r for r in await asyncio.gather(*[_bir(i) for i in range(n)], return_exceptions=True)
                if r and not isinstance(r, BaseException)]
    sonuclar.sort(key=lambda x: x[1])
    ozetler = [(t, d) for (s, _i, t, d) in sonuclar if s == "ok"]
    hatalar = [(t, e) for (s, _i, t, e) in sonuclar if s == "hata"]

    combined = _birlestir(base_title, ozetler, n, hatalar)
    if atlanan:
        combined += (f"\n<sub>{atlanan} bölüm (içindekiler/dizin/künye gibi) özetlenmedi; "
                     f"metni 'kaynağa sor'da aranabilir.</sub>\n")
    (OUT_DIR / f"{job_id}.md").write_text(uyari_md + combined, encoding="utf-8")
    ilk = ozetler[0][1] if ozetler else None
    await _bitir(
        job_id, collection=koleksiyon,
        title=f"{base_title} — Tüm hali", result_path=str(OUT_DIR / f"{job_id}.md"),
        meta={
            "kind": kind, "is_combined": True, "parts": n,
            "parts_failed": len(hatalar), "pages": len(pages), "atlanan_bolum": atlanan,
            "topics": ilk.topics if ilk else [],
            "learning_type": ilk.learning_type if ilk else "genel",
            "tur": ilk.tur if ilk else "genel",
            "transcript_path": str(tpath),
            **(ek_meta or {}),
        },
    )
    print(f"[belge-part] {job_id} tum hali: {len(ozetler)} ok, {len(hatalar)} hata, "
          f"{atlanan} bolum atlandi", flush=True)


async def _process_document(job_id: str, pdf: Path, work: Path) -> None:
    """PDF hattı: sayfa metni (katman ya da OCR) → aynı segment/summarize/defter.

    Ses/kare yok; sayfa numarası 'zaman' olarak kodlanıyor (bkz. document.py).
    Kurtarılan görsel/sessizlik kavramları PDF'e uymaz (birincil kanal sayfanın
    kendisi) — o alanlar boş kalır, arayüz buna göre uyarlanır.
    """
    if not pdf.exists() and not _kayit_yolu(work, "sayfalar").exists():
        raise RuntimeError(f"PDF bulunamadı: {pdf}")

    # Yükleme orijinal dosya adını başlık yaptıysa koru (yoksa dosya adı = job_id).
    title = (db.get(job_id) or {}).get("title") or pdf.stem
    db.update(job_id, status="running", stage="pages", title=title)

    assets_rel = f"{job_id}_pages"
    # Taranmış PDF sayfa sayfa OCR → saatlerce sürebilir; arayüz 'sayfa N/M' görsün
    # (yoksa 'asıldı' sanılıyor) ve MAX_PDF_PAGES'i aşan kısmı işleme (kuyruk bloku).
    durum = {"toplam": 0, "islenecek": 0}

    def _ilerleme(okunan, islenecek, toplam):
        durum["toplam"], durum["islenecek"] = toplam, islenecek
        db.update(job_id, stage=f"pages:{okunan}/{islenecek}")

    async def _sayfalar():
        p = await asyncio.to_thread(
            document.extract, pdf, OUT_DIR / assets_rel, assets_rel, _ilerleme, MAX_PDF_PAGES
        )
        # Bölüm başlıkları da burada (PDF henüz elde): tekrar denemede PDF silinmiş olabilir.
        basliklar = await asyncio.to_thread(document.pdf_bolum_basliklari, pdf, p)
        return {"pages": p, "durum": dict(durum), "basliklar": basliklar}

    # Taranmış PDF'in OCR'ı saatler sürebilir: tekrar denemede yeniden yapılmasın.
    kayit = await _asama(work, "sayfalar", _sayfalar)
    pages = kayit["pages"]
    durum.update(kayit["durum"])
    sinirli = 0 < durum["islenecek"] < durum["toplam"]
    segments = document.to_segments(pages)
    if not segments:
        raise RuntimeError(
            "Hiçbir sayfadan güvenilir metin çıkmadı — belge tümüyle taranmış "
            "ve okunamadı olabilir. Karantina kanıtları defterde."
        )

    # Uzun PDF (kitap) → bölüm bölüm: tek dev özet 200 sayfada max_tokens'ı taşırıyor
    # ve gezilmesi zor. Kısa belge (makale, slayt destesi) tek özet kalır.
    if len(pages) > UZUN_BELGE_BIRIM:
        uyari = ""
        if sinirli:
            uyari = (f"> ⚠️ Bu PDF **{durum['toplam']} sayfa**; işlem süresi için yalnız ilk "
                     f"**{durum['islenecek']} sayfa** işlendi (env `MAX_PDF_PAGES`).\n\n")
        await _process_document_long(
            job_id, db.get(job_id) or {}, pages, title, assets_rel, kayit.get("basliklar") or [],
            birim="sayfa", kisa="s.", kind="document", uyari_md=uyari,
            ek_meta={"pdf_toplam_sayfa": durum["toplam"] or len(pages), "pdf_sinirli": sinirli},
        )
        # Düşen parça varsa kayıtlar kalsın: "tekrar dene" yalnız onları işler.
        if not ((db.get(job_id) or {}).get("meta") or {}).get("parts_failed"):
            shutil.rmtree(work, ignore_errors=True)
        return

    transcript = transcribe.to_timestamped_text(segments)
    transcript_path = OUT_DIR / f"{job_id}.transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")
    prov = llm.provider()
    canli = _Canli(job_id)
    canli.transkript(transcript)

    db.update(job_id, stage="segment")
    sections = await _asama(
        work, f"bolumler.{prov}",
        lambda: segment.split_into_sections(segments, title, transcript, belge=True),
    )

    db.update(job_id, stage="summarize")
    digest = await _asama(
        work, f"ozet.{prov}",
        lambda: summarize.summarize(sections, transcript, [], canli.bolum_ilerleme()),
    )

    # LLM belgeyi de bir çalışmaya (koleksiyona) otomatik atar — konuya göre.
    koleksiyon = await summarize.classify_collection(
        title, digest.topics, db.distinct_collections()
    )
    db.update(job_id, collection=koleksiyon)

    db.update(job_id, stage="render")
    markdown = render.render_document(digest, title, pages, assets_rel)
    if sinirli:
        # Sessizce kırpmak ürünün DÜRÜSTLÜK tezine aykırı — özetin başında söyle.
        markdown = (
            f"> ⚠️ Bu PDF **{durum['toplam']} sayfa**; işlem süresi için yalnız "
            f"**ilk {durum['islenecek']} sayfa** özetlendi. Tamamı için PDF'i bölüp "
            f"ayrı ayrı yükleyebilir ya da (sunucu env) `MAX_PDF_PAGES` değerini "
            f"artırabilirsin.\n\n"
        ) + markdown
    out_path = OUT_DIR / f"{job_id}.md"
    out_path.write_text(markdown, encoding="utf-8")

    okunan = sum(1 for p in pages if not p.quarantined and p.text.strip())
    oranlar = [p.word_ratio for p in pages if p.word_ratio is not None]

    await _bitir(
        job_id,
        result_path=str(out_path),
        meta={
            "kind": "document",
            "learning_type": digest.learning_type,
            "tur": digest.tur,
            "topics": digest.topics,
            "pages": len(pages),
            "pdf_toplam_sayfa": durum["toplam"] or len(pages),
            "pdf_sinirli": sinirli,
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

    pages, basliklar = await asyncio.to_thread(document.extract_metin, path)
    segments = document.to_segments(pages)
    if not segments:
        raise RuntimeError("Dosyada özetlenecek metin yok (boş ya da yalnızca başlık).")

    # Uzun metin (kitap) → bölüm bölüm, PDF kitaplarla aynı yol. Birim ~2500
    # karakter ("kesit", kabaca bir kitap sayfası) — sayfası olmayan kaynağa "sayfa"
    # demek yanlış olurdu.
    if len(pages) > UZUN_BELGE_BIRIM:
        await _process_document_long(
            job_id, db.get(job_id) or {}, pages, title, "", basliklar,
            birim="kesit", kisa="k.", kind="markdown",
            ek_meta={"words": sum(len(p.text.split()) for p in pages)},
        )
        if not ((db.get(job_id) or {}).get("meta") or {}).get("parts_failed"):
            shutil.rmtree(work, ignore_errors=True)
        return

    transcript = transcribe.to_timestamped_text(segments)
    transcript_path = OUT_DIR / f"{job_id}.transcript.txt"
    transcript_path.write_text(transcript, encoding="utf-8")

    db.update(job_id, stage="segment")
    sections = await segment.split_into_sections(segments, title, transcript, belge=True)

    db.update(job_id, stage="summarize")
    digest = await summarize.summarize(sections, transcript, [])

    koleksiyon = await summarize.classify_collection(
        title, digest.topics, db.distinct_collections()
    )
    db.update(job_id, collection=koleksiyon)

    db.update(job_id, stage="render")
    markdown = render.render_document(
        digest, title, pages, assets_rel="", birim="kesit", kisa="k."
    )
    out_path = OUT_DIR / f"{job_id}.md"
    out_path.write_text(markdown, encoding="utf-8")

    kelime = sum(len(p.text.split()) for p in pages)
    await _bitir(
        job_id,
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


# Çalışan işin başlangıç anı: _bitir yalnız bu andan sonra yazılan çıktıları yükler.
_is_t0: float = 0.0


async def _bitir(job_id: str, **alanlar) -> None:
    """İşi 'done' işaretle — ama ÖNCE çıktılarını kalıcı depoya (Blob) yükle.

    Sıra kritik: 'done' görünür görünmez arayüz özeti ister. Önce işaretleyip sonra
    yüklersek (eski sıra) kısa süre "özet bulunamadı" döner; işçi tam o arada
    kapanırsa (PC kapandı, Ctrl+C) iş kalıcı olarak 'done' ama özetsiz kalır
    (testte yakalandı). Coolify'da (Blob yok) yükleme no-op, davranış aynı.
    Parça işleri de buradan geçer; storage aynı dosyayı iki kez yüklemez.
    """
    await asyncio.to_thread(storage.sync_since, _is_t0)
    # Canlı önizlemenin işi bitti: asıl özet artık hazır (satır da şişmesin).
    db.update(job_id, status="done", stage="done", canli=None, **alanlar)


def _saklama_temizle(job_id: str) -> None:
    """İş bittikten sonra yüklenen kaynağı (en büyük dosya) sil — özet/transkript/
    slaytlar kalır. delete_job ile aynı desen (UPLOAD_DIR/<id>.*); origin_url DB'de
    durduğu için zaman damgaları hâlâ tıklanabilir.
    """
    if not DELETE_SOURCE_AFTER_DONE:
        return
    # Vercel: tarayıcının Blob'a yüklediği kaynak da gitsin (Hobby depolama kotası).
    kaynak = (db.get(job_id) or {}).get("source") or ""
    if kaynak.startswith(storage.BLOB_ONEK):
        try:
            storage.sil_kaynak(kaynak)
            print(f"[saklama] {job_id} Blob kaynagi silindi", flush=True)
        except Exception as exc:
            print(f"[saklama] {job_id} Blob kaynagi silinemedi: {exc!r}", flush=True)
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
    elif ("401" in dl or "user not found" in dl or "invalid api key" in dl
          or "unauthorized" in dl) and "anahtarı geçersiz" not in dl:
        a = ("Özet sağlayıcısının API anahtarı geçersiz. Ev bilgisayarındaki .env'de "
             "düzelt ya da işi başka bir sağlayıcıyla tekrar gönder.")
    elif "429" in dl or "rate limit" in dl or "kota" in dl or "quota" in dl or "too many" in dl:
        a = ("Sağlayıcı kotası doldu (dakikalık/günlük istek ya da token sınırı). "
             "Öneri: birkaç dakika bekleyip tekrar dene; ya da arayüzden farklı "
             "sağlayıcı seç (OpenRouter/Gemini/Groq).")
    elif "sign in to confirm" in dl or ("youtube" in dl and "bot" in dl):
        a = ("YouTube geçici bir 'bot kontrolü' yaptı; 3 kez aralıklı denendi, geçmedi. "
             "Genelde birkaç dakika sonra kendiliğinden kalkar: 'tekrar dene'. Sürekli "
             "oluyorsa yt-dlp güncellenmeli (ev bilgisayarında).")
    elif "postprocessing" in dl or "conversion failed" in dl or "audio conversion" in dl:
        a = ("Video İNDİ ama yt-dlp'nin ses dönüştürme adımı (ffmpeg) başarısız oldu — "
             "indirme değil, işleme sorunu (link/referer ile ilgisi yok). Genelde 'sadece "
             "ses' seçilince ffmpeg sesi hedef biçime çeviremeyince olurdu; bu artık "
             "düzeltildi (ses native indirilip tek adımda WAV'a çevriliyor). Öneri: "
             "'tekrar dene' — güncel kodla geçmeli. Sürerse 'Video' içerik türüyle dene.")
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


_CALISMA_OMRU_SN = 3 * 86400


def _eski_calismalari_temizle(haric: str) -> None:
    """Hata alıp tekrar denenmeyen işlerin çalışma klasörleri (indirilmiş video
    dahil) 3 gün sonra silinir; disk sessizce şişmesin."""
    if not WORK_DIR.exists():
        return
    sinir = time.time() - _CALISMA_OMRU_SN
    for d in WORK_DIR.iterdir():
        try:
            if d.is_dir() and d.name != haric and d.stat().st_mtime < sinir:
                shutil.rmtree(d, ignore_errors=True)
                print(f"[saklama] eski calisma klasoru silindi: {d.name}", flush=True)
        except OSError:
            pass


async def _run_one(job_id: str) -> None:
    t0 = time.time()  # işlenme süresi: kuyruk beklemesi HARİÇ, running→done
    _eski_calismalari_temizle(job_id)
    global _is_t0
    _is_t0 = t0
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
                         storage.read_text(job["result_path"])),
            "meta": job.get("meta"),
        }
    except Exception as exc:
        traceback.print_exc()
        # Hatayı LLM'le paylaşılabilir açıklamayla zenginleştir (ne demek + öneri).
        aciklama = _hata_acikla(exc)
        db.update(job_id, status="error", stage="error", error=aciklama)
        # Çalışma klasörü KALIR: "tekrar dene" indirilmiş medyayı (yt-dlp mevcut
        # dosyayı atlar) ve transkripti (transcribe önbelleği) yeniden kullansın —
        # eskiden hata sonrası her şey baştan yapılıyordu (israf). Eskiyenleri
        # _eski_calismalari_temizle siler.
        payload = {"job_id": job_id, "status": "error", "error": aciklama}

    # Vercel: bu işte üretilen çıktıları (ana iş + parça işleri + görseller)
    # geçici diskten kalıcı Blob'a kopyala. Hatada da: hatadan önce biten parçalar
    # kaybolmasın. Coolify'da (token yok) no-op.
    try:
        n = await asyncio.to_thread(storage.sync_since, t0)
        if n:
            print(f"[blob] {job_id}: {n} dosya yuklendi", flush=True)
    except Exception as exc:
        print(f"[blob] {job_id} senkron HATA: {exc!r}", flush=True)

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
    # Bitmiş ama bazı parçaları düşmüş uzun iş de yeniden denenebilir: biten
    # parçalar atlanır (kaldığı yerden devam), yalnız düşenler işlenir.
    eksik_parca = job["status"] == "done" and (job.get("meta") or {}).get("parts_failed")
    if job["status"] not in ("error", "cancelled") and not eksik_parca:
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
