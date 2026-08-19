import asyncio
import io
import re
import secrets
import shutil
import time
import uuid
import zipfile
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

import httpx

from . import auth, db, llm, worker
from .config import (
    ANTHROPIC_API_KEY,
    OPENROUTER_API_KEY,
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
from .pipeline import ask, frames, summarize

STATIC_DIR = Path(__file__).parent / "static"
# Ders materyali (interaktif testler + study-pack PDF'leri) repo kökünde durur;
# /ders altında servis edilir. Oturum korumasının ARKASINDA (login gerekir).
DERS_DIR = Path(__file__).parent.parent / "ders-materyali"


def _authed(request: Request) -> bool:
    """İki yol da geçerli: tarayıcı oturum çerezi VEYA HTTP Basic (script/curl).
    Basic korunuyor ki indir-yukle.ps1 / kaydet.ps1 / upload komutları kırılmasın."""
    tok = request.cookies.get(auth.COOKIE)
    if tok and auth.verify_token(tok):
        return True
    return auth.verify_basic(request.headers.get("authorization"))


class JobRequest(BaseModel):
    source: str
    callback_url: str | None = None
    # Boş = sunucunun varsayılanı (.env / anahtar durumu).
    provider: str | None = None
    # CDN (Bunny/b-cdn.net gibi) referer-gated akışlarda kaynak site. Boş = yok.
    referer: str | None = None
    # true = yalnızca ses işlensin (video indirilmez, ekran-OCR atlanır). Ekranda
    # eğitici bir şey olmayan konuşmalar/podcast'ler için hızlı + gürültüsüz.
    sadece_ses: bool = False
    # Transkript kaynağı: "" / "groq" = sunucu (Groq Whisper, hızlı, kısa videolar);
    # "yerel" = ev bilgisayarı (agent faster-whisper ile PC'de yazar, Groq 502 yok,
    # uzun videolar için). "yerel" seçilirse link agent'a yönlenir (m3u8/CDN dahil).
    transkript: str | None = None


class CollectionUpdate(BaseModel):
    collection: str


class TitleUpdate(BaseModel):
    title: str


class UserCreate(BaseModel):
    username: str
    password: str


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

    # Kimlik: şifreyi DB'ye (hash'li) tohumla. İLK tohumda kurtarma kodu döner —
    # loga BİR KEZ yaz (Coolify loglarından alınır; şifre unutulursa bununla sıfırlanır).
    kurtarma = auth.init()
    if kurtarma:
        print("=" * 60, flush=True)
        print(f"[auth] KURTARMA KODU (bir kez gösterilir, kaydet): {kurtarma}", flush=True)
        print("[auth] Şifreni unutursan /reset sayfasında bu kodla sıfırlarsın.", flush=True)
        print("=" * 60, flush=True)

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


# Kimlik gerektirmeyen yollar: sağlık, login/reset/logout ekranları ve onların
# CSS'i (/static). /out (slayt görselleri = kullanıcı içeriği) KORUNUR.
_MUAF = ("/health", "/login", "/reset", "/logout", "/static")


@app.middleware("http")
async def koruma(request: Request, call_next):
    """Tarayıcı için oturum çerezi, script için HTTP Basic. İkisi de yoksa:
    tarayıcı gezinmesini /login'e yönlendirir (popup YOK), API'ye 401 döner.

    Middleware olarak yazıldı çünkü /out altındaki slayt görselleri StaticFiles
    ile servis ediliyor ve bir dependency onları kapsamazdı.
    """
    if auth.is_open() or request.url.path.startswith(_MUAF):
        return await call_next(request)

    if _authed(request):
        return await call_next(request)

    # Kimliksiz tarayıcı gezinmesi → login sayfası. WWW-Authenticate GÖNDERMİYORUZ
    # (yoksa tarayıcı yine popup açardı). curl zaten Basic'i baştan yolluyor.
    if request.method == "GET" and "text/html" in request.headers.get("accept", ""):
        return RedirectResponse("/login", status_code=302)
    return Response(status_code=401, content="Yetkisiz")


@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


# ── kimlik: login / reset / logout ──────────────────────────────────────────
@app.get("/login", include_in_schema=False)
async def login_page(request: Request):
    # Zaten girişliyse (ya da koruma kapalıysa) doğrudan uygulamaya.
    if auth.is_open() or _authed(request):
        return RedirectResponse("/", status_code=302)
    return FileResponse(STATIC_DIR / "login.html")


@app.post("/login", include_in_schema=False)
async def login_submit(
    kullanici: str = Form(...), sifre: str = Form(...), hatirla: str | None = Form(None)
):
    if not auth.verify_login(kullanici, sifre):
        return RedirectResponse("/login?hata=1", status_code=303)
    token, max_age = auth.make_token(bool(hatirla))
    resp = RedirectResponse("/", status_code=303)
    # httponly: JS okuyamaz (XSS'te çalınamaz); samesite=lax: CSRF'e makul koruma.
    resp.set_cookie(auth.COOKIE, token, max_age=max_age, httponly=True, samesite="lax")
    return resp


@app.post("/logout", include_in_schema=False)
async def logout():
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(auth.COOKIE)
    return resp


@app.get("/reset", include_in_schema=False)
async def reset_page() -> FileResponse:
    return FileResponse(STATIC_DIR / "reset.html")


@app.post("/reset", include_in_schema=False)
async def reset_submit(kod: str = Form(...), sifre: str = Form(...)):
    if not auth.verify_recovery(kod):
        return RedirectResponse("/reset?hata=1", status_code=303)
    yeni_kod = auth.set_password(sifre)  # şifreyi değiştirir + kurtarma kodunu yeniler
    return HTMLResponse(_reset_ok_html(yeni_kod))


def _reset_ok_html(yeni_kod: str) -> str:
    """Sıfırlama başarılı: YENİ kurtarma kodunu bir kez göster (eskisi kullanıldı)."""
    return f"""<!doctype html><html lang="tr" data-theme="light"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Şifre değişti · Tanık</title><link rel="stylesheet" href="/static/ds/styles.css">
<style>body{{background:var(--surface-app);min-height:100vh;margin:0;display:flex;
align-items:center;justify-content:center;padding:var(--space-5)}}
.kutu{{width:100%;max-width:400px;text-align:center}}
.marka{{font-family:var(--font-serif);font-size:42px;font-weight:var(--fw-semibold);
color:var(--text-strong);margin-bottom:var(--space-6)}}.marka b{{color:var(--accent)}}
.kart{{background:var(--surface-card);border:1px solid var(--border-hair);
border-radius:var(--radius-2);padding:var(--space-6)}}
h1{{font-family:var(--font-serif);font-size:var(--fs-h2);margin:0 0 var(--space-3)}}
.ipucu{{color:var(--text-muted);font-size:var(--fs-sm);margin-bottom:var(--space-5);line-height:1.5}}
.kod{{font-family:var(--font-mono);font-size:22px;letter-spacing:2px;color:var(--text-strong);
background:var(--surface-sunken);border:1px dashed var(--border-strong);
border-radius:var(--radius-1);padding:14px;margin-bottom:var(--space-5)}}
.btn{{display:inline-block;font-weight:var(--fw-medium);padding:11px 22px;border-radius:var(--radius-1);
background:var(--accent);color:var(--text-on-accent);text-decoration:none}}</style></head>
<body><div class="kutu"><div class="marka">Tanık<b>.</b></div><div class="kart">
<h1>Şifren değişti ✓</h1>
<p class="ipucu">YENİ kurtarma kodun aşağıda. Eskisi artık geçersiz — bunu güvenli
bir yere KAYDET, bir daha gösterilmez.</p>
<div class="kod">{yeni_kod}</div>
<a class="btn" href="/login">Girişe dön</a></div></div></body></html>"""


@app.get("/api/jobs")
async def list_jobs() -> list[dict]:
    return db.list_jobs()


@app.get("/api/providers")
async def providers() -> list[dict]:
    return _providers()


@app.get("/api/usage")
async def usage() -> dict:
    """OpenRouter anahtarının kalan kullanımı — header'da yüzdelik bar. Limit varsa
    kalan yüzde; pay-as-you-go'da (limitsiz) yalnız harcanan. Diğer sağlayıcıda veri
    yok → {var:false} (bar gizlenir)."""
    if not OPENROUTER_API_KEY:
        return {"var": False}
    try:
        async with httpx.AsyncClient(timeout=10) as c:
            r = await c.get(
                "https://openrouter.ai/api/v1/auth/key",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
            )
            d = (r.json() or {}).get("data") or {}
    except Exception:
        return {"var": False}
    limit = d.get("limit")
    rem = d.get("limit_remaining")
    if limit and rem is not None:
        return {"var": True, "yuzde": max(0, min(100, round(rem / limit * 100))),
                "kalan": round(rem, 2), "limit": limit}
    return {"var": True, "yuzde": None, "kullanilan": d.get("usage")}


@app.get("/api/pending-downloads")
async def pending_downloads() -> list[dict]:
    """Ev makinesindeki agent'ın indirmesi gereken linkler.

    referer: korumalı m3u8/CDN için — agent yt-dlp'ye geçirir (sunucudaki desenle aynı).
    transkript: "yerel" ise agent videoyu indirdikten SONRA PC'de faster-whisper ile
    transkript edip json3'ü altyazı olarak yükler → sunucu Groq'u tümden atlar.
    """
    out = []
    for j in db.list_jobs(200):
        if j["stage"] == "awaiting_download" and j["status"] == "waiting":
            tam = db.get(j["id"]) or {}
            out.append({
                "id": j["id"],
                "source": j["source"],
                "referer": tam.get("referer"),
                "transkript": tam.get("transkript"),
            })
    return out


# Ev-agent'ının son "yaşıyorum" sinyali. Bellekte tutuluyor — sunucu yeniden
# başlarsa sıfırlanır, agent bir sonraki turda yine ping'ler (kalıcılık gereksiz).
_agent_last_seen: float | None = None
_AGENT_ONLINE_ESIK = 90  # sn: bu süre içinde ping geldiyse "çevrimiçi"


@app.post("/api/agent/heartbeat")
async def agent_heartbeat() -> dict:
    """Ev-agent'ı periyodik ping'ler; site 'ev bilgisayarı çevrimiçi mi' bilsin."""
    global _agent_last_seen
    _agent_last_seen = time.time()
    return {"ok": True}


@app.get("/api/agent/status")
async def agent_status() -> dict:
    if _agent_last_seen is None:
        return {"online": False, "ever_seen": False, "seconds_ago": None}
    ago = time.time() - _agent_last_seen
    return {"online": ago <= _AGENT_ONLINE_ESIK, "ever_seen": True, "seconds_ago": int(ago)}


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
    sadece_ses: str | None = Form(None),
) -> dict:
    """Dosya yükleyip iş oluşturur.

    YouTube veri merkezi IP'lerini engellediği için sunucuda link indirmek
    çalışmıyor; videoyu evde indirip buraya yüklemek o duvarı tamamen atlıyor.
    Yükleme bitince makinenizi kapatabilirsiniz, iş sunucuda devam eder.

    sadece_ses: doluysa yüklenen dosyanın görüntüsü olsa bile OCR atlanır.
    """
    secilen = _validate_provider(provider)
    job_id = uuid.uuid4().hex[:12]
    dest, boyut = await _save_upload(file, job_id)
    db.create_job(job_id, str(dest), callback_url or DEFAULT_CALLBACK_URL, secilen)
    # Orijinal dosya adını başlık yap: dosya diskte job_id'ye adlandığı için
    # yoksa başlık "4cf24e9629fc" gibi anlamsız çıkıyordu (kütüphanede bulunmuyordu).
    orig = Path(file.filename or "").stem.strip()
    if orig:
        db.update(job_id, title=orig)
    if sadece_ses:
        db.update(job_id, audio_only=1)
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


