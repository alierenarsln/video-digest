# Spec — Dürüst Özet Motoru

Durum: **tez kilitli** (2026-07-16). Kilit üç girdiye dayanıyor: iki araştırma raporu (belge araçları, video/meeting araçları) + kullanıcının gerçek arşivinde yapılan beş ölçüm. Hiçbir madde tahmin değil; tahmin olanlar açıkça öyle etiketlendi.

---

## 0. Tez

**Gezilemeyen kaynaklarda (video, taranmış PDF, slayt destesi) özet, kaynağın yerine geçer — o yüzden özetin dürüst olması gerekir.**

Gezilebilir kaynakta (metin katmanlı PDF, altyazılı düz konuşma) özet bir *indeks*tir: yanlışsa kullanıcı kaynağa dönüp düzeltir. Gezilemeyen kaynakta özet *ikame*dir: yanlışsa kullanıcının düzeltme yolu yoktur, çünkü kaynağa dönmek zaten yapamadığı şeydi. Bu asimetri ürünün tüm ağırlığını taşır.

Bundan üç bağlantı çıkar — hiçbiri süs değil, üçü de ölçüme veya mimariye bağlı:

1. **Defter = talep deneyi.** Honesty ledger bir güven özelliği değil; görünmez kaybı görünür yapan tek alet, dolayısıyla talebi ölçen enstrüman. (§4)
2. **Sessizlik = görsel tetikleyici.** Sesin uydurduğu an, ekranın öğrettiği andır. Aynı sinyal iki iş yapar. (§3)
3. **Türkçe = ekran kanalının iki kez amorti olduğu yer.** Konuşulmayanı kurtarır VE ASR'ın en değerli yanlış token'larını onarır. (§5)

---

## 1. Kanıt tabanı

### 1.1 Ölçümler (kullanıcının gerçek arşivi, bu makinede koşuldu)

| # | Bulgu | Sayı | Sonuç |
|---|---|---|---|
| M1 | `pypdf` (belge-beyni'nin `extract_pdf`'i birebir) **sessizce boş** dönüyor | 17/120 = **%14** | LLM boş metinden emin bir özet uydurur; kimse fark etmez |
| M2 | Tesseract kelime-güveni çöple sağlamı **ayırıyor** | medyan **23 vs 91**; çöpün %82'si <60, sağlamın %8-16'sı | Çift tepe gerçek → vadi kalibrasyonu OCR tarafında mümkün |
| M3 | Düşük güven ≠ okunamaz | s.1 ters (180°); döndürünce **23 → 91**, Türkçe kusursuz | **Önce onar, sonra ölç, sonra karantina** |
| M4 | Tesseract OSD güvenilmez | güven **11.24**, Türkçe belgeye "Script=Cyrillic" dedi | OSD'ye sorma; dört açıyı ölç, medyanı seç |
| M5 | Metin katmanı kalitesi **tek tepeli** | 122 dosya, gerçek-kelime oranı min **0.800**, medyan 0.959 | Vadi yok → bu külliyatta eşik uydurulamaz (§2.2) |

**Geçersiz sayılan iki test** (spec yazarken tekrarlanmayacak):
- **Bulanıklaştırma çöp proxy'si değil.** Tesseract bulanıklığa dayanıklı: yalnız 6.8 puan düştü, metin hâlâ doğru. Güven **haklı olarak** yüksek kaldı — ölçtüğüm şey bozulma değildi.
- **Karakter saymak çöpü yakalamaz.** Çöp bol karakter üretir; uzunluk sinyal değil.

### 1.2 Araştırma (video/meeting araçları, 12 araç)

