# video-digest — Faz 1

Video (YouTube linki veya yerel dosya) → zaman damgalı transkript → kapsayıcı özet.

n8n orkestratör, bu servis işçi. Ağır iş (ffmpeg, transkript, özet) burada koşar;
n8n sadece tetikler ve sonucu dağıtır.

## Boru hattı

```
kaynak → yt-dlp/ffmpeg → 16kHz mono WAV (+ video, varsa)
       → altyazı varsa onu kullan, yoksa Groq Whisper (parçalı, zaman damgalı)
       → görsel katman: örnekleme → phash tekrar eleme → OCR   [Faz 2]
       → transkript onarımı (yalnızca metin bölük pörçükse)
       → semantik bölümleme (konu sınırları, sabit uzunluk DEĞİL)
       → bölüm özetleri (paralel; transkript + ekran metni birlikte)
       → eleştirmen geçişi  ← kapsayıcılığı asıl sağlayan adım
       → sentez (TL;DR + sözlük)
       → markdown (slaytlar gömülü)
```

**Altyazı önceliği.** YouTube'da elle yazılmış altyazı varsa Whisper'a hiç gidilmez:
bedava, anlık ve genelde **Whisper'dan daha doğru** (noktalama, özel isimler, satırlar
cümle sınırında biter). Ama bir tuzak var: YouTube otomatik altyazıyı makine
çevirisiyle yüzlerce dile çoğaltıyor — İngilizce bir videonun otomatik listesinde
157 dil çıkıyor ve oradaki `tr` İngilizce ASR'dan makine çevirisidir. Bu yüzden:

- Elle yazılmış altyazı (`subtitles`) → kullanılır.
- Otomatik altyazı (`automatic_captions`) → yalnızca videonun **orijinal dilinde** ve
  yalnızca `ALLOW_AUTO_SUBTITLES=true` ise. Kalitesi dile göre değişiyor (İngilizcede
  artık noktalama var, Türkçede zayıf), Whisper genelde daha iyi olduğu için kapalı.
- Makine çevirisi altyazı → **asla** otomatik seçilmez.

Altyazı indirilemezse iş düşmez, Whisper'a dönülür.

**Transkript onarımı.** Otomatik altyazı ve zayıf ASR cümle ortasından bölünmüş
satırlar üretir (`[8.5] resolution of 28x28 pixels. But your`), Türkçe gibi az
kaynaklı dillerde noktalama tamamen kaybolabilir. Bu, semantik bölümlemeyi bozar:
konu sınırı arayan model cümle sınırı olmayan bir metinde çalışmak zorunda kalır.
Onarım geçişi metni tam cümlelere çevirir, zaman damgalarını korur, bariz ASR
hatalarını bağlamdan düzeltir ve **ekran metnini (OCR) kullanarak** terimleri
düzeltir (ASR "BG slash NBD" → slaytta "BG/NBD" yazıyorsa düzeltilir).

> **Onarım özetleme değildir** — en büyük risk modelin "temizle" derken metni
> kısaltması. Çıktı kelime sayısı girdinin %60'ının altına düşerse onarım
> **reddedilir** ve orijinal korunur. Onarım koşarsa ham transkript de
> `<job_id>.transcript.raw.txt` olarak saklanır; onarımın bir şeyi bozup bozmadığı
> ancak ikisini karşılaştırarak görülür.
>
> **Tespit metriği ölçülerek kalibre edildi, tahminle değil.** İlk metrik (noktalama
> yoğunluğu) ölçünce **ters sıralama** yaptı: elle yazılmış altyazı 3.9, YouTube
> otomatik altyazı 5.0 — yani daha iyi kaynak daha düşük skor alıyordu, çünkü metrik
> aslında cümle uzunluğunu ölçüyor (iyi metinde cümleler uzun). Asıl ayrımı
> **büyük-harf oranı** yapıyor: elle yazılmış 0.45, otomatik 0.10 (satırların %90'ı
> cümle ortasında başlıyor). Şimdi noktalama eşiği yalnızca "hiç noktalama yok"
> halini yakalıyor, kararı büyük-harf sinyali veriyor.

İki adım kapsayıcılığı taşıyor:

**Görsel katman (Faz 2).** Konuşmacı slaytta yazan her şeyi söylemez — sadece sesten
üretilen özet, ekranda 20 saniye duran bir tanımı ya da komut satırını tamamen kaçırır.
ffmpeg ekranı `SAMPLE_INTERVAL` saniyede bir örnekler, algısal hash (phash) tekrarları
eler, OCR ekranı okur. Metni `MIN_OCR_CHARS`'ın altında kalan kare slayt sayılmaz
(konuşan kafa), atılır. Kalan metin hem bölüm özetine hem eleştirmene girer.

