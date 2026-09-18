"""Hızlı yol: YouTube videosunu Gemini'ye LİNKİYLE ver — indirme, ffmpeg, Whisper,
slayt OCR'ı yok. Gemini videoyu (ses + görüntü) kendisi izler ve iki şey döndürür:
  konuşma : zaman damgalı transkript (Whisper'ın yerine)
  ekran   : ekranda görünen metin (slayt OCR'ının yerine)
Geri kalan hat (bölümleme, özet, eleştirmen, render) hiç değişmeden bunları işler;
özetin biçimi ve "kaynağa sor" aynı kalır. Tek fark: slayt GÖRÜNTÜLERİ yok (video
indirilmediği için), ekran metni ise özete girer.

Neden: klasik yolda süre indirme + kare çıkarma + OCR + transkriptte gidiyordu;
YouTube "bot kontrolü" de yalnız indirmede çıkıyor. Hata/kota/uzunluk sorununda
çağıran klasik yola döner — bu yol hiçbir zaman işi düşürmez.

İki kanal:
  Gemini API (GEMINI_API_KEY)   : videoMetadata ile PENCERELERE bölünür (uzun
                                  videolar paralel, her yanıt küçük). Ücretsiz katman var.
  OpenRouter (video_url)        : pencere desteği yok → yalnız kısa videolar;
                                  hesapta en az $1 bakiye ister (ölçüldü: 402).
"""

import asyncio
import json
import re
from pathlib import Path

import httpx

from ..config import (
    GEMINI_API_KEY,
    GEMINI_BASE_URL,
    GEMINI_MODELS,
    OPENROUTER_API_KEY,
    OPENROUTER_BASE_URL,
    VIDEO_HIZLI,
    VIDEO_HIZLI_MAX_DK,
)
from ..llm import google_istemci
from .frames import Frame
from .transcribe import Segment

_YT = re.compile(r"(?:youtube\.com/(?:watch|shorts/|live/|embed/)|youtu\.be/)", re.I)
PENCERE_SN = 900          # 15 dk: yanıt ~5k token, hızlı ve kesilmez
PENCERE_ESZAMANLI = 4
OPENROUTER_MAX_SN = 40 * 60
_OR_MODEL = "google/gemini-2.5-flash"

_SEMA = {
    "type": "object",
    "properties": {
        "konusma": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t": {"type": "string"}, "metin": {"type": "string"}},
                "required": ["t", "metin"],
            },
        },
        "ekran": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"t": {"type": "string"}, "metin": {"type": "string"}},
                "required": ["t", "metin"],
            },
        },
    },
    "required": ["konusma", "ekran"],
}

_ISTEM = """Bu videonun ({aralik}) iki dökümünü çıkar. JSON döndür.

konusma: Konuşulanların KELİMESİ KELİMESİNE transkripti, konuşulan dilde (çevirme,
özetleme, atlama yok). 10-30 saniyelik parçalar; her parçanın BAŞLADIĞI an "t"
alanında VİDEONUN BAŞINDAN itibaren MM:SS ya da SS:MM:SS olarak.

ekran: Ekranda görünen ve konuşmada söylenmeyebilecek METİN — slayt başlıkları ve
maddeleri, kod, formüller, tablo/grafik etiketleri, terminal çıktısı. Yalnız YENİ
bir ekran/slayt belirdiğinde bir kayıt; "t" o ekranın ilk göründüğü an. Metni
ekranda yazdığı gibi aktar. Konuşan kişinin yüzü, logo, alt yazı ekran değildir.
Ekranda öğretici metin yoksa boş liste."""


class HizliYolYok(RuntimeError):
    """Bu video/ortam için hızlı yol uygun değil — klasik yola dön."""


def uygun_mu(url: str) -> bool:
    return VIDEO_HIZLI and bool(_YT.search(url)) and bool(GEMINI_API_KEY or OPENROUTER_API_KEY)


def _sn(t: str) -> float:
    parca = [float(x) for x in re.findall(r"\d+(?:\.\d+)?", t or "")][:3]
    if not parca:
        return 0.0
    s = 0.0
    for p in parca:
        s = s * 60 + p
    return s


