# Toplanti / sistem sesini KAYDEDER ve sunucuya is olarak yukler.
# Ozetleme tarafi sesli-kaynagi zaten isliyor (gorsel katman kendiliginden atlanir),
# eksik olan tek sey KAYIT'ti — bu betik onu kapatiyor (Faz 3'un ilk dilimi).
#
#   Cihazlari listele (once bunu calistir, loopback cihazini bul):
#     .\kaydet.ps1 -Liste
#
#   5 dakikalik toplanti kaydi + yukle:
#     .\kaydet.ps1 -Cihaz "Stereo Mix (Realtek Audio)" -Sure 300
#
#   Sinirsiz kayit (bitirmek icin ffmpeg penceresinde 'q'):
#     .\kaydet.ps1 -Cihaz "CABLE Output (VB-Audio Virtual Cable)"
#
#   Toplanti sesi + kendi mikrofonun birlikte (ikisi karistirilir):
#     .\kaydet.ps1 -Cihaz "Stereo Mix (...)" -Mikrofon "Microphone (...)" -Sure 600
#
# ONEMLI — toplantida KARSI TARAFI duymak icin LOOPBACK cihazi sart:
#   Windows sesi "duydugunu" varsayilan olarak kaydetmez. Iki secenek:
#   (a) Ses ayarlari > Kayit > "Stereo Mix"i etkinlestir (Realtek'te vardir), ya da
#   (b) VB-Audio Virtual Cable kur ve cikisi oraya yonlendir.
#   -Cihaz olarak bu loopback'i ver; sadece mikrofon verirsen yalnizca SENI kaydeder.

param(
    [string]$Cihaz,
    [string]$Mikrofon,
    [int]$Sure = 0,                              # 0 = 'q' ile durana kadar
    [switch]$Liste,                              # ses cihazlarini listele ve cik
    [string]$Sunucu = $env:VIDEO_DIGEST_URL,
    [string]$Kullanici = "admin",
    [string]$Sifre = $env:VIDEO_DIGEST_PASSWORD,
    [ValidateSet("groq", "openrouter", "anthropic", "gemini")]
    [string]$Model = "groq"
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

# winget kurulumlarindan sonra PATH tazelenmezse ffmpeg bulunamaz.
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")

$ffmpeg = (Get-Command ffmpeg -ErrorAction SilentlyContinue).Source
if (-not $ffmpeg) { throw "ffmpeg bulunamadi. winget install Gyan.FFmpeg ile kurun." }

# --- Cihazlari listele (loopback adini bulmak icin) ---
if ($Liste) {
    Write-Host "Ses giris cihazlari (dshow):" -ForegroundColor Cyan
    # -list_devices ciktiyi stderr'e yazar; PowerShell'de 2>&1 ile yakalanir.
    & $ffmpeg -hide_banner -list_devices true -f dshow -i dummy 2>&1 |
        Select-String -Pattern '\(audio\)|Alternative name' |
        ForEach-Object { $_.Line.Trim() }
    Write-Host ""
    Write-Host "Toplanti icin loopback (ornek 'Stereo Mix' / 'CABLE Output') secin." -ForegroundColor DarkGray
    return
}

if (-not $Cihaz) {
    throw "Cihaz yok. Once '.\kaydet.ps1 -Liste' ile cihazlari gorun, sonra -Cihaz verin."
}

# --- Kayit ---
$tmp = Join-Path $env:TEMP "video-digest-kayit"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null
$damga = Get-Date -Format "yyyyMMdd-HHmmss"
$wav = Join-Path $tmp "toplanti-$damga.wav"

# Pipeline zaten 16kHz mono istiyor; burada uretmek dosyayi kucultur, yuklemeyi hizlandirir.
$ffargs = @("-hide_banner", "-y")
if ($Mikrofon) {
    # Iki giris + amix: toplanti sesi ve mikrofon tek kanalda birlesir.
    $ffargs += @("-f", "dshow", "-i", "audio=$Cihaz",
               "-f", "dshow", "-i", "audio=$Mikrofon",
               "-filter_complex", "amix=inputs=2:duration=longest")
} else {
    $ffargs += @("-f", "dshow", "-i", "audio=$Cihaz")
}
if ($Sure -gt 0) { $ffargs += @("-t", "$Sure") }
$ffargs += @("-ac", "1", "-ar", "16000", $wav)

if ($Sure -gt 0) {
    Write-Host "Kaydediliyor ($Sure sn)... bitince otomatik yuklenir." -ForegroundColor Cyan
} else {
    Write-Host "Kaydediliyor... DURDURMAK icin ffmpeg penceresinde 'q' tusuna basin." -ForegroundColor Cyan
}
& $ffmpeg @args
# 'q' ya da -t ile normal bitiste ffmpeg 0 doner; Ctrl+C 255 verir ama dosya yine kullanilabilir.
if (-not (Test-Path $wav)) { throw "Kayit olusmadi. Cihaz adi dogru mu? (-Liste ile kontrol edin)" }
$mb = [math]::Round((Get-Item $wav).Length / 1MB, 1)
Write-Host "Kayit tamam: $([IO.Path]::GetFileName($wav)) ($mb MB)" -ForegroundColor Green

# --- Sunucuya yukle (indir-yukle.ps1 ile ayni desen) ---
if (-not $Sunucu) { throw "Sunucu adresi yok. `$env:VIDEO_DIGEST_URL ayarlayin ya da -Sunucu verin." }
if (-not $Sifre)  { throw "Sifre yok. `$env:VIDEO_DIGEST_PASSWORD ayarlayin ya da -Sifre verin." }
$Sunucu = $Sunucu.TrimEnd("/")

Write-Host "Yukleniyor -> $Sunucu ..." -ForegroundColor Cyan
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${Kullanici}:${Sifre}"))
$yanit = curl.exe -s -X POST "$Sunucu/jobs/upload" `
    -H "Authorization: Basic $b64" `
    -F "file=@$wav" `
    -F "provider=$Model"
if ($LASTEXITCODE -ne 0) { throw "Yukleme basarisiz." }
try { $is = $yanit | ConvertFrom-Json } catch { throw "Sunucu beklenmedik yanit verdi: $yanit" }
if (-not $is.job_id) { throw "Sunucu hata dondu: $yanit" }

Remove-Item $wav -Force -ErrorAction SilentlyContinue
Write-Host ""
Write-Host "Is olusturuldu: $($is.job_id)  (model: $($is.provider))" -ForegroundColor Green
Write-Host "Ozet hazir olunca: $Sunucu" -ForegroundColor Cyan
