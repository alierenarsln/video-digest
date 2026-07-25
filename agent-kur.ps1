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

# 2) Gorevi kur: oturum acilisinda basla, olurse 1 dk'da bir yeniden dene, sonsuz.
$action = New-ScheduledTaskAction -Execute "powershell.exe" `
    -Argument "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -File `"$agent`""
$trigger = New-ScheduledTaskTrigger -AtLogOn
$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -RestartInterval (New-TimeSpan -Minutes 1) -RestartCount 999 -ExecutionTimeLimit ([TimeSpan]::Zero)

Register-ScheduledTask -TaskName $GorevAdi -Action $action -Trigger $trigger `
    -Settings $settings -Description "Tanik ev indiricisi (YouTube IP-engelini asar)" -Force | Out-Null

Write-Host "Kuruldu: '$GorevAdi' gorevi oturum acilisinda agent.ps1'i baslatir." -ForegroundColor Green

# 3) Simdi de baslat (yeniden baslatmadan).
try {
    Start-ScheduledTask -TaskName $GorevAdi
    Start-Sleep -Seconds 2
    $durum = (Get-ScheduledTask -TaskName $GorevAdi).State
    Write-Host "Agent simdi baslatildi (durum: $durum)." -ForegroundColor Green
} catch {
    Write-Host "Simdi baslatilamadi ama sorun degil - bir sonraki oturum acilisinda calisir." -ForegroundColor Yellow
    Write-Host "Hemen istersen elle: .\agent.ps1" -ForegroundColor DarkGray
}

Write-Host ""
Write-Host "Bitti. Artik siteye YouTube linki yapistir -> PC acikken otomatik iner." -ForegroundColor Cyan
Write-Host "Sitenin ustunde 'ev bilgisayari: cevrimici' yazmasi gerekir (birkac sn icinde)." -ForegroundColor DarkGray
Write-Host "Kaldirmak: Unregister-ScheduledTask -TaskName '$GorevAdi' -Confirm:`$false" -ForegroundColor DarkGray
