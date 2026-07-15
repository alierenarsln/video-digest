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
    title        TEXT,
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


def create_job(job_id: str, source: str, callback_url: Optional[str]) -> None:
    now = time.time()
    with _conn() as conn:
        conn.execute(
            "INSERT INTO jobs (id, status, stage, source, callback_url, created_at, updated_at)"
            " VALUES (?, 'queued', 'queued', ?, ?, ?, ?)",
            (job_id, source, callback_url, now, now),
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
            "SELECT id, status, stage, source, title, error, created_at, updated_at, meta"
            " FROM jobs ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    out = []
    for row in rows:
        job = dict(row)
        job["meta"] = json.loads(job["meta"]) if job["meta"] else None
        out.append(job)
    return out


def pending_ids() -> list[str]:
    """Yeniden başlatmadan sağ çıkan işler — kuyruğa geri konur."""
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status IN ('queued', 'running') ORDER BY created_at"
        ).fetchall()
    return [r["id"] for r in rows]
