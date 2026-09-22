import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# override=True: .env kabuktaki ESKİ env değişkenlerini EZER. Varsayılan (False)
# bir footgun'du: kabukta eski bir OPENROUTER_API_KEY export'lu kalmışsa .env'e
# yeni anahtar yazmak sessizce yok sayılıyordu (401 "User not found" kovaladık).
# Coolify'da .env yok → orada zararsız (env var'lar kullanılır).
# TANIK_NO_DOTENV: ev-isci.py env'i KENDİSİ, doğru sırayla yükler (yerel .env →
# Vercel env'i üstüne); burada tekrar yüklenirse yerel .env Vercel ayarlarını ezerdi.
if not os.environ.get("TANIK_NO_DOTENV"):
    load_dotenv(override=True)


def _int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    return float(raw) if raw else default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    return raw in {"1", "true", "yes", "on"} if raw else default


GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "").strip()
# Çok anahtar: cascade gibi GROQ_API_KEY / _2 / _3 / _4 arasında dönüşümlü kullan.
# Tek anahtar rate-limit'e takılınca koca iş retry'de dakikalarca asılı kalıyordu
# (büyük yüklemelerde yaşandı); ikinci anahtar throughput'u ikiye katlar + hata
# olunca sıradaki anahtara anında geçilir. Boşlar elenir; hiç yoksa liste boş.
GROQ_API_KEYS = [
    k
    for k in (
        os.environ.get(f"GROQ_API_KEY{s}", "").strip() for s in ("", "_2", "_3", "_4")
    )
    if k
]
GROQ_TRANSCRIBE_MODEL = os.environ.get("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-opus-4-8")

# Gemini: bu iş için ölçülen fiyat/güvenilirlik dengesinin kazananı. Native
# responseSchema (yapısal JSON) + iyi Türkçe + ucuz. 2.5 Flash varsayılan;
# Flash-Lite daha da ucuz ama biraz daha zayıf. Anahtar: Google AI Studio.
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
# Model ZİNCİRİ: biri 404 (ölçüldü: 2.5-flash "yeni kullanıcılara kapalı"), 503
# (ölçüldü: 3.6/3.7-flash "yüksek talep") ya da günlük ücretsiz kotası dolu (429)
# ise sıradakine geçilir. Her modelin ücretsiz kotası AYRI → zincir kapasiteyi de
# artırır. Sıra: kalite > hız. GEMINI_MODEL (tek) verilirse zincirin başına girer.
GEMINI_MODELS = [
    m.strip() for m in (
        os.environ.get("GEMINI_MODEL", "") + "," + os.environ.get(
            "GEMINI_MODELS",
            "gemini-3.6-flash,gemini-3.5-flash,"
            "gemini-3.5-flash-lite,gemini-3.1-flash-lite",
        )
    ).split(",") if m.strip()
]
GEMINI_MODELS = list(dict.fromkeys(GEMINI_MODELS))  # tekrarları at, sırayı koru
GEMINI_MODEL = GEMINI_MODELS[0]
GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
# 1M bağlam → OpenRouter gibi büyük pencere; istek/kota derdi Groq kadar değil.
# Gemini 3 düşünen model: düşünme token'ları da bu bütçeden yer → 16k.
GEMINI_MAX_OUTPUT = _int("GEMINI_MAX_OUTPUT", 16000)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY", "").strip()
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
# Ölçüldü — ücretsiz VE katı JSON şeması destekleyenler arasından, TEKRARLI test:
#   google/gemma-4-26b-a4b-it:free -> 6/6 sağlam (SEÇİLEN). Verimli de.
#   tencent/hy3:free               -> GÜVENİLMEZ: 3 denemeden 1'i boş ya da kesik
#                                     JSON döndürüyor. Bütçe meselesi değil,
#                                     rastgele. Tek testte iyi görünüp aldatıyor.
#   nvidia/nemotron-3-super-120b   -> 1M bağlamlı AMA 3/3 tek jenerik bölüm (KÖTÜ)
#   openai/gpt-oss-20b:free        -> boş yanıt
# Model değiştirirseniz TEK denemeye güvenmeyin; en az 3 kez koşup ölçün.
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemma-4-26b-a4b-it:free")
# gemma 8000'de de sağlam ölçüldü. Çağıranların Anthropic'e göre verdiği cömert
# değerleri (16000) yine de kısıyoruz: gereksiz büyük bütçe kotayı hızlandırmıyor.
OPENROUTER_MAX_OUTPUT = _int("OPENROUTER_MAX_OUTPUT", 8000)

