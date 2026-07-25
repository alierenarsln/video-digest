r"""Videoyu/sesi YERELDE transkripte çevirir (Groq'a gitmeden).

Groq Whisper 502 verip duruyorsa: bununla sesi kendi makinende metne çevir.

İki çıktı biçimi:
    .\.venv\Scripts\python.exe yerel-transkript.py ders.mp4
        → ders.txt (markdown; elle Tanık'a yüklersin, markdown yolu özetler)
    .\.venv\Scripts\python.exe yerel-transkript.py ders.mp4 --json3 ders.json3
        → json3 (zaman damgalı; agent bunu altyazı olarak yükler → sunucu Groq'u
          atlar, damgalar özet içinde tıklanabilir kalır)

Model (opsiyonel son arg): small (varsayılan, denge) | base (hızlı) | medium (kaliteli).
İlk çalıştırmada model iner (~460MB, tek sefer). CPU/int8'de koşar — 40 dk video
~10-30 dk sürebilir.
"""

import json
import subprocess
import sys
from pathlib import Path

from faster_whisper import WhisperModel

args = sys.argv[1:]
if not args:
    sys.exit("Kullanım: yerel-transkript.py <dosya> [--json3 <çıktı>] [model]")

# --json3 <yol>: zaman damgalı json3 üret (agent bunu kullanır). Yoksa .txt.
json3_out = None
if "--json3" in args:
    i = args.index("--json3")
    if i + 1 >= len(args):
        sys.exit("--json3 sonrası çıktı yolu gerekli.")
    json3_out = Path(args[i + 1])
    del args[i:i + 2]

src = Path(args[0])
model_size = args[1] if len(args) > 1 else "small"
if not src.exists():
    sys.exit(f"Dosya yok: {src}")

wav = src.with_suffix(".16k.wav")

# 1) ffmpeg ile 16kHz mono ses çıkar (Whisper'ın istediği format).
print("Ses çıkarılıyor (ffmpeg)...", flush=True)
subprocess.run(
    ["ffmpeg", "-y", "-i", str(src), "-ac", "1", "-ar", "16000", str(wav)],
    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
)

# 2) faster-whisper (CPU, int8) — Türkçe.
print(f"Model yükleniyor ({model_size}, CPU/int8)... ilk seferde iner.", flush=True)
model = WhisperModel(model_size, device="cpu", compute_type="int8")

print("Transkript başladı (uzun sürebilir, satır satır akar)...", flush=True)
# segments bir ÜRETEÇ — yalnızca bir kez gezilebilir; iki mod ayrık, sorun yok.
segments, info = model.transcribe(str(wav), language="tr", vad_filter=True)

if json3_out is not None:
    # json3: Tanık'ın altyazı çözümleyicisiyle (subtitles.parse_json3) birebir uyumlu.
    # Her segment bir "event": tStartMs + dDurationMs + segs[].utf8. Sunucu bunu
    # altyazı sayar → transkript adımını (Groq) atlar, zaman damgaları tıklanır.
    events = []
    n = 0
    for seg in segments:
        metin = seg.text.strip()
        if not metin:
            continue
        events.append({
            "tStartMs": int(seg.start * 1000),
            "dDurationMs": int(max(0.0, seg.end - seg.start) * 1000),
            "segs": [{"utf8": metin}],
        })
        n += 1
        dk, sn = int(seg.start // 60), int(seg.start % 60)
        print(f"  [{dk:02d}:{sn:02d}] {metin[:70]}", flush=True)
    json3_out.write_text(
        json.dumps({"events": events}, ensure_ascii=False), encoding="utf-8"
    )
    wav.unlink(missing_ok=True)
    print(f"\nBitti -> {json3_out}  ({n} olay)", flush=True)
else:
    out = src.with_suffix(".txt")
    n = 0
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# {src.stem}\n\n")  # markdown başlığı: Tanık bunu bölüm sınırı sayar
        for seg in segments:
            metin = seg.text.strip()
            if not metin:
                continue
            f.write(metin + "\n")
            n += 1
            dk, sn = int(seg.start // 60), int(seg.start % 60)
            print(f"  [{dk:02d}:{sn:02d}] {metin[:70]}", flush=True)
    wav.unlink(missing_ok=True)
    print(f"\nBitti -> {out}  ({n} satır)", flush=True)
    print("Şimdi bu .txt'yi Tanık'a yükle (Dosya sekmesi ya da curl ile).", flush=True)
