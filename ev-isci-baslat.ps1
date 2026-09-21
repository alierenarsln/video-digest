# EV ISCISI BASLATICI - Vercel'deki tanik'in islerini bu PC'de isler (ev-isci.py).
#
#   powershell -NoProfile -ExecutionPolicy Bypass -File ev-isci-baslat.ps1
#
# - Guncel koda gecer (git fetch + origin/main). -GuncellemeYok ile atlanir.
# - API anahtarlari yerel .env'den: varsayilan $HOME\video-digest\.env
#   (sirlar tek yerde kalir; EV_YEREL_ENV ile degistirilebilir).
# - Neon + Blob erisimi .env.vercel'den (vercel env pull ile gelir).
# - Iki kopya ayni anda calismasin: global mutex (agent.ps1 ile ayni desen).
# - Cikti: data-ev\ev-isci.log
# - Surec kapanirsa 30 sn sonra kendiliginden yeniden baslar (bekci dongusu).
#
# NOT: Bu dosya bilerek ASCII. BOM'suz .ps1 PowerShell 5.1'de ANSI okunur,
# Turkce karakterler bozulur.

param([switch]$GuncellemeYok)

$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$script:kilit = New-Object System.Threading.Mutex($false, "Global\TanikEvIsci")
try {
    $aldi = $script:kilit.WaitOne(0)
} catch [System.Threading.AbandonedMutexException] {
    $aldi = $true   # onceki kopya temiz kapanmadan olmus; kilit bize gecti
}
if (-not $aldi) {
    Write-Host "Ev iscisi zaten calisiyor - bu kopya cikiyor."
    exit 0
}

if (-not $GuncellemeYok) {
    git fetch -q origin
    git checkout -q --detach origin/main
}

if (-not $env:EV_YEREL_ENV) {
    $env:EV_YEREL_ENV = Join-Path $HOME "video-digest\.env"
}
$py = Join-Path $HOME "video-digest\.venv\Scripts\python.exe"
if (-not (Test-Path $py)) { throw "Python bulunamadi: $py" }

New-Item -ItemType Directory -Force "data-ev" | Out-Null
$env:PYTHONIOENCODING = "utf-8"
$log = Join-Path $PSScriptRoot "data-ev\ev-isci.log"
# Yonlendirme cmd'de: PowerShell 5.1'in *>> yonlendirmesi dosyayi UTF-16 yazar
# (Python'un UTF-8 ciktisi harf aralikli, okunmaz hale gelir). cmd baytlari aynen yazar.
# Bekci: surec herhangi bir sebeple kapanirsa (ag kopmasi, beklenmedik hata)
# 30 sn sonra yeniden baslat. Eskiden tek bir DNS hatasi isciyi kalici olarak
# durduruyordu ve arayuz gunlerce "ev PC kapali" diyordu.
while ($true) {
    cmd /c "echo ===== %DATE% %TIME% basladi =====>> `"$log`" & `"$py`" -u ev-isci.py >> `"$log`" 2>&1"
    $kod = $LASTEXITCODE
    cmd /c "echo ===== %DATE% %TIME% kapandi (kod $kod), 30 sn sonra yeniden =====>> `"$log`""
    Start-Sleep -Seconds 30
}
