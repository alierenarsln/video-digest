# video-digest'i yerel olarak (Docker'sız) başlatır.
#   .\run.ps1
# winget kurulumlarından sonra PATH'i tazeler — yoksa ffmpeg bulunamaz.

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")

foreach ($bin in @("ffmpeg", "ffprobe")) {
    if (-not (Get-Command $bin -ErrorAction SilentlyContinue)) {
        throw "$bin bulunamadı. Kurulum: winget install --id Gyan.FFmpeg -e"
    }
}
if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
    throw "Sanal ortam yok. Kurulum: py -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
}
if (-not (Test-Path ".\.env")) {
    throw ".env yok. Kopyalayın: Copy-Item .env.example .env  (sonra anahtarları doldurun)"
}

Write-Host ""
Write-Host "  Arayuz: http://127.0.0.1:8080" -ForegroundColor Cyan
Write-Host "  (durdurmak icin Ctrl+C)" -ForegroundColor DarkGray
Write-Host ""
Start-Process "http://127.0.0.1:8080"
& ".\.venv\Scripts\python.exe" -m uvicorn app.main:app --host 127.0.0.1 --port 8080
