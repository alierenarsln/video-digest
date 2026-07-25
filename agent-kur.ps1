# Ev indiricisini (agent.ps1) OTOMATIK-BASLATMA olarak Windows'a kurar.
# BIR KEZ calistir; bundan sonra PC her acildiginda agent arka planda kosar,
# siteye yapistirdigin YouTube linklerini otomatik indirir. PowerShell'i bir
# daha acmana gerek kalmaz.
#
#   .\agent-kur.ps1 -Sunucu "https://tanik.automaterhub.com" -Sifre "SIFREN"
#
# Kaldirmak icin:
#   Unregister-ScheduledTask -TaskName "TanikEvAgent" -Confirm:$false

param(
    [Parameter(Mandatory = $true)][string]$Sunucu,
    [Parameter(Mandatory = $true)][string]$Sifre,
    [string]$GorevAdi = "TanikEvAgent"
)

$ErrorActionPreference = "Stop"
$agent = Join-Path $PSScriptRoot "agent.ps1"
if (-not (Test-Path $agent)) { throw "agent.ps1 bulunamadi: $agent (bu betigi video-digest klasorunde calistir)" }

# 1) Kimlik bilgilerini KULLANICI ortam degiskeni olarak kalici yaz. Zamanlanmis
#    gorev bunlari okur; sifre gorev tanimina (dolayisiyla plaintext'e) girmez.
[Environment]::SetEnvironmentVariable("VIDEO_DIGEST_URL", $Sunucu.TrimEnd("/"), "User")
[Environment]::SetEnvironmentVariable("VIDEO_DIGEST_PASSWORD", $Sifre, "User")
# Bu oturumda da tanimla ki hemen baslatinca agent bulabilsin.
$env:VIDEO_DIGEST_URL = $Sunucu.TrimEnd("/")
$env:VIDEO_DIGEST_PASSWORD = $Sifre

function Start-AgentSimdi {
    Start-Process powershell.exe -WindowStyle Hidden -ArgumentList `
        "-NoProfile", "-WindowStyle", "Hidden", "-ExecutionPolicy", "Bypass", "-File", "`"$agent`""
}

# 2) Once Zamanlanmis Gorev (olurse yeniden baslar). Yetki yoksa Baslangic
#    klasorune dus (YONETICI GEREKMEZ) - agent zaten kendi while dongusunde
#    hatalari yutup devam ettigi icin yeniden-baslatma kritik degil.
$kuruldu = $null
try {
    $action = New-ScheduledTaskAction -Execute "powershell.exe" `
        -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$agent`""
    $trigger = New-ScheduledTaskTrigger -AtLogOn
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 -ExecutionTimeLimit ([TimeSpan]::Zero)
    Register-ScheduledTask -TaskName $GorevAdi -Action $action -Trigger $trigger `
        -Settings $settings -Description "Tanik ev indiricisi (YouTube IP-engelini asar)" `
        -Force -ErrorAction Stop | Out-Null
    $kuruldu = "gorev"
    Write-Host "Kuruldu: Zamanlanmis Gorev '$GorevAdi' (oturum acilisinda basar, olurse yeniden)." -ForegroundColor Green
} catch {
    Write-Host "Zamanlanmis Gorev yetki istedi ($($_.Exception.Message))." -ForegroundColor Yellow
    Write-Host "Yonetici gerektirmeyen yola geciliyor: Baslangic klasoru..." -ForegroundColor Cyan
    $startup = [Environment]::GetFolderPath("Startup")
    $cmd = Join-Path $startup "TanikEvAgent.cmd"
    $icerik = "@echo off`r`nstart """" /min powershell.exe -NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$agent`""
    Set-Content -Path $cmd -Value $icerik -Encoding ASCII
    $kuruldu = "baslangic"
    Write-Host "Kuruldu: Baslangic klasoru -> $cmd (oturum acilisinda baslar, YONETICI GEREKMEZ)." -ForegroundColor Green
}

# 3) Simdi de baslat (yeniden baslatmadan).
if ($kuruldu -eq "gorev") {
    try { Start-ScheduledTask -TaskName $GorevAdi; Write-Host "Agent simdi baslatildi." -ForegroundColor Green }
    catch { Start-AgentSimdi; Write-Host "Agent simdi baslatildi (dogrudan)." -ForegroundColor Green }
} else {
    Start-AgentSimdi
    Write-Host "Agent simdi arka planda baslatildi." -ForegroundColor Green
}

Write-Host ""
Write-Host "Bitti. Artik siteye YouTube linki yapistir -> PC acikken otomatik iner." -ForegroundColor Cyan
Write-Host "Redeploy sonrasi sitenin ustunde 'ev bilgisayari: cevrimici' gorunur." -ForegroundColor DarkGray
if ($kuruldu -eq "gorev") {
    Write-Host "Kaldirmak: Unregister-ScheduledTask -TaskName '$GorevAdi' -Confirm:`$false" -ForegroundColor DarkGray
} else {
    Write-Host "Kaldirmak: Baslangic klasorunden TanikEvAgent.cmd'yi sil." -ForegroundColor DarkGray
}