# Cerebras: OpenAI uyumlu, çok hızlı. ÖLÇÜLDÜ (2026-09-18, kredili hesap):
#   qwen-3.8-27b : 450 istek/dk, 150k token/dk, tek istekte 88k token geçti (3 sn)
#   gpt-oss-120b : 5 istek/dk, 30k token/dk → 60k'lık istek bile 429 (kullanışsız)
# Kredisiz hesapta HER istek 402 "Payment required" (ölçüldü) — anahtar_kontrol yakalar.
CEREBRAS_API_KEY = os.environ.get("CEREBRAS_API_KEY", "").strip()
CEREBRAS_MODEL = os.environ.get("CEREBRAS_MODEL", "qwen-3.8-27b").strip()
CEREBRAS_BASE_URL = "https://api.cerebras.ai/v1"
CEREBRAS_MAX_OUTPUT = _int("CEREBRAS_MAX_OUTPUT", 8000)

# Mistral: OpenAI uyumlu, katı JSON şeması destekli. Ücretsiz "Experiment" planı
# konsoldan ETKİNLEŞTİRİLMEDEN dakikalık istek sınırı 0 (ölçüldü: 429, limit 0).
MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "").strip()
MISTRAL_MODEL = os.environ.get("MISTRAL_MODEL", "mistral-medium-latest").strip()
MISTRAL_BASE_URL = "https://api.mistral.ai/v1"
MISTRAL_MAX_OUTPUT = _int("MISTRAL_MAX_OUTPUT", 8000)

# Özet/bölümleme/eleştirmen/onarım hangi sağlayıcıda koşsun?
#   anthropic  : Claude — en iyi, 1M bağlam, ücretli
#   groq       : gpt-oss-120b — ücretsiz. Sınır: 8000 token/dk. İstek sayısı
#                sınırsız → sınırsız video, ama yavaş ve istekler küçük olmalı.
#   openrouter : tencent/hy3 — ücretsiz. Sınır: GÜNDE 50 İSTEK (kredi 0 iken;
#                $10 kredi alınırsa 1000). 262k bağlam → büyük istek serbest,
#                ama istek sayısı kıymetli.
# Boş bırakılırsa öncelik: Anthropic (en iyi) > Gemini (fiyat/kalite) > OpenRouter
# (hızlı, büyük bağlam) > Groq (ücretsiz ama 8000 TPM → uzun özetlerde YAVAŞ, en son).
# OpenRouter/Gemini anahtarı varken groq'a düşmek özetlemeyi gereksiz süründürüyordu.
# Kullanıcı zaten arayüzden iş başına seçebilir.
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "").strip().lower() or (
    "anthropic" if ANTHROPIC_API_KEY
    else "gemini" if GEMINI_API_KEY
    else "openrouter" if OPENROUTER_API_KEY
    else "cerebras" if CEREBRAS_API_KEY
    else "mistral" if MISTRAL_API_KEY
    else "groq"
)
# Groq'ta KATI JSON şeması destekleyen model. Ölçüldü: gpt-oss-120b destekliyor,
# llama-3.3-70b DESTEKLEMİYOR (HTTP 400). Değiştirirken bunu doğrulayın.
GROQ_LLM_MODEL = os.environ.get("GROQ_LLM_MODEL", "openai/gpt-oss-120b")
# ÜCRETSİZ KATMANIN ASIL SINIRI BAĞLAM DEĞİL, DAKİKALIK TOKEN KOTASI.
# Ölçüldü (x-ratelimit-limit-tokens): gpt-oss-120b ve gpt-oss-20b = 8000,
# llama-4-scout = 30000 (ama 17B, akıl yürütmesi zayıf). Groq bu bütçeye
# max_tokens'ı DA sayıyor, yani büyük çıktı istemek tek başına isteği reddettirir.
# Kotadan büyük tek bir istek ASLA geçmez — beklemek çözmez, bölmek gerekir.
GROQ_TPM = _int("GROQ_TPM", 8000)
# Kota küresel olduğu için eşzamanlı istek kotayı anında doldurup herkesi 429'a
# sokuyor; paralellik burada hız kazandırmıyor. 1 = sırayla.
GROQ_CONCURRENCY = _int("GROQ_CONCURRENCY", 1)

