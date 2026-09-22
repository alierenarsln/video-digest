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


def extract(pdf: Path, out_dir: Path, assets_rel: str, ilerleme=None,
            max_sayfa: int | None = None) -> list[Page]:
    """Her sayfa: önce metin katmanı, boş/çöpse OCR (onar→ölç→karantina).

    ilerleme(okunan, islenecek, toplam): her sayfada çağrılır (arayüz 'sayfa N/M'
    göstersin — taranmış PDF saatlerce sürebiliyor, ilerleme görünmezse 'asıldı'
    sanılıyor). max_sayfa: bundan çoksa yalnız ilk N işlenir (saatlerce OCR +
    kuyruk bloku olmasın); çağıran özete 'ilk N (PDF M sayfa)' notu koyar.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    reader = PdfReader(str(pdf))
    doc = fitz.open(str(pdf))
    pages: list[Page] = []
    toplam = len(reader.pages)
    n = min(toplam, max_sayfa) if max_sayfa else toplam

    for i in range(n):
        if ilerleme:
            try:
                ilerleme(i + 1, n, toplam)
            except Exception:
                pass
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

        # Sayfa görüntüsünü YALNIZ karantinada sakla: özet onu kanıt olarak gösterir
        # (render_document) — başka tüketicisi yok. Eskiden OCR'a düşen HER sayfa
        # 300 DPI (~1,2 MB) kaydediliyordu: kullanılmayan 400 MB birikti, Vercel
        # Blob'da her sayfa bir yazma işlemi (Hobby: 2.000/ay) demekti. Kanıt için
        # 1600 px genişlik yeter; OCR yukarıda tam çözünürlükte zaten koştu.
        img_rel = None
        if karantina:
            dst = out_dir / f"page_{i+1:04d}.jpg"
            kanit = img.convert("RGB")
            if kanit.width > 1600:
                kanit = kanit.resize((1600, round(kanit.height * 1600 / kanit.width)))
            kanit.save(dst, quality=75, optimize=True)
            img_rel = f"{assets_rel}/{dst.name}"

        pages.append(
            Page(
                number=i + 1,
                text="" if karantina else temiz,
                source="ocr",
                conf=round(conf, 1) if conf is not None else None,
                rotation=rotation,
                quarantined=karantina,
                img_rel=img_rel,
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


# ── Kitaplar: bölüm tespiti + uzun metin ──────────────────────────────────────
# Uzun belge parça parça özetlenir; parça sınırı KİTABIN BÖLÜMLERİ olmalı, rastgele
# 15 sayfa değil (eskiden 690 sayfalık kitap 46 anlamsız parçaya bölünüyordu).
# Ölçüm (kullanıcı kütüphanesi, 296 PDF): %44'ünde içindekiler yer imi var;
# olmayanların bir kısmında sayfa başında "CHAPTER 3" / "PART ONE" yazıyor.
# Sıra: yer imi → sayfa başı başlığı → eşit parçalar. Hepsi aynı normalleştirmeden
# geçer: çok kısa bölüm komşusuyla birleşir, çok uzunu bölünür.
BOLUM_HEDEF = 35   # birim (sayfa ~ 2-3 bin karakter): bölüm yoksa parça boyu
BOLUM_EN_AZ = 10   # bundan kısa bölüm (önsöz, kısa ara bölüm) komşusuna katılır
BOLUM_EN_COK = 70  # bundan uzun bölüm eşit alt parçalara bölünür

_BOLUM_KALIBI = re.compile(
    r"^(chapter|bölüm|kısım|ünite|part|section|lesson|ders)\s+"
    r"([0-9]+|[ivxlc]+|one|two|three|four|five|six|seven|eight|nine|ten|eleven|"
    r"twelve|thirteen|fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|"
    r"bir|iki|üç|dört|beş|altı|yedi|sekiz|dokuz|on)\b",
    re.I,
)


def _temiz_baslik(t: str) -> str:
    # " — " başlık ayracıdır (kütüphane gruplaması son ekten ayırır): içeride olmasın.
    t = re.sub(r"\s+", " ", (t or "").replace("—", "-")).strip()
    return t[:70]


def pdf_bolum_basliklari(pdf: Path, pages: list[Page]) -> list[tuple[str, int]]:
    """[(başlık, sayfa_indeksi)] — sayfa_indeksi pages listesindeki konum (0'dan)."""
    try:
        doc = fitz.open(str(pdf))
        toc = doc.get_toc()
        doc.close()
    except Exception:
        toc = []
    n = len(pages)
    girdiler = [(lv, _temiz_baslik(b), s - 1) for lv, b, s in toc if 1 <= s <= n]
    ust = min((lv for lv, _b, _s in girdiler), default=None)
    if ust is not None:
        # Üst seviyeden başla; çok uzun bir bölümün (ölçüldü: "PART II" 146 sayfa)
        # içinde alt seviye yer imleri varsa onları kullan — eşit dilimlemeden iyi.
        secili = sorted({i: b for lv, b, i in girdiler if lv == ust}.items())
        for alt in (ust + 1, ust + 2):
            yeni = []
            for k, (a, ad) in enumerate(secili):
                son = secili[k + 1][0] if k + 1 < len(secili) else n
                yeni.append((a, ad))
                if son - a > BOLUM_EN_COK:
                    cocuk = sorted({i: b for lv, b, i in girdiler if lv == alt and a < i < son}.items())
                    if len(cocuk) >= 2:
                        yeni += cocuk
            secili = sorted(dict(yeni).items())
        if len(secili) >= 3:
            return [(b, i) for i, b in secili]
    # Yer imi yok: sayfanın İLK satırı "Chapter 3" / "PART ONE" gibi mi? İçindekiler
    # sayfasında bu kelimeler satır ortasında/sonunda geçer, ilk satırda değil.
    sonuc: list[tuple[str, int]] = []
    for i, p in enumerate(pages):
        satirlar = [s.strip() for s in (p.text or "").splitlines() if s.strip()][:2]
        if satirlar and len(satirlar[0]) < 60 and _BOLUM_KALIBI.match(satirlar[0]):
            ad = satirlar[0] if len(satirlar) < 2 else f"{satirlar[0]}: {satirlar[1]}"
            sonuc.append((_temiz_baslik(ad), i))
    return sonuc if len(sonuc) >= 3 else []


def bolum_araliklari(basliklar: list[tuple[str, int]], n: int) -> list[tuple[str, int, int]]:
    """Ham bölüm başlangıçları → [(ad, başlangıç, bitiş)] (bitiş hariç), normalleşmiş.
    Bölüm yoksa BOLUM_HEDEF'lik eşit parçalar (ad boş)."""
    if n <= 0:
        return []
    araliklar: list[list] = []
    basl = sorted({i: b for b, i in basliklar if 0 <= i < n}.items())
    if basl:
        if basl[0][0] > 0:
            basl.insert(0, (0, "Giriş"))
        for k, (i, ad) in enumerate(basl):
            son = basl[k + 1][0] if k + 1 < len(basl) else n
            if son > i:
                araliklar.append([ad, i, son])
        # Kısa bölümleri birleştir (sonrakine; sonuncuysa öncekine).
        k = 0
        while k < len(araliklar) and len(araliklar) > 1:
            ad, a, b = araliklar[k]
            if b - a < BOLUM_EN_AZ:
                if k + 1 < len(araliklar):
                    nxt = araliklar[k + 1]
                    # Kısa önsöz sonrakinin adını alır; iki kısa bölüm adları birleşir.
                    if ad != "Giriş" and b - a >= 3:
                        nxt[0] = f"{ad} + {nxt[0]}"[:70]
                    nxt[1] = a
                else:
                    araliklar[k - 1][2] = b
                araliklar.pop(k)
                continue
            k += 1
        # Ardışık kısa bölümleri paketle: ölçüldü — 984 sayfalık kitap alt başlıklarla
        # 41 parçaya (çoğu 10-15 sayfa) bölünüyordu; kütüphane dağılıyor, her parça
        # ayrı özet çağrısı. Toplamı ~HEDEF'i geçmeyen komşular birleşir. İçindekiler/
        # dizin gibi atlanacak bölüm içerikle karıştırılmaz.
        k = 0
        while k + 1 < len(araliklar):
            a1, a2 = araliklar[k], araliklar[k + 1]
            if (a2[2] - a1[1] <= BOLUM_HEDEF * 1.3
                    and gereksiz_mi(a1[0]) == gereksiz_mi(a2[0])):
                a1[0] = f"{a1[0]} + {a2[0]}"[:70] if a2[0] != "Giriş" else a1[0]
                a1[2] = a2[2]
                araliklar.pop(k + 1)
                continue
            k += 1
    else:
        parca = max(1, round(n / BOLUM_HEDEF))
        boy = -(-n // parca)
        araliklar = [["", i, min(i + boy, n)] for i in range(0, n, boy)]
    # Çok uzunları eşit alt parçalara böl.
    sonuc: list[tuple[str, int, int]] = []
    for ad, a, b in araliklar:
        uz = b - a
        if uz > BOLUM_EN_COK:
            k = -(-uz // BOLUM_HEDEF)
            boy = -(-uz // k)
            for j, s in enumerate(range(a, b, boy)):
                sonuc.append((f"{ad} ({j + 1}/{k})" if ad else "", s, min(s + boy, b)))
        else:
            sonuc.append((ad, a, b))
    return sonuc


_GEREKSIZ = re.compile(
    r"^(table of contents|contents|index|içindekiler|dizin|bibliography|kaynakça|"
    r"references|acknowledg\w*|teşekkür|copyright|title page|also by|about the author|"
    r"yazar hakkında|notes|notlar|endnotes)\b",
    re.I,
)


def gereksiz_mi(ad: str) -> bool:
    """Bölümün TÜM parçaları içindekiler/dizin/künye gibiyse özetlemeye değmez
    (ölçüldü: 984 sayfalık kitapta içindekiler + dizin = 53 sayfa, boşa kredi)."""
    parcalar = [p.strip() for p in (ad or "").split(" + ") if p.strip()]
    return bool(parcalar) and all(_GEREKSIZ.match(p) for p in parcalar)


_BIRIM_KARAKTER = 2500  # uzun metinde bir "birim" (kabaca bir kitap sayfası)


def _birimlere_bol(parcalar: list[tuple[str | None, str]]) -> tuple[list[Page], list[tuple[str, int]]]:
    """[(bölüm_başlığı|None, metin)] → (≤~2500 karakterlik birimler, bölüm başlangıçları).
    Birim paragraf sınırında kesilir; bölüm sınırını asla aşmaz."""
    pages: list[Page] = []
    basliklar: list[tuple[str, int]] = []

    def ekle(t: str) -> None:
        if t.strip():
            pages.append(Page(number=len(pages) + 1, text=t.strip(), source="metin-katmani"))

    for baslik, metin in parcalar:
        paragraflar = [p.strip() for p in re.split(r"\n\s*\n", metin) if p.strip()]
        if not paragraflar:
            continue
        if baslik:
            basliklar.append((_temiz_baslik(baslik), len(pages)))
        cur = ""
        for par in paragraflar:
            # Düz TXT'de paragraf ayrımı olmayabilir: dev paragrafı dilimle.
            while len(par) > _BIRIM_KARAKTER * 1.5:
                ekle(f"{cur}\n\n{par[:_BIRIM_KARAKTER]}" if cur else par[:_BIRIM_KARAKTER])
                cur, par = "", par[_BIRIM_KARAKTER:]
            if cur and len(cur) + len(par) > _BIRIM_KARAKTER:
                ekle(cur)
                cur = ""
            cur = f"{cur}\n\n{par}" if cur else par
        ekle(cur)
    return pages, basliklar


def extract_metin(path: Path) -> tuple[list[Page], list[tuple[str, int]]]:
    """Markdown/TXT → (birimler, bölüm başlangıçları). Bölüm: markdown'da '#'/'##'
    başlıkları, düz metinde 'Chapter 3' / 'BÖLÜM 2' ile başlayan kısa satırlar."""
    text = path.read_text(encoding="utf-8", errors="replace")
    md = path.suffix.lower() in (".md", ".markdown")
    parcalar: list[tuple[str | None, str]] = []
    baslik: str | None = None
    cur: list[str] = []
    for ln in text.splitlines():
        s = ln.strip()
        yeni = None
        if md and re.match(r"^#{1,2}\s+\S", ln):
            yeni = ln.lstrip("#").strip()
        elif (not md and len(s) < 60 and _BOLUM_KALIBI.match(s)
              # Kaynakça/dipnot satırı bölüm değil (ölçüldü: 'Chapter 8, "Mourning
              # and Melancholia," pp. 155' başlık sanıldı): numaradan sonra virgül
              # ya da sayfa atfı varsa atla.
              and not re.match(r"^\S+\s+\S+,", s) and not re.search(r"\bpp?\.\s*\d", s)):
            yeni = s
        if yeni is not None:
            if any(x.strip() for x in cur):
                parcalar.append((baslik, "\n".join(cur)))
            baslik, cur = yeni, []
        cur.append(ln)
    if any(x.strip() for x in cur):
        parcalar.append((baslik, "\n".join(cur)))
    return _birimlere_bol(parcalar)


def to_segments(pages: list[Page]) -> list[Segment]:
    """Sayfa numarası = 'saniye'. Boş/karantinalı sayfa segmente girmez —
    metni LLM'e gitmez, ama defterde kanıtıyla durur."""
    return [
        Segment(start=float(p.number), end=float(p.number) + 1, text=p.text)
        for p in pages
        if p.text.strip()
    ]
