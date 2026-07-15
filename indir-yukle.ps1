# YouTube videosunu EVDE indirir, uzaktaki sunucuya YUKLER.
#
#   .\indir-yukle.ps1 "https://www.youtube.com/watch?v=..."
#
# Neden: YouTube veri merkezi IP'lerini engelliyor ("Sign in to confirm you're
# not a bot"), yani sunucu linki kendi indiremiyor. Ev IP'niz geciyor. Bu betik
# isi bolusturuyor: indirme evde, agir is (transkript/OCR/ozet) sunucuda.
# Yukleme bitince makineyi kapatabilirsiniz.

param(
    [Parameter(Mandatory = $true, Position = 0)]
    [string]$Link,

    [string]$Sunucu = $env:VIDEO_DIGEST_URL,
    [string]$Kullanici = "admin",
    [string]$Sifre = $env:VIDEO_DIGEST_PASSWORD,
    [ValidateSet("groq", "openrouter", "anthropic")]
    [string]$Model = "groq"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")

if (-not $Sunucu) {
    throw "Sunucu adresi yok. Ya -Sunucu verin ya da: `$env:VIDEO_DIGEST_URL = 'https://...'"
}
if (-not $Sifre) {
    throw "Sifre yok. Ya -Sifre verin ya da: `$env:VIDEO_DIGEST_PASSWORD = '...'"
}
$Sunucu = $Sunucu.TrimEnd("/")

$ytdlp = ".\.venv\Scripts\yt-dlp.exe"
if (-not (Test-Path $ytdlp)) { throw "yt-dlp yok. Once: .\.venv\Scripts\python.exe -m pip install -r requirements.txt" }

# --- 1. Evde indir (ev IP'si YouTube'u geciyor) ---
$tmp = Join-Path $env:TEMP "video-digest-indir"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
Get-ChildItem $tmp -File | Remove-Item -Force -ErrorAction SilentlyContinue

Write-Host "Indiriliyor (ev baglantisi)..." -ForegroundColor Cyan
# 720p yeter: slayt OCR'i icin fazlasiyla, dosya cok daha kucuk = hizli yukleme.
& $ytdlp --no-playlist --no-warnings `
    -f "bestvideo[height<=720]+bestaudio/best[height<=720]/best" `
    --merge-output-format mp4 `
    -o "$tmp\video.%(ext)s" $Link
if ($LASTEXITCODE -ne 0) { throw "yt-dlp indiremedi." }

$dosya = Get-ChildItem $tmp -File | Sort-Object Length -Descending | Select-Object -First 1
$mb = [math]::Round($dosya.Length / 1MB, 1)
Write-Host "Indi: $($dosya.Name) ($mb MB)" -ForegroundColor Green

# --- 2. Sunucuya yukle ---
Write-Host "Yukleniyor -> $Sunucu ..." -ForegroundColor Cyan
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${Kullanici}:${Sifre}"))

# curl.exe: PowerShell 5.1'in Invoke-RestMethod'u multipart'ta buyuk dosyayi
# bellege aliyor ve 500 MB'ta cokuyor. curl akitarak gonderiyor.
$yanit = curl.exe -s -X POST "$Sunucu/jobs/upload" `
    -H "Authorization: Basic $b64" `
    -F "file=@$($dosya.FullName)" `
    -F "provider=$Model"
if ($LASTEXITCODE -ne 0) { throw "Yukleme basarisiz." }

try { $is = $yanit | ConvertFrom-Json } catch { throw "Sunucu beklenmedik yanit verdi: $yanit" }
if (-not $is.job_id) { throw "Sunucu hata dondu: $yanit" }

Remove-Item $dosya.FullName -Force -ErrorAction SilentlyContinue

Write-Host ""
Write-Host "Is olusturuldu: $($is.job_id)  (model: $($is.provider))" -ForegroundColor Green
Write-Host "Bu makineyi artik kapatabilirsiniz - is sunucuda devam ediyor." -ForegroundColor DarkGray
Write-Host "Takip: $Sunucu" -ForegroundColor Cyan
