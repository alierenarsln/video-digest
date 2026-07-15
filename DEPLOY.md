# Coolify'a kurulum

## Önce oku: bu deploy'un tek gerçek riski

**YouTube veri merkezi IP'lerini engelliyor.** `yt-dlp` bir VPS'ten çalıştığında
YouTube sık sık "bot olmadığınızı doğrulayın" diyerek reddediyor; ev IP'sinden aynı
istek geçiyor. Bu boru hattının **ilk adımı**: video inmezse ne transkript var ne OCR.
Altyazı da yt-dlp ile çekildiği için o kaçış da yok.

Deploy'dan sonra ilk işiniz gerçek bir YouTube linki denemek olsun. Engellenirse
seçenekler: çerez aktarmak (kırılgan) ya da servisi ev makinesinde tutup dışarı
Cloudflare Tunnel ile açmak (YouTube ev IP'sini görür).

## 1. Kaynak

- Repo: `alierenarsln/video-digest` (private — Coolify'a GitHub erişimi vermeniz gerekir)
- Branch: `main`
- Build Pack: **Dockerfile** (repo kökündeki `Dockerfile`)
- Port: **8080**

## 2. Kalıcı disk (ZORUNLU)

Coolify → Storages → volume ekle:

| | |
|---|---|
| Mount path | `/data` |

Bağlamazsanız **her deploy'da tüm geçmiş silinir** — iş kayıtları (SQLite), özetler
ve slayt görselleri hep burada.

## 3. Ortam değişkenleri

### Zorunlu

| Değişken | Değer | Not |
|---|---|---|
| `APP_PASSWORD` | *(güçlü bir şifre)* | **Boş bırakılamaz.** Konteyner şifresiz açılmayı reddeder — servis internete açık ve korumasız kalırsa linki bulan herkes iş atıp API kotanızı yakar. |
| `GROQ_API_KEY` | `gsk_...` | Transkript (Whisper) + varsayılan özet modeli |

### Opsiyonel

| Değişken | Değer | Not |
|---|---|---|
| `APP_USER` | `admin` | Varsayılan `admin` |
| `OPENROUTER_API_KEY` | `sk-or-v1-...` | İkinci ücretsiz sağlayıcı; arayüzde seçilebilir olur |
| `ANTHROPIC_API_KEY` | `sk-ant-...` | Girilirse en iyi kalite ve varsayılan sağlayıcı olur |
| `OUTPUT_LANGUAGE` | `Türkçe` | Varsayılan zaten Türkçe |
| `TRANSCRIBE_LANGUAGE` | *(boş bırakın)* | Zorlamadır: `tr` yazarsanız İngilizce videolar bozulur |

### ⚠️ Bunları KESİNLİKLE ayarlamayın

| Değişken | Neden |
|---|---|
| `TESSERACT_CMD` | Yerel Windows kurulumuna özel. Konteynerde tesseract zaten PATH'te; ayarlarsanız OCR kırılır. |
| `TESSDATA_PREFIX` | Aynı sebep — dil paketleri imajda kurulu (`tur`, `eng`). |
| `DATA_DIR` | Dockerfile `/data` veriyor; değiştirmeyin, volume oraya bağlı. |
| `IN_DOCKER` | Dockerfile `true` veriyor; şifre zorunluluğunu bu sağlıyor. |

## 4. Kaynak beklentisi

Görsel katman CPU-yoğun (ffmpeg örnekleme + her benzersiz slayt için Tesseract OCR).
Küçük bir VPS'te 1 saatlik video uzun sürer — arka plan işi olduğu için sorun değil,
ama 2 vCPU'nun altına inmeyin. Disk: video indirme geçici (`/data/work`, iş bitince
silinir) ama slayt görselleri kalıcı (~35 KB/slayt, yoğun kursta ~3 MB/video).

## 5. Deploy sonrası

1. `https://<adres>/health` → `{"ok": true}` dönmeli (bu uç şifresiz, ayrıntı sızdırmaz).
2. `https://<adres>/` → tarayıcı şifre soracak (`APP_USER` / `APP_PASSWORD`).
3. Arayüzde sağ üstteki gösterge: sağlayıcı, özet dili ve **OCR dili uyarısı**.
   `tur` eksikse orada görünür — eksik dil paketi sessizce bozuk Türkçe metin üretir.
4. **Gerçek bir YouTube linki deneyin** (yukarıdaki risk).

## 6. n8n

`DEFAULT_CALLBACK_URL` verirseniz her iş bitiminde oraya POST atılır:

```json
{ "job_id": "...", "status": "done", "title": "...", "markdown": "# ...", "meta": {...} }
```

n8n'den API'yi çağırırken Basic auth başlığı eklemeyi unutmayın (`APP_USER`/`APP_PASSWORD`).
