import asyncio
import base64
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, llm, worker
from .config import (
    ANTHROPIC_API_KEY,
    APP_PASSWORD,
    APP_USER,
    DEFAULT_CALLBACK_URL,
    ENABLE_FRAMES,
    GROQ_API_KEY,
    IN_DOCKER,
    LLM_PROVIDER,
    OUT_DIR,
    OUTPUT_LANGUAGE,
    PROVIDER_INFO,
    UPLOAD_DIR,
    USE_LOCAL_AGENT,
    ensure_dirs,
    provider_available,
)
from .pipeline import ask, frames

STATIC_DIR = Path(__file__).parent / "static"


def _auth_ok(header: str | None) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        raw = base64.b64decode(header[6:]).decode("utf-8")
        user, _, password = raw.partition(":")
    except Exception:
        return False
    # compare_digest: şifre karşılaştırmasını zamanlama saldırısına kapatır.
    return secrets.compare_digest(user, APP_USER) and secrets.compare_digest(
        password, APP_PASSWORD
    )


class JobRequest(BaseModel):
    source: str
    callback_url: str | None = None
    # Boş = sunucunun varsayılanı (.env / anahtar durumu).
    provider: str | None = None


class CollectionUpdate(BaseModel):
    collection: str


class Question(BaseModel):
    question: str
    # Takip soruları için önceki tur(lar): [{"soru","cevap"}]. Sunucu durum
    # tutmuyor — geçmişi istemci taşıyor, her istekte gönderiyor.
    history: list[dict] = []


def _providers() -> list[dict]:
    """Anahtarı olan sağlayıcılar + arayüzde gösterilecek artı/eksileri."""
    out = []
    for name, info in PROVIDER_INFO.items():
        if not provider_available(name):
            continue
        out.append({"id": name, "varsayilan": name == LLM_PROVIDER, **info})
    return out


def _validate_provider(provider: str | None) -> str:
    secilen = (provider or LLM_PROVIDER).strip().lower()
    if not provider_available(secilen):
        raise HTTPException(
            400,
            f"'{secilen}' sağlayıcısının anahtarı tanımlı değil. "
            f"Kullanılabilir: {', '.join(p['id'] for p in _providers()) or 'hiçbiri'}",
        )
    return secilen


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Konteynerde çalışıyorsa dışarı açık kabul edilir: şifresiz açılmasına izin
    # vermiyoruz. Aksi halde linki bulan herkes iş atıp API kotasını yakabilir
    # ve anahtarlar sunucuda duruyor. Yerelde (127.0.0.1) şifre opsiyonel.
    if IN_DOCKER and not APP_PASSWORD:
        raise RuntimeError(
            "APP_PASSWORD tanımlı değil. Konteynerde şifresiz çalıştırmak, servisi "
            "internete açıkken korumasız bırakır (API kotanız yakılabilir). "
            "Coolify'da APP_PASSWORD ortam değişkenini ayarlayın."
        )

    ensure_dirs()
    db.init()

    # Eksik OCR dili sessizce bozuk metin üretir; açılışta yüksek sesle söyle.
    if ENABLE_FRAMES:
        ok, message = frames.check_ocr_langs()
        print(f"[ocr] {'OK' if ok else 'UYARI'}: {message}", flush=True)
    # Referansı tut: asyncio görevlerine güçlü referans tutulmazsa çöp toplayıcı
    # onları çalışırken toplayabiliyor.
    gorevler = [
        asyncio.create_task(worker.loop()),
        asyncio.create_task(worker.kurtarici()),
    ]
    # Yeniden başlatmadan sağ çıkan işleri kuyruğa geri koy.
    for job_id in db.pending_ids():
        await worker.enqueue(job_id)
    yield
    for g in gorevler:
        g.cancel()


app = FastAPI(title="video-digest", lifespan=lifespan)


@app.middleware("http")
async def koruma(request: Request, call_next):
    """APP_PASSWORD doluysa her şeyi HTTP Basic ile korur.

    Middleware olarak yazıldı çünkü /out altındaki slayt görselleri StaticFiles
    ile servis ediliyor ve bir dependency onları kapsamazdı — özet içeriği
    resimlerin içinde de var.
    """
    # /health muaf: konteyner healthcheck'i kimlik bilgisi taşıyamaz. Kimliksiz
    # çağrıda yalnızca {"ok": true} döner, ayrıntı sızmaz (bkz. health()).
    if request.url.path == "/health":
        return await call_next(request)

    if APP_PASSWORD and not _auth_ok(request.headers.get("authorization")):
        return Response(
            status_code=401,
            content="Yetkisiz",
            headers={"WWW-Authenticate": 'Basic realm="video-digest"'},
        )
    return await call_next(request)


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/jobs")
async def list_jobs() -> list[dict]:
    return db.list_jobs()