> **Neden sahne tespiti yok:** İlk tasarım PySceneDetect kullanıyordu. Ölçüldü:
> bir slayt destesinde **dört dedektör de (Content, Adaptive, Histogram, Hash) sıfır
> kesme buldu** — slaytta ekranın büyük kısmı sabit kalıp yalnızca metin değişiyor,
> fark eşiğin altında. Kontrol deneyi (siyah→beyaz kesme) düzgün çalıştı, yani araç
> bozuk değil; sahne dedektörleri sinema kurgusu için tasarlanmış ve slayt destesi
> onların en kötü olduğu durum. Aynı karelerde phash temiz ayrım verdi (aynı slayt=0,
> farklı slayt=6-8), ayrım tamamen ona bırakıldı. `scenedetect` ve `opencv`
> bağımlılıkları böylece tamamen kalktı.

**Eleştirmen geçişi.** Taslak özeti ham transkript + ekran metniyle karşılaştırıp
"hangi sayı, isim, tanım, uyarı düşmüş?" diye sorar ve bulduklarını ilgili bölüme geri
ekler. Çıktıdaki dipnot kaç madde eklendiğini söyler — sürekli 0 ise özet zaten
kapsayıcıdır, sürekli yüksekse bölüm özetleri fazla eleyicidir.

Videosu olmayan kaynakta (meeting kaydı, podcast) görsel katman kendiliğinden atlanır.

## Model sağlayıcısı: Anthropic anahtarı olmadan da çalışır

Özetleme, bölümleme, eleştirmen ve onarımın hepsi model çağrısı — bir LLM olmadan
sistem transkript üretir ama özet üretemez. İki yol var, kod ikisini de destekler:

| | `anthropic` | `groq` (varsayılan) | `openrouter` |
|---|---|---|---|
| Model | Claude Opus 4.8 | `openai/gpt-oss-120b` | `tencent/hy3:free` |
| Maliyet | ücretli | **ücretsiz** (Whisper ile aynı anahtar) | **ücretsiz** |
| Bağlam | 1M | 131k | 262k |
| Asıl kısıt | yok | **8000 token/dk** | **50 istek/gün** |
| Pratikte | en iyi kalite | **sınırsız video**, ~10-15 dk/video | **~3 video/gün**, daha az istek |

**İki ücretsiz sağlayıcının kısıtı zıt yönde** ve bu mimariyi doğrudan etkiliyor.
Groq'u token kotası boğuyor ama istek sayısı sınırsız → **küçük pencereler, çok
istek**. OpenRouter'ı istek sayısı boğuyor ama bağlamı geniş → **büyük pencereler,
az istek** (tüm transkript tek çağrıda bölümlenir, bu aynı zamanda daha iyi
bölümleme demek). Pencere boyutları bu yüzden `config.py`'de sağlayıcıya göre
otomatik ayarlanıyor; aynı boyutu ikisine vermek birini mutlaka bozar — Groq'ta
kalıcı 413, OpenRouter'da günlük kotanın erken bitmesi.

Varsayılan `groq`: günlük sınır olmadığı için istediğiniz kadar video işlersiniz.
Az sayıda videoyu daha iyi bölümlemeyle işlemek isterseniz `LLM_PROVIDER=openrouter`.

**Groq ücretsiz katmanının asıl sınırı bağlam değil, dakikalık token kotası** — ve
Groq bu bütçeye istediğiniz `max_tokens`'ı da sayar (2k girdi + `max_tokens=16000`
→ "Requested 18127" → 413). Kotadan büyük tek bir istek **asla** geçmez; beklemek
çözmez, bölmek gerekir. Boru hattı bu yüzden pencereli: bölümleme transkripti
parça parça tarar, eleştirmen tüm transkript yerine **bölüm bazında** koşar (kotaya
sığmasının yanı sıra daha odaklı), sentez yalnızca bölüm özetlerini görür. Bu mimari
Anthropic'te de çalışır — dev bağlam varsayımı tamamen kalktı.

Anahtar girdiğiniz gün `LLM_PROVIDER` kendiliğinden `anthropic`'e döner.

## Kurulum — yerel (Docker'sız), Windows

Bu makinede kurulu ve doğrulanmış olan yol budur.