# ── başlık: anlamsız ("playlist") başlıkları düzelt + elle yeniden adlandır ──────
_GENERIK_BASLIK = {"", "playlist", "index", "master", "chunklist", "media", "video", "stream"}


def _baz(title: str) -> str:
    """'X — Tüm hali' / 'X — Part 3 (..)' → 'X' (video başlığının çekirdeği)."""
    t = title or ""
    return t.rsplit(" — ", 1)[0].strip() if " — " in t else t.strip()


def _parcalari(parent_id: str, hepsi: list[dict]) -> list[dict]:
    return [p for p in hepsi if (p.get("meta") or {}).get("part_of") == parent_id]


def _grubu_adlandir(parent_id: str, parts: list[dict], baz: str, kol: str | None) -> None:
    """Video-grubunu yeniden adlandır: parent '{baz} — Tüm hali', her parça
    '{baz} — Part N (aralık)' — aralık ekini KORU. kol verilirse koleksiyonu da yaz."""
    pupd = {"title": f"{baz} — Tüm hali"}
    if kol is not None:
        pupd["collection"] = kol
    db.update(parent_id, **pupd)
    for p in parts:
        eski = p.get("title") or ""
        son = eski.rsplit(" — ", 1)[-1] if " — " in eski else "Part"
        upd = {"title": f"{baz} — {son}"}
        if kol is not None:
            upd["collection"] = kol
        db.update(p["id"], **upd)


