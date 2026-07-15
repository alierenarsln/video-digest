"""Düşük kaliteli transkripti anlamlı metne çevirir — zaman damgalarını koruyarak.

Neden gerekli: YouTube'un otomatik altyazısı ve zayıf ASR çıktısı cümle ortasından
bölünmüş satırlar üretir ("...28x28 pixels. But your" / "brain has no trouble..."),
Türkçe gibi az kaynaklı dillerde noktalama tamamen kaybolabilir. Özetleyici bunu
okuyabilir ama SEMANTİK BÖLÜMLEME bozulur: konu sınırı arayan model, cümle sınırı
olmayan bir metinde çalışmak zorunda kalır.

Onarım ÖZETLEME DEĞİLDİR. En büyük risk modelin "temizle" derken metni kısaltması;
buna karşı çıktı/girdi kelime oranı kontrol edilir ve düşükse onarım reddedilip
orijinal korunur.
"""

import asyncio
import re

from ..config import REPAIR_MIN_CAPS, REPAIR_MIN_PUNCT, REPAIR_MODE
from ..llm import complete_json, windows
from .frames import Frame, as_prompt_text, for_range
from .transcribe import Segment, fmt_ts

REPAIR_CONCURRENCY = 3
# Onarım sonrası metin bu oranın altına düşerse model özetlemiş demektir → reddet.
MIN_WORD_RATIO = 0.6

