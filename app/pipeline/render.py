"""Özeti tek bir markdown dosyasına dök. YouTube kaynaklıysa zaman damgaları
tıklanabilir olur."""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .frames import ACILAR
from .summarize import Digest
from .transcribe import fmt_ts


_TUR_BASLIK = {
    "tutorial": "Adım adım uygula",
    "kurs": "Kendini test et",
    "gelisim": "Bu hafta yap",
}


def _learning_md(digest: Digest) -> list[str]:
    """Öğrenme çıktısı: adım/eylem/quiz + derinleşme prompt'u. Boşsa hiç yazma."""
    out: list[str] = []
    if digest.steps:
        out += ["## Adım adım uygula", ""]
        out += [f"{i}. {s}" for i, s in enumerate(digest.steps, 1)]
        out.append("")
    if digest.actions:
        out += ["## Bu hafta yap", ""]
        out += [f"- {a}" for a in digest.actions]
        out.append("")
    if digest.quiz:
        out += ["## Kendini test et", ""]
        for q in digest.quiz:
            out.append(f"- **{q['soru']}**")
            out.append(f"  <sub>Cevap: {q['cevap']}</sub>")
        out.append("")
    if digest.deepen_prompt:
        out += ["## Daha derine — araştırma prompt'u", ""]
        out += ["Bunu ChatGPT ya da Claude'a olduğu gibi yapıştır:", ""]
        # Fenced blok: arayüz kopyala butonu ekliyor, düz metinde de seçilebilir.
        out += ["```", digest.deepen_prompt, "```", ""]
    return out


def _link(url: str | None, seconds: float) -> str:
    label = fmt_ts(seconds)
    if not url:
        return f"`{label}`"
    parsed = urlparse(url)
    query = dict(parse_qsl(parsed.query))
    query["t"] = f"{int(seconds)}s"
    deep = urlunparse(parsed._replace(query=urlencode(query)))
    return f"[`{label}`]({deep})"


def render(
    digest: Digest,
    title: str,
    duration: float,
    meta: dict,
    assets_rel: str | None = None,
) -> str:
    url = meta.get("url")
    out: list[str] = [f"# {title}", ""]

    facts = [f"Süre: {fmt_ts(duration)}"]
    if meta.get("uploader"):
        facts.append(f"Kanal: {meta['uploader']}")
    if url:
        facts.append(f"[Kaynak]({url})")
    out += [" · ".join(facts), ""]

    out += ["## TL;DR", ""]
    out += [f"- {item}" for item in digest.tldr]
    out += ["", "## Detaylı Özet", ""]

    for section in digest.sections:
        out.append(f"### {_link(url, section.section.start)} {section.section.title}")
        out += ["", section.summary, ""]
        for ts, text in section.points:
            out.append(f"- {_link(url, ts)} {text}")
        out.append("")

        if assets_rel and section.frames:
            for frame in section.frames:
                rel = f"{assets_rel}/{frame.path.name}"
                out.append(f"![Ekran {fmt_ts(frame.ts)}]({rel})")
                if frame.quarantined:
                    # Karantina "s.4 okunamadı" deyip geçmez: sayfayı masaya
                    # koyar. Defter kendi ölçümüne de kefil olmaz — karar
                    # kullanıcının, kanıt önünde.
                    out.append(
                        f"<sub>{_link(url, frame.ts)} bu ekranı okuyamadım "
                        f"(güven medyanı {frame.conf}, {len(ACILAR)} açı denendi). "
                        f"Metnini özete katmadım — işte görüntüsü, sen bak.</sub>"
                    )
                else:
                    dondu = (
                        f", {frame.rotation}° döndürülerek okundu"
                        if frame.rotation
                        else ""
                    )
                    out.append(
                        f"<sub>{_link(url, frame.ts)} ekran görüntüsü{dondu}</sub>"
                    )
                out.append("")

    if digest.glossary:
        out += ["## Terimler", ""]
        out += [f"- **{term}** — {definition}" for term, definition in digest.glossary]
        out.append("")

    out += _learning_md(digest)

    footer = (
        f"Otomatik üretildi · {len(digest.sections)} bölüm · "
        f"eleştirmen geçişi {digest.added_by_critic} eksik madde ekledi"
    )
    if digest.frames_used:
        footer += f" · {digest.frames_used} slayt OCR ile okundu"
    out += ["---", "", f"<sub>{footer}</sub>", ""]
    return "\n".join(out)


def _sayfa(n: float) -> str:
    """Sayfa numarası 'zaman' olarak kodlanmıştı (document.py); geri çeviriyoruz."""
    return f"s. {int(n)}"


def render_document(digest: Digest, title: str, pages, assets_rel: str) -> str:
    """Belge özeti: video render'ının aynısı ama referanslar 'zaman' değil 'sayfa'.

    Karantina sayfaları özetten sonra ayrı bölümde, kanıtıyla: defter
    okuyamadığını gizlemez, sayfayı masaya koyar.
    """
    out: list[str] = [f"# {title}", ""]
    okunan = sum(1 for p in pages if not p.quarantined and p.text.strip())
    out += [f"{len(pages)} sayfa · {okunan} okundu", ""]

    out += ["## TL;DR", ""]
    out += [f"- {item}" for item in digest.tldr]
    out += ["", "## Detaylı Özet", ""]

    for section in digest.sections:
        out.append(f"### {_sayfa(section.section.start)} · {section.section.title}")
        out += ["", section.summary, ""]
        for ts, text in section.points:
            out.append(f"- `{_sayfa(ts)}` {text}")
        out.append("")

    if digest.glossary:
        out += ["## Terimler", ""]
        out += [f"- **{term}** — {definition}" for term, definition in digest.glossary]
        out.append("")

    out += _learning_md(digest)

    karantina = [p for p in pages if p.quarantined]
    if karantina:
        out += ["## Okuyamadığım sayfalar", ""]
        out += ["Bu sayfaların metnini özete katmadım. Kanıtı önünde — sen bak.", ""]
        for p in karantina:
            if p.img_rel:
                out.append(f"![Sayfa {p.number}]({p.img_rel})")
            out.append(
                f"<sub>`{_sayfa(p.number)}` okunamadı "
                f"(güven medyanı {p.conf}, {len(ACILAR)} açı denendi).</sub>"
            )
            out.append("")

    footer = (
        f"Otomatik üretildi · {len(pages)} sayfa · {okunan} okundu · "
        f"{len(digest.sections)} bölüm · eleştirmen {digest.added_by_critic} madde ekledi"
    )
    if karantina:
        footer += f" · {len(karantina)} sayfa karantinada"
    out += ["---", "", f"<sub>{footer}</sub>", ""]
    return "\n".join(out)