@app.get("/api/providers")
async def providers() -> list[dict]:
    return _providers()


@app.get("/api/pending-downloads")
async def pending_downloads() -> list[dict]:
    """Ev makinesindeki agent'ın indirmesi gereken linkler."""
    return [
        {"id": j["id"], "source": j["source"]}
        for j in db.list_jobs(200)
        if j["stage"] == "awaiting_download" and j["status"] == "waiting"
    ]


@app.post("/jobs/{job_id}/attach")
async def attach_file(
    job_id: str,
    file: UploadFile = File(...),
    subtitles: UploadFile | None = File(None),
    title: str | None = Form(None),
) -> dict:
    """Agent'ın evde indirdiği dosyayı bekleyen işe bağlar ve kuyruğa alır.

    subtitles: agent linkten altyazı da çekebildiyse json3 dosyası. Sunucu linki
    hiç görmediği için altyazıyı kendisi bulamaz; gönderilmezse Whisper'a düşer
    ve elle yazılmış altyazının kalite + kota avantajı kaybolur.
    """
    job = db.get(job_id)
    if job is None:
        raise HTTPException(404, "iş bulunamadı")
    if job["stage"] != "awaiting_download":
        raise HTTPException(409, f"iş indirme beklemiyor (aşama: {job['stage']})")

    dest, boyut = await _save_upload(file, job_id)

    if subtitles is not None and subtitles.filename:
        # fetch.from_file bu dosyayı videonun yanında arıyor.
        sub_path = UPLOAD_DIR / f"{dest.stem}.subs.json3"
        sub_path.write_bytes(await subtitles.read())
    # Kaynağı indirilen dosyayla değiştiriyoruz ama ORİJİNAL LİNKİ saklıyoruz:
    # onsuz başlık dosya adı (iş numarası) oluyor ve özetteki zaman damgaları
    # videoya tıklanamıyor — ürünün en değerli özelliği sessizce ölüyor.
    db.update(
        job_id,
        source=str(dest),
        origin_url=job["source"],
        title=(title or "").strip() or None,
        status="queued",
        stage="queued",
    )
    await worker.enqueue(job_id)
    return {"job_id": job_id, "bayt": boyut}


async def _save_upload(file: UploadFile, job_id: str) -> tuple[Path, int]:
    # Dosya adına güvenmiyoruz (yol gezinme); yalnızca uzantıyı alıp adı
    # kendimiz üretiyoruz.
    ext = Path(file.filename or "").suffix.lower()[:10] or ".mp4"
    dest = UPLOAD_DIR / f"{job_id}{ext}"

    boyut = 0
    try:
        with dest.open("wb") as fh:
            # Parça parça yaz: 1 GB'lık videoyu belleğe almak sunucuyu düşürür.
            while chunk := await file.read(1024 * 1024):
                fh.write(chunk)
                boyut += len(chunk)
    except Exception:
        dest.unlink(missing_ok=True)
        raise

    if boyut == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "boş dosya")
    return dest, boyut


@app.post("/jobs/upload")
async def upload_job(
    file: UploadFile = File(...),
    provider: str | None = Form(None),
    callback_url: str | None = Form(None),
) -> dict:
    """Dosya yükleyip iş oluşturur.

    YouTube veri merkezi IP'lerini engellediği için sunucuda link indirmek
    çalışmıyor; videoyu evde indirip buraya yüklemek o duvarı tamamen atlıyor.
    Yükleme bitince makinenizi kapatabilirsiniz, iş sunucuda devam eder.
    """
    secilen = _validate_provider(provider)
    job_id = uuid.uuid4().hex[:12]
    dest, boyut = await _save_upload(file, job_id)
    db.create_job(job_id, str(dest), callback_url or DEFAULT_CALLBACK_URL, secilen)
    await worker.enqueue(job_id)
    return {"job_id": job_id, "status": "queued", "provider": secilen, "bayt": boyut}