- **Q2 = sıfır.** Hiçbir araç neyi düşürdüğünü, neyden emin olmadığını, neyi işleyemediğini söylemiyor. Tek epistemik işaret yok. Belge tarafındaki bulgu aynen taşınıyor, üstelik daha kötü: videoda hiç izlenmeyen ikinci kanal (ekran) var → boşluk raporu yok, boşluğun *farkındalığı* yok.
- **Q1: slayt-OCR commodity DEĞİL.** NotebookLM transkript-only (3 bağımsız kullanıcı). Eightify/Summarize.tech/Glasp caption tüketicisi. Otter en yakını ama **pull**: slayt görüntü olarak giriyor, metni çıkarmak için Business planı + sohbette anahtar kelimeyle sorman gerekiyor → konuşulmamış formül varsayılan özete girmiyor.
- **Sessizlik = halüsinasyon tetikleyicisi.** Whisper, AGC bozulup ses kısılınca uydurma üretiyor (HN `nullc`, doğrulanmış). Koenecke/FAccT + AP: ~%1 transkripsiyonda uydurma; olmayan ilaç adları; 30.000+ klinisyen etkilenmiş.
- **Halüsinasyon kamufle.** HN `camdenreslink`: *"they fabricate things that were never said (but always kind of close to something that was said)"* → doğrulanmamış incelemeyi geçecek kadar makul. Sessiz arıza bu yüzden tehlikeli, sadece can sıkıcı değil.
- **Türkçe yapısal ikinci sınıf.** Fathom: transkript 38 dil (TR dahil), **özet çevirisi 6 dil (TR yok)**. Whisper TR = Tier-2, WER %10-18, hata **özel isim + jargonda** yoğun. Kod-karışımı kırık: model başta bir dil seçip dayatıyor.

### 1.3 Bu oturumun kendi ürettiği veri

Arıza sınıfı, onu araştıran iki kişilik oturumda **üç kez** çıktı:
1. **Kayıp ajan** — üçüncü araştırma bağlantı kesintisinde düştü, hiç sinyal üretmedi; görev listesinde yok, bildirim yok. Sonsuza dek "hâlâ çalışıyor" gibi okundu.
2. **Sessiz-ses "Thank you"** — Whisper'ın boş sesten ürettiği klasik.
3. **Uydurma HN alıntısı** — araştırma ajanı gerçek bir thread ID'ye, o thread'de olmayan bir şikâyet yamadı; ham JSON'u `curl` ile yeniden çekince yakalandı.

Bu alıntı değil, **üretilmiş veri**: arıza nadir/egzotik değil, *denetimsiz üretimin varsayılan davranışı*. Pitch'in açılışı bu.

---

## 2. Belge hattı

### 2.1 Onarım → ölçüm → karantina (bu sıra, M3'ten)

Karantina **son çare**, ilk refleks değil. M3 kanıtı: sağlam bir sayfayı yalnız ters durduğu için atacaktık.

**Onarım durma kuralı (kenar 1 — duruyor):** bir onarım adımı yalnızca **başarısının ölçülebilir sinyali varsa** meşrudur.

| Adım | Ölçülebilir sinyal? | Karar |
|---|---|---|
| Döndürme (0/90/180/270) | **Evet** — dört açıyı OCR'la, güven medyanını karşılaştır, en iyisini seç | **Uygula** |
| Denoise / upscale / PSM taraması | **Hayır** — hangi çıktının "daha doğru" olduğunu söyleyen ground-truth'suz sinyal yok | **Uygulama** |

OSD'ye sorulmaz (M4). Dört açı ölçülür; bu 4× OCR maliyeti, yalnız düşük-güvenli sayfalarda ödenir.

### 2.2 Metin katmanı kapısı — **loglanmış sigorta** (kenar 2, ölçüm sonrası indirildi)

M5: bu külliyatta çift tepe yok, min 0.800 → eşik uydurmak bulanıklık kontrolünü tekrarlamak olurdu.

**Scope kaydı:** çürütme bu külliyata ait. Çöp-metin-katmanı belgelenmiş bir üretici sorunu (gömülü-olmayan/CID font, bazı Türkçe "yazdır→PDF" hatları). Bu arşiv o üreticileri içermiyor — o kadar.

**Bu yüzden kapı ateşlemez ama susmaz:** her belge için **en düşük gerçek-kelime oranı** loglanır, hiç tetiklenmese bile. Tek seferlik bulgu → izlenen değişmez. Bimodal bir külliyat gelirse veri söyler, sessizce geçmez.

```
belge işlendi: min_gercek_kelime_orani=0.83  (esik: yok — izleniyor)
```

