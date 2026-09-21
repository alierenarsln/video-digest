"""Ev işçisi — Vercel'deki tanik'in işlerini BU bilgisayarda baştan sona işler.

Neden: Vercel sunucusuz. Arka planda dönen worker yok, fonksiyon ≤300 sn, disk
geçici, ffmpeg/Tesseract yok. Medya işini (indirme, transkript, slayt OCR, özet)
bu PC yapar; sonuçlar doğrudan Neon'a (iş kayıtları) ve Blob'a (özetler, slaytlar)
yazılır, arayüz Vercel'de anında görür. Evde süre sınırı yok ve YouTube'u ev
IP'sinden indirir ("bot" engeli yok).

agent.ps1'den (Coolify) farkı: o yalnız indirip sunucuya yüklüyordu; bu, işin
TAMAMINI yapar ve sunucuya dosya yüklemez.

Kurulum (bir kez):
    vercel link --project tanik
    vercel env pull .env.vercel --environment=production
Çalıştırma:
    .venv\\Scripts\\python.exe ev-isci.py

Env sırası: yerel .env (API anahtarları, TESSERACT_CMD, GROQ_API_KEY_2...; yolu
EV_YEREL_ENV ile değişir) → .env.vercel'in DOLU değerleri üstüne (DATABASE_URL,
BLOB token; Sensitive olanlar boş gelir, yereli ezmez) → DATA_DIR zorla ./data-ev.
"""

import asyncio
import os
import socket
import sys
import time
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

KOK = Path(__file__).resolve().parent
ENV_DOSYASI = Path(os.environ.get("EV_ENV", KOK / ".env.vercel"))
BOSTA_BEKLE = 10       # sn: iş yokken yoklama aralığı
KALP_ARALIGI = 30      # sn: "ev bilgisayarı çevrimiçi" sinyali
CANLI_TUT = 60         # sn: işlenen işin updated_at'ini tazeleme (kira)
KIRA = 15 * 60         # sn: bu kadar tazelenmeyen iş sahipsiz sayılır → sıraya geri
IPTAL_BAK = 5          # sn: arayüzden iptal edildi mi


def _env_yukle() -> None:
    if not ENV_DOSYASI.exists():
        sys.exit(
            f"{ENV_DOSYASI} yok. Önce: vercel link --project tanik && "
            f"vercel env pull .env.vercel --environment=production"
        )
    # Yerel .env: API anahtarları + makineye özel ayarlar (TESSERACT_CMD...).
    # override=True ŞART: Windows kullanıcı ortamında ESKİ bir OPENROUTER_API_KEY
    # tanımlıydı; her süreç onu miras alıyor, override'sız .env'deki YENİ anahtar
    # yok sayılıyor ve işler 401 alıyordu (ölçümde yakalandı). Bu PC'de .env esas.
    load_dotenv(Path(os.environ.get("EV_YEREL_ENV", KOK / ".env")), override=True)
    # Vercel env'i ÜSTÜNE — ama yalnız DOLU değerler. Vercel'de "Sensitive" işaretli
    # değişkenler (API anahtarları, şifre) `env pull` ile BOŞ gelir; onlar yerel
    # .env'dekini ezmemeli. Böylece sırlar Vercel'den dışarı hiç çıkmaz; buradan
    # gelen asıl şey DATABASE_URL + BLOB_READ_WRITE_TOKEN.
    for k, v in dotenv_values(ENV_DOSYASI).items():
        if v:
            os.environ[k] = v
    os.environ["DATA_DIR"] = str(KOK / "data-ev")  # yerel .env'in DATA_DIR'i değil
    os.environ["TANIK_NO_DOTENV"] = "1"            # config .env'i tekrar yüklemesin
    url = os.environ.get("DATABASE_URL", "")
    if not url.startswith(("postgres://", "postgresql://")):
        sys.exit("DATABASE_URL Postgres değil — ev işçisi yalnız Neon'la çalışır.")
    if not os.environ.get("BLOB_READ_WRITE_TOKEN"):
        sys.exit("BLOB_READ_WRITE_TOKEN yok — çıktılar Vercel'e ulaşamazdı.")


_env_yukle()

from app import db, worker  # noqa: E402  (env yüklendikten SONRA içe aktarılmalı)
from app.config import ensure_dirs  # noqa: E402

KIMLIK = f"ev:{socket.gethostname()}"
_kullanici_iptali: set[str] = set()


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} [ev] {msg}", flush=True)


async def _kalp_atisi() -> None:
    while True:
        try:
            await asyncio.to_thread(db.kv_set, "agent_seen", KIMLIK)
        except Exception as exc:  # ağ kesintisi işçiyi öldürmesin
            _log(f"kalp atisi hatasi: {exc!r}")
        await asyncio.sleep(KALP_ARALIGI)


