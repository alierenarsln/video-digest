"""Gönderim anında hızlı link kontrolü (Vercel'de, iş kuyruğa girmeden).

Ev bilgisayarı kapalıyken gönderilen bozuk bir link saatlerce "sırada" bekleyip
PC açılınca patlıyordu; kullanıcı hatayı çok geç görüyordu. Burada 1-2 saniyede,
indirme yapmadan bakılır:
  YouTube      : oEmbed — video var mı, gizli/silinmiş mi; başlığı da verir.
  m3u8/medya   : ilk baytlar (Range) referer ile — 401/403/404 hemen görünür.
Belirsizlikte (ağ hatası, zaman aşımı, bilinmeyen site) ENGELLENMEZ: asıl kontrol
ev işçisinde (fetch.onkontrol) zaten var. Yalnız KESİN hata reddedilir.
"""

import re

import httpx

_YT = re.compile(r"(?:youtube\.com/(?:watch|shorts/|live/|embed/)|youtu\.be/)", re.I)
_MEDYA = (".m3u8", ".mp4", ".mkv", ".webm", ".mov", ".m4a", ".mp3", ".wav", ".ts")
_TARAYICI = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")


class LinkHatasi(ValueError):
    pass


async def kontrol(url: str, referer: str | None) -> dict:
    """Kesin hata → LinkHatasi (mesaj kullanıcıya). Dönen: {"baslik": ...} (varsa)."""
    try:
        async with httpx.AsyncClient(timeout=8, follow_redirects=True,
                                     headers={"User-Agent": _TARAYICI}) as c:
            if _YT.search(url):
                return await _youtube(c, url)
            if url.split("?")[0].lower().endswith(_MEDYA):
                await _medya(c, url, referer)
    except LinkHatasi:
        raise
    except Exception:
        pass  # ağ belirsizliği: engelleme
    return {}


async def _youtube(c: httpx.AsyncClient, url: str) -> dict:
    r = await c.get("https://www.youtube.com/oembed", params={"url": url, "format": "json"})
    if r.status_code in (400, 404):
        raise LinkHatasi("Video bulunamadı — silinmiş, gizli ya da link hatalı.")
    if r.status_code == 200:
        return {"baslik": (r.json().get("title") or "").strip()}
    # 401/403: video var ama gömme kapalı — indirilebilir, engelleme.
    return {}


async def _medya(c: httpx.AsyncClient, url: str, referer: str | None) -> None:
    h = {"Range": "bytes=0-2047"}
    if referer:
        h["Referer"] = referer
        m = re.match(r"https?://[^/]+", referer)
        if m:
            h["Origin"] = m.group(0)
    r = await c.get(url, headers=h)
    if r.status_code in (401, 403):
        if referer:
            raise LinkHatasi(
                f"Video sunucusu reddetti (HTTP {r.status_code}). Kaynak sayfa adresi "
                f"yanlış olabilir ya da linkin süresi dolmuş — sayfayı yenileyip linki "
                f"yeniden al."
            )
        raise LinkHatasi(
            f"Video korumalı (HTTP {r.status_code}). Videonun olduğu sayfanın adresini "
            f"de ver ya da 'Tanık'a gönder' yer imiyle o sayfadan gönder."
        )
    if r.status_code in (404, 410):
        raise LinkHatasi("Video bulunamadı (HTTP 404) — link bozuk ya da süresi dolmuş.")
