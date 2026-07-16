# Claude Design Prompt

> Bu dosyanın içeriği doğrudan Claude'a yapıştırılmak içindir. `docs/SPEC.md` girdisidir; okumadan bu prompt'u kullanma.

---

Bir ürünün arayüzünü tasarlamanı istiyorum. Ürün çalışıyor — boru hattı uçtan uca ayakta, Türkçe ve İngilizce, ücretsiz modellerle. Eksik olan tek şey, ürünün asıl iddiasını kullanıcıya **gösteren** yüzey.

## Ürün ne yapar

Video, taranmış PDF ve slayt destesi alır; transkript + ekran OCR'ı + özet üretir. Ama bu bir "özetleyici" değil.

## Tez — tasarımın taşıması gereken tek fikir

**Gezilemeyen kaynaklarda özet, kaynağın yerine geçer — o yüzden özetin dürüst olması gerekir.**

Gezilebilir kaynakta (metin katmanlı PDF) özet bir *indeks*tir: yanlışsa kullanıcı kaynağa dönüp düzeltir. Gezilemeyen kaynakta (1.5 saatlik ders videosu, taranmış sözleşme) özet *ikame*dir: yanlışsa kullanıcının düzeltme yolu yoktur — çünkü kaynağa dönmek zaten yapamadığı şeydi. Tüm tasarım ağırlığı bu asimetrinin üstünde durur.

Bu piyasada karşılanmamış: incelenen 12 video/toplantı aracının **hiçbiri** neyi düşürdüğünü, neyden emin olmadığını söylemiyor. Sıfır. Her arıza sessiz. Ve arızalar makul görünüyor — belgelenmiş bir kullanıcı şikâyeti şöyle: uydurulan şeyler *"her zaman söylenmiş bir şeye yakın"*. Yani doğrulanmamış incelemeyi geçecek kadar inandırıcı. Sessiz arıza bu yüzden tehlikeli, sadece can sıkıcı değil.

## Tasarlaman gereken üç yüzey

### 1. Defter (ledger) — ürünün kalbi

**Kritik biçim kararı, bunu yanlış yaparsan ürün ölür:** defter bir *boşluk raporu* değil, **kurtardığını masaya koyan** şeydir.

- Yanlış: "3 bölgeyi işleyemedim."
- Doğru: "Hoca sesli okumadığı bir slayttan şu formülü aldım — işte, 14:22." *(ve formülün görüntüsü orada)*

Sebep: kullanıcı **hissetmediği kaybı talep edemez**. Ekranda kaçan içerik görünmez bir kayıptır; kullanıcı NotebookLM'in grafikleri okumadığını fark etmez. Defter kaybı görünür yapan tek alettir — o hâlde kaybı değil, **kurtarmayı** göstermeli.

Alt gereksinimler:
- **Temiz koşu tek satır olmalı, genişleyebilir.** Her koşuda ekrana dökülen rapor duvar kâğıdıdır, okunmaz hâle gelir ve tam da güvenilmesi gereken anda göz ardı edilir. Örnek iskelet (metni sen tasarla): `✓ 47 dk, 2 dil, 31 slayt okundu, 4 tanım geri eklendi [aç]`
- **Eleştirmen sayısı tür taşımalı.** "6 madde geri eklendi" anlamsız. "geri eklendi: 3 sayı, 2 tanım, 1 uyarı" kullanıcıya *neyin* riskte olduğunu söyler. **Taksonomiyi sen öner** — sayı/tanım/uyarı yeterli mi, başka tür gerekir mi?
- **Sıkıştırma oranı bölüm başına gösterilmeli:** "3. bölüm 41:1 sıkıştı". Bu, ölçülemeyenin ölçülebilir vekilidir. Defter ölçtüğü boşlukları bilir; **ölçemediğini** bilmez. Bu oran "burada bir şey kaybolmuş olabilir" demenin dürüst yolu — iddia değil, davet.
- **Karantina kanıt sunmalı.** "s.4 okunamadı" demez; sayfanın görüntüsünü masaya koyar: "Bu sayfayı okuyamadım (güven medyanı 24, dört açı denendi). İşte görüntüsü — sen bak." Defter kendi ölçümüne de kefil olmaz.

