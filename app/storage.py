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
# sync_since'in bu süreçte yüklediği dosyalar: yol -> mtime
_yuklenen: dict[str, float] = {}


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
    # Windows yolu Linux'ta (Vercel) TEK bir dosya adı sanılıyordu: ev işçisinin
    # yazdığı "C:\...\out\<id>.md" → Path.name tüm dizeyi döndürüyor, safe_rel
    # "C:" yüzünden reddediyordu → ev işçisinin işlediği HER özet sitede "özet
    # dosyası bulunamadı" verdi (yaşandı). Ayraçları önce normalleştir.
    p = Path(str(path).replace("\\", "/"))
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
        if not p.is_file():
            continue
        mt = p.stat().st_mtime
        # Aynı içerik (aynı yol + aynı mtime) ikinci kez gitmesin: çok parçalı
        # işte her parça bitişi senkron çağırır; önceki parçaların yüzlerce slayt
        # görseli her seferinde yeniden yüklenmesin.
        if mt < t0 - 1 or _yuklenen.get(str(p)) == mt:
            continue
        save(p)
        _yuklenen[str(p)] = mt
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


# --- Faz 4: tarayıcıdan doğrudan Blob'a yükleme ------------------------------
# Vercel'in istek gövdesi sınırı 4.5 MB: dosya sunucudan geçemez. Tarayıcı
# sunucudan TEK bir yola bağlı, kısa ömürlü bir izin alır ve dosyayı doğrudan
# Blob'a yükler (büyükse parçalı). İş kaydının kaynağı "blob:in/<anahtar>/<ad>";
# ev işçisi işi alınca dosyayı yerel diske indirir, hat oradan aynen devam eder.

BLOB_ONEK = "blob:"
GIRIS_ONEK = "in/"


def istemci_izni(pathname: str, max_bayt: int, gecerlilik_sn: int = 2 * 3600) -> str:
    """@vercel/blob'un generateClientTokenFromReadWriteToken'ının birebir karşılığı
    (JS kaynağından alındı; aynı girdiyle bayt eşdeğerliği testle doğrulandı).

    İzin yalnız verilen pathname'e ve boyut sınırına geçerlidir, üzerine yazamaz;
    RW anahtarı tarayıcıya hiç gitmez. Parçalı yüklemede her parça izni yeniden
    sunduğu için geçerlilik tüm yükleme boyunca sürmeli (varsayılan 2 saat).
    """
    import base64
    import hashlib
    import hmac
    import json
    import time

    store_id = (BLOB_READ_WRITE_TOKEN.split("_") + [""] * 4)[3]
    if not store_id:
        raise RuntimeError("BLOB_READ_WRITE_TOKEN geçersiz (depo kimliği yok)")
    govde = {
        "maximumSizeInBytes": int(max_bayt),
        "allowOverwrite": False,
        "addRandomSuffix": False,
        "pathname": pathname,
        "validUntil": int((time.time() + gecerlilik_sn) * 1000),
    }
    # JSON.stringify ile aynı bayt: boşluksuz, ASCII-dışı kaçışsız, UTF-8.
    veri = base64.b64encode(
        json.dumps(govde, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).decode()
    imza = hmac.new(
        BLOB_READ_WRITE_TOKEN.encode(), veri.encode(), hashlib.sha256
    ).hexdigest()
    return f"vercel_blob_client_{store_id}_" + base64.b64encode(
        f"{imza}.{veri}".encode()
    ).decode()


def kaynak_boyutu(pathname: str) -> int | None:
    """Yüklenen kaynak Blob'da var mı? Varsa boyutu, yoksa None."""
    b = _blob()
    try:
        return b.head(pathname, token=BLOB_READ_WRITE_TOKEN).size
    except b.BlobNotFoundError:
        return None


def indir_kaynak(job_id: str, kaynak: str, hedef_dizin: Path) -> Path:
    """'blob:in/...' kaynağını yerel diske indir (akışla — 1 GB'lık video belleğe
    dolmaz). Yarım kalan indirme .part'ta kalır, bitince yerine taşınır; aynı iş
    yeniden denenirse tamamlanmış dosya tekrar indirilmez."""
    pathname = kaynak[len(BLOB_ONEK):]
    uzanti = Path(pathname).suffix.lower()[:10] or ".bin"
    hedef = hedef_dizin / f"{job_id}{uzanti}"
    if hedef.is_file() and hedef.stat().st_size > 0:
        return hedef
    gecici = hedef.with_name(hedef.name + ".part")
    _blob().download_file(
        pathname, gecici, access=BLOB_ACCESS, token=BLOB_READ_WRITE_TOKEN
    )
    gecici.replace(hedef)
    return hedef


def sil_kaynak(kaynak: str) -> None:
    """İş bitince Blob'daki yüklenmiş kaynağı sil (Hobby depolama kotası)."""
    if USE_BLOB and kaynak.startswith(BLOB_ONEK):
        _blob().delete(kaynak[len(BLOB_ONEK):], token=BLOB_READ_WRITE_TOKEN)