```powershell
# 1. İkililer (bir kez)
winget install --id Gyan.FFmpeg -e
winget install --id UB-Mannheim.TesseractOCR -e

# 2. Türkçe OCR dil paketi — kurulum yalnızca eng getirir.
#    Program Files'a yazmak yönetici ister; bu yüzden proje içinde tutuyoruz.
mkdir tessdata
Copy-Item "C:\Program Files\Tesseract-OCR\tessdata\eng.traineddata" tessdata\
Copy-Item "C:\Program Files\Tesseract-OCR\tessdata\osd.traineddata" tessdata\
Invoke-WebRequest "https://github.com/tesseract-ocr/tessdata/raw/main/tur.traineddata" `
  -OutFile tessdata\tur.traineddata

# 3. Python ortamı
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt

# 4. Ayarlar
Copy-Item .env.example .env    # GROQ_API_KEY ve ANTHROPIC_API_KEY'i doldurun

# 5. Çalıştır
.\run.ps1
```

`run.ps1` PATH'i tazeler — `winget` kurulumdan sonra PATH'i değiştirir ve **açık olan
kabuklar eski PATH'i taşımaya devam eder**, o yüzden aynı pencerede `ffmpeg` "yok"
görünür. Yeni bir kabuk açmak da çözer.

`.env` içindeki `TESSERACT_CMD` ve `TESSDATA_PREFIX` yalnızca yerel kurulum içindir;
Docker'a geçerseniz ikisini de boşaltın (imajda tesseract PATH'te ve diller yerinde).

## Kurulum — Docker

```bash
cp .env.example .env          # TESSERACT_CMD / TESSDATA_PREFIX satırlarını boş bırakın
docker compose up -d --build
curl http://localhost:8080/health
```

`GROQ_API_KEY` ücretsiz: https://console.groq.com/keys

Türkçe içerik işleyecekseniz `.env` içinde `TRANSCRIBE_LANGUAGE=tr` verin — otomatik
algılamaya bırakmaktan belirgin daha doğru sonuç verir.

## Kullanım

### Arayüz (önerilen)

```powershell
.\run.ps1          # sunucuyu başlatır
```

Sonra tarayıcıda **http://127.0.0.1:8080** — link yapıştır, ilerlemeyi izle, özeti
slaytlarıyla birlikte oku. Geçmiş işler solda; sekmeyi kapatsanız da iş sunucuda
devam eder.

Üstteki gösterge sağlığı söyler: sağlayıcı, özet dili ve **OCR dili eksikse uyarı**
(eksik dil paketi sessizce bozuk Türkçe metin üretir — bkz. dil desteği bölümü).

### Tek komut

```powershell
.\ozet.ps1 "https://www.youtube.com/watch?v=..."
.\ozet.ps1 "C:\yol\video.mp4"
```

Sunucu kapalıysa kendisi başlatır, bekler, bitince markdown'ı açar.

### API

```bash
# İş at
curl -X POST http://localhost:8080/jobs \
  -H 'content-type: application/json' \
  -d '{"source": "https://www.youtube.com/watch?v=...", "callback_url": "https://n8n.../webhook/digest-done"}'
# → {"job_id": "a1b2c3d4e5f6", "status": "queued"}

# Durum
curl http://localhost:8080/jobs/a1b2c3d4e5f6