@app.post("/api/backfill-titles")
async def backfill_titles() -> dict:
    """Mevcut 'playlist' gibi anlamsız başlıklı video-gruplarına içerikten başlık üretir
    (tek seferlik). Başlık üretimi eklenmeden önce işlenmiş videolar için."""
    hepsi = db.list_jobs(1000)
    yenilenen = []
    for j in hepsi:
        m = j.get("meta") or {}
        if not m.get("is_combined") or _baz(j.get("title") or "").lower() not in _GENERIK_BASLIK:
            continue
        parts = _parcalari(j["id"], hepsi)
        konular = []
        for p in parts:
            konular += (p.get("meta") or {}).get("topics") or []
        konular += m.get("topics") or []
        konular = list(dict.fromkeys(t for t in konular if t))
        if not konular:
            continue
        llm.set_provider(j.get("provider") or llm.provider())
        yeni = await summarize.generate_title([], konular)
        if not yeni:
            continue
        kol = await summarize.classify_collection(yeni, konular, db.distinct_collections())
        _grubu_adlandir(j["id"], parts, yeni, kol)
        yenilenen.append(yeni)
    return {"yenilenen": len(yenilenen), "basliklar": yenilenen}


@app.post("/jobs/{job_id}/title")
async def set_title(job_id: str, req: TitleUpdate) -> dict:
    """Başlığı elle değiştir. Video-grubu (parent ya da parça) ise TÜM grubu yeniden
    adlandırır (base + parçalar, aralık korunur); tekil işse yalnız onu."""
    job = db.get(job_id)
    if job is None:
        raise HTTPException(404, "iş bulunamadı")
    yeni = req.title.strip()
    if not yeni:
        raise HTTPException(400, "başlık boş olamaz")
    m = job.get("meta") or {}
    if m.get("is_combined") or m.get("part_of"):
        pid = job_id if m.get("is_combined") else m["part_of"]
        _grubu_adlandir(pid, _parcalari(pid, db.list_jobs(1000)), yeni, None)
    else:
        db.update(job_id, title=yeni)
    return {"ok": True, "title": yeni}


