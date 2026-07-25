r"""Videoyu/sesi YERELDE transkripte çevirir (Groq'a gitmeden).

Groq Whisper 502 verip duruyorsa: bununla sesi kendi makinende metne çevir,
çıkan .txt'yi Tanık'a yükle → markdown yolu özetler (transkripsiyon adımı yok).

Kullanım:
    .\.venv\Scripts\python.exe yerel-transkript.py ders.mp4
    .\.venv\Scripts\python.exe yerel-transkript.py ders.mp4 base   # daha hızlı, biraz düşük kalite

Model: small (denge) varsayılan. İlk çalıştırmada model iner (~460MB, tek sefer).
CPU'da koşar (GPU MX110 uygun değil) — 40 dk video ~10-30 dk sürebilir.
"""

import subprocess
import sys
from pathlib import Path

from faster_whisper import WhisperModel

src = Path(sys.argv[1])
model_size = sys.argv[2] if len(sys.argv) > 2 else "small"
if not src.exists():
    sys.exit(f"Dosya yok: {src}")

out = src.with_suffix(".txt")
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
segments, info = model.transcribe(str(wav), language="tr", vad_filter=True)

satir_sayisi = 0
with open(out, "w", encoding="utf-8") as f:
    f.write(f"# {src.stem}\n\n")  # markdown başlığı: Tanık bunu bölüm sınırı sayar
    for seg in segments:
        metin = seg.text.strip()
        if not metin:
            continue
        f.write(metin + "\n")
        satir_sayisi += 1
        dk, sn = int(seg.start // 60), int(seg.start % 60)
        print(f"  [{dk:02d}:{sn:02d}] {metin[:70]}", flush=True)

wav.unlink(missing_ok=True)
print(f"\nBitti -> {out}  ({satir_sayisi} satır)", flush=True)
print("Şimdi bu .txt'yi Tanık'a yükle (Dosya sekmesi ya da curl ile).", flush=True)
