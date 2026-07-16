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
| M6 | **Geçerli bozma kontrolü** bulundu | 1/9 küçültme: **92 → 34** (58 puan) | Bulanıklığın yerine geçen gerçek çöp üreteci |
| M7 | Eşik 60 **bu külliyatta doğrulandı** | sağlam 91-92 (**6/6** yanlış pozitif yok), ters 39, bozuk 34 | 60 üç vakayı da ayıran vadide (§2.3) |
| M8 | Sıkışma **1'in altına düşebiliyor** | sessiz videoda özet 350 kelime, transkript 3 | `genişleme` kademesi doğdu (§4.3) |

**Geçersiz sayılan iki test** (spec yazarken tekrarlanmayacak):
- **Bulanıklaştırma çöp proxy'si değil.** Tesseract bulanıklığa dayanıklı: yalnız 6.8 puan düştü, metin hâlâ doğru. Güven **haklı olarak** yüksek kaldı — ölçtüğüm şey bozulma değildi. **Yerine M6 geçti.**
- **Karakter saymak çöpü yakalamaz.** Çöp bol karakter üretir; uzunluk sinyal değil. — Bu yalnız bir test kusuru değildi: `frames.py` tam olarak bunu yapıyordu (`MIN_OCR_CHARS`), yani düşük güvenli çöp prompt'a gerçek bilgi gibi giriyordu. **Ölçümün önceden haber verdiği hata koda girmişti** (§2.6'da düzeltildi).

### 1.2 Araştırma (video/meeting araçları, 12 araç)

- **Q2 = sıfır.** Hiçbir araç neyi düşürdüğünü, neyden emin olmadığını, neyi işleyemediğini söylemiyor. Tek epistemik işaret yok. Belge tarafındaki bulgu aynen taşınıyor, üstelik daha kötü: videoda hiç izlenmeyen ikinci kanal (ekran) var → boşluk raporu yok, boşluğun *farkındalığı* yok.
- **Q1: slayt-OCR commodity DEĞİL.** NotebookLM transkript-only (3 bağımsız kullanıcı). Eightify/Summarize.tech/Glasp caption tüketicisi. Otter en yakını ama **pull**: slayt görüntü olarak giriyor, metni çıkarmak için Business planı + sohbette anahtar kelimeyle sorman gerekiyor → konuşulmamış formül varsayılan özete girmiyor.
- **Sessizlik = halüsinasyon tetikleyicisi.** Whisper, AGC bozulup ses kısılınca uydurma üretiyor (HN `nullc`, doğrulanmış). Koenecke/FAccT + AP: ~%1 transkripsiyonda uydurma; olmayan ilaç adları; 30.000+ klinisyen etkilenmiş.
- **Halüsinasyon kamufle.** HN `camdenreslink`: *"they fabricate things that were never said (but always kind of close to something that was said)"* → doğrulanmamış incelemeyi geçecek kadar makul. Sessiz arıza bu yüzden tehlikeli, sadece can sıkıcı değil.
- **Türkçe yapısal ikinci sınıf.** Fathom: transkript 38 dil (TR dahil), **özet çevirisi 6 dil (TR yok)**. Whisper TR = Tier-2, WER %10-18, hata **özel isim + jargonda** yoğun. Kod-karışımı kırık: model başta bir dil seçip dayatıyor.

### 1.3 Bu oturumun kendi ürettiği veri

Arıza sınıfı, onu araştıran ve inşa eden oturumda **altı kez** çıktı. Hiçbiri aranmadı; hepsi başımıza geldi:

1. **Kayıp ajan** — üçüncü araştırma bağlantı kesintisinde düştü, hiç sinyal üretmedi; görev listesinde yok, bildirim yok. Sonsuza dek "hâlâ çalışıyor" gibi okundu.
2. **Sessiz-ses "Thank you"** — Whisper'ın boş sesten ürettiği klasik.
3. **Uydurma HN alıntısı** — araştırma ajanı gerçek bir thread ID'ye, o thread'de olmayan bir şikâyet yamadı; ham JSON'u `curl` ile yeniden çekince yakalandı.
4. **Yanlış hizalanmış test** — eleştirmen testi, sıralanmış listeyle etiketleri yan yana koyup modele iftira attı. Ham çıktı okununca model **3/3 doğruydu**. Sessiz hizalama kayması, makul görünen yanlış sonuç.
5. **`loading="lazy"` sessizce boş slaytlar** — `curl` 200 veriyor, sayfada **sıfır istek**. Boyutsuz görseller 0 yüksekliğe çöküp aynı noktaya yığılıyor, tarayıcı hiçbirini istemiyor. Kurtarılan görsel — ürünün tek farkı — sessizce kayboluyordu.
6. **`MIN_OCR_CHARS`** — ürünün kendi OCR filtresi çöpü karakter sayısıyla eliyordu; M2 bunun işe yaramadığını *önceden* söylemişti. Düşük güvenli çöp prompt'a gerçek bilgi gibi giriyordu.

Bu alıntı değil, **üretilmiş veri**: arıza nadir/egzotik değil, *denetimsiz üretimin varsayılan davranışı*. En güçlü kanıt 2 + 6'nın birlikteliği: ürün, kendi tarif ettiği arızayı hem sergiledi hem barındırdı.

**Ve tez ürünün kendi test verisinde kazara kanıtlandı:** bitmiş üç işin transkriptinin tamamı `[00:00] Thank you.` — ama özetler 350 kelime ve **tamamı ekrandan geldi** (BG/NBD, 90 gün, 0.35 eşik). Piyasadaki her araç bu videoya "Thank you." deyip geçerdi. §3'ün sessizlik→halüsinasyon zinciri ile §0'ın ekran kanalı, aynı 16 saniyede birlikte görüldü.

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

> **Durum: UYGULANMADI — henüz uygulanamaz.** Bu depoda PDF/belge hattı yok; §2.2 belge-beyni birleştirmesiyle birlikte gelir. §2.1/2.3/2.5/2.6 (OCR tarafı) uygulandı, çünkü video hattı ekranı zaten OCR'lıyor.

M5: bu külliyatta çift tepe yok, min 0.800 → eşik uydurmak bulanıklık kontrolünü tekrarlamak olurdu.

**Scope kaydı:** çürütme bu külliyata ait. Çöp-metin-katmanı belgelenmiş bir üretici sorunu (gömülü-olmayan/CID font, bazı Türkçe "yazdır→PDF" hatları). Bu arşiv o üreticileri içermiyor — o kadar.

**Bu yüzden kapı ateşlemez ama susmaz:** her belge için **en düşük gerçek-kelime oranı** loglanır, hiç tetiklenmese bile. Tek seferlik bulgu → izlenen değişmez. Bimodal bir külliyat gelirse veri söyler, sessizce geçmez.

```
belge işlendi: min_gercek_kelime_orani=0.83  (esik: yok — izleniyor)
```

### 2.3 Vadi kalibrasyonu — OCR tarafında (kenar 3) — **UYGULANDI**

Çift tepe **OCR güveninde gerçek** (M2: 23 vs 91), metin katmanında yok (M5).

`frames.OCR_CONF_ESIK = 60.0` — devralınmış değil, M7'de bu külliyatta doğrulandı: sağlam 91-92 (6/6 yanlış pozitif yok), ters 39, bozuk 34. Üç vakayı da ayırıyor.

**Ama sabit sayı gömülü sayılmıyor:** her karenin güveni ateşlese de ateşlemese de loglanır —
```
[frames]  2642s guven= 24.0 aci=  0 KARANTINA (412 karakter)
[frames]     0s guven= 92.0 aci=180 ok (218 karakter)
```
Eşik tek seferlik bulgudan değil, biriken ölçümden gelmeli. §2.2'nin kuralı burada da geçerli.