# Özet hangi dilde yazılsın? Boş = kaynağın dili.
# "Türkçe" derseniz İngilizce bir kursun özeti Türkçe çıkar — asıl kullanım bu.
# Boş bırakmak dil karışmasına yol açabiliyor (başlık Türkçe, özet İngilizce),
# çünkü prompt'lar Türkçe ama kaynak İngilizce.
OUTPUT_LANGUAGE = os.environ.get("OUTPUT_LANGUAGE", "Türkçe").strip()

# --- Pencere boyutları: SAĞLAYICIYA GÖRE ters yönde ayarlanır ---
# Groq'un derdi token kotası (8000/dk), istek sayısı sınırsız → KÜÇÜK pencereler,
# çok istek. OpenRouter'ın derdi istek SAYISI (50/gün), bağlamı 262k → BÜYÜK
# pencereler, az istek. Aynı boyutu ikisine vermek birini mutlaka bozar:
# Groq'ta 413 (kotadan büyük istek asla geçmez), OpenRouter'da günlük kota biter.
#
# İş BAŞINA sağlayıcı seçilebildiği için bunlar sabit değil, sağlayıcıya bakan
# bir tablo. "repair" istisna: metni yeniden yazdığı için çıktısı ≈ girdisi kadar,
# bölümleme gibi 120k okuyup kısa liste döndüremez.
PROVIDER_WINDOWS = {
    "groq":       {"boundary": 6_000,   "section": 8_000,  "repair": 4_000},
    "openrouter": {"boundary": 120_000, "section": 40_000, "repair": 10_000},
    "anthropic":  {"boundary": 120_000, "section": 40_000, "repair": 40_000},
    # Gemini 1M bağlam: OpenRouter gibi büyük pencere, ama günlük istek kotası
    # yok → hem büyük istek hem çok istek serbest.
    "gemini":     {"boundary": 120_000, "section": 40_000, "repair": 20_000},
    # Cerebras qwen: 150k token/DAKİKA. Bölüm özetleri 4'er paralel gider →
    # 4 × (25k + çıktı) kotaya sığsın. Bölümleme tek çağrı, 60k rahat.
    "cerebras":   {"boundary": 60_000,  "section": 25_000, "repair": 10_000},
    # Mistral 128k bağlam; ücretsiz planın dakikalık sınırları yayınlanmıyor →
    # Cerebras kadar ölçülü pencere (429'da llm beklemeyi zaten biliyor).
    "mistral":    {"boundary": 60_000,  "section": 25_000, "repair": 10_000},
}


def provider_available(name: str) -> bool:
    return {
        "groq": bool(GROQ_API_KEY),
        "openrouter": bool(OPENROUTER_API_KEY),
        "anthropic": bool(ANTHROPIC_API_KEY),
        "gemini": bool(GEMINI_API_KEY),
        "cerebras": bool(CEREBRAS_API_KEY),
        "mistral": bool(MISTRAL_API_KEY),
    }.get(name, False)


# Arayüzde gösterilen açıklamalar — hepsi ÖLÇÜLMÜŞ değerler, tahmin değil.
PROVIDER_INFO = {
    "groq": {
        "ad": "Groq — sınırsız video",
        "model": GROQ_LLM_MODEL,
        "artisi": "Günlük sınır yok, istediğiniz kadar video işleyin.",
        "eksisi": "Dakikada 8000 token kotası → video başına ~10-15 dk.",
    },
    "openrouter": {
        "ad": "OpenRouter — daha iyi bölümleme",
        "model": OPENROUTER_MODEL,
        "artisi": "262k bağlam: tüm transkript tek çağrıda bölümlenir, daha tutarlı.",
        "eksisi": "Günde 50 istek → ~3 video/gün. ($10 kredi ile 1000/gün)",
    },
    "anthropic": {
        "ad": "Claude — en iyi kalite",
        "model": SUMMARY_MODEL,
        "artisi": "En iyi eleştirmen geçişi ve bölümleme, 1M bağlam, kota derdi yok.",
        "eksisi": "Ücretli.",
    },
    "gemini": {
        "ad": "Gemini — fiyat/kalite dengesi",
        "model": GEMINI_MODEL,
        "artisi": "Native yapısal JSON (flake yok), iyi Türkçe, 1M bağlam, ~$3/ay.",
        "eksisi": "Ücretli (ama çok ucuz). Google AI Studio anahtarı gerekir.",
    },
    "cerebras": {
        "ad": "Cerebras — en hızlı",
        "model": CEREBRAS_MODEL,
        "artisi": "Çok hızlı; dakikada 150k token, büyük bağlam.",
        "eksisi": "Kredi harcar (hesaptaki kredi bitince durur).",
    },
    "mistral": {
        "ad": "Mistral — ücretsiz yedek",
        "model": MISTRAL_MODEL,
        "artisi": "Ücretsiz plan, 128k bağlam, iyi Türkçe.",
        "eksisi": "Ücretsiz planın hız sınırları düşük olabilir.",
    },
}