def _job_id_of(name: str) -> str:
    """data/out içindeki bir girdinin hangi işe ait olduğunu çıkarır.
    Biçimler: <id>.md | <id>.transcript.txt | <id>.transcript.raw.txt | <id>_frames
    """
    if name.endswith("_frames"):
        return name[: -len("_frames")]
    return name.split(".")[0]


def _entries_of(job_id: str) -> list[Path]:
    return [p for p in OUT_DIR.iterdir() if _job_id_of(p.name) == job_id]


def _size_of(path: Path) -> int:
    if path.is_dir():
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())
    return path.stat().st_size


def _remove(path: Path) -> None:
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


@app.post("/jobs/{job_id}/collection")
async def set_collection(job_id: str, req: CollectionUpdate) -> dict:
    """Kullanıcı LLM'in atadığı çalışmayı değiştirir (şeffaf + düzeltilebilir).

    Yeni bir ad da yazabilir (yeni çalışma) ya da mevcut bir adı — gruplama
    ada göre olduğu için birebir aynı ad aynı grup demek.
    """
    if db.get(job_id) is None:
        raise HTTPException(404, "iş bulunamadı")
    ad = req.collection.strip()
    db.update(job_id, collection=ad or None)
    return {"ok": True, "collection": ad}


@app.get("/api/collections")
async def collections() -> list[str]:
    return db.distinct_collections()


@app.delete("/jobs/{job_id}")
async def delete_job(job_id: str) -> dict:
    job = db.get(job_id)
    if job is None:
        raise HTTPException(404, "iş bulunamadı")
    # Koşan işin dosyalarını silmek worker'ı ortasında yakalar.
    if job["status"] == "running":
        raise HTTPException(409, "iş çalışıyor — bitmesini bekleyin")

    silinen, bayt = [], 0
    for p in _entries_of(job_id):
        bayt += _size_of(p)
        silinen.append(p.name)
        _remove(p)

    # Yüklenmiş kaynak dosya OUT_DIR'de değil; ayrıca silinmeli yoksa video
    # diskte öksüz kalır (en büyük dosya odur).
    for p in UPLOAD_DIR.glob(f"{job_id}.*"):
        bayt += _size_of(p)
        silinen.append(p.name)
        _remove(p)

    db.delete_job(job_id)
    return {"silinen": silinen, "bayt": bayt}


@app.post("/api/cleanup")
async def cleanup(uygula: bool = False) -> dict:
    """Veritabanında karşılığı olmayan çıktıları bulur.

    uygula=false (varsayılan) yalnızca listeler — ne silineceğini görmeden
    silmek istemiyoruz.
    """
    bilinen = db.all_ids()
    oksuz = [p for p in OUT_DIR.iterdir() if _job_id_of(p.name) not in bilinen]
    oksuz += [p for p in UPLOAD_DIR.iterdir() if _job_id_of(p.name) not in bilinen]
    bayt = sum(_size_of(p) for p in oksuz)

    if uygula:
        for p in oksuz:
            _remove(p)

    return {
        "uygulandi": uygula,
        "adet": len(oksuz),
        "bayt": bayt,
        "dosyalar": sorted(p.name for p in oksuz),
    }


@app.get("/health")
async def health(request: Request) -> dict:
    # Bu uç korumadan muaf (healthcheck için). Kimliksiz çağrıya yapılandırma
    # ayrıntısı vermiyoruz — hangi anahtarların tanımlı olduğu dahil.
    if APP_PASSWORD and not _auth_ok(request.headers.get("authorization")):
        return {"ok": True}

    ocr_ok, ocr_message = frames.check_ocr_langs() if ENABLE_FRAMES else (True, "kapalı")
    return {
        "ok": True,
        "groq_key": bool(GROQ_API_KEY),
        "anthropic_key": bool(ANTHROPIC_API_KEY),
        "llm_provider": LLM_PROVIDER,
        "output_language": OUTPUT_LANGUAGE or "(kaynağın dili)",
        "ocr_ok": ocr_ok,
        "ocr": ocr_message,
    }


