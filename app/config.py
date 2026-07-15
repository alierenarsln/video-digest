import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


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
GROQ_TRANSCRIBE_MODEL = os.environ.get("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
GROQ_BASE_URL = "https://api.groq.com/openai/v1"

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "claude-opus-4-8")

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

# Özet/bölümleme/eleştirmen/onarım hangi sağlayıcıda koşsun?
#   anthropic  : Claude — en iyi, 1M bağlam, ücretli
#   groq       : gpt-oss-120b — ücretsiz. Sınır: 8000 token/dk. İstek sayısı
#                sınırsız → sınırsız video, ama yavaş ve istekler küçük olmalı.
#   openrouter : tencent/hy3 — ücretsiz. Sınır: GÜNDE 50 İSTEK (kredi 0 iken;
#                $10 kredi alınırsa 1000). 262k bağlam → büyük istek serbest,
#                ama istek sayısı kıymetli.
# Boş bırakılırsa: Anthropic anahtarı varsa anthropic, yoksa groq (sınırsız video).
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "").strip().lower() or (
    "anthropic" if ANTHROPIC_API_KEY else "groq"
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
}


def provider_available(name: str) -> bool:
    return {
        "groq": bool(GROQ_API_KEY),
        "openrouter": bool(OPENROUTER_API_KEY),
        "anthropic": bool(ANTHROPIC_API_KEY),
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

DATA_DIR = Path(os.environ.get("DATA_DIR", "./data")).resolve()
WORK_DIR = DATA_DIR / "work"
OUT_DIR = DATA_DIR / "out"
# Yüklenen dosyalar OUT_DIR'e KONULMAZ: orası /out altında servis ediliyor,
# yüklediğiniz video internete açılırdı.
UPLOAD_DIR = DATA_DIR / "uploads"
DB_PATH = DATA_DIR / "jobs.sqlite3"

CHUNK_SECONDS = _int("CHUNK_SECONDS", 600)
TRANSCRIBE_CONCURRENCY = _int("TRANSCRIBE_CONCURRENCY", 3)
TRANSCRIBE_LANGUAGE = os.environ.get("TRANSCRIBE_LANGUAGE", "").strip() or None
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


def ensure_dirs() -> None:
    for d in (DATA_DIR, WORK_DIR, OUT_DIR, UPLOAD_DIR):
        d.mkdir(parents=True, exist_ok=True)
