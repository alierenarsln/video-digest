"""Kalıcı çıktı deposu — iki modlu (yerel disk | Vercel Blob).

Coolify'da (token yok) çıktılar OUT_DIR'de, kalıcı volume'da durur; bu modül yalnız
diski okur — davranış değişmez. Vercel'de disk geçici (/tmp, çağrı bitince gider):
hat dokunulmadan çalışsın diye dosyalar YİNE yerel OUT_DIR'e yazılır, iş bitince
`sync_since` ile Blob'a kopyalanır; sonraki okumalar yerelde yoksa Blob'dan gelir.

Anahtar = OUT_DIR'e göre yol, "out/" önekiyle: out/<id>.md, out/<id>_frames/f.jpg.
Depo PRIVATE: özetler/slaytlar herkese açık URL almaz, sunucu token'la okur.
"""

import mimetypes
from pathlib import Path, PurePosixPath

from .config import BLOB_ACCESS, BLOB_READ_WRITE_TOKEN, OUT_DIR

USE_BLOB = bool(BLOB_READ_WRITE_TOKEN)
_PREFIX = "out/"


def job_id_of(name: str) -> str:
    """<id>.md | <id>.transcript.txt | <id>_frames | <id>_pages → <id>
    Parça işleri (<id>p1...) ayrı iştir. main._job_id_of da bunu kullanır."""
    for ek in ("_frames", "_pages"):
        if name.endswith(ek):
            return name[: -len(ek)]
    return name.split(".")[0]


def safe_rel(rel: str) -> str | None:
    """URL'den gelen göreli yolu doğrula: '..' / mutlak yol / sürücü harfi / boş
    → None. (Windows'ta OUT_DIR / "C:/x" mutlak yola sıçrardı.)"""
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    if (
        not parts
        or parts[0].startswith("/")
        or any(p in ("..", ".", "") or ":" in p for p in parts)
    ):
        return None
    return "/".join(parts)


def _rel_of(path) -> str:
    """Bir çıktı yolunun OUT_DIR'e göre göreli hali. Başka makinenin mutlak yolu
    (ör. Coolify'dan gelen result_path) ise yalnız dosya adı alınır."""
    p = Path(path)
    try:
        return p.resolve().relative_to(OUT_DIR.resolve()).as_posix()
    except ValueError:
        return p.name


def _blob():
    from vercel import blob  # tembel içe aktarım: Coolify'da SDK hiç yüklenmez

    return blob


def save(path: Path) -> None:
    """Yerelde yazılmış bir çıktıyı Blob'a kopyala (Blob modu değilse no-op)."""
    if not USE_BLOB:
        return
    ct = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    if path.suffix in (".md", ".txt"):
        ct = "text/plain; charset=utf-8"
    _blob().put(
        _PREFIX + _rel_of(path),
        path.read_bytes(),
        access=BLOB_ACCESS,
        content_type=ct,
        overwrite=True,
        token=BLOB_READ_WRITE_TOKEN,
    )


def sync_since(t0: float) -> int:
    """OUT_DIR'de t0'dan beri değişen her dosyayı Blob'a yükler. İş bitince bir kez
    çağrılır: ana iş + parça işleri + slayt/sayfa görselleri tek geçişte gider."""
    if not USE_BLOB or not OUT_DIR.exists():
        return 0
    n = 0
    for p in OUT_DIR.rglob("*"):
        if p.is_file() and p.stat().st_mtime >= t0 - 1:
            save(p)
            n += 1
    return n


def read_rel(rel: str) -> bytes | None:
    """OUT_DIR'e göre göreli yol → içerik. Önce yerel, yoksa Blob. Yoksa None."""
    rel = safe_rel(rel)
    if rel is None:
        return None
    local = OUT_DIR / rel
    if local.is_file():
        return local.read_bytes()
    if not USE_BLOB:
        return None
    b = _blob()
    try:
        return b.get(
            _PREFIX + rel, access=BLOB_ACCESS, token=BLOB_READ_WRITE_TOKEN
        ).content
    except b.BlobNotFoundError:
        return None


def read_text(path) -> str | None:
    """Bir çıktı dosyası (result_path / transcript_path) → metin, yoksa None."""
    data = read_rel(_rel_of(path))
    return data.decode("utf-8", "replace") if data is not None else None


def delete_job(job_id: str) -> int:
    """İşin Blob'daki tüm çıktılarını siler (yerel silme main.delete_job'da).
    Parça işleri (<id>p1...) önek eşleşse de ayrı iştir — dokunulmaz."""
    if not USE_BLOB:
        return 0
    b = _blob()
    urls, cursor = [], None
    while True:
        r = b.list_objects(
            prefix=_PREFIX + job_id, cursor=cursor, token=BLOB_READ_WRITE_TOKEN
        )
        for item in r.blobs:
            ilk = item.pathname[len(_PREFIX):].split("/")[0]
            if job_id_of(ilk) == job_id:
                urls.append(item.url)
        if not r.has_more:
            break
        cursor = r.cursor
    if urls:
        b.delete(urls, token=BLOB_READ_WRITE_TOKEN)
    return len(urls)