### 2.3 Vadi kalibrasyonu — OCR tarafında (kenar 3, sabitlendi)

Çift tepe **OCR güveninde gerçek** (M2: 23 vs 91), metin katmanında yok (M5). Eşik oradan, gerçek külliyat histogramının vadisinden kalibre edilir; sabit sayı gömülmez, külliyat büyüdükçe yeniden ölçülür.

### 2.4 Figür yakalama — bağımsız (kullanıcının B2 düzeltmesi)

Sayfa döngüsü üç dik soruyu **karıştırmamalı**:
1. Metin katmanı var mı / güvenilir mi?
2. OCR gerekiyor mu / güveni ne?
3. **Sayfada figür/tablo/grafik var mı?**

(3), (1) ve (2)'den **bağımsız koşar**. Metin katmanı kusursuz bir sayfada da yakalanması gereken bir grafik olabilir. Eşik belge-uyarlamalı.

### 2.5 Kanıt sunan karantina (kenar 4 — duruyor)

Karantina "s.4 okunamadı" demez. **Görüntüyü masaya koyar:** *"Bu sayfayı okuyamadım (güven medyanı 24, dört açı denendi). İşte görüntüsü — sen bak."* Defterin kendi alçakgönüllülüğü: kendi ölçümüne de kefil olmaz.

---

## 3. Video hattı — sessizlik kuplajı

**Bugün iki alt-sistem bağımsız koşuyor.** `silencedetect` çıktısı var ama yalnız nota gidiyor; kare örnekleme sabit fps; phash dedup ediyor. Bağlanmamış tek şey **sinyalin kendisi**.

**Mimari emir (korelasyon değil):** ses sustuğunda o pencerede
- kare örnekleme sıklığı **yükselir**,
- OCR önceliği **yükselir**,
- ASR çıktısı o pencerede **düşük güvenli** işaretlenir.

Tek sinyal, çift iş: **sesin yalanını karantinaya alır VE kamerayı gerçeğe çevirir.** Bedava — çıktı zaten üretiliyor.

Gerekçe (§1.2): sessizlik hem Whisper'ın belgelenmiş halüsinasyon tetikleyicisi, hem hocanın slaytı okumaya bıraktığı an. Aynı saniye.

---

## 4. Defter — üç rol

Sırayla: **güven özelliği → savunulabilirlik → talebi keşfeden deney.**

### 4.1 Biçim: kurtardığını masaya koyar, boşluk raporu vermez

Yanlış biçim: *"3 bölgeyi işleyemedim."*
Doğru biçim: *"Hoca sesli okumadığı bir slayttan şu formülü aldım — işte, 14:22."*

Sebep: iki okumayı (fark etmiyorlar / umursamıyorlar) ayıramıyoruz çünkü **kayıp görünmez**. Kullanıcı hissetmediği kaybı talep edemez. Defter onu görünür yapan tek alet — o hâlde kaybı değil, **kurtarmayı** göstermeli.

### 4.2 Temiz koşu: genişleyen tek satır, duvar kâğıdı değil

```
✓ 47 dk, 2 dil, 31 slayt okundu, 4 tanım geri eklendi        [aç]
```
Tıklayınca açılır. Her koşuda ekrana dökülen rapor **gürültü**dür ve okunmaz hâle gelir.

### 4.3 Sıkıştırma oranı — ölçülemeyenin ölçülebilir vekili

Bölüm başına: *"3. bölüm 41:1 sıkıştı"*. Defter ölçtüğü boşlukları (karantina, düşük güven) bilir; **ölçemediği** boşluğu bilmez. Sıkıştırma oranı, "burada bir şey kaybolmuş olabilir" demenin dürüst yolu — iddia değil, davet.

### 4.4 Eleştirmen sayısı türlerle

Yanlış: *"6 madde geri eklendi."*
Doğru: *"geri eklendi: 3 sayı, 2 tanım, 1 uyarı."*
Sayı tek başına anlamsız; tür kullanıcıya *neyin* riskte olduğunu söyler.

### 4.5 Enstrümantasyon — açık riski kapatan deney

**Tek metrik:** özeti okuyan kullanıcı, gömülü kurtarılmış slayta / zaman damgasına **dokunuyor mu?**