@app.post("/jobs", status_code=202)
async def create_job(req: JobRequest) -> dict:
    if not req.source.strip():
        raise HTTPException(400, "source boş olamaz")

    secilen = _validate_provider(req.provider)

    job_id = uuid.uuid4().hex[:12]
    kaynak = req.source.strip()
    db.create_job(job_id, kaynak, req.callback_url or DEFAULT_CALLBACK_URL, secilen)

    # Sunucu YouTube'a erişemiyorsa link işleri kuyruğa GİRMEZ: ev makinesindeki
    # agent onları indirip /attach ile bağlayana kadar bekler. Böylece telefondan
    # link atıp PC açılınca işlenmesini sağlayabiliyoruz.
    if USE_LOCAL_AGENT and kaynak.startswith(("http://", "https://")):
        db.update(job_id, status="waiting", stage="awaiting_download")
        return {"job_id": job_id, "status": "waiting", "provider": secilen}

    await worker.enqueue(job_id)
    return {"job_id": job_id, "status": "queued", "provider": secilen}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = db.get(job_id)
    if job is None:
        raise HTTPException(404, "iş bulunamadı")
    return job


@app.post("/jobs/{job_id}/ask")
async def ask_job(job_id: str, req: Question) -> dict:
    """Kaynağa soru sor — cevap yalnızca transkriptten gelir, uydurmaz.

    Rakiplerdeki "içerikle sohbet"in dürüst hâli: cevap kaynakta yoksa found=false
    döner ("bunu kaynakta bulamadım"). Transkript iş üretilirken diske yazıldı
    (worker: transcript_path); onu okuyup soruyla birlikte modele veriyoruz.
    """
    job = db.get(job_id)
    if job is None:
        raise HTTPException(404, "iş bulunamadı")
    if job["status"] != "done":
        raise HTTPException(409, f"iş henüz hazır değil (durum: {job['status']})")
    soru = req.question.strip()
    if not soru:
        raise HTTPException(400, "soru boş olamaz")

    meta = job.get("meta") or {}
    tpath = meta.get("transcript_path")
    if not tpath or not Path(tpath).exists():
        raise HTTPException(409, "bu işin transkripti yok — soru sorulamıyor")
    transcript = Path(tpath).read_text(encoding="utf-8")

    # Sağlayıcı iş başına seçilmişti (özetleme hangi modeli kullandıysa sohbet de
    # onu kullansın); windows() ve complete_json bunu ContextVar'dan okur.
    llm.set_provider(job.get("provider") or llm.provider())
    return await ask.answer(job.get("title") or "", transcript, soru, req.history)


@app.post("/jobs/{job_id}/cancel")
async def cancel_job(job_id: str) -> dict:
    """Queued ya da running işi durdur. running olan da iptal edilebilir (o an
    işleyen görev cancel edilir) — DELETE'in yapamadığı buydu."""
    r = worker.cancel(job_id)
    if r is None:
        raise HTTPException(404, "iş bulunamadı")
    if r == "not-active":
        raise HTTPException(409, "iş zaten bitmiş/başlamamış — iptal edilecek bir şey yok")
    return {"ok": True, "durum": "cancelled"}


@app.post("/jobs/{job_id}/retry")
async def retry_job(job_id: str) -> dict:
    """Hata almış ya da iptal edilmiş işi yeniden kuyruğa koy."""
    r = await worker.retry(job_id)
    if r is None:
        raise HTTPException(404, "iş bulunamadı")
    if r == "not-retryable":
        raise HTTPException(409, "yalnızca hata/iptal işleri yeniden denenebilir")
    return {"ok": True, "durum": "queued"}


@app.get("/jobs/{job_id}/markdown", response_class=PlainTextResponse)
async def get_markdown(job_id: str) -> str:
    job = db.get(job_id)
    if job is None:
        raise HTTPException(404, "iş bulunamadı")
    if job["status"] != "done":
        raise HTTPException(409, f"iş henüz hazır değil (durum: {job['status']})")
    with open(job["result_path"], encoding="utf-8") as fh:
        return fh.read()


# Özetteki slayt görüntüleri buradan servis ediliyor; markdown onlara göreli
# yolla (<job_id>_frames/...) referans veriyor.
ensure_dirs()
app.mount("/out", StaticFiles(directory=OUT_DIR), name="out")

# Tasarım sistemi (ds/tokens/*.css) buradan geliyor. index.html artık CSS'i
# gömülü taşımıyor; token'lar zip'ten birebir kopyalandığı için ayrı dosyalar.
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
