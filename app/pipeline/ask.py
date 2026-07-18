"""Kaynağa soru sor — ama DÜRÜST.

Her rakipte (NoteGPT, NotebookLM, Recall) "içerikle sohbet" var. Fark şu: onlar
cevabı bulamayınca da bir şey uydurur. Burada tek kural, ürünün tezinin sohbete
uygulanmış hâli: cevap YALNIZCA çıkarılan transkript + ekran metninden gelir;
kaynakta yoksa model "bunu kaynakta bulamadım" der, tahmin etmez. Böylece sohbet
de kaynağın yerine geçer — onu çarpıtmadan.
"""

from ..llm import complete_json, language_rule, windows

_ASK_SCHEMA = {
    "type": "object",
    "properties": {
        # found=false ise cevap "bunu kaynakta bulamadım" ekseninde olmalı.
        "found": {"type": "boolean"},
        "answer": {"type": "string"},
        # Dayandığı transkript parçaları (zaman damgalı). Kanıt: kullanıcı
        # cevabı kaynağa geri götürebilsin. found=false ise boş.
        "kaynak": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["found", "answer", "kaynak"],
    "additionalProperties": False,
}

_ASK_SYSTEM = """Sana bir videonun/belgenin zaman damgalı transkripti ve \
kullanıcının bir sorusu veriliyor. Görevin: soruyu YALNIZCA bu transkripte \
dayanarak yanıtlamak.

Mutlak kurallar:
- Cevabı SADECE verilen transkriptten çıkar. Genel bilgiden, tahminden, \
transkriptte olmayan hiçbir şeyden yararlanma.
- Cevap transkriptte YOKSA: found=false, answer'da kısaca "Bunu kaynakta \
bulamadım" de ve neyi aradığını bir cümleyle söyle. UYDURMA. Kaynakta \
olmayan bir cevabı olduğu gibi sunmak, bu ürünün tam olarak karşı durduğu şey.
- Cevap transkriptte VARSA: found=true, net ve kapsayıcı yanıt ver. Somut olan \
her şeyi koru (sayı, isim, komut, adım).
- kaynak: cevabı dayandırdığın transkript parçalarını, mümkünse zaman \
damgalarıyla (örn. "[12:30]") kısa alıntılar hâlinde ver. Kullanıcı cevabı \
kaynağa geri götürebilsin. found=false ise boş bırak.
- Soru kısmen yanıtlanabiliyorsa: yanıtlanabilen kısmı ver, eksik kalanı \
açıkça söyle ("… kısmını kaynakta bulamadım").
- ÖNCEKİ KONUŞMA verilirse: yalnızca soruyu ANLAMAK için kullan (örn. "peki ya \
bu?" neyi kastediyor). Cevap yine SADECE transkriptten gelir — geçmiş konuşma \
bir kaynak değildir, orada söyleneni "doğru" kabul edip üzerine ekleme."""
_ASK_SYSTEM += language_rule()

# Takip sorularında bağlamı taşımak için son N tur yeter; fazlası bütçeyi yer
# ve konuyu dağıtır.
_HISTORY_TURNS = 6


async def answer(
    title: str, transcript: str, question: str, history: list | None = None
) -> dict:
    """Kaynağa dayalı, uydurmayan soru-cevap (takip sorularını destekler).

    Sağlayıcı çağrıdan ÖNCE llm.set_provider ile seçilmiş olmalı (windows() ve
    complete_json onu ContextVar'dan okur). Dönen: {found, answer, kaynak}.
    history: [{"soru","cevap"}] — yalnızca soruyu anlamak için (pronoun/atıf
    çözme); cevap yine transkriptten gelir. Transkript sağlayıcının bağlam
    bütçesini aşarsa kırpılır ve bu DÜRÜSTÇE söylenir — sessizce yarısını atıp
    "bulamadım" demek, ürünün suçladığı şeyin ta kendisi olurdu.
    """
    win = windows()
    # Bölüm penceresini bağlam tavanı için vekil alıyoruz; transkript sığmıyorsa
    # kullanıcıya kırptığımızı söylüyoruz (aşağıda not).
    limit = max(win.get("section", 12000) * 6, 40000)
    kirpildi = len(transcript) > limit
    govde = transcript[:limit]

    onceki = ""
    for tur in (history or [])[-_HISTORY_TURNS:]:
        if not isinstance(tur, dict):
            continue
        s = (tur.get("soru") or "").strip()
        c = (tur.get("cevap") or "").strip()
        if s and c:
            onceki += f"K: {s}\nC: {c}\n"
    onceki_blok = (
        f"ÖNCEKİ KONUŞMA (yalnızca soruyu anlamak için):\n{onceki}\n"
        if onceki else ""
    )

    user = (
        f"BAŞLIK: {title}\n\n"
        f"TRANSKRİPT{' (yalnızca ilk kısmı — tümü sığmadı)' if kirpildi else ''}:\n"
        f"{govde}\n\n"
        f"{onceki_blok}"
        f"SORU: {question}"
    )
    r = await complete_json(
        system=_ASK_SYSTEM, user=user, schema=_ASK_SCHEMA,
        effort="low", max_tokens=1200,
    )
    found = bool(r.get("found"))
    cevap = (r.get("answer") or "").strip()
    kaynak = [k for k in (r.get("kaynak") or []) if isinstance(k, str) and k.strip()]
    if kirpildi:
        # Kırpmayı gizlemek yalan olurdu: cevap eksik transkriptten gelmiş
        # olabilir. Kullanıcı bunu bilerek okusun.
        cevap += ("\n\n(Not: kaynak bu sohbet için çok uzundu, yalnızca ilk "
                  "kısmına bakabildim — cevap eksik olabilir.)")
    return {"found": found, "answer": cevap or "Bunu kaynakta bulamadım.", "kaynak": kaynak}
