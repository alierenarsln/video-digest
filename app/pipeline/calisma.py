"""Çalışma modu: özetten çoktan seçmeli test + bilgi kartları (flashcard).

Özet okumak pasif; hatırlamayı sınamak (active recall) öğrenmeyi kalıcı yapan
şey. Recall/Coconote'taki quiz/kart özelliğinin karşılığı. İsteğe bağlı üretilir
(her özette değil — maliyet yalnız kullanılınca), sonuç DB'de saklanır: ikinci
açılışta anında gelir. Girdi yalnız ÖZET (transkript değil): kısa, hızlı ve
Vercel'in süre sınırına rahat sığar; sorular da özetin kapsadığı konulardan çıkar.
"""

from ..llm import complete_json, language_rule, provider

_SEMA = {
    "type": "object",
    "properties": {
        "sorular": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "soru": {"type": "string"},
                    "secenekler": {"type": "array", "items": {"type": "string"}},
                    "dogru": {"type": "integer"},
                    "aciklama": {"type": "string"},
                    "zaman": {"type": "string"},
                },
                "required": ["soru", "secenekler", "dogru", "aciklama", "zaman"],
                "additionalProperties": False,
            },
        },
        "kartlar": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"on": {"type": "string"}, "arka": {"type": "string"}},
                "required": ["on", "arka"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["sorular", "kartlar"],
    "additionalProperties": False,
}

_SISTEM = """Bir ders/video özetinden ÇALIŞMA MATERYALİ üretiyorsun.

sorular: 8 çoktan seçmeli soru. Kurallar:
- Anlamayı ve uygulamayı ölç, ezberi değil ("X nedir?" yerine "şu durumda hangisi
  doğru olur?" tipi de olsun). Kolaydan zora sırala.
- Her soruda TAM 4 seçenek; yalnız biri doğru. Yanlış seçenekler akla yatkın
  olsun (yaygın yanılgılar), saçma olmasın. Doğru cevabın yeri sorudan soruya değişsin.
- dogru: doğru seçeneğin 0-3 arası sırası.
- aciklama: doğru cevabın NEDEN doğru olduğu, 1-2 cümle.
- zaman: bu konunun özetteki zaman damgası (örn. "12:34"); yoksa boş.
- YALNIZ özette geçen bilgiden sor; özette olmayanı uydurma.
- İÇERİK sor, belgeyi değil: "hangi dakikada/zaman damgasında", "hangi bölümde",
  "kaç madde var" gibi özetin biçimine dair sorular YASAK.

kartlar: 12 bilgi kartı. on = kısa soru/terim/kavram; arka = 1-3 cümlelik net cevap.
Özetin en önemli tanımları, sayıları, adımları ve ilişkileri."""


async def uret(baslik: str, ozet_md: str) -> dict:
    # Özet çok uzunsa (Tüm hali birleşiği) baştan kes: sorular için yeter, istek küçük kalır.
    # Groq dakikalık token kotası küçük (8000): orada kısa kes.
    metin = ozet_md[:9_000 if provider() == "groq" else 60_000]
    sonuc = await complete_json(
        system=_SISTEM + language_rule(),
        user=f"Başlık: {baslik}\n\nÖZET:\n\n{metin}",
        schema=_SEMA,
        effort="medium",
        max_tokens=8000,
    )
    sorular = []
    for s in sonuc.get("sorular") or []:
        secenek = [str(x).strip() for x in s.get("secenekler") or [] if str(x).strip()]
        dogru = s.get("dogru")
        if len(secenek) < 2 or not isinstance(dogru, int) or not 0 <= dogru < len(secenek):
            continue  # bozuk soruyu gösterme
        sorular.append({
            "soru": s["soru"].strip(), "secenekler": secenek, "dogru": dogru,
            "aciklama": (s.get("aciklama") or "").strip(), "zaman": (s.get("zaman") or "").strip(),
        })
    kartlar = [
        {"on": k["on"].strip(), "arka": k["arka"].strip()}
        for k in sonuc.get("kartlar") or []
        if (k.get("on") or "").strip() and (k.get("arka") or "").strip()
    ]
    return {"sorular": sorular, "kartlar": kartlar}
