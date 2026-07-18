"""Üç aşamalı özetleme: bölüm özetleri → sentez → eleştirmen geçişi.

Eleştirmen geçişi, kapsayıcılığı tek başına en çok artıran adım: taslak özeti
ham transkriptle karşılaştırıp "hangi tanım, sayı, isim, uyarı düşmüş?" diye
sorar ve bulduklarını geri ekler.
"""

import asyncio
from dataclasses import dataclass, field

from ..llm import complete_json, language_rule, windows
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


# --- Öğrenme sentezi ---
# Özet "ne dedi"yi verir; öğrenme sentezi "şimdi ne YAPACAĞIM"ı verir. İçeriği
# türüne göre şekillendirir (tutorial→adım, gelişim→eylem, kurs→öz-test) ve
# başka bir yapay zekâya yapıştırılabilecek DERİNLEŞME prompt'u üretir.
LEARNING_TYPES = ("tutorial", "kurs", "gelisim", "genel")

_LEARNING_SCHEMA = {
    "type": "object",
    "properties": {
        "type": {"type": "string", "enum": list(LEARNING_TYPES)},
        "steps": {"type": "array", "items": {"type": "string"}},
        "actions": {"type": "array", "items": {"type": "string"}},
        "quiz": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "soru": {"type": "string"},
                    "cevap": {"type": "string"},
                },
                "required": ["soru", "cevap"],
                "additionalProperties": False,
            },
        },
        "deepen_prompt": {"type": "string"},
    },
    "required": ["type", "steps", "actions", "quiz", "deepen_prompt"],
    "additionalProperties": False,
}

_LEARNING_SYSTEM = """Sana bir videonun bölüm notları veriliyor. Amacın: izleyici \
videoyu izledikten SONRA ne yapacağını bilsin — daha derin öğrensin, uygulasın, \
kendini test etsin. Şunları üret:

1. type: İçeriğin türünü sınıflandır:
   - "tutorial": bir şeyi nasıl yapacağını adım adım gösteren (kod, tarif, kurulum...)
   - "kurs": ders/anlatım/kavram öğreten (teori, kavram, konu anlatımı)
   - "gelisim": kişisel gelişim/motivasyon/alışkanlık/tavsiye
   - "genel": yukarıdakilerin hiçbiri (haber, eğlence, vlog...)

2. steps: SADECE tutorial ise — videoda gösterilen prosedürü sıralı, uygulanabilir \
adımlara böl (her adım tek bir eylem; komut/araç/değer varsa dahil et; ekranda \
gösterilenler burada kritik). Tutorial değilse BOŞ liste.

3. actions: SADECE gelisim ise — soyut fikri "bu hafta yap" tipi 3-5 somut eyleme \
çevir ("daha disiplinli ol" DEĞİL, "her sabah 10 dk planla" gibi). Gelisim değilse BOŞ.

4. quiz: kurs/tutorial ise — öğreneni test eden 3-5 soru + kısa cevap. Ezber değil \
kavrama ölçen sorular. genel/gelisim ise BOŞ liste.

5. deepen_prompt: Videoda geçen KONULARI adıyla anan, başka bir yapay zekâya \
(ChatGPT/Claude) OLDUĞU GİBİ yapıştırılabilecek, kendi kendine yeten bir prompt. \
Amaç: izleyici bu prompt'u yapıştırıp konuyu araştırsın, örneklerle pekiştirsin, \
kendini sınasın. Prompt şunları içermeli: videonun ana konuları (adıyla), \
"bana şunları öğret / örneklerle açıkla / beni sorularla test et / sık yapılan \
hataları göster" gibi somut istekler. Genel kalma — video ÖZELİNDE yaz."""

_LEARNING_SYSTEM += language_rule()