Elimizdeki video-digest bunu **bugün** ölçebilir. Büyük çalışma gerekmez.

**Önceden kayıt (§6, F1):** ilk N gerçek kullanımda kurtarılan-görsel etkileşimi eşiğin üstünde → okuma (1), talep gerçek. ≈0 → okuma (2), pivot.

---

## 5. Türkçe — ayrı kapı değil, ekranın iki kez ödediği yer

Whisper TR hatası **özel isim + jargonda** yoğunlaşıyor; kod-karışımı ("deployment", "cash flow") belgelenmiş kırık. Yani **en yüksek bilgili token'lar, tam da yanlış yazılanlar.**

Onları ne düzeltir? **Slaytta doğru yazan hâlleri.**

Ekran kanalı:
- konuşulmayanı **kurtarır**,
- transkriptin en değerli yanlış token'larını **onarır**.

Belge tarafında zaten kurulmuş olan "OCR, ASR terimini düzeltir" ilkesinin video hâli. Fathom'un "kelimeni yazarım ama Türkçe özetlemem" yapısal ikinci-sınıflığı tam buraya oturuyor: **değer katmanından dışlanan token = ekran kanalının geri kazandığı token.**

Türkçe yan-not değil; çekirdek özelliğin kendini iki kez amorti ettiği yer.

---

## 6. Yanlışlanma tablosu

Karar kuralları **önceden** kaydedildi. Sonuç geldiğinde yeniden yorumlanmaz.

| # | Yanlışlayıcı gözlem | Statü | Karar |
|---|---|---|---|
| **F1** | Kurtarılan-görsel etkileşimi ilk N gerçek kullanımda **≈0** | **AÇIK** — tek gerçek risk | Ekran-okuma okuma (2)'ye düşer → pivot: hendek defter + Türkçe + YouTube-dışı kaynaklar |
| **F2** | Bir araç görünür "neyi düşürdüm" raporu **shipping ediyor** | **Ateşlemedi** (Q2 = 0/12) | Tez doğrulandı |
| **F3** | Video araçları ekranı **zaten okuyor** (push, varsayılan) | **Ateşlemedi** — Otter yalnız pull + paywall | Ateşleseydi: hendek defter + Türkçe'ye kayardı |
| **F4** | Kullanıcı "kısa istiyor", dürüstlük ayrıntısını istemiyor | **AÇIK** | Defter tek satıra katlanır (§4.2 zaten bu biçimde) |
| **F5** | Gezilebilir metnin özeti de isteniyor | **AÇIK** | Metin-PDF indeks vakası "atla-ve-dön haritası" olarak yeniden açılır |
| **F6** | Bimodal metin-katmanı külliyatı geliyor | **İzleniyor** (§2.2 logu) | Kapı sigortadan kalibre eşiğe yükseltilir |

**Kilitlenmemiş sayılan:** F1. `snthpy` (churn eden kullanıcı) talepsizlik hipotezinin açıklaması gereken bir gözlem — ama n=1 hipotez seçmez. Karar F1'in enstrümanına bırakıldı.

---

## 7. Dondurulmuş

**B2 (OCR ince ayarı) dondu.** Ölçüme oturdu; daha fazla cila azalan getiri. §2 yeterli.

## 8. Açık uçlar (spec dışı, kayıt için)

- İlk gerçek YouTube işi (`c23e86cac7c3`) doğrulanmadı — Groq TPM darboğazı (her çağrı kalan bütçenin ~%85'ini istiyor → ~1 çağrı/dk). Optimizasyon tespit edildi, ölçülmedi.
- Coolify: `USE_LOCAL_AGENT=true` + Redeploy gerekiyor.
- Otter Business slayt-çıkarma davranışı **canlı hesapta doğrulanmadı** (help.otter.ai 403). En yakın rakibin gerçek kabiliyeti çözülmemiş.
- Türkçe kullanıcı şikâyeti **bulunamadı** — Reddit crawler'a kapalı, Türkçe forumlar US-English aramanın dışında. Bu yokluk büyük olasılıkla arama artefaktı, memnuniyet kanıtı değil.