_SCHEMA = {
    "type": "object",
    "properties": {
        "sentences": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ts": {"type": "string"},
                    "text": {"type": "string"},
                },
                "required": ["ts", "text"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sentences"],
    "additionalProperties": False,
}

_SYSTEM = """Sen bir transkript editörüsün. Sana otomatik üretilmiş, bu yüzden cümle \
ortasından bölünmüş ve noktalaması eksik olabilen zaman damgalı satırlar veriliyor. \
Görevin bunu OKUNABİLİR CÜMLELERE çevirmek.

Yapman gerekenler:
- Bölünmüş satırları birleştirip tam cümleler kur.
- Noktalama ve büyük harfleri ekle.
- Bariz ses tanıma hatalarını bağlamdan düzelt (örn. "the SKU is large" → "the skew \
is large"). EKRAN METNİ verilmişse özel isimleri ve terimleri ona göre düzelt \
(örn. ASR "BG slash NBD" diyorsa ve ekranda "BG/NBD" yazıyorsa "BG/NBD" yaz).
- Anlamsız dolgu işaretlerini at: "[Music]", "[Applause]", "ııı", "şey yani".
- Her cümleye, o cümlenin BAŞLADIĞI satırın zaman damgasını ver.

ASLA yapmayacakların:
- ÖZETLEME. Bu bir düzeltme işi, kısaltma işi değil. Her fikir, her sayı, her isim, \
her örnek çıktıda kalmalı. Metin girdiyle yaklaşık aynı uzunlukta olmalı.
- Bilgi eklemek. Transkriptte olmayan hiçbir şey yazma.
- Anlamı değiştirmek. Emin olmadığın bir kelimeyi olduğu gibi bırak.
- Dili değiştirmek. Girdinin dilinde yaz.
- Zaman damgası uydurmak. Yalnızca girdide geçen damgaları kullan."""


def _words(text: str) -> int:
    return len(text.split())


def punct_density(segments: list[Segment]) -> float:
    """100 kelimeye düşen cümle-sonu noktalaması.

    DİKKAT — bu bir KALİTE ölçüsü değil, "noktalama VAR MI" ölçüsüdür. Ölçüldü:
    elle yazılmış altyazı 3.9, YouTube otomatik altyazı 5.0. Yani daha kaliteli
    kaynak DAHA DÜŞÜK skor alıyor, çünkü bu metrik esasen cümle uzunluğunu ölçüyor
    (iyi yazılmış metinde cümleler uzun). Bu yüzden eşik "iyi/kötü" ayırmak için
    değil, yalnızca sıfıra yakın (= noktalama hiç yok) durumu yakalamak için düşük
    tutulur.
    """
    text = " ".join(s.text for s in segments)
    total = _words(text)
    if total == 0:
        return 0.0
    return len(re.findall(r"[.!?]", text)) * 100 / total


def caps_ratio(segments: list[Segment]) -> float:
    """İlk harfi büyük olan satırların oranı. ASR çıktısı çoğu zaman baştan sona
    küçük harftir; noktalama varmış gibi görünse bile bu onu ele verir."""
    starts = []
    for seg in segments:
        for ch in seg.text:
            if ch.isalpha():
                starts.append(ch.isupper())
                break
    if not starts:
        return 1.0
    return sum(starts) / len(starts)


def needs_repair(segments: list[Segment]) -> bool:
    if REPAIR_MODE == "off":
        return False
    if REPAIR_MODE == "always":
        return True
    return punct_density(segments) < REPAIR_MIN_PUNCT or caps_ratio(segments) < REPAIR_MIN_CAPS


def _windows(segments: list[Segment]) -> list[list[Segment]]:
    """Pencerelere böl; mümkünse cümle sonunda veya sessizlik boşluğunda kes ki
    bir cümle iki pencereye bölünmesin."""
    limit = windows()["repair"]
    out: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0

    for i, seg in enumerate(segments):
        current.append(seg)
        size += len(seg.text) + 12
        if size < limit:
            continue

        ends_sentence = seg.text.rstrip().endswith((".", "!", "?"))
        gap = (
            segments[i + 1].start - seg.end > 0.8 if i + 1 < len(segments) else True
        )
        if ends_sentence or gap or size > limit * 1.5:
            out.append(current)
            current, size = [], 0

    if current:
        out.append(current)
    return out


async def _repair_window(window: list[Segment], frames: list[Frame]) -> list[Segment]:
    source = "\n".join(f"[{fmt_ts(s.start)}] {s.text}" for s in window)
    user = f"TRANSKRİPT:\n\n{source}"

    shots = for_range(frames, window[0].start, window[-1].end)
    if shots:
        user += f"\n\n---\n\nEKRAN METNİ (terim ve özel isim düzeltmek için):\n\n{as_prompt_text(shots)}"

    result = await complete_json(
        system=_SYSTEM,
        user=user,
        schema=_SCHEMA,
        effort="medium",
        max_tokens=16000,
    )

    from .segment import parse_ts

    repaired: list[Segment] = []
    for item in result.get("sentences", []):
        text = item["text"].strip()
        if text:
            repaired.append(Segment(start=parse_ts(item["ts"]), end=0.0, text=text))

    if not repaired:
        return window

    # Model özetlemiş mi? Onarım metni kısaltmamalı.
    before = _words(" ".join(s.text for s in window))
    after = _words(" ".join(s.text for s in repaired))
    if before and after / before < MIN_WORD_RATIO:
        print(
            f"[repair] pencere REDDEDILDI: {before} kelime -> {after} kelime "
            f"(orani {after / before:.0%}, esik {MIN_WORD_RATIO:.0%}) - model ozetlemis, "
            f"orijinal korunuyor",
            flush=True,
        )
        return window

    # Bitişleri bir sonrakinin başlangıcından türet; sonuncu pencerenin sonunu alır.
    repaired.sort(key=lambda s: s.start)
    for i, seg in enumerate(repaired):
        seg.end = repaired[i + 1].start if i + 1 < len(repaired) else window[-1].end
    return repaired


async def repair(segments: list[Segment], frames: list[Frame]) -> list[Segment]:
    windows = _windows(segments)
    sem = asyncio.Semaphore(REPAIR_CONCURRENCY)

    async def one(window: list[Segment]) -> list[Segment]:
        async with sem:
            try:
                return await _repair_window(window, frames)
            except Exception as exc:
                # Onarım kozmetik; başarısız olursa iş düşmemeli.
                print(f"[repair] pencere onarilamadi, orijinal korunuyor: {exc}", flush=True)
                return window

    results = await asyncio.gather(*(one(w) for w in windows))
    out = [seg for window in results for seg in window]
    out.sort(key=lambda s: s.start)
    return out