def _guvenli_ad(s: str, uzun: int = 80) -> str:
    """Dosya/klasör adı için güvenli: geçersiz karakterleri boşluğa çevir."""
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', " ", (s or "").strip())
    s = re.sub(r"\s+", " ", s).strip()
    return s[:uzun] or "adsiz"


@app.get("/api/export-zip")
async def export_zip() -> Response:
    """Tüm hazır özetleri tek .zip'te ver — koleksiyona göre klasörlenmiş .md dosyaları."""
    buf = io.BytesIO()
    seen: set[str] = set()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for j in db.list_jobs(2000):
            if j["status"] != "done":
                continue
            # list_jobs result_path döndürmüyor; markdown her zaman OUT_DIR/{id}.md.
            p = OUT_DIR / f"{j['id']}.md"
            if not p.exists():
                continue
            kol = _guvenli_ad(j.get("collection") or "atanmamis", 60)
            ad = _guvenli_ad(j.get("title") or j["id"])
            yol = f"{kol}/{ad}.md"
            if yol in seen:  # aynı ad → id ekle (çakışma)
                yol = f"{kol}/{ad} ({j['id'][:6]}).md"
            seen.add(yol)
            try:
                z.writestr(yol, p.read_text(encoding="utf-8"))
            except Exception:
                continue
    buf.seek(0)
    return Response(
        buf.getvalue(), media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="tanik-kutuphane.zip"'},
    )


