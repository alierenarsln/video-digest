import asyncio
import base64
import secrets
import shutil
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, worker
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
    ensure_dirs,
    provider_available,
)
from .pipeline import frames

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


def _providers() -> list[dict]:
    """Anahtarı olan sağlayıcılar + arayüzde gösterilecek artı/eksileri."""
    out = []
    for name, info in PROVIDER_INFO.items():
        if not provider_available(name):
            continue
        out.append({"id": name, "varsayilan": name == LLM_PROVIDER, **info})
    return out


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
    task = asyncio.create_task(worker.loop())
    # Yeniden başlatmadan sağ çıkan işleri kuyruğa geri koy.
    for job_id in db.pending_ids():
        await worker.enqueue(job_id)
    yield
    task.cancel()


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

    secilen = (req.provider or LLM_PROVIDER).strip().lower()
    if not provider_available(secilen):
        raise HTTPException(
            400,
            f"'{secilen}' sağlayıcısının anahtarı tanımlı değil. "
            f"Kullanılabilir: {', '.join(p['id'] for p in _providers()) or 'hiçbiri'}",
        )

    job_id = uuid.uuid4().hex[:12]
    db.create_job(
        job_id,
        req.source.strip(),
        req.callback_url or DEFAULT_CALLBACK_URL,
        secilen,
    )
    await worker.enqueue(job_id)
    return {"job_id": job_id, "status": "queued", "provider": secilen}


@app.get("/jobs/{job_id}")
async def get_job(job_id: str) -> dict:
    job = db.get(job_id)
    if job is None:
        raise HTTPException(404, "iş bulunamadı")
    return job


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