### 2.4 Figür yakalama — bağımsız (kullanıcının B2 düzeltmesi)

Sayfa döngüsü üç dik soruyu **karıştırmamalı**:
1. Metin katmanı var mı / güvenilir mi?
2. OCR gerekiyor mu / güveni ne?
3. **Sayfada figür/tablo/grafik var mı?**

(3), (1) ve (2)'den **bağımsız koşar**. Metin katmanı kusursuz bir sayfada da yakalanması gereken bir grafik olabilir. Eşik belge-uyarlamalı.

### 2.5 Kanıt sunan karantina (kenar 4) — **UYGULANDI**

Karantina "s.4 okunamadı" demez. **Görüntüyü masaya koyar:** *"Bu ekranı okuyamadım. Metnini özete katmadım — işte görüntüsü, sen bak."* + güven medyanı + denenen açı. Defterin kendi alçakgönüllülüğü: kendi ölçümüne de kefil olmaz.

Yük taşıyan garanti, test edildi: **karantina metni `as_prompt_text`'ten geçmez** — çöp LLM'e sızmaz. Kare kaybolmaz; `meta.quarantined` ile defterde kanıtıyla görünür. Nötr arduvaz (`--sig-quarantine`), alarm değil — okuyamamak bir olgu.

`frames_used` yalnız **okunan** ekranı sayar: "31 slayt okundu" derken okuyamadığımızı saymak yalan olurdu.

### 2.6 Onarım–ölçüm–karantina zinciri — **UYGULANDI** (`frames.py`)

Ölçümün önceden haber verdiği hata koda girmişti: `_ocr` güveni hiç ölçmüyordu (`image_to_string`) ve çöpü **karakter sayısıyla** eliyordu (`MIN_OCR_CHARS`). Düşük güvenli çöp prompt'a gerçek bilgi gibi giriyordu.

Şimdiki akış:
1. `image_to_data` (tsv) → kelime-güveni medyanı
2. güven < eşik → **önce onar**: dört açı (`ACILAR`), en iyi medyan. OSD'ye sorulmaz (M4). Denoise/upscale/PSM yok (§2.1 durma kuralı).
3. onarım kurtarmadıysa → **karantina**: metin LLM'e gitmez, kare kanıt olarak kalır.

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

### 4.3 Sıkıştırma oranı — ölçülemeyenin ölçülebilir vekili — **UYGULANDI**

Bölüm başına: *"3. bölüm 41:1 sıkıştı"*. Defter ölçtüğü boşlukları (karantina, düşük güven) bilir; **ölçemediği** boşluğu bilmez. Oran, "burada bir şey kaybolmuş olabilir" demenin dürüst yolu — iddia değil, davet.

**Kaynak = transkript + ekran metni.** Yalnız transkript sayılsaydı, çok slaytlı ama az konuşmalı bir bölüm "az sıkıştı" görünürdü — oysa en çok kaybın olabileceği yer tam orası. 120 kelime altındaki bölüm listeye **hiç girmez**: 30 kelimeden "6:1" üretmek ölçüm değil, rakam uydurmak.

**`genişleme` kademesi M8'den doğdu.** Devralınan eşikler (`≥30 yüksek`, `≥12 orta`, else `düşük`) oranı 1'in altına düşen bölümü **`düşük · yeşil` yani "güvenli"** gösteriyordu — defter en çok bakılması gereken yerde susuyordu. 1.0 keyfi değil, **ilkesel sınır**: çıktı girdiden uzunsa o bir özet değildir. Meşru da olabilir (içerik ekrandan gelmiştir) → suçlama değil işaret:

> *"Bir bölümde özet, giren metinden uzun. İçerik ekrandan gelmiş olabilir — ya da model eklemiş. O bölümü kaynakla karşılaştır."*

