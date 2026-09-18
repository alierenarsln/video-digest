"""Coolify → Vercel kitaplık taşıması (tek seferlik araç; tekrar koşulabilir).

Kaynak: Coolify'daki tanik (/api/goc/envanter + /out/<yol>, yönetici Basic).
Hedef : Neon (iş satırları + ek kullanıcılar) ve private Blob (out/<yol>).

Yalnız GEREKENİ taşır: her işin özeti (.md), transkriptleri ve özetin/meta'nın
gerçekten atıf yaptığı görseller. İşi silinmiş (öksüz) dosyalar ve hiçbir yerde
kullanılmayan PDF sayfa görselleri taşınmaz — Vercel Hobby Blob'da her yazma
kotadan (2.000/ay) düşer, aşılırsa Blob 30 gün kilitlenir.

Tekrar koşulabilir: Blob'da aynı boyutta zaten olan dosya atlanır, iş satırları
upsert edilir. Domain geçişinden hemen önce bir kez daha koşulur (aradaki işler).

    python goc-coolify-vercel.py --kuru            # yalnız plan, yazma yok
    python goc-coolify-vercel.py                   # taşı
    python goc-coolify-vercel.py --vercel-env yol  # varsayılan: .env.vercel

Kimlik: Coolify şifresi VIDEO_DIGEST_PASSWORD (kullanıcı env'i); Neon + Blob
.env.vercel'den. Hiçbir gizli değer yazdırılmaz.
"""

import argparse
import base64
import json
import os
import re
import sys
import time

import httpx
import psycopg
from dotenv import dotenv_values

COOLIFY = os.environ.get("COOLIFY_TANIK_URL", "https://tanik.automaterhub.com")
TERMINAL = ("done", "error", "cancelled")
_GORSEL = re.compile(r"\]\(([^)\s]+\.(?:jpg|jpeg|png|webp))\)", re.I)


def _coolify_sifre() -> str:
    pw = os.environ.get("VIDEO_DIGEST_PASSWORD", "")
    if not pw and sys.platform == "win32":
        import winreg
        try:
            k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment")
            pw = winreg.QueryValueEx(k, "VIDEO_DIGEST_PASSWORD")[0]
        except OSError:
            pass
    if not pw:
        sys.exit("VIDEO_DIGEST_PASSWORD yok (Coolify yönetici şifresi).")
    return pw


