"""Üç aşamalı özetleme: bölüm özetleri → sentez → eleştirmen geçişi.

Eleştirmen geçişi, kapsayıcılığı tek başına en çok artıran adım: taslak özeti
ham transkriptle karşılaştırıp "hangi tanım, sayı, isim, uyarı düşmüş?" diye
sorar ve bulduklarını geri ekler.
"""

import asyncio
from dataclasses import dataclass, field

from ..llm import complete_json, language_rule
from .frames import Frame, as_prompt_text, for_range
from .segment import Section, parse_ts
from .transcribe import fmt_ts

SECTION_CONCURRENCY = 4

_SECTION_SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "points": {
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
        },
        "terms": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "definition": {"type": "string"},
                },
                "required": ["term", "definition"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["summary", "points", "terms"],
    "additionalProperties": False,
}

_SECTION_SYSTEM = """Sen bir uzman not tutucusun. Sana bir videonun tek bir \
bölümünün zaman damgalı transkripti veriliyor. Amaç: bu bölümü izlemiş kadar \
olmayı sağlayacak bir not.

Kurallar:
- Kapsayıcı ol. Somut olan her şeyi koru: sayılar, tarihler, isimler, komutlar, \
formüller, adım sıraları, "şuna dikkat" uyarıları, karşı örnekler.
- Yüzeysel özet yazma. "X'ten bahsedildi" değil, X'in NE olduğunu yaz.
- Her maddeye o bilginin geçtiği zaman damgasını koy.
- Konuşmanın dolgu kısımlarını (selamlama, reklam, "abone olun") at.
- terms: bu bölümde tanıtılan teknik terimler ve konuşmacının verdiği tanım. \
Terim tanıtılmadıysa boş bırak.
- Verilen kaynaklarda olmayan hiçbir şey ekleme.

EKRAN METNİ hakkında:
- Transkriptin yanı sıra, ekranda görünen slaytlardan OCR ile okunmuş metin de \
verilebilir. Konuşmacı slaytta yazan her şeyi söylemez — ekranda olup söylenmeyen \
bilgi (tanımlar, komutlar, madde listeleri, rakamlar) özete GİRMELİ.
- OCR gürültülü olabilir: bozuk kelimeleri düzelt, anlamsız karakter yığınlarını \
yok say. Emin olamadığın bir OCR parçasını uydurarak tamamlama, atla.
- Ekrandan gelen bir bilgiyi maddeye yazarken o karenin zaman damgasını kullan."""

_SECTION_SYSTEM += language_rule()

