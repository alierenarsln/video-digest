# Ders materyali

> **Testleri ac** — kurulum yok, telefonda da calisir:
> **[▶ INE4307 Karar Analizi](https://alierenarsln.github.io/video-digest/ders-materyali/ine4307-decision-analysis-drill.html)** · **[▶ INE4106 BPR](https://alierenarsln.github.io/video-digest/ders-materyali/ine4106-bpr-drill.html)**
> (GitHub `.html` dosyalarini render etmez, ham kod gosterir; calistirmak icin bu Pages linklerini kullan.)

Ders slaytlarindan uretilen sinav calisma materyalleri. Bu klasor `video-digest`'in
"kurs icerigi -> calisilabilir ozet" akisinin ciktilarini tutar; fark su ki kaynak
video degil, ders notu PDF'leri.

## Icerik

| Dosya | Ders | Tur |
|---|---|---|
| `ine4307-decision-analysis-drill.html` | INE4307 Introduction to Decision Analysis | Interaktif test, 150 soru |
| `ine4106-bpr-drill.html` | INE4106 Business Process Reengineering | Interaktif test, 207 soru |
| `ine4106-bpr-studypack.pdf` | INE4106 Business Process Reengineering | 100 D/Y + 60 CS + tablolar |
| `ine3015-ppc-studypack.pdf` | INE3015 Production Planning and Control | 75 D/Y + 40 CS + 14 cozumlu problem |
| `ine4307-karar-analizi-studypack.pdf` | INE4307 Introduction to Decision Analysis | 60 D/Y + 40 CS + 10 cozumlu problem + 2 vaka |

## INE4307 testi

`ine4307-decision-analysis-drill.html`, finalin Part A (10 dogru/yanlis) ve Part B
(10 coktan secmeli) bolumlerini hedefler. Sinav yapisinin bes kati hacim: **50 dogru/yanlis
+ 100 coktan secmeli**.

| Konu | Soru | Kaynak deste |
|---|---|---|
| Introduction & Influence Diagrams | 36 | Lecture 1&2 (88 s.) |
| Decision Trees, Risk Profiles, Dominance | 39 | Lecture 3&4 (82 s.) |
| Sensitivity Analysis & Probability | 20 | Lecture 5&6 (57 s.) |
| Value of Information | 19 | ValueofInformation (36 s.) |
| Utility & Risk Attitudes | 36 | Utility (80 s.) |
| | **150** | |

- Soru ve siklar Ingilizce (sinav dili), aciklamalar Turkce
- Her sorunun kaynagi ekranda yazar: `L3&4 - Dominance s.64`
- Konu ve soru tipi filtresi; sadece VOI ya da sadece dogru/yanlis calisilabilir
- Bitiste konu bazli isabet orani; %55 altindaki konu icin slayta donmek onerilir
- Yanlislar toplanir, "kacirdiklarimi tekrar coz" turu acilir
- Klavye: `1`-`4` sik, `T`/`F` dogru-yanlis, `Enter` sonraki

**Kapsam disi:** Monte Carlo Simulation (silabusta W5 konusu ama ders slayti yok) ve
multi-attribute / additive value function (Utility destesinde tek sayfa). Cognitive
biases konusu kapsam icinde - Allais paradoksu, St Petersburg, endowment effect ve
preference reversal sorulari Utility destesinden geliyor.

## INE4106 testi

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

`ine4307-decision-analysis-drill.html` ayri bir sema kullanir, cunku iki soru tipi var:

```js
{k:"tf", t:"VOI", n:"<kaynak>", q:"<soru>", a:true,  e:"<aciklama>"}
{k:"mc", t:"UTL", n:"<kaynak>", q:"<soru>", o:[...], e:"<aciklama>"}
```

`t` konu filtresini besler (`L1&2` `L3&4` `L5&6` `VOI` `UTL`), `a` dogru/yanlis sorunun
cevabidir. Coktan secmelide kural ayni:

**Dogru cevap her zaman `o` dizisinin ilk elemanidir**; siklar ekrana basilirken
karistirilir. Yeni soru eklerken indeks tutmaya calismak gerekmez, dogruyu basa
yazmak yeterli.
