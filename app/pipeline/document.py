"""Belge hattı: PDF → sayfa metni → aynı defter.

Tez (SPEC §0): taranmış PDF gezilemeyen bir kaynaktır — özet, kaynağın YERİNE
geçer, o yüzden dürüst olmalı. Bu modül belge-beyni'nin `extract_pdf` fikrini
(pypdf metin katmanı) video hattının OCR makinesiyle birleştirir.

Video hattıyla AYNI makine kullanılır — bir taranmış PDF sayfası, bir karedir:
  frames._oku          → kelime-güveni medyanı (çöp/sağlam ayrımı)
  frames._en_iyi_aci   → dört-açı onarım (ters taranmış sayfa)
  karantina            → okunamayanı LLM'e verme, kanıtı sakla

Fark: PDF'te OCR BİRİNCİL metindir (videoda ekran ikincil kanaldı). Ve önce
metin katmanı denenir; yalnız o boş/çöpse (ölçüm M1: arşivin %14'ü) OCR'a
düşülür.

Sayfa numarası "zaman" olarak kodlanır (sayfa N = N. saniye) → segment ve
summarize DEĞİŞMEDEN çalışır; yalnız render "s. N" gösterir.
"""

import io
import re
from dataclasses import dataclass
from pathlib import Path

import fitz  # PyMuPDF
from PIL import Image
from pypdf import PdfReader

from .frames import ACILAR, OCR_CONF_ESIK, _en_iyi_aci, _oku
from .transcribe import Segment

# Bir sayfanın metin katmanı bundan kısaysa OCR'a düşer. Ölçüm M1: pypdf
# taranmış sayfalarda "" ya da bir-iki karakter döndürüyor.
MIN_TEXT_LAYER_CHARS = 40

# Metin katmanı DOLU ama çöpse (gömülü-olmayan/CID font) yine OCR'a düş.
# SPEC §2.2: bu eşik kalibre EDİLMEDİ (kullanıcı arşivi tek tepeliydi, min
# 0.80) — düşük tutuldu ki yalnız bariz çöpü yakalasın, sağlamı elemesin. Her
# sayfanın oranı loglanır: külliyat bimodal olursa veri söyler, sessizce geçmez.
MIN_WORD_RATIO = 0.50

_SESLI = set("aeıioöuüAEIİOÖUÜ")


def gercek_kelime_orani(metin: str) -> float | None:
    """Ground-truth istemeden 'bu metin dil mi, çöp mü' vekili (ölçüm metin_kalite.py).

    Türkçe ve İngilizce'de her hecede sesli harf var; OCR/font çöpü bunu bozar.
    20 kelimeden az metinde anlamlı değil → None.
    """
    kelimeler = re.findall(r"[^\W\d_]{2,}", metin, flags=re.UNICODE)
    if len(kelimeler) < 20:
        return None
    iyi = 0
    for k in kelimeler:
        sesli = sum(1 for c in k if c in _SESLI)
        if sesli >= 1 and 0.15 <= sesli / len(k) <= 0.75 and len(k) <= 22:
            iyi += 1
    return iyi / len(kelimeler)


@dataclass
class Page:
    number: int
    text: str
    source: str  # "metin-katmani" | "ocr"
    conf: float | None = None
    rotation: int = 0
    quarantined: bool = False
    img_rel: str | None = None  # karantina/kanıt görüntüsü (OUT altına göreli)
    word_ratio: float | None = None


def _sayfa_goruntu(doc: "fitz.Document", i: int, dpi: int = 300) -> Image.Image:
    pix = doc.load_page(i).get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png")))