_SYNTH_SCHEMA = {
    "type": "object",
    "properties": {
        "tldr": {"type": "array", "items": {"type": "string"}},
        "glossary": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "term": {"type": "string"},
                    "definition": {"type": "string"},
                },
                "required": ["term", "definition"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["tldr", "glossary"],
    "additionalProperties": False,
}

_SYNTH_SYSTEM = """Sana bir videonun bölüm bölüm notları veriliyor. İki şey üret:

1. tldr: Videonun ana çıkarımları, 3-6 madde. Her madde tek başına anlamlı bir \
cümle olmalı — "birçok konu işlendi" gibi içi boş maddeler yazma. Okuyucu sadece \
bunları okusa videonun ne dediğini bilmeli.
2. glossary: Bölüm notlarındaki terimleri birleştir, tekrarları at, alfabetik sırala."""

_SYNTH_SYSTEM += language_rule()

_CRITIC_SCHEMA = {
    "type": "object",
    "properties": {
        "missing": {
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
    "required": ["missing"],
    "additionalProperties": False,
}

_CRITIC_SYSTEM = """Sen bir denetçisin. Sana bir videonun BİR BÖLÜMÜNÜN ham \
transkripti, o bölümde ekranda görünenlerin OCR metni (EKRAN METNİ) ve bunlardan \
üretilmiş TASLAK ÖZET veriliyor. Tek görevin: kaynaklarda olan ama özete girmemiş, \
girmesi gereken şeyleri bulmak.

Özellikle şunları ara:
- Sayılar, oranlar, tarihler, fiyatlar, versiyonlar
- Özel isimler: kişi, ürün, kütüphane, komut, dosya adı
- Bir terimin verilmiş ama özete geçmemiş tanımı
- "Şuna dikkat edin", "bu tuzağa düşmeyin" türü uyarılar
- Bir kuralın istisnası veya karşı örneği
- Adım sırasında atlanmış bir adım
- EKRANDA yazan ama konuşmacının hiç söylemediği, bu yüzden özete girmemiş bilgi \
(bu en sık kaçan kategoridir — özellikle ara)

Kurallar:
- Sadece GERÇEKTEN eksik olanı bildir. Özette farklı kelimelerle zaten varsa bildirme.
- OCR gürültüsünü eksik bilgi sanma. Anlamlı bir bilgi çıkaramıyorsan atla.
- Her madde için transkriptteki zaman damgasını ver.
- text alanı, özete olduğu gibi eklenebilecek tam bir cümle olsun.
- Eksik bir şey yoksa boş liste döndür. Liste doldurmak için madde uydurma."""

_CRITIC_SYSTEM += language_rule()


@dataclass
class SectionSummary:
    section: Section
    summary: str
    points: list[tuple[float, str]]
    terms: list[tuple[str, str]] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)


@dataclass
class Digest:
    tldr: list[str]
    sections: list[SectionSummary]
    glossary: list[tuple[str, str]]
    added_by_critic: int = 0
    frames_used: int = 0


async def _summarize_section(
    section: Section, frames: list[Frame]
) -> SectionSummary:
    user = f"Bölüm başlığı: {section.title}\n\nTranskript:\n\n{section.text}"
    if frames:
        user += f"\n\n---\n\nEKRAN METNİ (OCR):\n\n{as_prompt_text(frames)}"

    result = await complete_json(
        system=_SECTION_SYSTEM,
        user=user,
        schema=_SECTION_SCHEMA,
        effort="high",
        # Cömert iste: Groq yolunda kalan kotaya göre otomatik kısılıyor,
        # Anthropic yolunda olduğu gibi kullanılıyor.
        max_tokens=16000,
    )
    return SectionSummary(
        section=section,
        summary=result["summary"].strip(),
        points=[(parse_ts(p["ts"]), p["text"].strip()) for p in result["points"]],
        terms=[(t["term"].strip(), t["definition"].strip()) for t in result["terms"]],
        frames=frames,
    )


def _draft_text(summaries: list[SectionSummary]) -> str:
    lines: list[str] = []
    for s in summaries:
        lines.append(f"## [{fmt_ts(s.section.start)}] {s.section.title}")
        lines.append(s.summary)
        lines.extend(f"- [{fmt_ts(ts)}] {text}" for ts, text in s.points)
        lines.append("")
    return "\n".join(lines)


def _section_draft(s: SectionSummary) -> str:
    lines = [s.summary]
    lines += [f"- [{fmt_ts(ts)}] {text}" for ts, text in s.points]
    return "\n".join(lines)


async def _critic_one(s: SectionSummary) -> int:
    """Tek bölümü kendi kaynağıyla karşılaştırır.

    Eskiden tüm transkript + tüm taslak tek çağrıda gidiyordu; Groq'un dakikalık
    token kotasında bu istek asla geçmiyor. Bölüm bazında koşmak hem kotaya sığıyor
    hem de daha odaklı: model bir seferde tek bölümün kaynağına bakıyor.
    """
    user = f"BÖLÜM TRANSKRİPTİ:\n\n{s.section.text}"
    if s.frames:
        user += f"\n\n---\n\nEKRAN METNİ (OCR):\n\n{as_prompt_text(s.frames)}"
    user += f"\n\n---\n\nTASLAK ÖZET:\n\n{_section_draft(s)}"

    result = await complete_json(
        system=_CRITIC_SYSTEM,
        user=user,
        schema=_CRITIC_SCHEMA,
        effort="high",
        max_tokens=16000,
    )

    added = 0
    for item in result.get("missing", []):
        text = item["text"].strip()
        if not text:
            continue
        s.points.append((parse_ts(item["ts"]), text))
        added += 1

    s.points.sort(key=lambda p: p[0])
    return added


async def _apply_critic(summaries: list[SectionSummary]) -> int:
    sem = asyncio.Semaphore(SECTION_CONCURRENCY)

    async def one(s: SectionSummary) -> int:
        async with sem:
            try:
                return await _critic_one(s)
            except Exception as exc:
                # Eleştirmen bir iyileştirme; düşerse özet yine de üretilmeli.
                print(f"[critic] bolum atlandi ({s.section.title}): {exc}", flush=True)
                return 0

    return sum(await asyncio.gather(*(one(s) for s in summaries)))


# Türkçe alfabe sırası. Python'un varsayılan sıralaması kod-noktası sırası olduğu
# için Ç/Ğ/İ/Ö/Ş/Ü harfleri Z'den SONRA düşüyordu (ölçüldü: "... Zaman, Çekirdek,
# Ölçüm, Ürün, Şema" — oysa Türkçe'de Ç, C'den hemen sonra gelir).
# Sondaki q/w/x Türkçe alfabede yok; yabancı terimler için sona konuldu.
_TR_ALPHABET = "abcçdefgğhıijklmnoöprsştuüvyzqwx"
_TR_ORDER = {ch: i for i, ch in enumerate(_TR_ALPHABET)}


def _glossary_key(term: str) -> str:
    """Sözlükte tekrarı elemek için anahtar.

    Çıplak .lower() Türkçe'de bozuk: 'İ'.lower() 'i' + U+0307 (birleşik nokta)
    üretiyor, bu yüzden 'İşlem' ile 'işlem' FARKLI anahtara düşüp aynı terim
    sözlükte iki kez çıkıyordu. Birleşik noktayı atınca ikisi birleşiyor.

    Burada TÜRKÇE küçültme (I→ı) KULLANILMAZ: İngilizce terimlerde 'Iteration' ile
    'iteration' farklı anahtara düşer ve İngilizce sözlük tekrarlanırdı. Kullanıcının
    içeriği TR+EN karışık.
    """
    return term.lower().replace("̇", "").strip()


def _tr_lower(text: str) -> str:
    """Türkçe küçültme: I→ı, İ→i. Python'un .lower()'ı bunu bilmez.

    Yalnızca SIRALAMA için. Tekrar-eleme anahtarında kullanılmaz (yukarıdaki nota
    bakın) — burada güvenli, çünkü sıra yanlış olursa görünüm bozulur, veri değil.
    """
    return text.replace("I", "ı").replace("İ", "i").lower().replace("̇", "")


def _tr_sort_key(text: str) -> list[int]:
    # Alfabede olmayan karakter (rakam, noktalama, yabancı harf) sona gitsin.
    return [_TR_ORDER.get(ch, len(_TR_ALPHABET)) for ch in _tr_lower(text).strip()]


def _merge_glossary(
    from_synth: list[tuple[str, str]], summaries: list[SectionSummary]
) -> list[tuple[str, str]]:
    merged: dict[str, str] = {}
    labels: dict[str, str] = {}

    for s in summaries:
        for term, definition in s.terms:
            key = _glossary_key(term)
            merged[key] = definition
            labels[key] = term

    for term, definition in from_synth:
        key = _glossary_key(term)
        merged[key] = definition
        labels[key] = term

    return sorted(
        ((labels.get(k, k), v) for k, v in merged.items()),
        key=lambda pair: _tr_sort_key(pair[0]),
    )


async def summarize(
    sections: list[Section],
    transcript: str,
    frames: list[Frame] | None = None,
) -> Digest:
    frames = frames or []
    sem = asyncio.Semaphore(SECTION_CONCURRENCY)

    async def one(section: Section, is_last: bool) -> SectionSummary:
        # Son bölümün üst sınırını açık bırak: kapanış slaytı transkriptin son
        # sözünden sonra gelebilir, aksi halde kaybolurdu.
        end = float("inf") if is_last else section.end
        async with sem:
            return await _summarize_section(
                section, for_range(frames, section.start, end)
            )

    summaries = list(
        await asyncio.gather(
            *(one(s, i == len(sections) - 1) for i, s in enumerate(sections))
        )
    )

    added = await _apply_critic(summaries)

    # Sentez yalnızca bölüm ÖZETLERİNİ görür (ham transkripti değil), o yüzden
    # uzun videoda bile kotaya sığar.
    synth = await complete_json(
        system=_SYNTH_SYSTEM,
        user=_draft_text(summaries),
        schema=_SYNTH_SCHEMA,
        effort="high",
        max_tokens=2500,
    )

    return Digest(
        tldr=[t.strip() for t in synth["tldr"] if t.strip()],
        sections=summaries,
        glossary=_merge_glossary(
            [(g["term"], g["definition"]) for g in synth["glossary"]], summaries
        ),
        added_by_critic=added,
        frames_used=len(frames),
    )