# resolve(): result_path API'de ve n8n callback'inde dışarı veriliyor. Göreli
# bırakılırsa yalnızca sunucunun çalışma dizininden anlamlı olur ve başka bir
# dizinden okuyan istemci "dosya yok" alır.
# --- Yerel indirici ---
# true: link (http/https) işleri indirilmeyi BEKLER; sunucu kendi indirmeye
# çalışmaz. Sebep: YouTube veri merkezi IP'lerini engelliyor ("Sign in to confirm
# you're not a bot") — canlı sunucuda gerçek videoyla doğrulandı. Ev IP'si geçiyor.
# Ev makinesindeki agent.ps1 bekleyen işleri görür, indirir, yükler.
# Yerel kullanımda false: makine zaten ev IP'sinde, doğrudan indirir.
USE_LOCAL_AGENT = _bool("USE_LOCAL_AGENT", False)

# --- Erişim koruması ---
# BOŞ = koruma yok. Yalnızca 127.0.0.1'e bağlıyken güvenli; internete açık bir
# sunucuda (Coolify/VPS) boş bırakmak, linki bulan herkesin iş atıp API
# kotanızı yakabilmesi demek. Docker'da zorunlu tutuluyor (bkz. main.py).
APP_USER = os.environ.get("APP_USER", "admin").strip()
APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
# Konteynerde çalışıyorsak dışarı açık sayılır → şifresiz açılışa izin verme.
IN_DOCKER = _bool("IN_DOCKER", False)
# Vercel de internete açık: VERCEL=1 çalışma anında platformca set edilir.
ON_VERCEL = bool(os.environ.get("VERCEL"))
EXPOSED = IN_DOCKER or ON_VERCEL

# --- Saklama (retention) ---
# İş "done" olunca yüklenen KAYNAK dosyayı (video/ses/PDF) sil. Bu, diskin asıl
# yükü: özet + transkript + slayt görselleri (kütüphanenin gösterdiği her şey)
# KALIR, yalnızca özetlendikten sonra işe yaramayan ham kaynak gider. Zaman
# damgaları origin_url'e bağlı olduğu için tıklanabilirlik de bozulmaz.
# Yeniden işlemek isteyen kapatır (KEEP source).
DELETE_SOURCE_AFTER_DONE = _bool("DELETE_SOURCE_AFTER_DONE", True)

# Vercel'de (VERCEL=1) paket dizini salt-okunur; yazılabilir tek yer /tmp (geçici,
# çağrı bitince gider — kalıcılık Postgres + Blob'da). Açılıştaki mkdir buradan
# çöküyordu (500 FUNCTION_INVOCATION_FAILED).
DATA_DIR = Path(
    os.environ.get("DATA_DIR")
    or ("/tmp/tanik-data" if os.environ.get("VERCEL") else "./data")
).resolve()
WORK_DIR = DATA_DIR / "work"
OUT_DIR = DATA_DIR / "out"
# Yüklenen dosyalar OUT_DIR'e KONULMAZ: orası /out altında servis ediliyor,
# yüklediğiniz video internete açılırdı.
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "jobs.sqlite3"
# Postgres (Vercel + Neon): tanımlıysa SQLite yerine kullanılır (bkz. dbconn.py).
# Vercel'de kalıcı disk yok — SQLite dosyası her çağrıda kaybolurdu. Coolify'da
# boş kalır ve volume'daki SQLite aynen çalışmaya devam eder.
DATABASE_URL = (
    os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL") or ""
).strip()
# Vercel Blob (bkz. storage.py): token varsa çıktılar Blob'a da yazılır, yoksa
# yalnız yerel disk (Coolify). Depo PRIVATE kurulmalı — özetler herkese açık URL
# almasın; sunucu token'la okuyup oturum korumalı /out yolundan verir.
BLOB_READ_WRITE_TOKEN = os.environ.get("BLOB_READ_WRITE_TOKEN", "").strip()
BLOB_ACCESS = os.environ.get("BLOB_ACCESS", "private").strip() or "private"