### 2. Kurtarılan görsel — hem özellik hem enstrüman

Ekrandan kurtarılan içerik (konuşulmamış formül, tablo, kod parçası) özetin **içine gömülü** görünmeli, zaman damgasına bağlı, tıklanabilir.

Bunun ikinci bir işi var ve tasarımı belirler: **ölçmemiz gereken tek şey, kullanıcının o gömülü görsele dokunup dokunmadığı.** Elimizde tek açık risk var — ekran-okuma talebinin organik kanıtı n=1 (tek bir kullanıcı, tam bu yüzden NotebookLM'i bıraktı). Dokunma oranı eşiğin üstündeyse talep gerçek; ≈0 ise pivot ediyoruz. Yani bu yüzey **enstrümanlı** olmalı ve dokunmayı doğal kılmalı — ama sahte şekilde teşvik etmemeli, yoksa ölçüm kirlenir.

### 3. Akış — iki giriş, tek boru hattı

- YouTube linki yapıştır → indir → işle → özet
- Ya da lokalden dosya seç → yükle → aynı akış

Uzun işler (bir video 20+ dk sürebilir). İlerleme dürüst olmalı: hangi aşamada, ne kadar kaldı, **ve tahmin yanlışsa onu söyle**. Sessizce çalışıp sonsuza dek "işleniyor" gösteren bir ekran, ürünün eleştirdiği arızanın ta kendisidir.

## Bilmen gereken kısıtlar

- Halihazırda FastAPI + statik HTML (`app/static/index.html`), sunucu tarafı işleme, SQLite. Ağır bir SPA framework'ü getirmene gerek yok — ama gerekçesi varsa öner.
- Türkçe ve İngilizce. Arayüz dili Türkçe.
- Karanlık/aydınlık ikisi de.
- Ücretsiz modeller kullanılıyor (Groq / OpenRouter), kullanıcı sağlayıcıyı seçebiliyor. Kotalar dar; arayüz bunu saklamamalı.

## Türkçe neden yan-not değil

Whisper'ın Türkçe hatası **özel isim ve jargonda** yoğunlaşıyor (WER %10-18), kod-karışımı ("deployment", "cash flow" diyen Türk konuşmacı) belgelenmiş kırık. Yani en yüksek bilgili token'lar tam da yanlış yazılanlar. Onları düzelten şey: **slaytta doğru yazan hâlleri.** Ekran kanalı hem konuşulmayanı kurtarıyor hem transkriptin en değerli yanlış token'larını onarıyor. Tasarım bunu görünür kılabilirse (ör. "bu terimi slayttan düzelttim"), çekirdek özellik kendini iki kez amorti eder.

## İstediğim çıktı

1. **Ekran akışı** — hangi ekranlar, hangi sırayla, ne gösteriyorlar.
2. **Defterin tam biçimi** — tek satır hâli ve açılmış hâli. Gerçek metinle, lorem ipsum'la değil.
3. **Eleştirmen tür taksonomisi** — önerin ve gerekçesi.
4. **Kurtarılan görselin özet içindeki yerleşimi** — dokunmayı doğal kılan, ama sahte teşvik etmeyen.
5. **Uzun-iş ilerleme durumu** — dürüst belirsizlik nasıl gösterilir.

Her karar için **tek cümle gerekçe** ver. Güzel görünen ama tezi taşımayan bir tasarım başarısızlıktır; tezi taşıyan ama çirkin olan tasarım düzeltilebilir.

## Son uyarı

Ürünün iddiası dürüstlük. Arayüzü, ürünün kendisinin yapmadığı bir şeyi iddia ediyormuş gibi göstermek — "AI destekli", "her şeyi yakalar", "%99 doğru" — tezle doğrudan çelişir. Ürün ne yaptığını ve **ne yapamadığını** aynı sakinlikte söylemeli.
