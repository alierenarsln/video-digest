"""Transkripti sabit uzunlukta değil, KONU sınırlarında böler.

Sabit uzunlukta bölmek, bir fikri ortasından ikiye ayırıp iki yarım özet
üretiyor — jenerik çıktının başlıca sebebi bu. Sınırları modele buldurup
her bölümü kendi içinde bütün bir fikir olarak özetliyoruz.
"""

import asyncio
import re
from dataclasses import dataclass

from ..llm import complete_json, language_rule, windows
from .transcribe import Segment, fmt_ts

BOUNDARY_CONCURRENCY = 3

# Her pencere kendi sınırlarını bulduğu için birleştirince aşırı parçalanma
# oluyor (6 dakikalık klipte 18 bölüm). Birbirine bu kadar yakın sınırları tek
# bölüm sayıyoruz.
MIN_SECTION_SECONDS = 45.0

_BOUNDARY_SCHEMA = {
    "type": "object",
    "properties": {
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "start_ts": {"type": "string"},
                },
                "required": ["title", "start_ts"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["sections"],
    "additionalProperties": False,
}

_SYSTEM = """Sen bir içerik yapılandırma uzmanısın. Sana zaman damgalı bir \
transkript verilecek; görevin konunun gerçekten değiştiği yerleri bulmak.

Kurallar:
- Sınırlar konu değişimine göre olmalı, eşit uzunluğa göre değil. Bir bölüm \
2 dakika, diğeri 25 dakika olabilir.
- Her bölüm kendi başına anlaşılabilir, bütün bir fikir olmalı.
- Başlıklar içeriği söylemeli ("Giriş", "Devamı" gibi boş başlıklar değil; \
"BM/BND modelinin varsayımları" gibi).
- İlk bölüm 00:00'da başlar.
- Tipik olarak 3-12 bölüm. Kısa videoda tek bölüm de olabilir.
- start_ts, transkriptte GERÇEKTEN geçen bir zaman damgası olmalı."""

_SYSTEM += language_rule()


@dataclass
class Section:
    title: str
    start: float
    end: float
    segments: list[Segment]

    @property
    def text(self) -> str:
        return "\n".join(f"[{fmt_ts(s.start)}] {s.text}" for s in self.segments)


def parse_ts(value: str) -> float:
    parts = re.findall(r"\d+", value)
    if not parts:
        return 0.0
    nums = [int(p) for p in parts][-3:]
    total = 0.0
    for n in nums:
        total = total * 60 + n
    return total


def _slice(segments: list[Segment], start: float, end: float) -> list[Segment]:
    return [s for s in segments if start <= s.start < end]


def _split_oversized(section: Section) -> list[Section]:
    """Tek bir konu bile bağlam bütçesini aşabiliyorsa alt parçalara böl."""
    limit = windows()["section"]
    if len(section.text) <= limit:
        return [section]

    parts: list[Section] = []
    current: list[Segment] = []
    size = 0
    for seg in section.segments:
        piece = len(seg.text) + 12
        if size + piece > limit and current:
            parts.append(
                Section(
                    title=f"{section.title} ({len(parts) + 1}. kısım)",
                    start=current[0].start,
                    end=current[-1].end,
                    segments=current,
                )
            )
            current, size = [], 0
        current.append(seg)
        size += piece
    if current:
        parts.append(
            Section(
                title=f"{section.title} ({len(parts) + 1}. kısım)",
                start=current[0].start,
                end=current[-1].end,
                segments=current,
            )
        )
    return parts


def _windows(segments: list[Segment], iskelet: bool = False) -> list[list[Segment]]:
    limit = windows()["boundary"]
    out: list[list[Segment]] = []
    current: list[Segment] = []
    size = 0
    for seg in segments:
        current.append(seg)
        size += (len(_iskelet(seg.text)) if iskelet else len(seg.text)) + 12
        if size >= limit:
            out.append(current)
            current, size = [], 0
    if current:
        # Son kırıntıyı ayrı pencere yapma; öncekine ekle.
        if out and size < limit // 3:
            out[-1].extend(current)
        else:
            out.append(current)
    return out


# Belgede (PDF/metin) bir segment = bir sayfa/kesit ve sınır zaten sayfa başında
# olabilir: modelin tam metni okumasına gerek yok, her sayfanın AÇILIŞI + içindeki
# başlık satırları yeter. Ölçüldü: bölümleme 150 sayfada 85k token okuyup 5k
# token (yalnız sınır listesi) üretiyordu — kitap maliyetinin ~üçte biri.
_ISKELET_ACILIS = 400
_BASLIK_SATIRI = re.compile(
    r"^(#{1,4}\s+\S|(\d+(\.\d+){0,3}|[IVXLC]+)[.)]?\s+[A-ZÇĞİÖŞÜ]|"
    r"(chapter|bölüm|part|kısım|section)\b)",
    re.I,
)


def _iskelet(metin: str) -> str:
    """Sayfanın açılışı + sayfa ortasındaki başlık satırları (en çok 6)."""
    acilis = metin[:_ISKELET_ACILIS]
    basliklar = []
    for satir in metin[_ISKELET_ACILIS:].splitlines():
        s = satir.strip()
        if 3 <= len(s) <= 90 and not s.endswith((".", ",", ";")) and (
            _BASLIK_SATIRI.match(s) or (s.isupper() and len(s.split()) <= 12)
        ):
            basliklar.append(s)
            if len(basliklar) >= 6:
                break
    return acilis + (f" … [sayfa içi başlıklar: {' / '.join(basliklar)}]" if basliklar else " …")


async def _boundaries(
    window: list[Segment], title: str, belge: bool = False
) -> list[tuple[str, float]]:
    if belge:
        text = "\n".join(f"[{fmt_ts(s.start)}] {_iskelet(s.text)}" for s in window)
    else:
        text = "\n".join(f"[{fmt_ts(s.start)}] {s.text}" for s in window)
    result = await complete_json(
        system=_SYSTEM,
        user=f"Video başlığı: {title}\n\nTranskript parçası:\n\n{text}",
        schema=_BOUNDARY_SCHEMA,
        effort="medium",
        max_tokens=8000,
    )
    marks: list[tuple[str, float]] = []
    for item in result.get("sections") or []:
        ts = parse_ts(item["start_ts"])
        # Model penceredeki damgaları kullanmalı; dışına taşanı atıyoruz.
        if window[0].start <= ts <= window[-1].end:
            marks.append((item["title"].strip(), ts))
    return marks


def _thin(marks: list[tuple[str, float]], en_az: float = MIN_SECTION_SECONDS) -> list[tuple[str, float]]:
    """Birbirine çok yakın sınırları ele — pencere pencere aranınca aynı konu
    birden çok kez bölünebiliyor."""
    kept: list[tuple[str, float]] = []
    for mark in marks:
        if kept and mark[1] - kept[-1][1] < en_az:
            continue
        kept.append(mark)
    return kept


# Belgede birim sayfadır ("zaman" = sayfa no): 45 "saniye" eşiği 45 SAYFA demekti
# ve 35 sayfalık bir kitap bölümünde modelin bulduğu tüm konu sınırları atılıyor,
# bölüm yalnız boyuta göre "(1. kısım)" diye dilimleniyordu. Belgede eşik 3 birim.
BELGE_EN_AZ_BIRIM = 3


async def split_into_sections(
    segments: list[Segment], title: str, transcript: str, belge: bool = False
) -> list[Section]:
    """belge=True: PDF/metin — sınır bulma sayfa iskeletiyle (ucuz) ve sayfa
    ölçeğinde inceltme. Video (varsayılan) değişmedi."""
    end_of_video = segments[-1].end

    # Pencere pencere gidiyoruz: tüm transkripti tek çağrıda göndermek Groq
    # ücretsiz katmanında kotayı aşıp kalıcı 413 veriyor. Belgede pencere iskelet
    # boyutuna göre ölçülür (iskelet ~5 kat küçük → çok daha az çağrı).
    windows = _windows(segments, iskelet=belge)
    sem = asyncio.Semaphore(BOUNDARY_CONCURRENCY)

    async def one(window: list[Segment]) -> list[tuple[str, float]]:
        async with sem:
            try:
                return await _boundaries(window, title, belge)
            except Exception as exc:
                print(f"[segment] pencere atlandi: {exc}", flush=True)
                return []

    found = await asyncio.gather(*(one(w) for w in windows))
    marks = [m for window_marks in found for m in window_marks if m[1] < end_of_video]
    marks.sort(key=lambda m: m[1])
    marks = _thin(marks, BELGE_EN_AZ_BIRIM if belge else MIN_SECTION_SECONDS)

    if not marks:
        marks = [(title, 0.0)]
    if marks[0][1] > 0:
        marks.insert(0, ("Giriş", 0.0))

    sections: list[Section] = []
    for i, (sec_title, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else end_of_video + 1
        chunk = _slice(segments, start, end)
        if not chunk:
            continue
        sections.append(
            Section(title=sec_title, start=chunk[0].start, end=chunk[-1].end, segments=chunk)
        )

    expanded: list[Section] = []
    for section in sections:
        expanded.extend(_split_oversized(section))
    return expanded