**Açık:** `≥30` / `≥12` hâlâ **devralındı, kalibre edilmedi** — tek bir gerçek özet yok (üçü halüsinasyon transkriptli, gerçek olan TPD'den düştü). Kural aynı: eşik uydurma, ölç, logla, külliyat birikince kalibre et. `genişleme` bundan ayrı — o ölçülmüş bir sınır.

### 4.4 Eleştirmen sayısı türlerle — **UYGULANDI**

Yanlış: *"6 madde geri eklendi."*
Doğru: *"geri eklendi: 3 sayı · 2 tanım · 1 uyarı."*

**Tasarım paketinin taksonomisi düzeltildi.** `3 sayı · 2 tanım · 1 uyarı · 4 terim` diyordu — ama "terim" bir *tür* değil. Doğrusu iki **dik eksen**:

- **tür** = ne kaçtı → `sayı · isim · tanım · uyarı · istisna · adım`
- **kaynak** = nereden geldi → `ekran · konuşma`

Bir sayı ekrandan da gelebilir konuşmadan da. Ayrımın karşılığı var: **`kaynak=ekran` sayısı, defterin daha önce uydurduğu "ekrandan kurtarıldı" iddiasının gerçek ölçümü** — §0'ın tek farkı artık sayılabiliyor.

Türler uydurulmadı: eleştirmen prompt'unun **zaten aradığı** kategoriler. Sıra `CRITIC_TURLER`'de sabit, sayıya göre değil — koşular karşılaştırılabilir olmalı. Bilinmeyen etiket → madde **korunur**, sınıflandırma düşer.

Gerçek model doğrulaması (OpenRouter): transkriptte olmayan, yalnız ekranda olan üç bilgiyi **3/3** doğru etiketledi ve taslakta zaten olanı geri eklemedi.

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

## 7. Durum

**Uygulandı ve doğrulandı** (commit `3ed10d2`, `e0766c7`, `ca2b5e1`):
arayüz (Tanık defteri + kurtarılan görsel + dürüst ilerleme), tür ayrımlı eleştirmen, sıkışma oranı, onarım–ölçüm–karantina zinciri.

**B2 (OCR ince ayarı) dondu.** Ölçüme oturdu; daha fazla cila azalan getiri.

**Sırada:** gerçek video koşusu (kota) → §3 sessizlik kuplajı → F1 ilk verisi.

## 8. Açık uçlar (spec dışı, kayıt için)

- **İlk gerçek YouTube işi (`c23e86cac7c3`) öldü — sebep TPM değil, TPD.** Groq'un *günlük* 200.000 token sınırı doldu (196.949 kullanılmış, 7.306 istendi). Önceki "yavaş, ~1 çağrı/dk" teşhisi eksikti: yavaşlık gerçekti ama ölüm sebebi günlük kotanın bitmesi. Kota yenilenince koşulmalı.
- **Hiçbir ölçüm gerçek videoda uçtan uca koşmadı.** Üçü de birim + gerçek-veri düzeyinde doğrulandı (gerçek slaytlar, gerçek model çağrısı), ama tam boru hattı koşusu bekliyor. O koşu aynı zamanda **F1 enstrümanının ilk gerçek verisini** üretecek.
- Coolify: `USE_LOCAL_AGENT=true` + Redeploy gerekiyor.
- **§3 sessizlik kuplajı UYGULANMADI** — mimarideki tek bağlanmamış sinyal. `silencedetect` çıktısı var ama yalnız nota gidiyor; kare örnekleme hâlâ sabit fps. M8 (sessiz videoda özetin tamamı ekrandan geldi) bu kuplajın değerini ölçtü: sesin sustuğu an, ekranın öğrettiği an.
- Otter Business slayt-çıkarma davranışı **canlı hesapta doğrulanmadı** (help.otter.ai 403). En yakın rakibin gerçek kabiliyeti çözülmemiş.
- Türkçe kullanıcı şikâyeti **bulunamadı** — Reddit crawler'a kapalı, Türkçe forumlar US-English aramanın dışında. Bu yokluk büyük olasılıkla arama artefaktı, memnuniyet kanıtı değil.