def _cevir(veri: dict, taban: float, bitis: float | None) -> tuple[list[Segment], list[Frame]]:
    """Göreli/mutlak damga karışıklığına dayanıklı: pencere içi damga pencere
    başından küçükse (model pencereye göre saydıysa) tabana eklenir."""
    def mutlak(t: str) -> float:
        s = _sn(t)
        return s + taban if taban and s < taban else s

    def pencerede(s: float) -> bool:
        # Pencere dışına düşen damga modelin hatası: tutarsa bir segmenti saatlerce
        # uzatır ve tıklanabilir zamanı bozar. 30 sn pay (sınır cümleleri).
        return (not taban or s >= taban - 30) and (not bitis or s <= bitis + 30)

    konusma = sorted(
        (x for x in ((mutlak(k.get("t", "")), (k.get("metin") or "").strip())
                     for k in veri.get("konusma") or []) if x[1] and pencerede(x[0])),
        key=lambda x: x[0],
    )
    segs: list[Segment] = []
    for j, (bas, metin) in enumerate(konusma):
        son = konusma[j + 1][0] if j + 1 < len(konusma) else (bitis or bas + 15)
        son = min(son, bas + 60)  # tek segment bir dakikayı geçmez
        segs.append(Segment(bas, max(son, bas + 1), metin))
    kareler = [
        Frame(ts=ts, path=Path(""), text=metin)
        for ts, metin in ((mutlak(e.get("t", "")), (e.get("metin") or "").strip())
                          for e in veri.get("ekran") or [])
        if metin and pencerede(ts)
    ]
    return segs, kareler


