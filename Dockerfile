FROM python:3.11-slim

# ffmpeg   : ses/video dönüştürme + kare örnekleme
# tesseract: slayt OCR. Dil paketleri BURADA kuruluyor — yerel kurulumdaki
#            tessdata/ klasörü ve TESSDATA_PREFIX/TESSERACT_CMD ayarları
#            konteynerde KULLANILMAMALI (Coolify'da boş bırakın).
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg \
        tesseract-ocr \
        tesseract-ocr-tur \
        tesseract-ocr-eng \
        ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

# IN_DOCKER: konteynerde APP_PASSWORD zorunlu kılınır (bkz. main.py). Şifresiz
# açılış, internete açık bir serviste API kotasını herkese açar.
ENV PYTHONUNBUFFERED=1 DATA_DIR=/data IN_DOCKER=true

# İş kayıtları (SQLite), özetler ve slayt görselleri burada. Coolify'da KALICI
# VOLUME bağlanmalı, yoksa her deploy'da tüm geçmiş silinir.
VOLUME ["/data"]
EXPOSE 8080

# /health kimlik doğrulamadan muaf (ayrıntı sızdırmıyor, bkz. main.py).
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/health', timeout=4)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
