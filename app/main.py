import asyncio
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import db, worker
from .config import (
    ANTHROPIC_API_KEY,
    DEFAULT_CALLBACK_URL,
    ENABLE_FRAMES,
    GROQ_API_KEY,
    LLM_PROVIDER,
    OUT_DIR,
    OUTPUT_LANGUAGE,
    ensure_dirs,
)
from .pipeline import frames

STATIC_DIR = Path(__file__).parent / "static"


class JobRequest(BaseModel):
    source: str
    callback_url: str | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
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


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/jobs")
async def list_jobs() -> list[dict]:
    return db.list_jobs()


@app.get("/health")
async def health() -> dict:
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

    job_id = uuid.uuid4().hex[:12]
    db.create_job(job_id, req.source.strip(), req.callback_url or DEFAULT_CALLBACK_URL)
    await worker.enqueue(job_id)
    return {"job_id": job_id, "status": "queued"}


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