async def _gemini_pencere(c: httpx.AsyncClient, url: str, bas: float, son: float | None) -> dict:
    parca: dict = {"fileData": {"fileUri": url, "mimeType": "video/*"}}
    if son is not None:
        parca["videoMetadata"] = {"startOffset": f"{int(bas)}s", "endOffset": f"{int(son)}s"}
    aralik = f"{int(bas // 60)}. dakikadan {int(son // 60)}. dakikaya" if son else "tamamı"
    govde = {
        "contents": [{"role": "user", "parts": [parca, {"text": _ISTEM.format(aralik=aralik)}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _SEMA,
            "maxOutputTokens": 32000,
            # Düşük çözünürlük: kare başına 66 token (varsayılan 258). Slayt
            # metnini okumaya yeter, uzun videoda bağlamı ve maliyeti 4'te 1'e indirir.
            "mediaResolution": "MEDIA_RESOLUTION_LOW",
        },
    }
    son_hata = ""
    # Model zinciri (bkz. config.GEMINI_MODELS): yok/aşırı yüklü/kotası dolu → sıradaki.
    modeller = list(GEMINI_MODELS)
    for deneme in range(len(modeller) + 2):
        model = modeller[min(deneme, len(modeller) - 1)]
        r = await c.post(
            f"{GEMINI_BASE_URL}/models/{model}:generateContent",
            headers={"x-goog-api-key": GEMINI_API_KEY}, json=govde,
        )
        if r.status_code in (404, 429) or r.status_code >= 500:
            son_hata = f"Gemini {model} {r.status_code}"
            await asyncio.sleep(3)
            continue
        if r.status_code != 200:
            raise HizliYolYok(f"Gemini {r.status_code}: {r.text[:200]}")
        aday = (r.json().get("candidates") or [{}])[0]
        if aday.get("finishReason") == "MAX_TOKENS":
            raise HizliYolYok("Gemini yanıtı kesildi (pencere çok yoğun)")
        metin = "".join(p.get("text", "") for p in (aday.get("content") or {}).get("parts") or [])
        try:
            return json.loads(metin)
        except ValueError:
            son_hata = "bozuk JSON"
    raise HizliYolYok(son_hata or "Gemini yanıt vermedi")


async def _gemini(url: str, sure: float) -> tuple[list[Segment], list[Frame]]:
    pencereler: list[tuple[float, float | None]] = []
    if sure and sure > PENCERE_SN * 1.3:
        bas = 0.0
        while bas < sure:
            pencereler.append((bas, min(bas + PENCERE_SN, sure)))
            bas += PENCERE_SN
    else:
        pencereler = [(0.0, None)]
    sem = asyncio.Semaphore(PENCERE_ESZAMANLI)
    async with google_istemci(timeout=600) as c:
        async def bir(b, s):
            async with sem:
                return b, s, await _gemini_pencere(c, url, b, s)
        sonuc = await asyncio.gather(*(bir(b, s) for b, s in pencereler))
    segs: list[Segment] = []
    kareler: list[Frame] = []
    for b, s, veri in sonuc:
        a, k = _cevir(veri, b, s or sure)
        segs += a
        kareler += k
    return segs, kareler


async def _openrouter(url: str, sure: float) -> tuple[list[Segment], list[Frame]]:
    if sure and sure > OPENROUTER_MAX_SN:
        raise HizliYolYok(f"OpenRouter kanalı en fazla {OPENROUTER_MAX_SN // 60} dk (pencere yok)")
    govde = {
        "model": _OR_MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": _ISTEM.format(aralik="tamamı")},
            {"type": "video_url", "video_url": {"url": url}},
        ]}],
        "response_format": {"type": "json_schema",
                            "json_schema": {"name": "dokum", "schema": _SEMA}},
        "max_tokens": 32000,
    }
    async with httpx.AsyncClient(timeout=600) as c:
        r = await c.post(f"{OPENROUTER_BASE_URL}/chat/completions", json=govde,
                         headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"})
    if r.status_code != 200:
        raise HizliYolYok(f"OpenRouter {r.status_code}: {r.text[:200]}")
    secim = (r.json().get("choices") or [{}])[0]
    if secim.get("finish_reason") == "length":
        raise HizliYolYok("OpenRouter yanıtı kesildi")
    metin = (secim.get("message") or {}).get("content") or ""
    ilk, son = metin.find("{"), metin.rfind("}")
    try:
        veri = json.loads(metin[ilk:son + 1])
    except ValueError:
        raise HizliYolYok("OpenRouter bozuk JSON döndürdü") from None
    return _cevir(veri, 0.0, sure)


async def oku(url: str, sure: float) -> tuple[list[Segment], list[Frame]]:
    """(transkript segmentleri, ekran metinleri). Uygun değilse/başarısızsa HizliYolYok."""
    if sure and sure > VIDEO_HIZLI_MAX_DK * 60:
        raise HizliYolYok(f"video {int(sure // 60)} dk; hızlı yol sınırı {VIDEO_HIZLI_MAX_DK} dk")
    hatalar = []
    for ad, kanal, anahtar in (("Gemini", _gemini, GEMINI_API_KEY),
                               ("OpenRouter", _openrouter, OPENROUTER_API_KEY)):
        if not anahtar:
            continue
        try:
            segs, kareler = await kanal(url, sure)
        except (HizliYolYok, httpx.HTTPError) as exc:
            hatalar.append(f"{ad}: {exc}")
            continue
        sorun = _kalite_sorunu(segs, sure)
        if sorun:
            hatalar.append(f"{ad}: {sorun}")
            continue
        return segs, kareler
    raise HizliYolYok("; ".join(hatalar) or "anahtar yok")


def _kalite_sorunu(segs: list[Segment], sure: float) -> str | None:
    """Model transkript yerine ÖZET döndürdüyse (bilinen arıza) klasik yola dön.
    Konuşma hızı ~100-160 kelime/dk; 15'in altı 'döküm değil özet' demek. Sessiz
    uzun demolar da düşük çıkar — onlarda klasik yol zaten daha doğru."""
    if not segs:
        return "boş transkript"
    if sure and sure > 120:
        kelime = sum(len(s.text.split()) for s in segs)
        if kelime / (sure / 60) < 15:
            return f"transkript çok seyrek ({kelime} kelime / {int(sure // 60)} dk) — özetlemiş olabilir"
        kapsam = max(s.end for s in segs)
        if kapsam < sure * 0.7:
            return f"transkript videonun yalnız %{int(100 * kapsam / sure)}'ini kapsıyor"
    return None
