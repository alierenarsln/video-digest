"""Özeti tek bir markdown dosyasına dök. YouTube kaynaklıysa zaman damgaları
tıklanabilir olur."""

from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .summarize import Digest
from .transcribe import fmt_ts


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
                out.append(f"<sub>{_link(url, frame.ts)} ekran görüntüsü</sub>")
                out.append("")

    if digest.glossary:
        out += ["## Terimler", ""]
        out += [f"- **{term}** — {definition}" for term, definition in digest.glossary]
        out.append("")

    footer = (
        f"Otomatik üretildi · {len(digest.sections)} bölüm · "
        f"eleştirmen geçişi {digest.added_by_critic} eksik madde ekledi"
    )
    if digest.frames_used:
        footer += f" · {digest.frames_used} slayt OCR ile okundu"
    out += ["---", "", f"<sub>{footer}</sub>", ""]
    return "\n".join(out)
