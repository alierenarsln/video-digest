import json
import sqlite3
import time
from typing import Any, Optional

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    status       TEXT NOT NULL,
    stage        TEXT,
    source       TEXT NOT NULL,
    -- Agent bir linki indirip yükleyince source dosya yolu olur ve link kaybolur.
    -- Orijinali burada saklıyoruz: başlık ve TIKLANABİLİR ZAMAN DAMGALARI için
    -- gerekli — onsuz özetin en değerli özelliği sessizce ölüyor.
    origin_url   TEXT,
    title        TEXT,
    provider     TEXT,
    callback_url TEXT,
    error        TEXT,
    result_path  TEXT,
    meta         TEXT,
    created_at   REAL NOT NULL,
    updated_at   REAL NOT NULL
);
"""


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init() -> None:
    with _conn() as conn:
        conn.executescript(SCHEMA)

        # CREATE TABLE IF NOT EXISTS mevcut tabloya yeni sütun EKLEMEZ; şema
        # büyüdükçe eski kurulumlar sessizce kırılır (SELECT/UPDATE hata verir).
        var = {r["name"] for r in conn.execute("PRAGMA table_info(jobs)")}
        # collection: LLM'in otomatik atadığı çalışma/konu adı (koleksiyon).
        for ad, tanim in (("origin_url", "TEXT"), ("provider", "TEXT"), ("collection", "TEXT")):
            if ad not in var:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {ad} {tanim}")
                print(f"[db] goc: jobs.{ad} sutunu eklendi", flush=True)


def create_job(
    job_id: str, source: str, callback_url: Optional[str], provider: str
) -> None:
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, status, stage, source, provider, callback_url,"
            " created_at, updated_at) VALUES (?, 'queued', 'queued', ?, ?, ?, ?, ?)",
            (job_id, source, provider, callback_url, now, now),
        )


def update(job_id: str, **fields: Any) -> None:
    if "meta" in fields and fields["meta"] is not None:
        fields["meta"] = json.dumps(fields["meta"], ensure_ascii=False)
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k} = ?" for k in fields)
    with _conn() as conn:
        conn.execute(f"UPDATE jobs SET {cols} WHERE id = ?", (*fields.values(), job_id))


def get(job_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        return None
    job = dict(row)
    job["meta"] = json.loads(job["meta"]) if job["meta"] else None
    return job


def list_jobs(limit: int = 50) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id, status, stage, source, origin_url, title, provider, error,"
            " collection, created_at, updated_at, meta FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        job = dict(row)
        job["meta"] = json.loads(job["meta"]) if job["meta"] else None
        out.append(job)
    return out


def distinct_collections() -> list[str]:
    """LLM'in otomatik ataması için: mevcut çalışma/konu adları (en yeni önce)."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT collection, MAX(created_at) c FROM jobs "
            "WHERE collection IS NOT NULL AND collection != '' "
            "GROUP BY collection ORDER BY c DESC"
        ).fetchall()
    return [r["collection"] for r in rows]


def delete_job(job_id: str) -> None:
    with _conn() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))


def all_ids() -> set[str]:
    with _conn() as conn:
        return {r["id"] for r in conn.execute("SELECT id FROM jobs")}


def pending_ids() -> list[str]:
    """Yeniden başlatmadan sağ çıkan işler — kuyruğa geri konur."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
        ).fetchall()
    return [r["id"] for r in rows]
