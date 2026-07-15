"""YouTube altyazılarını transkript olarak kullanır — varsa bedava ve anlık.

İki tür altyazı var ve araları kalite olarak uçurum:

- `subtitles`          : ELLE yazılmış (kanal sahibi / topluluk). Noktalama, büyük
                         harf, özel isimler doğru; satırlar cümle sınırında biter.
                         Whisper'dan bile iyi.
- `automatic_captions` : YouTube'un ASR'ı VE onun makine çevirileri. Kalitesi dile
                         göre değişir: İngilizcede artık noktalama var (ölçüldü),
                         ama satırlar cümle ortasından bölünüyor ve az kaynaklı
                         dillerde (Türkçe dahil) doğruluk belirgin düşük. Bu yüzden
                         varsayılan kapalı — açmak isteyen ALLOW_AUTO_SUBTITLES.

Kritik incelik: otomatik listede bir dilin bulunması onun "orijinal" olduğunu
GÖSTERMEZ. İngilizce bir videonun otomatik listesinde 157 dil çıkıyor; oradaki `tr`
İngilizce ASR'dan makine çevirisi. Orijinal ASR yalnızca videonun kendi dilindeki
(`info["language"]`) girdidir. Bu yüzden makine çevirileri asla seçilmez.
"""

import asyncio
import json
from pathlib import Path

from ..config import ALLOW_AUTO_SUBTITLES, SUBTITLE_LANGS, USE_SUBTITLES
from .transcribe import Segment


def _match(available: dict, wanted: str) -> str | None:
    """Dil kodunu bölgesel varyantları da hesaba katarak eşler.

    Tam eşleşme aramak gerçek videolarda altyazıyı kaçırıyordu: canlı testte bir
    videonun dili 'en' ama elle yazılmış altyazısı yalnızca 'en-GB' olarak duruyordu
    → eşleşme bulunamayıp elle yazılmış altyazı varken Whisper'a düşülüyordu.

    Önce tam eşleşme, sonra taban dili aynı olan EN KISA anahtar. Kısa olanı
    seçmek önemli: YouTube 'tr-Kg4hpM-Q4eM' gibi uzun sonekli türetilmiş parçalar
    da üretiyor, gerçek 'tr' varken onlara düşmek istemiyoruz.
    """
    if wanted in available:
        return wanted
    base = wanted.split("-")[0]
    variants = [k for k in available if k.split("-")[0] == base]
    return min(variants, key=len) if variants else None


def pick(info: dict) -> tuple[str, bool] | None:
    """(dil, otomatik_mu) seçer. Uygun altyazı yoksa None → Whisper'a düşülür."""
    if not USE_SUBTITLES:
        return None

    manual = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    original = info.get("language")

    # Tercih sırası: kullanıcının listesi, yoksa videonun kendi dili.
    wanted = SUBTITLE_LANGS or ([original] if original else [])

    for lang in wanted:
        if not lang:
            continue
        hit = _match(manual, lang)
        if hit:
            return hit, False

    # Otomatik altyazı yalnızca ORİJİNAL dilde kabul edilir; diğerleri çeviridir.
    if ALLOW_AUTO_SUBTITLES and original:
        hit = _match(auto, original)
        if hit:
            return hit, True

    return None


async def download(url: str, lang: str, is_auto: bool, work: Path) -> list[Segment]:
    flag = "--write-auto-subs" if is_auto else "--write-subs"
    proc = await asyncio.create_subprocess_exec(
        "yt-dlp", "--no-playlist", "--no-warnings", "--skip-download",
        flag, "--sub-langs", lang, "--sub-format", "json3",
        "-o", str(work / "subs.%(ext)s"), url,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Altyazı indirilemedi:\n{stderr.decode('utf-8', 'replace')[-1000:]}"
        )

    files = list(work.glob("subs*.json3"))
    if not files:
        raise RuntimeError(f"Altyazı dosyası bulunamadı ({lang}).")
    return parse_json3(files[0])


def parse_json3(path: Path) -> list[Segment]:
    data = json.loads(path.read_text(encoding="utf-8"))
    segments: list[Segment] = []

    for event in data.get("events", []):
        segs = event.get("segs")
        if not segs:
            continue
        # Otomatik altyazılarda aAppend, önceki satırı tekrar eden yuvarlanan
        # kayıttır — sayarsak metin ikiye katlanır.
        if event.get("aAppend"):
            continue

        text = "".join(s.get("utf8", "") for s in segs)
        text = " ".join(text.split())
        if not text:
            continue

        start = event.get("tStartMs", 0) / 1000
        end = start + event.get("dDurationMs", 0) / 1000
        segments.append(Segment(start=start, end=end, text=text))

    if not segments:
        raise RuntimeError("Altyazı dosyası boş çözümlendi.")
    return segments