def _job_id_of(ilk: str) -> str:
    for ek in ("_frames", "_pages"):
        if ilk.endswith(ek):
            return ilk[: -len(ek)]
    return ilk.split(".")[0]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--kuru", action="store_true", help="yalnız plan, yazma yok")
    ap.add_argument("--vercel-env", default=".env.vercel")
    a = ap.parse_args()

    v = {k: (x or "").strip().strip('"') for k, x in dotenv_values(a.vercel_env).items()}
    if not v.get("DATABASE_URL") or not v.get("BLOB_READ_WRITE_TOKEN"):
        sys.exit(f"{a.vercel_env}: DATABASE_URL / BLOB_READ_WRITE_TOKEN eksik")
    tok = v["BLOB_READ_WRITE_TOKEN"]
    from vercel import blob

    auth = "Basic " + base64.b64encode(f"admin:{_coolify_sifre()}".encode()).decode()
    cf = httpx.Client(base_url=COOLIFY, headers={"Authorization": auth}, timeout=120)

    env = cf.get("/api/goc/envanter")
    env.raise_for_status()
    env = env.json()
    isler, kul = env["isler"], env["kullanicilar"]
    var_dosya = {f["yol"]: f["bayt"] for f in env["dosyalar"]}
    idler = {j["id"] for j in isler}
    print(f"Coolify: {len(isler)} iş, {len(kul)} ek kullanıcı, {len(var_dosya)} dosya")

    # --- gereken dosyalar: md + transkript + md/meta'nın atıf yaptığı görseller
    gerek: dict[str, int] = {}
    md_icerik: dict[str, str] = {}
    for j in isler:
        jid = j["id"]
        for ad in (f"{jid}.md", f"{jid}.transcript.txt", f"{jid}.transcript.raw.txt"):
            if ad in var_dosya:
                gerek[ad] = var_dosya[ad]
        if f"{jid}.md" in var_dosya:
            r = cf.get(f"/out/{jid}.md")
            if r.status_code == 200:
                md_icerik[jid] = r.content.decode("utf-8", "replace")
                for yol in _GORSEL.findall(md_icerik[jid]):
                    if yol in var_dosya:
                        gerek[yol] = var_dosya[yol]
        meta = j.get("meta")
        meta = json.loads(meta) if isinstance(meta, str) and meta else (meta or {})
        for q in meta.get("quarantined") or []:
            src = q.get("src")
            if src and src in var_dosya:
                gerek[src] = var_dosya[src]
    oksuz = [y for y in var_dosya if _job_id_of(y.split("/")[0]) not in idler]
    atlanan = len(var_dosya) - len(gerek) - len(oksuz)

    # --- Blob'da zaten olanlar (list: 1 işlem / 1000 kayıt)
    mevcut: dict[str, int] = {}
    imlec = None
    while True:
        r = blob.list_objects(prefix="out/", cursor=imlec, limit=1000, token=tok)
        mevcut.update({b.pathname[4:]: b.size for b in r.blobs})
        if not r.has_more:
            break
        imlec = r.cursor
    yuklenecek = {y: b for y, b in gerek.items() if mevcut.get(y) != b}

    print(f"Taşınacak dosya : {len(gerek)} ({sum(gerek.values())/1024/1024:.1f} MB)")
    print(f"  Blob'da zaten : {len(gerek) - len(yuklenecek)}")
    print(f"  YÜKLENECEK    : {len(yuklenecek)} ({sum(yuklenecek.values())/1024/1024:.1f} MB)"
          f"  -> ~{len(yuklenecek)} Blob yazma işlemi (Hobby: 2.000/ay)")
    print(f"Taşınmayan      : {len(oksuz)} öksüz + {atlanan} atıfsız (kullanılmayan) dosya")
    yarim = [j["id"] for j in isler if j["status"] not in TERMINAL]
    if yarim:
        print(f"Yarım iş (Vercel'de 'tekrar gönderin' hatası olacak): {yarim}")
    if a.kuru:
        print("\n--kuru: hiçbir şey yazılmadı.")
        return

    # --- iş satırları + kullanıcılar → Neon
    with psycopg.connect(v["DATABASE_URL"], prepare_threshold=None) as c:
        sutun = {r[0] for r in c.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name='jobs' AND table_schema=current_schema()")}
        for j in isler:
            s = {k: x for k, x in j.items() if k in sutun and k != "claimed_by"}
            if s["status"] not in TERMINAL:
                s.update(status="error", stage="error",
                         error="Coolify→Vercel taşımasında yarım kaldı; lütfen tekrar gönderin.")
            kol = list(s)
            c.execute(
                f"INSERT INTO jobs ({', '.join(kol)}) VALUES ({', '.join(['%s'] * len(kol))})"
                f" ON CONFLICT (id) DO UPDATE SET "
                + ", ".join(f"{k}=EXCLUDED.{k}" for k in kol if k != "id"),
                [s[k] for k in kol],
            )
        for u in kul:
            c.execute(
                "INSERT INTO users (username, pass_hash, pass_salt, created_at)"
                " VALUES (%s,%s,%s,%s) ON CONFLICT (username) DO NOTHING",
                (u["username"], u["pass_hash"], u["pass_salt"], u["created_at"]),
            )
        print(f"Neon: {len(isler)} iş upsert, {len(kul)} kullanıcı")

    # --- dosyalar → Blob
    t0, n = time.time(), 0
    for yol in sorted(yuklenecek):
        r = cf.get(f"/out/{yol}")
        r.raise_for_status()
        ct = "text/plain; charset=utf-8" if yol.endswith((".md", ".txt")) else (
            r.headers.get("content-type") or "application/octet-stream")
        blob.put("out/" + yol, r.content, access="private", content_type=ct,
                 overwrite=True, token=tok)
        n += 1
        if n % 25 == 0:
            print(f"  {n}/{len(yuklenecek)} yüklendi ({time.time()-t0:.0f} sn)")
    print(f"Blob: {n} dosya yüklendi ({time.time()-t0:.0f} sn)")

    # --- doğrulama: her hazır işin özeti Blob'dan okunabiliyor mu?
    eksik = []
    for j in isler:
        if j["status"] == "done" and f"{j['id']}.md" in var_dosya:
            try:
                icerik = blob.get(f"out/{j['id']}.md", access="private", token=tok).content
                if icerik.decode("utf-8", "replace") != md_icerik.get(j["id"]):
                    eksik.append((j["id"], "içerik farklı"))
            except Exception as exc:
                eksik.append((j["id"], type(exc).__name__))
    print(f"Doğrulama: {sum(1 for j in isler if j['status']=='done')} hazır özet, "
          f"{len(eksik)} sorunlu" + (f" -> {eksik[:5]}" if eksik else " ✓"))


if __name__ == "__main__":
    main()