async def _canli_tut(jid: str) -> None:
    """Uzun tek bir adım (6 saatlik transkript, uzun LLM çağrısı) sırasında da işin
    sahipli olduğu görünsün; yoksa kira dolunca başka tur işi sıraya geri koyardı."""
    while True:
        await asyncio.sleep(CANLI_TUT)
        try:
            await asyncio.to_thread(db.touch, jid)
        except Exception as exc:
            _log(f"{jid} kira tazelenemedi: {exc!r}")


async def _iptal_izle(jid: str, gorev: asyncio.Task) -> None:
    """Arayüzden 'durdur' → DB'de cancelled. Evdeki görevi de durdur."""
    while not gorev.done():
        await asyncio.sleep(IPTAL_BAK)
        try:
            is_ = await asyncio.to_thread(db.get, jid)
        except Exception:
            continue
        if is_ and is_["status"] == "cancelled":
            _log(f"{jid} arayuzden iptal edildi, durduruluyor")
            _kullanici_iptali.add(jid)
            gorev.cancel()
            return


async def _isle(jid: str) -> None:
    gorev = asyncio.create_task(worker._run_one(jid))
    yan = [asyncio.create_task(_canli_tut(jid)), asyncio.create_task(_iptal_izle(jid, gorev))]
    try:
        await gorev
    except asyncio.CancelledError:
        # İki kaynak: (a) arayüzden iptal → işi bırak, işçi yaşasın; (b) işçi
        # kapatılıyor (Ctrl+C) → yay, main() işi sıraya GERİ koyar (iptal DEĞİL).
        if jid not in _kullanici_iptali:
            raise
        _kullanici_iptali.discard(jid)
        db.update(jid, status="cancelled", stage="cancelled", error="Kullanıcı iptal etti")
    finally:
        for g in yan:
            g.cancel()
    son = db.get(jid) or {}
    _log(f"{jid} bitti: {son.get('status')} ({(son.get('title') or '')[:60]})")


AG_BEKLE = (5, 15, 30, 60)  # sn: ağ yokken artan bekleme (sonuncusu tekrarlanır)


async def _db_hazir() -> None:
    """Açılışta ağ henüz yoksa (PC uykudan/açılıştan yeni kalktı) bekle, çökme.
    Eskiden ilk DNS hatasında süreç kapanıyor ve bir daha açılmıyordu — arayüz
    günlerce 'ev PC kapalı' dedi (yaşandı: 18.09 ve 21.09)."""
    i = 0
    while True:
        try:
            await asyncio.to_thread(db.init)
            return
        except Exception as exc:
            bekle = AG_BEKLE[min(i, len(AG_BEKLE) - 1)]
            i += 1
            _log(f"veritabanina ulasilamadi ({str(exc)[:100]}), {bekle} sn sonra yeniden")
            await asyncio.sleep(bekle)


async def main() -> None:
    ensure_dirs()
    await _db_hazir()
    _log(f"basladi ({KIMLIK}) — Neon + Blob, is bekleniyor")
    kalp = asyncio.create_task(_kalp_atisi())
    son_bakim = 0.0
    aktif: str | None = None
    ag_hata = 0
    try:
        while True:
            # Ağ kopması (uyku/uyanma, modem) döngüyü ÖLDÜRMESİN: eskiden
            # claim_next'teki tek bir DNS hatası süreci kapatıyordu.
            try:
                if time.time() - son_bakim > 60:
                    geri = await asyncio.to_thread(db.requeue_stale, KIRA)
                    for g in geri:
                        _log(f"{g} sahipsiz kalmis (kira doldu), siraya geri kondu")
                    son_bakim = time.time()
                aktif = await asyncio.to_thread(db.claim_next, KIMLIK)
                ag_hata = 0
                if not aktif:
                    await asyncio.sleep(BOSTA_BEKLE)
                    continue
                is_ = db.get(aktif) or {}
                _log(f"{aktif} kapildi: {(is_.get('title') or is_.get('source') or '')[:80]}")
                await _isle(aktif)
                aktif = None
            except Exception as exc:
                # İş kendi hatasını _run_one'da zaten yakalayıp DB'ye yazar; buraya
                # yalnız DB/ağ hataları düşer. Yarım kalan iş kira dolunca sıraya döner.
                bekle = AG_BEKLE[min(ag_hata, len(AG_BEKLE) - 1)]
                ag_hata += 1
                _log(f"is kuyruguna ulasilamadi ({str(exc)[:100]}), {bekle} sn sonra yeniden")
                await asyncio.sleep(bekle)
    finally:
        kalp.cancel()
        # Ctrl+C ile kapatılırken yarım kalan işi hemen sıraya geri koy (kiranın
        # dolmasını beklemesin); PC tekrar açılınca kaldığı yerden başlanır.
        if aktif:
            try:
                db.update(aktif, status="queued", stage="queued", claimed_by=None)
                _log(f"{aktif} yarim kaldi, siraya geri kondu")
            except Exception as exc:
                _log(f"{aktif} geri konamadi: {exc!r}")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        _log("durduruldu")
