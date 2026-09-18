import json
import time
from typing import Any, Optional

from .dbconn import FLOAT, IS_PG, connect as _conn, init_lock

SCHEMA = f"""
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
    created_at   {FLOAT} NOT NULL,
    updated_at   {FLOAT} NOT NULL
)
"""


def _columns(conn) -> set[str]:
    if IS_PG:
        # current_schema() ŞART: yoksa başka bir şemadaki 'jobs' tablosunun
        # sütunları da sayılır, eksik sütun "var" sanılıp ALTER atlanır ve
        # sorgular "column ... does not exist" ile patlar (testte yakalandı).
        rows = conn.execute(
            "SELECT column_name AS name FROM information_schema.columns"
            " WHERE table_name = 'jobs' AND table_schema = current_schema()"
        )
    else:
        rows = conn.execute("PRAGMA table_info(jobs)")
    return {r["name"] for r in rows}


def init() -> None:
    with _conn() as conn:
        init_lock(conn)
        conn.execute(SCHEMA)

        # CREATE TABLE IF NOT EXISTS mevcut tabloya yeni sütun EKLEMEZ; şema
        # büyüdükçe eski kurulumlar sessizce kırılır (SELECT/UPDATE hata verir).
        var = _columns(conn)
        # collection: LLM'in otomatik atadığı çalışma/konu adı (koleksiyon).
        # referer: CDN'den (Bunny gibi) sunucu-tarafı indirmede gereken kaynak site.
        # audio_only: "sadece ses" seçildiyse 1 — video indirilmez, OCR atlanır.
        # transkript: '' /auto | groq | yerel — transkript kaynağı seçimi (video/ses).
        # claimed_by: işi kapan ev işçisinin adı (Vercel modu; bkz. claim_next).
        for ad, tanim in (
            ("origin_url", "TEXT"), ("provider", "TEXT"),
            ("collection", "TEXT"), ("referer", "TEXT"), ("audio_only", "INTEGER"),
            ("transkript", "TEXT"), ("claimed_by", "TEXT"),
            # canli: iş sürerken arayüzün gösterdiği ara sonuç (transkript, biten
            # bölüm özetleri) — JSON. Blob'a değil DB'ye: Blob'un aylık işlem
            # kotası küçük, önizleme sık yazılır. İş bitince silinir.
            ("canli", "TEXT"),
        ):
            if ad not in var:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {ad} {tanim}")
                print(f"[db] goc: jobs.{ad} sutunu eklendi", flush=True)

        # Süreçler arası küçük durum (ev işçisi kalp atışı vb.). Vercel'de bellek
        # örnekler arası paylaşılmıyor; "ev bilgisayarı çevrimiçi" DB'den okunmalı.
        conn.execute(
            f"CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT, t {FLOAT})"
        )


# --- ev işçisi: iş kapma (Vercel modu) ---------------------------------------
# Kapılabilen durumlar: queued (yükleme/belge) ve waiting (link, eskiden agent'ın
# indirmesini bekliyordu — ev işçisi kendisi indirir).
_KAPILIR = ("queued", "waiting")


def claim_next(worker_id: str) -> Optional[str]:
    """Sıradaki işi ATOMİK olarak kap: running'e çek, kimin aldığını yaz, id dön.
    İki işçi aynı işi alamaz (PG: FOR UPDATE SKIP LOCKED; SQLite: koşullu UPDATE)."""
    now = time.time()
    with _conn() as conn:
        if IS_PG:
            row = conn.execute(
                "UPDATE jobs SET status='running', stage='claimed', claimed_by=?,"
                " updated_at=? WHERE id = (SELECT id FROM jobs WHERE status IN (?, ?)"
                " ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED) RETURNING id",
                (worker_id, now, *_KAPILIR),
            ).fetchone()
            return row["id"] if row else None
        row = conn.execute(
            "SELECT id FROM jobs WHERE status IN (?, ?) ORDER BY created_at LIMIT 1",
            _KAPILIR,
        ).fetchone()
        if not row:
            return None
        cur = conn.execute(
            "UPDATE jobs SET status='running', stage='claimed', claimed_by=?,"
            " updated_at=? WHERE id=? AND status IN (?, ?)",
            (worker_id, now, row["id"], *_KAPILIR),
        )
        return row["id"] if cur.rowcount == 1 else None


def requeue_stale(lease_s: float) -> list[str]:
    """Bir ev işçisinin kapıp lease_s boyunca ilerletmediği işleri sıraya geri koy
    (işçi çöktü / PC kapandı). Yalnız claimed_by dolu işler — Coolify'ın kendi
    süreç-içi worker'ının işlerine dokunulmaz."""
    sinir = time.time() - lease_s
    with _conn() as conn:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE status='running' AND claimed_by IS NOT NULL"
            " AND updated_at < ?",
            (sinir,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        for jid in ids:
            conn.execute(
                "UPDATE jobs SET status='queued', stage='queued', claimed_by=NULL,"
                " updated_at=? WHERE id=? AND status='running'",
                (time.time(), jid),
            )
    return ids


def touch(job_id: str) -> None:
    """Koşan işin kirasını tazele (ev işçisi: 'hâlâ bende, ölmedim')."""
    with _conn() as conn:
        conn.execute(
            "UPDATE jobs SET updated_at=? WHERE id=? AND status='running'",
            (time.time(), job_id),
        )


def kv_set(k: str, v: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT INTO kv (k, v, t) VALUES (?, ?, ?) ON CONFLICT (k) DO UPDATE"
            " SET v=excluded.v, t=excluded.t",
            (k, v, time.time()),
        )


def kv_get(k: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT v, t FROM kv WHERE k=?", (k,)).fetchone()
    return dict(row) if row else None


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
    # Önizleme büyük olabilir (transkript); iş yoklamasına binmesin, ayrı uçtan.
    job.pop("canli", None)
    return job


def canli_yaz(job_id: str, veri: dict | None) -> None:
    update(job_id, canli=json.dumps(veri, ensure_ascii=False) if veri else None)


def canli_oku(job_id: str) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT canli FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if not row or not row["canli"]:
        return None
    return json.loads(row["canli"])


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
