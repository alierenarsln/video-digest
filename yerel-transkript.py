r"""Cascade transkript: Groq (hizli bulut) ONCE dener, dusen parcayi YEREL
faster-whisper ile transkript eder (yedek). Boylece Groq ayaktayken cok hizli
(2h ~5 dk), Groq 502 verse bile is HER ZAMAN biter (yerel ~41 dk).

Kullanim (agent bunu cagirir):
    yerel-transkript.py <dosya> --json3 <cikti.json3> [model]
Elle (duz .txt):
    yerel-transkript.py <dosya> [model]

Mimari: sesi ~10 dk'lik parcalara boler; her parca PARALEL islenir:
  1) Groq'a yollar (GROQ_API_KEY / _2 / ... arasinda donusumlu -> tek-anahtar
     kota darbogazi yok); 5xx/429'da birkac dener.
  2) Groq basaramazsa O PARCA yerel faster-whisper'a duser (batched, en hizli).
Parca zaman-damgalari mutlak zamana kaydirilir (ozet icinde tiklanabilir kalir).
Anahtarlar .env'den okunur (koda girmez).
"""
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import httpx
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent / ".env", override=True)

GROQ_KEYS = [k for k in (os.getenv(f"GROQ_API_KEY{s}") for s in ("", "_2", "_3", "_4")) if k]
GROQ_MODEL = os.getenv("GROQ_TRANSCRIBE_MODEL", "whisper-large-v3-turbo")
GROQ_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
PART_SEC = 600      # 10 dk parca: 16k mono wav ~19MB, Groq 25MB limitine sigar
GROQ_RETRY = 2      # parca basina Groq deneme; sonra yerele duser
POOL = 6            # ayni anda islenen parca (Groq I/O; anahtar sayisina gore yeter)
LANG = "tr"

# --- arg ayikla ---
args = sys.argv[1:]
if not args:
    sys.exit("Kullanim: yerel-transkript.py <dosya> [--json3 <cikti>] [model]")
json3_out = None
if "--json3" in args:
    i = args.index("--json3")
    if i + 1 >= len(args):
        sys.exit("--json3 sonrasi cikti yolu gerekli.")
    json3_out = Path(args[i + 1])
    del args[i:i + 2]
src = Path(args[0])
model_size = args[1] if len(args) > 1 else "small"
if not src.exists():
    sys.exit(f"Dosya yok: {src}")


def _dur(w):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(w)], capture_output=True, text=True)
    return float(r.stdout.strip())


# --- yerel faster-whisper: model BIR KEZ yuklenir, ayni anda TEK transkript
# (batched zaten tum cekirdekleri kullanir; es zamanli ikinci pass hem contention
# hem model paylasim riski). ---
_model = None
_model_lock = threading.Lock()
_local_sem = threading.Semaphore(1)


def _local(wav):
    global _model
    from faster_whisper import WhisperModel, BatchedInferencePipeline
    with _local_sem:
        with _model_lock:
            if _model is None:
                _model = WhisperModel(model_size, device="cpu", compute_type="int8")
        bm = BatchedInferencePipeline(model=_model)
        segs, _info = bm.transcribe(str(wav), batch_size=8, language=LANG)
        return [(s.start, s.end, s.text) for s in segs]


def _groq(wav, key):
    with open(wav, "rb") as f:
        r = httpx.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (Path(wav).name, f, "audio/wav")},
            data={"model": GROQ_MODEL, "response_format": "verbose_json",
                  "language": LANG, "temperature": "0"},
            timeout=180,
        )
    r.raise_for_status()
    return [(s["start"], s["end"], s["text"]) for s in r.json().get("segments", [])]


def process_part(idx, wav, t0):
    """Cascade: once Groq (donusumlu anahtar + retry), olmazsa yerel. Segmentleri
    mutlak zamana kaydirir."""
    if GROQ_KEYS:
        for attempt in range(GROQ_RETRY + 1):
            key = GROQ_KEYS[(idx + attempt) % len(GROQ_KEYS)]
            try:
                segs = _groq(wav, key)
                print(f"  parca {idx + 1}: GROQ ok ({len(segs)} seg)", flush=True)
                return [(s + t0, e + t0, t) for (s, e, t) in segs]
            except Exception as ex:
                print(f"  parca {idx + 1}: groq deneme {attempt + 1} hata: {str(ex)[:90]}", flush=True)
                time.sleep(3 * (attempt + 1))
    print(f"  parca {idx + 1}: YEREL yedege dusuyor...", flush=True)
    segs = _local(wav)
    print(f"  parca {idx + 1}: yerel ok ({len(segs)} seg)", flush=True)
    return [(s + t0, e + t0, t) for (s, e, t) in segs]


# --- ses cikar + parcala ---
wav = src.with_suffix(".16k.wav")
print("Ses cikariliyor (ffmpeg)...", flush=True)
subprocess.run(
    ["ffmpeg", "-nostdin", "-y", "-i", str(src), "-ac", "1", "-ar", "16000",
     "-c:a", "pcm_s16le", str(wav)],
    check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

dur = _dur(wav)
n = max(1, round(dur / PART_SEC))
print(f"Sure {dur / 60:.1f} dk -> {n} parca | Groq anahtari: {len(GROQ_KEYS)} | cascade basliyor...",
      flush=True)

parts = []
if n == 1:
    parts = [(0, wav, 0.0)]
else:
    pdir = wav.parent / (wav.stem + "_parts")
    pdir.mkdir(exist_ok=True)
    chunk = dur / n
    for i in range(n):
        o = pdir / f"p{i:03d}.wav"
        subprocess.run(
            ["ffmpeg", "-nostdin", "-y", "-ss", str(i * chunk), "-t", str(chunk),
             "-i", str(wav), "-c", "copy", str(o)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        parts.append((i, o, i * chunk))

# --- paralel cascade ---
t0 = time.time()
results = [None] * len(parts)
with ThreadPoolExecutor(max_workers=min(POOL, len(parts))) as ex:
    futs = {ex.submit(process_part, idx, w, off): idx for (idx, w, off) in parts}
    for fut, idx in futs.items():
        results[idx] = fut.result()

all_segs = sorted((seg for r in results for seg in r), key=lambda x: x[0])
print(f"Cascade bitti: {len(all_segs)} segment, {time.time() - t0:.0f} sn", flush=True)

# --- cikti ---
if json3_out is not None:
    events = [{"tStartMs": int(s * 1000), "dDurationMs": int(max(0.0, e - s) * 1000),
               "segs": [{"utf8": t.strip()}]}
              for (s, e, t) in all_segs if t.strip()]
    json3_out.write_text(json.dumps({"events": events}, ensure_ascii=False), encoding="utf-8")
    print(f"Bitti -> {json3_out} ({len(events)} olay)", flush=True)
else:
    out = src.with_suffix(".txt")
    with open(out, "w", encoding="utf-8") as f:
        f.write(f"# {src.stem}\n\n")
        for (s, e, t) in all_segs:
            if t.strip():
                f.write(t.strip() + "\n")
    print(f"Bitti -> {out}", flush=True)

# temizlik
wav.unlink(missing_ok=True)