# ── kullanıcılar (çok-kullanıcı giriş; kütüphane paylaşımlı) ──────────────────
@app.get("/api/users")
async def get_users() -> dict:
    return {"admin": APP_USER, "users": auth.list_users()}


@app.post("/api/users")
async def create_user(req: UserCreate) -> dict:
    if not auth.add_user(req.username, req.password):
        raise HTTPException(400, "geçersiz: ad boş, admin ile aynı, ya da şifre boş")
    return {"ok": True, "username": req.username.strip()}


@app.delete("/api/users/{username}")
async def delete_user(username: str) -> dict:
    auth.remove_user(username)
    return {"ok": True}


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
    if not auth.is_open() and not _authed(request):
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


def _direkt_medya(url: str) -> bool:
    """Sunucu bu URL'i DOĞRUDAN indirebilir mi? m3u8 / doğrudan medya dosyası ya
    da bilinen CDN (Bunny) → evet. Bunlar YouTube'un IP-engelini yemez; yalnızca
    YouTube gibi SAYFA linkleri agent'ı bekler."""
    alt = url.split("?")[0].lower()
    if alt.endswith((".m3u8", ".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".ts")):
        return True
    return "b-cdn.net" in alt  # Bunny CDN — referer-gated, IP-gated DEĞİL


@app.post("/jobs", status_code=202)
async def create_job(req: JobRequest) -> dict:
    if not req.source.strip():
        raise HTTPException(400, "source boş olamaz")

    secilen = _validate_provider(req.provider)

    job_id = uuid.uuid4().hex[:12]
    kaynak = req.source.strip()
    db.create_job(job_id, kaynak, req.callback_url or DEFAULT_CALLBACK_URL, secilen)
    ref = (req.referer or "").strip() or None
    if ref:
        db.update(job_id, referer=ref)
    if req.sadece_ses:
        db.update(job_id, audio_only=1)
    # Transkript kaynağı: "yerel" seçilirse transkript ev bilgisayarında (agent)
    # üretilecek — sakla ki pending-downloads agent'a bildirsin.
    istek_yerel = (req.transkript or "").strip().lower() == "yerel"
    if istek_yerel:
        db.update(job_id, transkript="yerel")

    if kaynak.startswith(("http://", "https://")):
        # Sunucu YouTube'a erişemiyor (IP-engeli); onun için SAYFA linkleri
        # USE_LOCAL_AGENT açıkken ev agent'ını bekler. Ama m3u8/doğrudan medya
        # (Bunny CDN vb.) engeli yemez → SUNUCUDA iner, lokalde yt-dlp gerekmez.
        # AMA "yerel" transkript seçildiyse video da agent'a gider: transkript
        # PC'de üretilecek, dolayısıyla dosya orada lazım → _direkt_medya baypas.
        if USE_LOCAL_AGENT and (istek_yerel or not _direkt_medya(kaynak)):
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

# Ders materyali: interaktif testler (ör. /ders/ine4106-bpr-drill.html) ve
# study-pack PDF'leri. Oturum korumasının arkasında — girişliysen açılır.
app.mount("/ders", StaticFiles(directory=DERS_DIR, html=True), name="ders")