def extract(pdf: Path, out_dir: Path, assets_rel: str) -> list[Page]:
    """Her sayfa: önce metin katmanı, boş/çöpse OCR (onar→ölç→karantina)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(pdf))
    doc = fitz.open(str(pdf))
    pages: list[Page] = []

    for i in range(len(reader.pages)):
        try:
            ham = (reader.pages[i].extract_text() or "").strip()
        except Exception:
            ham = ""
        oran = gercek_kelime_orani(ham)

        # Metin katmanı yeterli mi? (dolu VE çöp değil)
        if len(ham) >= MIN_TEXT_LAYER_CHARS and (oran is None or oran >= MIN_WORD_RATIO):
            pages.append(
                Page(number=i + 1, text=ham, source="metin-katmani", conf=None,
                     word_ratio=round(oran, 3) if oran is not None else None)
            )
            print(f"[belge] s.{i+1:>3} metin-katmani ({len(ham)} karakter, "
                  f"oran={oran if oran is None else round(oran,2)})", flush=True)
            continue

        # OCR'a düş: önce onar, sonra ölç, sonra karantina (video hattıyla aynı).
        img = _sayfa_goruntu(doc, i)
        metin, conf = _oku(img)
        rotation = 0
        if conf is not None and conf < OCR_CONF_ESIK:
            metin, conf, rotation = _en_iyi_aci(img)

        temiz = "\n".join(satir.strip() for satir in metin.splitlines() if satir.strip())
        karantina = conf is None or conf < OCR_CONF_ESIK

        # Sayfa görüntüsünü sakla: karantinada kanıt, değilse kaynağa dönüş.
        dst = out_dir / f"page_{i+1:04d}.jpg"
        img.convert("RGB").save(dst, quality=80)

        pages.append(
            Page(
                number=i + 1,
                text="" if karantina else temiz,
                source="ocr",
                conf=round(conf, 1) if conf is not None else None,
                rotation=rotation,
                quarantined=karantina,
                img_rel=f"{assets_rel}/{dst.name}",
                word_ratio=round(oran, 3) if oran is not None else None,
            )
        )
        print(f"[belge] s.{i+1:>3} OCR guven={conf if conf is None else round(conf,1)} "
              f"aci={rotation} {'KARANTINA' if karantina else 'ok'} ({len(temiz)} karakter)",
              flush=True)

    doc.close()

    okunan = sum(1 for p in pages if not p.quarantined and p.text.strip())
    kat = sum(1 for p in pages if p.source == "metin-katmani")
    ocr = sum(1 for p in pages if p.source == "ocr" and not p.quarantined)
    kar = sum(1 for p in pages if p.quarantined)
    print(f"[belge] {len(pages)} sayfa -> {okunan} okundu ({kat} metin-katmani, "
          f"{ocr} OCR), {kar} karantinada", flush=True)
    return pages


def extract_markdown(path: Path) -> list[Page]:
    """Markdown/düz metin → bloklar. OCR/karantina YOK: metin zaten okunabilir,
    'gezilemeyen kaynak' değil — ama aynı defter (segment/summarize/eleştirmen)
    işlesin diye Page'e sarıyoruz. Blok numarası 'saniye' olarak kodlanır (PDF
    gibi), render 'bölüm N' der (sayfa değil — markdown'ın sayfası yok).

    Bölme: üst seviye (#, ##) başlıklar blok sınırı; başlık yoksa ~2500 karakter
    pencere. İnce bölme zararsız — split_into_sections konuya göre yeniden gruplar.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    lines = text.splitlines()
    bloklar: list[str] = []
    cur: list[str] = []
    for ln in lines:
        # Yeni üst-başlık, öncekinde içerik varsa, yeni blok başlatır.
        if re.match(r"^#{1,2}\s", ln) and any(s.strip() for s in cur):
            bloklar.append("\n".join(cur).strip())
            cur = [ln]
        else:
            cur.append(ln)
    if any(s.strip() for s in cur):
        bloklar.append("\n".join(cur).strip())

    # Başlık yok / tek dev blok → sabit pencere (aşırı uzun tek segment olmasın).
    if len(bloklar) <= 1:
        blob = text.strip()
        bloklar = (
            [blob[i : i + 2500] for i in range(0, len(blob), 2500)]
            if len(blob) > 3000
            else ([blob] if blob else [])
        )

    return [
        Page(number=i + 1, text=b, source="metin-katmani", quarantined=False)
        for i, b in enumerate(bloklar)
        if b.strip()
    ]


def to_segments(pages: list[Page]) -> list[Segment]:
    """Sayfa numarası = 'saniye'. Boş/karantinalı sayfa segmente girmez —
    metni LLM'e gitmez, ama defterde kanıtıyla durur."""
    return [
        Segment(start=float(p.number), end=float(p.number) + 1, text=p.text)
        for p in pages
        if p.text.strip()
    ]
