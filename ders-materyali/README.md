# Ders materyali

> **[▶ Testi simdi ac](https://alierenarsln.github.io/video-digest/ders-materyali/ine4106-bpr-drill.html)** — kurulum yok, telefonda da calisir.
> (GitHub `.html` dosyalarini render etmez, ham kod gosterir; calistirmak icin bu Pages linkini kullan.)

Ders slaytlarindan uretilen sinav calisma materyalleri. Bu klasor `video-digest`'in
"kurs icerigi -> calisilabilir ozet" akisinin ciktilarini tutar; fark su ki kaynak
video degil, ders notu PDF'leri.

## Icerik

| Dosya | Ders | Tur |
|---|---|---|
| `ine4106-bpr-drill.html` | INE4106 Business Process Reengineering | Interaktif test, 207 soru |
| `ine4106-bpr-studypack.pdf` | INE4106 Business Process Reengineering | 100 D/Y + 60 CS + tablolar |
| `ine3015-ppc-studypack.pdf` | INE3015 Production Planning and Control | 75 D/Y + 40 CS + 14 cozumlu problem |
| `ine4307-karar-analizi-studypack.pdf` | INE4307 Introduction to Decision Analysis | 60 D/Y + 40 CS + 10 cozumlu problem + 2 vaka |

## Interaktif test

`ine4106-bpr-drill.html` tek dosyalik, bagimliligi olmayan bir sayfa. Cift tiklayip
tarayicida acmak yeterli; sunucu, kurulum, internet gerekmez.

- Ekranda tek soru; sik tiklanarak ya da `1`-`4` tuslariyla cevaplanir
- Yanlista dogru sik ve gerekcesi ekranda kalir, `Enter`/`Space` ile devam edilir
- Dogruda 0.85 sn sonra otomatik ilerler (kapatilabilir)
- On ders notundan istenilenler secilerek kapsam daraltilabilir
- Sonuc ekraninda not bazinda dogruluk orani ve yanlislarin tam listesi cikar
- Yanlislar `localStorage`'a yazilir; sonraki acilista "sadece yanlislarim" modu gelir
- Sorular ve siklar her calistirmada karistirilir
- Acik/koyu tema destegi var

### Soru dagilimi

Sinav 40 coktan secmeli ve on ders notunu kapsiyor. Havuz, notlarin slayt
yogunluguna gore agirliklandirildi:

| # | Ders notu | Soru |
|---|---|---|
| 1 | BPR | 14 |
| 2 | Hammer paper (HBR 1990) | 20 |
| 3 | Change Management | 12 |
| 4 | Introduction to ERP | 13 |
| 5 | Introduction to SAP | 11 |
| 6 | SAP Concepts | 19 |
| 7 | SAP S/4HANA | 32 |
| 8 | SD (Sell) | 26 |
| 9 | MM (Buy) | 23 |
| 10 | PP (Make) | 37 |
| | **Toplam** | **207** |

## Kaynak ve kapsam

Sorular yalnizca dersin kendi slaytlarindan uretildi; on destenin tamami bastan sona
okundu. Disaridan SAP dokumantasyonu veya genel bilgi karistirilmadi, boylece
soru-cevap ciftleri dersin kendi terminolojisine ve konvansiyonlarina sadik kaliyor.

Kapsam disi tutulanlar: 11-18 numarali ders notlari (WM, FI, CO, HCM, OM,
Competitiveness, Process Selection, Product Design) ile Kaplan & Norton, Linear
Programming ve NSPE Ethics okumalari.

## Bakim

`ine4106-bpr-drill.html` icindeki `BANK` dizisi soru havuzudur. Her kayit:

```js
{n: <ders notu no>, q: "<soru>", o: ["<dogru>", "<yanlis>", "<yanlis>", "<yanlis>"], e: "<aciklama>"}
```

**Dogru cevap her zaman `o` dizisinin ilk elemanidir**; siklar ekrana basilirken
karistirilir. Yeni soru eklerken indeks tutmaya calismak gerekmez, dogruyu basa
yazmak yeterli.