# Sonuç
curl http://localhost:8080/jobs/a1b2c3d4e5f6/markdown
```

`source` bir URL ya da konteyner içinden görülen bir dosya yolu olabilir
(`./data` klasörü `/data` olarak bağlı — `./data/video.mp4` dosyası için
`source: "/data/video.mp4"`).

`callback_url` verilirse iş bitince oraya POST atılır:

```json
{ "job_id": "...", "status": "done", "title": "...", "markdown": "# ...", "meta": {...} }
```

Hata durumunda `{"job_id": "...", "status": "error", "error": "..."}`.

## Çıktılar

| Yol | İçerik |
|---|---|
| `data/out/<job_id>.md` | Özet |
| `data/out/<job_id>_frames/` | Özete gömülü slayt görüntüleri |
| `data/out/<job_id>.transcript.txt` | Kullanılan transkript (onarıldıysa onarılmış hali) |
| `data/out/<job_id>.transcript.raw.txt` | Onarım koştuysa ham hali — karşılaştırmak için |
| `data/jobs.sqlite3` | İş kayıtları |

Markdown, görselleri göreli yolla (`<job_id>_frames/...`) referanslar — `.md` dosyasını
taşırsanız yanındaki `_frames` klasörünü de taşıyın, yoksa resimler kırılır.

`data/work/<job_id>/` iş sırasında kullanılır, bitince silinir.

## n8n

`n8n/workflow.json` içe aktarılabilir bir başlangıç akışı: webhook ile link alır,
`POST /jobs` çağırır, tamamlanma webhook'unu bekler. Sonucu nereye göndereceğiniz
(WhatsApp, mail, Obsidian) size kalmış — son düğümü kendinize göre değiştirin.

## Ayarlar (.env)

| Değişken | Varsayılan | Not |
|---|---|---|
| `LLM_PROVIDER` | (boş) | Boş = Anthropic anahtarı varsa `anthropic`, yoksa `groq` |
| `GROQ_LLM_MODEL` | `openai/gpt-oss-120b` | Katı JSON şeması şart — llama-3.3-70b **desteklemiyor** |
| `GROQ_TPM` | `8000` | Ücretsiz katmanın dakikalık token kotası (ölçüldü) |
| `GROQ_CONCURRENCY` | `1` | Kota küresel; paralellik 429 üretir, hız kazandırmaz |
| `OUTPUT_LANGUAGE` | `Türkçe` | Boş = kaynağın dili. Boş bırakmak dil karıştırıyor |
| `SUMMARY_MODEL` | `claude-opus-4-8` | Yalnızca `LLM_PROVIDER=anthropic` iken |
| `CHUNK_SECONDS` | `600` | Ses parça uzunluğu; Groq boyut limiti için |
| `TRANSCRIBE_CONCURRENCY` | `3` | Eşzamanlı Groq isteği; free tier'da yükseltmeyin |
| `TRANSCRIBE_LANGUAGE` | (boş) | **Zorlama, ipucu değil.** `tr` yazılıysa İngilizce video bozulur. Karışık içerikte boş bırakın |
| `USE_SUBTITLES` | `true` | Elle yazılmış altyazı varsa Whisper'a hiç gitme |
| `SUBTITLE_LANGS` | (boş) | Boş = videonun kendi dili. `tr,en` = elle yazılmış Türkçe çeviri varsa onu tercih et |
| `ALLOW_AUTO_SUBTITLES` | `false` | YouTube ASR'ını da kabul et (yalnızca orijinal dilde) |
| `REPAIR_MODE` | `auto` | `auto` / `always` / `off` — bölük pörçük metni cümlelere çevir |
| `REPAIR_MIN_PUNCT` | `1.0` | Yalnızca "hiç noktalama yok" halini yakalar |
| `REPAIR_MIN_CAPS` | `0.15` | Asıl sinyal. Elle yazılmış 0.45, otomatik altyazı 0.10 |
| `ENABLE_FRAMES` | `true` | Kapatınca yalnızca ses iner; hızlı ama slaytlar kaçar |
| `SAMPLE_INTERVAL` | `5.0` | Ekran kaç sn'de bir örneklenir. Düşür = kısa görünen slaytlar da yakalanır, iş yavaşlar |
| `PHASH_DISTANCE` | `5` | Yükselt = benzer slaytlar da elenir. Ölçüm: aynı slayt=0, farklı=6-8 |
| `MIN_OCR_CHARS` | `15` | Bu kadar metni olmayan kare slayt sayılmaz |
| `MAX_FRAMES` | `80` | Aşılırsa eşit aralıkla seyreltilir (log'a yazılır) |

Ayar tutmuyorsa `data/out/<job_id>_frames/` klasörüne bakın: slaytlar eksikse
`SAMPLE_INTERVAL`'ı düşürün; konuşan kafa doluysa `MIN_OCR_CHARS`'ı yükseltin;
aynı slayt birden çok kez çıkıyorsa `PHASH_DISTANCE`'ı yükseltin.

## Dil desteği: Türkçe + İngilizce

Hedef bu iki dil. Çıktı her zaman `OUTPUT_LANGUAGE` (varsayılan Türkçe) — İngilizce
kursun Türkçe özeti asıl kullanım. Kaynak dili Whisper'ın otomatik algılamasına
bırakılır (`TRANSCRIBE_LANGUAGE` boş); `tr` yazmak İngilizce videoyu bozar.

Bir dil denetimi koşturuldu; bulgular ölçülerek doğrulandı veya çürütüldü:

| Bulgu | Sonuç |
|---|---|
| `'İ'.lower()` birleşik nokta (U+0307) üretiyor → `İşlem` ve `işlem` sözlükte **iki ayrı girdi** | ✅ gerçek, düzeltildi (`_glossary_key`) |
| Sözlük sırası Türkçe alfabeye göre değil → Ç/Ö/Ş/Ğ **Z'den sonra** düşüyor | ✅ gerçek, düzeltildi (`_tr_sort_key`, I→ı küçültme dahil) |
| Altyazı dil kodu **tam eşleşme** arıyor → video dili `en`, altyazı `en-GB` ise elle yazılmış altyazı varken Whisper'a düşülüyor | ✅ gerçek (canlı YouTube'da kanıtlandı), düzeltildi (`_match`) |
| `tur.traineddata` eksikse Tesseract **sessizce** İngilizce'ye düşüyor (hata yok, log yok) | ✅ gerçek, düzeltildi (`check_ocr_langs`, açılışta + `/health`) |
| `est_tokens()` Türkçe'de düşük tahmin ediyor (2.69 krk/token) → kota aşımı | ❌ **çürütüldü**: ölçüm `prompt_tokens`'taki sabit ~71 token'lık sohbet şablonu yüküyle kirlenmiş |
| `IL`/`il` çarpışması bir tanımı siliyor | ❌ **çürütüldü**: İngilizce'de de aynı, dile özgü değil |

## Doğrulama durumu (2026-07-15)

| Adım | Durum |
|---|---|
| API → SQLite → kuyruk → worker → hata yolu | ✅ gerçek iş atılarak doğrulandı |
| `fetch` (yerel dosya, ffmpeg → 16kHz wav, başlık, ses akışı kontrolü) | ✅ |
| Görsel katman (örnekleme → phash dedup → Türkçe OCR) | ✅ 32 örnek → 3 benzersiz → 3 slayt, metinler tam |
| `transcribe` — tek parça (Groq, gerçek API) | ✅ 8 segment, damgalar tutarlı |
| `transcribe` — çok parça + offset kaydırma | ✅ 5 parça → 11 segment, damgalar tek parçalı koşuyla birebir |
| Altyazı seçim politikası | ✅ 5 durum: orijinal dil / çeviri tercihi / makine çevirisine düşmeme / auto yalnız orijinal / kapalı |
| Altyazı json3 ayrıştırma | ✅ elle yazılmış 286 satır; otomatikte 499 `aAppend` yuvarlanan tekrar elendi, ardışık tekrar 0 |
| Onarım **tespiti** (hangi metin bozuk?) | ✅ 5 gerçek/türetilmiş kaynakta doğru sınıflandırma |
| Onarım **geçişi** (LLM ile cümleye çevirme) | ✅ `this is a 3` → `This is a 3.` — noktalama 0.0→4.2, büyük-harf 0.00→1.00, kelime oranı %100 (özetlememiş) |
| `segment` (pencereli) + `summarize` + eleştirmen + `render` | ✅ Groq'ta uçtan uca: 4 bölüm, 15 terim, eleştirmen 6 madde ekledi, İngilizce kaynaktan Türkçe özet |
| `fetch` (YouTube tam yolu: indirme + altyazı + kare) | ⚠️ hiç koşmadı |

Testlerde bulunup düzeltilen gerçek hatalar:
- **Sahne tespiti slaytlarda çalışmıyordu** → phash'e geçildi (yukarıdaki nota bakın).
- **Parça süresi ölçümü `KeyError` ile çöküyordu** — ffprobe, segment muxer'ın yazdığı
  FLAC'larda `format.duration` vermiyor. Bu **10 dakikadan uzun her videoyu** düşürürdü;
  tek parçalı testte görünmüyordu. Offset'ler artık ffmpeg'in `-segment_list` çıktısından.
- **Windows konsolunda `→` karakteri** worker'ı `UnicodeEncodeError` ile düşürüyordu.
- **Sessiz video** 40 satırlık anlaşılmaz ffmpeg hatası veriyordu → net mesaj.

Özet katmanı (Claude) anahtar girilene kadar **hiç çalışmadı**; orada sürpriz normal.

## Sınırlar

- Meeting kaydı ve konuşmacı ayrımı yok → **Faz 3**
- İşler sırayla koşar; tek makine için doğru, kuyruk uzarsa worker'ı çoğaltmak gerekir.
- Görsel katman videoyu indirip her sahneyi OCR'dan geçirir; işi belirgin yavaşlatır.
  Slaytsız içerikte (vlog, sohbet) `ENABLE_FRAMES=false` daha mantıklı.