# Uzun video parçalama: süre ~PART_SECONDS'ı belirgin aşarsa video part'lara
# bölünüp her part AYRI özetlenir (derin part özeti + ayrı kütüphane girdisi),
# tüm video ise "Tüm hali" olarak kalır. Part sayısı = round(süre/PART_SECONDS).
PART_SECONDS = _int("PART_SECONDS", 1800)  # 30 dk
# Uzun videoda kaç part AYNI ANDA işlensin. Groq transkripti kendi semaforuyla
# (GROQ_CONCURRENCY) sıralanır → Groq'la kazanç sınırlı (özetleme örtüşür); yerel
# transkriptte tam paralellik. 3 makul.
PART_CONCURRENCY = _int("PART_CONCURRENCY", 3)
# Üst sınır: bundan çok sayfalıysa ilk MAX_PDF_PAGES işlenir, özet "ilk N (PDF M
# sayfa)" der. Metin katmanlı PDF hızlı (pypdf); asıl maliyet TARANMIŞ PDF'te —
# her sayfa 300 DPI render + Tesseract OCR (düşük güvende 4-açı) → yüzlerce sayfa
# uzun sürer ve tek-işçi kuyruğunu bloklar. Varsayılan 600'e çıkarıldı (200+ sayfa
# belgeler için); daha büyükleri düzenli işleyeceksen env'den (MAX_PDF_PAGES)
# artır. Uzun PDF zaten PART_PAGES'a göre parçalanıp paralel özetlenir.
# Kitaplar 600 sayfayı aşabiliyor (ölçüldü: kullanıcı kütüphanesinde 8 kitap 600+,
# en uzunu 1272). Metin katmanlı sayfa hızlı (690 sayfa 64 sn); sınır yalnız
# taranmış dev PDF'in saatlerce OCR'ına karşı sigorta.
MAX_PDF_PAGES = _int("MAX_PDF_PAGES", 1500)
# Tarayıcıdan doğrudan Blob'a yüklemede tek dosya üst sınırı (Vercel modu).
# İzin bu boyuta kilitlenir; aşan yükleme Blob tarafından reddedilir.
MAX_YUKLEME_MB = _int("MAX_YUKLEME_MB", 2048)
# Uzun PDF de (uzun video gibi) parçalara bölünür: sayfa sayısı ~PART_PAGES'ı
# belirgin aşarsa her parça AYRI özetlenir (part sayısı = round(sayfa/PART_PAGES)).
# Tek dev özet 200 sayfada max_tokens'ı taşırıyor + gezilmesi zor.
# Bundan uzun belge (sayfa ya da ~2500 karakterlik kesit) BÖLÜM BÖLÜM özetlenir
# (bkz. document.bolum_araliklari); kısası (makale, slayt destesi) tek özet.
UZUN_BELGE_BIRIM = _int("UZUN_BELGE_BIRIM", 50)

CHUNK_SECONDS = _int("CHUNK_SECONDS", 600)
TRANSCRIBE_CONCURRENCY = _int("TRANSCRIBE_CONCURRENCY", 3)
TRANSCRIBE_LANGUAGE = os.environ.get("TRANSCRIBE_LANGUAGE", "").strip() or None
# Groq düşünce (kota/5xx/anahtar) ev bilgisayarında faster-whisper'a geç. Model
# "small": bu PC'de önbellekte hazır, CPU'da makul hız; Türkçe için "medium" daha
# iyi ama ~3 kat yavaş (env ile değiştirilebilir).
LOCAL_WHISPER = _bool("LOCAL_WHISPER", True)
LOCAL_WHISPER_MODEL = os.environ.get("LOCAL_WHISPER_MODEL", "small").strip() or "small"
DEFAULT_CALLBACK_URL = os.environ.get("DEFAULT_CALLBACK_URL", "").strip() or None

# --- Altyazı (varsa Whisper'a hiç gitmeden bedava transkript) ---
USE_SUBTITLES = _bool("USE_SUBTITLES", True)
# Tercih edilen altyazı dilleri, virgülle. Boş = videonun kendi dili (önerilen:
# Whisper'ın üreteceğinin sadık karşılığı). "tr,en" derseniz İngilizce videonun
# Türkçe ÇEVİRİ altyazısı varsa o kullanılır — özet de Türkçe çıkar.
SUBTITLE_LANGS = [
    s.strip() for s in os.environ.get("SUBTITLE_LANGS", "").split(",") if s.strip()
]
# YouTube'un otomatik (ASR) altyazısını da kabul et. Varsayılan kapalı: kalitesi
# dile göre değişiyor ve Whisper genelde daha iyi. Açılsa bile yalnızca videonun
# ORİJİNAL dilinde kullanılır (gerisi makine çevirisi).
ALLOW_AUTO_SUBTITLES = _bool("ALLOW_AUTO_SUBTITLES", False)

