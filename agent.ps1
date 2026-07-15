# EV INDIRICISI - IP engelini asan parca.
#
#   $env:VIDEO_DIGEST_URL = "https://...automaterhub.com"
#   $env:VIDEO_DIGEST_PASSWORD = "..."
#   .\agent.ps1
#
# Neden: YouTube veri merkezi IP'lerini engelliyor ("Sign in to confirm you're
# not a bot") - canli sunucuda gercek videoyla dogrulandi. Ev IP'si geciyor.
# Bu betik sunucudaki BEKLEYEN linkleri gorur, EVDE indirir, sunucuya yukler.
# Agir is (transkript/OCR/ozet) sunucuda kalir.
#
# Avantaji: telefondan oglen link atarsiniz, is sunucuda bekler; PC'niz akşam
# acilinca bu betik alir ve isler. Tunel yontemi PC'nin O AN acik olmasini ister.

param(
    [string]$Sunucu = $env:VIDEO_DIGEST_URL,
    [string]$Kullanici = "admin",
    [string]$Sifre = $env:VIDEO_DIGEST_PASSWORD,
    [int]$AralikSaniye = 30
)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
            [Environment]::GetEnvironmentVariable("Path", "User")

if (-not $Sunucu) { throw "Sunucu adresi yok: `$env:VIDEO_DIGEST_URL ayarlayin." }
if (-not $Sifre)  { throw "Sifre yok: `$env:VIDEO_DIGEST_PASSWORD ayarlayin." }
$Sunucu = $Sunucu.TrimEnd("/")

$ytdlp = ".\.venv\Scripts\yt-dlp.exe"
if (-not (Test-Path $ytdlp)) { throw "yt-dlp yok: .\.venv\Scripts\python.exe -m pip install -r requirements.txt" }

$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes("${Kullanici}:${Sifre}"))
$H = @{ Authorization = "Basic $b64" }
$tmp = Join-Path $env:TEMP "video-digest-agent"
New-Item -ItemType Directory -Path $tmp -Force | Out-Null

Write-Host "Ev indiricisi calisiyor -> $Sunucu" -ForegroundColor Cyan
Write-Host "Bekleyen linkleri $AralikSaniye sn'de bir kontrol ediyor. Durdurmak: Ctrl+C" -ForegroundColor DarkGray
Write-Host ""

while ($true) {
    try {
        $bekleyen = Invoke-RestMethod "$Sunucu/api/pending-downloads" -Headers $H -TimeoutSec 30

        foreach ($is in $bekleyen) {
            Write-Host "[$($is.id)] indiriliyor: $($is.source)" -ForegroundColor Cyan
            Get-ChildItem $tmp -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

            # 720p yeter: slayt OCR'i icin fazlasiyla, yukleme cok daha hizli.
            & $ytdlp --no-playlist --no-warnings `
                -f "bestvideo[height<=720]+bestaudio/best[height<=720]/best" `
                --merge-output-format mp4 `
                -o "$tmp\v.%(ext)s" $is.source 2>&1 | Out-Null

            $dosya = Get-ChildItem $tmp -File -ErrorAction SilentlyContinue |
                     Sort-Object Length -Descending | Select-Object -First 1
            if (-not $dosya) {
                Write-Host "[$($is.id)] INDIRILEMEDI - atlaniyor (sonraki turda yeniden denenir)" -ForegroundColor Yellow
                continue
            }

            $mb = [math]::Round($dosya.Length / 1MB, 1)

            # Altyaziyi da cek: sunucu linki HIC gormedigi icin kendisi bulamaz.
            # Elle yazilmis altyazi Whisper'dan daha iyi ve bedava - gondermezsek
            # o avantaj kaybolur ve her sey Whisper'a duser.
            #
            # Yalnizca videonun KENDI dilindeki altyaziyi aliyoruz (sunucudaki
            # politikayla ayni). Dil belirtmezsek YouTube'un makine cevirisi
            # altyazilarindan biri gelebilir - ceviri uzerine ceviri olur.
            $dil = (& $ytdlp --no-playlist --no-warnings --skip-download `
                        --print "%(language)s" $is.source 2>$null | Select-Object -First 1)
            $altyazi = $null
            if ($dil -and $dil -ne "NA") {
                & $ytdlp --no-playlist --no-warnings --skip-download `
                    --write-subs --sub-langs $dil --sub-format json3 `
                    -o "$tmp\s.%(ext)s" $is.source 2>&1 | Out-Null
                $altyazi = Get-ChildItem "$tmp\s*.json3" -ErrorAction SilentlyContinue |
                           Select-Object -First 1
            }

            if ($altyazi) {
                Write-Host "[$($is.id)] indi ($mb MB) + elle yazilmis altyazi, yukleniyor..." -ForegroundColor DarkGray
            } else {
                Write-Host "[$($is.id)] indi ($mb MB), altyazi yok -> Whisper, yukleniyor..." -ForegroundColor DarkGray
            }

            # curl.exe: PS 5.1 multipart'ta buyuk dosyayi bellege aliyor ve cokuyor.
            $curlArgs = @("-s", "-X", "POST", "$Sunucu/jobs/$($is.id)/attach",
                          "-H", "Authorization: Basic $b64",
                          "-F", "file=@$($dosya.FullName)")
            if ($altyazi) { $curlArgs += @("-F", "subtitles=@$($altyazi.FullName)") }
            $yanit = curl.exe @curlArgs

            if ($LASTEXITCODE -eq 0 -and $yanit -match '"job_id"') {
                Write-Host "[$($is.id)] YUKLENDI - sunucu isliyor" -ForegroundColor Green
                Remove-Item $dosya.FullName -Force -ErrorAction SilentlyContinue
            } else {
                Write-Host "[$($is.id)] yukleme basarisiz: $yanit" -ForegroundColor Red
            }
        }
    } catch {
        Write-Host "baglanti hatasi: $($_.Exception.Message)" -ForegroundColor DarkYellow
    }

    Start-Sleep -Seconds $AralikSaniye
}
