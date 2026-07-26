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

# TEK-INSTANCE KILIDI: iki agent ayni anda calisirsa (iki logon / elle baslatma)
# ayni isi kapip faster-whisper'i birbirinin uzerine kosuyorlar; biri olunce
# transkript yarim kaliyor ve is sessizce takiliyor (canli olarak yasandi). Global
# mutex ikinci kopyayi aninda cikartir; kilit surec omru boyunca tutulur (degisken
# yasadikca), surec olunce OS otomatik birakir.
$script:_agentMutex = New-Object System.Threading.Mutex($false, "Global\TanikEvAgent")
try {
    $gotLock = $script:_agentMutex.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    # Onceki agent temiz kapanmadan olmus (kilidi birakmadan). Kilit yine de bize
    # gecti; guvenle devam ediyoruz (yoksa acilista cokerdi).
    $gotLock = $true
}
if (-not $gotLock) {
    Write-Host "Zaten calisan bir agent var - bu kopya cikiyor (cift-instance onlendi)." -ForegroundColor Yellow
    exit 0
}

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
        # "Yasiyorum" sinyali: site "ev bilgisayari cevrimici" gostersin. Hata
        # olursa yut - kalp atisi kritik degil, indirme akisini durdurmasin.
        try { Invoke-RestMethod "$Sunucu/api/agent/heartbeat" -Method POST -Headers $H -TimeoutSec 15 | Out-Null } catch {}

        $bekleyen = Invoke-RestMethod "$Sunucu/api/pending-downloads" -Headers $H -TimeoutSec 30

        foreach ($is in $bekleyen) {
            Write-Host "[$($is.id)] indiriliyor: $($is.source)" -ForegroundColor Cyan
            Get-ChildItem $tmp -File -ErrorAction SilentlyContinue | Remove-Item -Force -ErrorAction SilentlyContinue

            # Korumali m3u8/CDN icin referer (sunucudaki _direkt_medya deseniyle ayni).
            # Yoksa bos dizi -> yt-dlp'ye hicbir sey eklenmez.
            $refArgs = @()
            if ($is.referer) { $refArgs = @("--referer", $is.referer) }

            # 720p yeter: slayt OCR'i icin fazlasiyla, yukleme cok daha hizli.
            & $ytdlp @refArgs --no-playlist --no-warnings `
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

            # Altyazi kaynagi iki yol: (a) YEREL TRANSKRIPT istendiyse videoyu bu
            # PC'de faster-whisper ile yaz; (b) degilse YouTube'un elle yazilmis
            # altyazisini cek. Ikisi de json3 -> sunucu altyazi sayip Groq'u ATLAR.
            $altyazi = $null
            if ($is.transkript -eq "yerel") {
                # Groq 502 yok: transkript PC'de kosar (uzun videolar icin secildi).
                # yerel-transkript.py --json3: zaman damgali json3 (damgalar tiklanir).
                Write-Host "[$($is.id)] indi ($mb MB) -> yerel transkript basliyor (faster-whisper/CPU, uzun surebilir)..." -ForegroundColor Cyan
                $j3 = Join-Path $tmp "t.json3"
                # try/catch SART: python.exe yoksa & cagrisi ErrorActionPreference=Stop
                # altinda TERMINATING throw eder; sarmazsak dis catch'e sicrar, foreach
                # kirilir (diger bekleyen isler atlanir). Sararak: yerel transkript
                # basarisizsa altyazi=null kalir -> video altyazisiz yuklenir -> sunucu
                # Groq'a duser (zarif degradasyon, sonsuz retry yok).
                try {
                    & ".\.venv\Scripts\python.exe" "yerel-transkript.py" $dosya.FullName "--json3" $j3
                } catch {
                    Write-Host "[$($is.id)] yerel transkript calistirilamadi: $($_.Exception.Message)" -ForegroundColor Yellow
                }
                if (Test-Path $j3) {
                    $altyazi = Get-Item $j3
                    Write-Host "[$($is.id)] yerel transkript bitti, yukleniyor..." -ForegroundColor Green
                } else {
                    Write-Host "[$($is.id)] yerel transkript URETILEMEDI -> sunucuda Groq'a dusecek" -ForegroundColor Yellow
                }
            } else {
                # Sunucu linki HIC gormedigi icin altyaziyi kendisi bulamaz. Yalnizca
                # videonun KENDI dilindeki altyazi (sunucudaki politikayla ayni); dil
                # belirtmezsek makine cevirisi gelebilir - ceviri uzerine ceviri olur.
                $dil = (& $ytdlp @refArgs --no-playlist --no-warnings --skip-download `
                            --print "%(language)s" $is.source 2>$null | Select-Object -First 1)
                if ($dil -and $dil -ne "NA") {
                    & $ytdlp @refArgs --no-playlist --no-warnings --skip-download `
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
            }

            # curl.exe: PS 5.1 multipart'ta buyuk dosyayi bellege aliyor ve cokuyor.
            # Basligi da gonder: sunucu yalnizca dosyayi goruyor, dosya adi da
            # is numarasi -> baslik "c23e86cac7c3" gibi anlamsiz cikiyordu.
            $baslik = (& $ytdlp @refArgs --no-playlist --no-warnings --skip-download `
                           --print "%(title)s" $is.source 2>$null | Select-Object -First 1)

            $curlArgs = @("-s", "-X", "POST", "$Sunucu/jobs/$($is.id)/attach",
                          "-H", "Authorization: Basic $b64",
                          "-F", "file=@$($dosya.FullName)")
            if ($altyazi) { $curlArgs += @("-F", "subtitles=@$($altyazi.FullName)") }
            if ($baslik)  { $curlArgs += @("-F", "title=$baslik") }
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
