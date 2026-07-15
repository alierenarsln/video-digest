# Tek komutla video ozeti.
#
#   .\ozet.ps1 "https://www.youtube.com/watch?v=..."
#   .\ozet.ps1 "C:\yol\video.mp4"
#
# Sunucu kapaliysa kendisi baslatir, isi atar, bitmesini bekler ve ozeti acar.

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Kaynak
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

# Konsol varsayilan cp1254; API UTF-8 dondugu icin Turkce harfler bozuk cikiyordu.
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")

$api = "http://127.0.0.1:8080"

function Test-Sunucu {
    try { Invoke-RestMethod "$api/health" -TimeoutSec 2 -ErrorAction Stop | Out-Null; return $true }
    catch { return $false }
}

# --- Sunucu ayakta mi? Degilse baslat ---
if (-not (Test-Sunucu)) {
    Write-Host "Sunucu kapali, baslatiliyor..." -ForegroundColor Yellow
    if (-not (Test-Path ".\.venv\Scripts\python.exe")) {
        throw "Sanal ortam yok. Once kurulum: py -m venv .venv; .\.venv\Scripts\python.exe -m pip install -r requirements.txt"
    }
    if (-not (Test-Path ".\.env")) { throw ".env yok. Copy-Item .env.example .env yapip anahtarlari doldurun." }

    Start-Process -FilePath ".\.venv\Scripts\python.exe" `
        -ArgumentList "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "8080" `
        -WindowStyle Hidden

    $bekle = 0
    while (-not (Test-Sunucu)) {
        Start-Sleep -Milliseconds 700
        $bekle++
        if ($bekle -gt 40) { throw "Sunucu acilmadi. Elle deneyin: .\run.ps1" }
    }
}

$saglik = Invoke-RestMethod "$api/health"
if (-not $saglik.groq_key) { throw "GROQ_API_KEY bos. .env dosyasini doldurun." }
if (-not $saglik.ocr_ok) { Write-Host "UYARI (OCR): $($saglik.ocr)" -ForegroundColor Yellow }

Write-Host "Saglayici: $($saglik.llm_provider) | Ozet dili: $($saglik.output_language)" -ForegroundColor DarkGray
Write-Host ""

# --- Isi at ---
$govde = @{ source = $Kaynak } | ConvertTo-Json
$is = Invoke-RestMethod "$api/jobs" -Method Post -Body $govde -ContentType "application/json"
Write-Host "Is olusturuldu: $($is.job_id)" -ForegroundColor Cyan
Write-Host "(1 saatlik video ~10-15 dk surer - Groq ucretsiz katman kotasi yuzunden)" -ForegroundColor DarkGray
Write-Host ""

# --- Bekle ---
$asamalar = @{
    "queued"     = "kuyrukta"
    "fetch"      = "video indiriliyor"
    "subtitles"  = "hazir altyazi kullaniliyor (Whisper atlandi)"
    "transcribe" = "transkript cikariliyor (Whisper)"
    "frames"     = "slaytlar okunuyor (OCR)"
    "repair"     = "transkript onariliyor"
    "segment"    = "konulara bolunuyor"
    "summarize"  = "ozetleniyor + elestirmen"
    "render"     = "markdown yaziliyor"
}
$sonAsama = ""
while ($true) {
    Start-Sleep -Seconds 3
    $durum = Invoke-RestMethod "$api/jobs/$($is.job_id)"
    if ($durum.stage -ne $sonAsama) {
        $sonAsama = $durum.stage
        $etiket = $asamalar[$sonAsama]
        if (-not $etiket) { $etiket = $sonAsama }
        Write-Host ("  -> " + $etiket)
    }
    if ($durum.status -eq "done" -or $durum.status -eq "error") { break }
}

Write-Host ""
if ($durum.status -eq "error") {
    Write-Host "HATA: $($durum.error)" -ForegroundColor Red
    exit 1
}

# --- Sonuc ---
Write-Host "BITTI: $($durum.title)" -ForegroundColor Green
$m = $durum.meta
Write-Host ("  {0} bolum | {1} slayt OCR | elestirmen {2} eksik madde ekledi" -f `
    $m.sections, $m.frames_used, $m.critic_added) -ForegroundColor DarkGray
if ($m.transcript_repaired) { Write-Host "  transkript onarildi (ham hali .transcript.raw.txt icinde)" -ForegroundColor DarkGray }
Write-Host ""
Write-Host "Ozet: $($durum.result_path)" -ForegroundColor Cyan

Invoke-Item $durum.result_path