# --- Transkript onarımı ---
# auto   : kalite eşiğin altındaysa onar (varsayılan)
# always : her zaman onar (Whisper çıktısı bile kırpılır/düzeltilir)
# off    : hiç onarma
REPAIR_MODE = os.environ.get("REPAIR_MODE", "auto").strip().lower()
# Eşikler ÖLÇÜLEREK kalibre edildi (tahmin değil):
#   elle yazılmış altyazı : noktalama 3.9  büyük-harf 0.93
#   YouTube otomatik      : noktalama 5.0  büyük-harf 0.30
#   Whisper (Groq)        : noktalama 11.2 büyük-harf 1.00
#   noktalaması silinmiş  : noktalama 0.0  büyük-harf 0.00
# Noktalama yoğunluğu KALİTEYİ değil cümle uzunluğunu ölçüyor (iyi metinde cümleler
# uzun → skor düşük), bu yüzden eşik yalnızca "hiç noktalama yok" halini yakalar.
REPAIR_MIN_PUNCT = _float("REPAIR_MIN_PUNCT", 1.0)
# İlk harfi büyük olan satır oranı. ASR baştan sona küçük harf yazar.
REPAIR_MIN_CAPS = _float("REPAIR_MIN_CAPS", 0.15)

# --- Görsel katman (Faz 2) ---
ENABLE_FRAMES = _bool("ENABLE_FRAMES", True)
VIDEO_MAX_HEIGHT = _int("VIDEO_MAX_HEIGHT", 720)
# Ekran kaç saniyede bir örneklenir. Slayt ayrımını sahne tespiti değil phash yapar
# (sahne dedektörleri slaytlarda ölçüldü, çalışmıyor — bkz. frames.py).
# Düşür = kısa süre görünen slaytlar da yakalanır, ama iş yavaşlar.
SAMPLE_INTERVAL = _float("SAMPLE_INTERVAL", 5.0)
# phash hamming mesafesi; bunun altındaki kareler "aynı" sayılıp elenir.
# Ölçüm: aynı slayt = 0, farklı slayt = 6-8. 5 bu ikisinin arasında.
PHASH_DISTANCE = _int("PHASH_DISTANCE", 5)
OCR_LANGS = os.environ.get("OCR_LANGS", "tur+eng").strip()
# Windows'ta tesseract PATH'te olmayabilir; Docker'da olur. Boşsa PATH'e güvenilir.
TESSERACT_CMD = os.environ.get("TESSERACT_CMD", "").strip() or None
# Bu kadar bile metni olmayan kare slayt değildir (kamera görüntüsü) — atılır.
MIN_OCR_CHARS = _int("MIN_OCR_CHARS", 15)
MAX_FRAMES = _int("MAX_FRAMES", 80)

# --- Hızlı yol: YouTube'u Gemini'ye linkiyle ver (bkz. pipeline/gemini_video.py) ---
# İndirme + kare çıkarma + OCR + Whisper yerine tek model çağrısı. Başarısızsa iş
# klasik yola döner (asla düşmez). ÖLÇÜLDÜ (2026-09-18, 15 dk konuşma): 90 sn,
# indirme yok; transkript ELLE YAZILMIŞ altyazıyla %98 aynı, zaman damgası
# sapması ort. 1,8 sn → varsayılan AÇIK. Bedeli: slayt GÖRÜNTÜSÜ yok (metni var).
VIDEO_HIZLI = _bool("VIDEO_HIZLI", True)
VIDEO_HIZLI_MAX_DK = _int("VIDEO_HIZLI_MAX_DK", 180)


def ensure_dirs() -> None:
    for d in (DATA_DIR, WORK_DIR, OUT_DIR, UPLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)

# yt-dlp'yi komut ADIYLA değil, çalışan Python'un modülü olarak çağır. Ev işçisi
# venv'i etkinleştirmeden python.exe ile koşuyor: venv\Scripts PATH'te değil,
# 'yt-dlp' bulunamıyor ve HER link işi [WinError 2] ile düşüyordu (ölçümde
# yakalandı). -m yt_dlp venv'de, Docker'da, her yerde aynı çalışır.
YTDLP = (sys.executable, "-m", "yt_dlp")