# Defter "6 madde geri eklendi" diyemez: sayı tek başına anlamsız, kullanıcıya
# NEYİN riskte olduğunu söylemez. "3 sayı, 2 tanım, 1 uyarı" söyler. Tür listesi
# uydurulmadı — eleştirmenin zaten aradığı kategorilerin ta kendisi (bkz.
# _CRITIC_SYSTEM). kaynak ayrı bir eksen: tür NE kaçtığını, kaynak NEREDEN
# kurtarıldığını söyler; bir sayı ekrandan da gelebilir konuşmadan da.
CRITIC_TURLER = ("sayı", "isim", "tanım", "uyarı", "istisna", "adım")
CRITIC_KAYNAKLAR = ("ekran", "konuşma")

# Bu kelime sayısının altındaki bölümde sıkışma oranı ölçüm değil gürültü:
# 30 kelimelik girdiden "6:1" üretmek rakam uydurmaktır. Ölçemiyorsak
# söylemeyiz — defterin kendi alçakgönüllülüğü buradan başlıyor.
_MIN_COMPRESSION_INPUT = 120

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
                    "tur": {"type": "string", "enum": list(CRITIC_TURLER)},
                    "kaynak": {"type": "string", "enum": list(CRITIC_KAYNAKLAR)},
                },
                "required": ["ts", "text", "tur", "kaynak"],
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
- Her madde için tur ver: sayı (sayı/oran/tarih/fiyat/versiyon), isim (kişi/ürün/\
kütüphane/komut/dosya), tanım, uyarı, istisna (istisna veya karşı örnek), adım.
Birden çok türe uyuyorsa en belirleyici olanı seç.
- Her madde için kaynak ver: bilgi YALNIZCA ekran metninde geçiyorsa (konuşmacı \
söylememiş) "ekran", transkriptte geçiyorsa "konuşma". Emin değilsen "konuşma" de — \
"ekran" bir iddiadır, kanıtı olmalı.
- text alanı, özete olduğu gibi eklenebilecek tam bir cümle olsun.
- Eksik bir şey yoksa boş liste döndür. Liste doldurmak için madde uydurma."""

_CRITIC_SYSTEM += language_rule()


def _kelime_say(metin: str) -> int:
    return len(metin.split())


@dataclass
class SectionSummary:
    section: Section
    summary: str
    points: list[tuple[float, str]]
    terms: list[tuple[str, str]] = field(default_factory=list)
    frames: list[Frame] = field(default_factory=list)
    # Eleştirmenin bu bölüme geri eklediği maddeler: (tur, kaynak).
    critic_items: list[tuple[str, str]] = field(default_factory=list)

    @property
    def compression(self) -> float | None:
        """Kaç kelime girdi, kaç kelime çıktı. "41:1 sıkıştı" bunun okunuşu.

        Defter ÖLÇTÜĞÜ boşlukları bilir (karantina, düşük güven); ÖLÇEMEDİĞİNİ
        bilmez. Bu oran, ölçülemeyenin ölçülebilir vekili: bir iddia değil,
        "burada bir şey kaybolmuş olabilir, kritikse kaynağa dön" daveti.

        Girdi = transkript + ekran metni. Yalnız transkript sayılsaydı, çok
        slaytlı ama az konuşmalı bir bölüm "az sıkıştı" görünürdü — oysa en çok
        kaybın olabileceği yer tam orası.
        """
        girdi = _kelime_say(self.section.text) + sum(_kelime_say(f.text) for f in self.frames)
        cikti = _kelime_say(self.summary) + sum(_kelime_say(t) for _, t in self.points)
        if not cikti or girdi < _MIN_COMPRESSION_INPUT:
            # Çok kısa bölümde oran gürültü: 30 kelimelik girdiden "6:1" çıkarmak
            # ölçüm değil, rakam üretmek olur.
            return None
        return girdi / cikti


@dataclass
class Digest:
    tldr: list[str]
    sections: list[SectionSummary]
    glossary: list[tuple[str, str]]
    added_by_critic: int = 0
    frames_used: int = 0
    # {"sayı": 3, "tanım": 2} — defter bunu "3 sayı · 2 tanım" diye yazar.
    critic_types: dict[str, int] = field(default_factory=dict)
    # [{"title": .., "ts": .., "ratio": 41.2}] — ölçülemeyenin ölçülebilir vekili.
    compression: list[dict] = field(default_factory=list)
    # Konuşmacının hiç söylemediği, YALNIZCA ekranda olan ve bu yüzden kaçmış
    # olacak bilgi sayısı. Ürünün tek farkının ölçülebilir hâli.
    critic_from_screen: int = 0
    # --- Öğrenme sentezi: "ne dedi" değil "şimdi ne YAPACAĞIM". ---
    learning_type: str = "genel"       # tutorial | kurs | gelisim | genel
    steps: list[str] = field(default_factory=list)         # tutorial: sıralı adım
    actions: list[str] = field(default_factory=list)       # gelisim: eylem maddesi
    quiz: list[dict] = field(default_factory=list)         # kurs/tutorial: öz-test
    deepen_prompt: str = ""            # araştır/test/derinleş — kopyalanabilir prompt


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


def _section_draft(s: SectionSummary) -> str:
    lines = [s.summary]
    lines += [f"- [{fmt_ts(ts)}] {text}" for ts, text in s.points]
    return "\n".join(lines)


def _synth_blocks(summaries: list[SectionSummary]) -> list[str]:
    """Sentez girdisi: yalnız bölüm başlığı + özeti, MADDELER değil.

    Sentezin işi TL;DR + sözlük — bunun için bölüm özetleri yeter; her madde
    zaten bölüm bazında çıktıda. _draft_text tüm maddeleri de gönderiyordu;
    eleştirmen (özellikle tür ayrımı + sessizlik kuplajından sonra) çok madde
    ekleyince taslak Groq kotasını aşıp sentezi düşürüyordu — gerçek koşuda
    11122 token. Kodun kendi yorumu zaten "sentez yalnız özetleri görür"
    diyordu; uygulama ona uydu.
    """
    return [
        f"## [{fmt_ts(s.section.start)}] {s.section.title}\n{s.summary}"
        for s in summaries
    ]


async def _synthesize(summaries: list[SectionSummary]) -> dict:
    """Kotayı ASLA aşmayan sentez.

    Özetler tek çağrıya sığıyorsa doğrudan. Sığmıyorsa (çok uzun video):
    parti parti sentezle, sonra ara TL;DR'leri son bir geçişte birleştir —
    hiyerarşik, hiçbir çağrı bütçeyi aşmaz. windows()["section"] burada da
    karakter bütçesi (kod tabanının geri kalanıyla tutarlı, bkz. segment).
    """
    bloklar = _synth_blocks(summaries)
    limit = windows()["section"]

    async def bir(metin: str) -> dict:
        return await complete_json(
            system=_SYNTH_SYSTEM, user=metin, schema=_SYNTH_SCHEMA,
            effort="high", max_tokens=2500,
        )

    if len("\n\n".join(bloklar)) <= limit:
        return await bir("\n\n".join(bloklar))

    partiler: list[list[str]] = []
    grup, boyut = [], 0
    for b in bloklar:
        # +2: bloklar "\n\n" ile birleşiyor; ayıracı saymazsak parti limiti
        # birkaç karakter aşabiliyor (kendi ilan ettiği sınıra uymalı).
        if boyut + len(b) + 2 > limit and grup:
            partiler.append(grup)
            grup, boyut = [], 0
        grup.append(b)
        boyut += len(b) + 2
    if grup:
        partiler.append(grup)

    print(f"[summarize] sentez {len(partiler)} partiye bölündü (uzun video)", flush=True)
    aralar = [await bir("\n\n".join(g)) for g in partiler]

    # Ara TL;DR'leri küçük; son geçiş güvenle sığar. Sözlükleri topla —
    # tekilleştirme _merge_glossary'de zaten yapılıyor.
    birlesik = "\n".join(
        f"- {t}" for r in aralar for t in r.get("tldr", []) if t.strip()
    )
    final = await bir(birlesik)
    final["glossary"] = (final.get("glossary") or []) + [
        g for r in aralar for g in (r.get("glossary") or [])
    ]
    return final


async def _learning_synthesis(summaries: list[SectionSummary]) -> dict:
    """Öğrenme çıktısı: tür + adım/eylem/quiz + derinleşme prompt'u.

    Tek ek çağrı; bitmiş bölüm özetlerini okur (sentez gibi, maddeleri değil →
    kotaya sığar). Eleştirmen bir iyileştirme olduğu gibi bu da: düşerse özet
    yine üretilir, öğrenme bloğu boş kalır.
    """
    girdi = "\n\n".join(_synth_blocks(summaries))
    # Uzun videoda özetler bile büyük olabilir; güvenli tarafta kal.
    girdi = girdi[: windows()["section"]]
    try:
        r = await complete_json(
            system=_LEARNING_SYSTEM, user=girdi, schema=_LEARNING_SCHEMA,
            effort="high", max_tokens=3000,
        )
    except Exception as exc:
        print(f"[learning] atlandi: {exc}", flush=True)
        return {}
    tur = r.get("type", "")
    return {
        "type": tur if tur in LEARNING_TYPES else "genel",
        "steps": [s.strip() for s in r.get("steps", []) if s.strip()],
        "actions": [a.strip() for a in r.get("actions", []) if a.strip()],
        "quiz": [
            {"soru": q.get("soru", "").strip(), "cevap": q.get("cevap", "").strip()}
            for q in r.get("quiz", [])
            if q.get("soru", "").strip()
        ],
        "deepen_prompt": (r.get("deepen_prompt") or "").strip(),
    }


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
        # Şema enum'u zorluyor ama sağlayıcılar arası şema desteği tek tip değil;
        # beklenmedik etiket gelirse madde sayılmalı, sınıflandırma kaybolmalı.
        tur = item.get("tur", "")
        kaynak = item.get("kaynak", "")
        s.critic_items.append((
            tur if tur in CRITIC_TURLER else "",
            kaynak if kaynak in CRITIC_KAYNAKLAR else "",
        ))
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

    # Sentez yalnız bölüm özetlerini görür (maddeleri değil) ve gerekirse parti
    # parti gider — uzun/yoğun videoda bile Groq kotasına sığar.
    synth = await _synthesize(summaries)
    # Öğrenme çıktısı: tür + adım/eylem/quiz + derinleşme prompt'u (ek çağrı).
    ogrenme = await _learning_synthesis(summaries)

    turler: dict[str, int] = {}
    ekrandan = 0
    for s in summaries:
        for tur, kaynak in s.critic_items:
            if tur:
                turler[tur] = turler.get(tur, 0) + 1
            if kaynak == "ekran":
                ekrandan += 1

    return Digest(
        tldr=[t.strip() for t in synth["tldr"] if t.strip()],
        sections=summaries,
        glossary=_merge_glossary(
            [(g["term"], g["definition"]) for g in synth["glossary"]], summaries
        ),
        added_by_critic=added,
        frames_used=len(frames),
        # Sunum sırası sabit (CRITIC_TURLER), sayıya göre değil: defter her koşuda
        # aynı yerde aynı türü göstermeli, yoksa karşılaştırılamaz.
        critic_types={t: turler[t] for t in CRITIC_TURLER if t in turler},
        critic_from_screen=ekrandan,
        # Ölçülemeyen bölüm (çok kısa) listeye HİÇ girmez; "?" göstermek yerine
        # susmak doğru — olmayan ölçümü göstermek ürünün suçladığı şeydir.
        compression=[
            {
                "title": s.section.title,
                "ts": fmt_ts(s.section.start),
                "ratio": round(s.compression, 1),
            }
            for s in summaries
            if s.compression is not None
        ],
        learning_type=ogrenme.get("type", "genel"),
        steps=ogrenme.get("steps", []),
        actions=ogrenme.get("actions", []),
        quiz=ogrenme.get("quiz", []),
        deepen_prompt=ogrenme.get("deepen_prompt", ""),
    )
