# Start / stop the AI trading system on Windows (paper trading only).
#
#   powershell -ExecutionPolicy Bypass -File ai_team\system.ps1 start|stop|status|telefon|local
#
# Same behaviour as ai_team/system.sh: risk gateway (127.0.0.1:8790), runner, dashboard (:8800).
# "telefon" opens only the read-only dashboard to the local Wi-Fi; the gateway stays on 127.0.0.1.
# Written for Windows PowerShell 5.1 (no PowerShell 7-only syntax).
param([string]$Command = "")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$State = Join-Path $Root "ai_team\state"
$Pids = Join-Path $State "pids"
$HostFile = Join-Path $State "dashboard.host"
$Venv = Join-Path $Root ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$GatewayPort = 8790
$DashboardPort = 8800
New-Item -ItemType Directory -Force -Path $Pids | Out-Null

function Get-BasePython {
    # Prefer the "py" launcher; "python" may be the Microsoft Store placeholder.
    foreach ($candidate in @("py", "python")) {
        if (-not (Get-Command $candidate -ErrorAction SilentlyContinue)) { continue }
        try {
            if ($candidate -eq "py") { $v = & py -3 --version 2>&1 } else { $v = & python --version 2>&1 }
        } catch { continue }
        if ("$v" -match "Python 3\.(1[1-9]|[2-9][0-9])") { return $candidate }
    }
    return $null
}

function Initialize-Venv {
    if (-not (Test-Path $Py)) {
        $base = Get-BasePython
        if (-not $base) {
            Write-Host ""
            Write-Host "Python 3.11 sau mai nou nu este instalat." -ForegroundColor Red
            Write-Host "1. Deschide https://www.python.org/downloads/ si instaleaza Python."
            Write-Host "2. IMPORTANT: bifeaza 'Add python.exe to PATH' la instalare."
            Write-Host "3. Porneste din nou acest fisier."
            exit 1
        }
        Write-Host "Creez mediul Python (.venv) ..."
        if ($base -eq "py") { & py -3 -m venv $Venv } else { & python -m venv $Venv }
        if ($LASTEXITCODE -ne 0) { throw "Nu am putut crea .venv" }
    }
    Write-Host "Verific/instalez pachetele (prima data dureaza cateva minute) ..."
    & $Py -m pip install -q --disable-pip-version-check -r (Join-Path $Root "ai_team\requirements.txt")
    if ($LASTEXITCODE -ne 0) { throw "Instalarea pachetelor a esuat" }
}

function Test-Running([string]$Name) {
    $pidFile = Join-Path $Pids "$Name.pid"
    if (-not (Test-Path $pidFile)) { return $false }
    $procId = [int](Get-Content $pidFile -Raw)
    return [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue)
}

function Start-One([string]$Name, [string[]]$Arguments) {
    if (Test-Running $Name) {
        Write-Host "  $Name ruleaza deja"
        return
    }
    $proc = Start-Process -FilePath $Py -ArgumentList $Arguments -WorkingDirectory $Root `
        -RedirectStandardOutput (Join-Path $State "$Name.out") `
        -RedirectStandardError (Join-Path $State "$Name.err") `
        -WindowStyle Hidden -PassThru
    Set-Content -Path (Join-Path $Pids "$Name.pid") -Value $proc.Id
    Write-Host "  $Name pornit (pid $($proc.Id))"
}

function Stop-One([string]$Name) {
    $pidFile = Join-Path $Pids "$Name.pid"
    if (Test-Running $Name) {
        Stop-Process -Id ([int](Get-Content $pidFile -Raw)) -Force
        Write-Host "  $Name oprit"
    } else {
        Write-Host "  $Name nu rula"
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $pidFile
}

function Wait-Http([string]$Url) {
    for ($i = 0; $i -lt 45; $i++) {
        try {
            Invoke-WebRequest -Uri $Url -UseBasicParsing -TimeoutSec 3 | Out-Null
            return $true
        } catch {
            Start-Sleep -Seconds 1
        }
    }
    return $false
}

function Get-LanIp {
    try {
        $cfg = Get-NetIPConfiguration | Where-Object {
            $_.IPv4DefaultGateway -ne $null -and $_.NetAdapter.Status -eq "Up"
        } | Select-Object -First 1
        if ($cfg) { return $cfg.IPv4Address[0].IPAddress }
    } catch { }
    return "IP-ul-calculatorului"
}

function Get-DashboardHost {
    if (Test-Path $HostFile) { return (Get-Content $HostFile -Raw).Trim() }
    return "127.0.0.1"
}

function Start-System {
    Initialize-Venv
    Write-Host ""
    Write-Host "Pornesc sistemul AI (DOAR PAPER TRADING, date Kraken) ..." -ForegroundColor Green
    Start-One "gateway" @("-m", "risk_gateway.server", "--port", "$GatewayPort", "--data", "kraken")
    if (-not (Wait-Http "http://127.0.0.1:$GatewayPort/health")) {
        throw "Risk gateway nu a pornit. Vezi ai_team\state\gateway.err"
    }
    Start-One "runner" @("-m", "ai_team.runner", "--data", "kraken", "--gateway", "http://127.0.0.1:$GatewayPort")
    $dashHost = Get-DashboardHost
    Start-One "dashboard" @("-m", "ai_team.dashboard.server", "--host", $dashHost, "--port", "$DashboardPort",
        "--gateway", "http://127.0.0.1:$GatewayPort")
    if (-not (Wait-Http "http://127.0.0.1:$DashboardPort/api/state")) {
        throw "Dashboard-ul nu a pornit. Vezi ai_team\state\dashboard.err"
    }
    Write-Host ""
    Write-Host "Dashboard pe acest calculator: http://127.0.0.1:$DashboardPort"
    if ($dashHost -eq "0.0.0.0") { Show-PhoneLink }
}

function Show-PhoneLink {
    $ip = Get-LanIp
    Write-Host ""
    Write-Host "==============================================================" -ForegroundColor Yellow
    Write-Host "  PE IPHONE (conectat la ACELASI Wi-Fi) deschide in Safari:" -ForegroundColor Yellow
    Write-Host ""
    Write-Host "      http://$($ip):$DashboardPort" -ForegroundColor Cyan
    Write-Host ""
    Write-Host "==============================================================" -ForegroundColor Yellow
    Write-Host "Daca Windows intreaba de Firewall pentru python: bifeaza 'Private networks' si apasa 'Allow access'."
    Write-Host "Fara parola: cine e pe Wi-Fi-ul tau poate doar VEDEA dashboard-ul (nu poate da ordine)."
}

function Stop-System {
    foreach ($n in @("dashboard", "runner", "gateway")) { Stop-One $n }
}

function Restart-Dashboard {
    Stop-One "dashboard"
    Start-Sleep -Seconds 1
    Start-System
}

function Show-Status {
    foreach ($n in @("gateway", "runner", "dashboard")) {
        if (Test-Running $n) { Write-Host "  ${n}: ruleaza" } else { Write-Host "  ${n}: oprit" }
    }
    if (Test-Running "dashboard") {
        & $Py -c "import json,urllib.request as u;o=u.build_opener(u.ProxyHandler({}));s=json.load(o.open('http://127.0.0.1:$DashboardPort/api/state',timeout=10));g=s['gateway']['health'];m=s['market'] or {};print('  mod:', 'PAPER' if g.get('mode')=='paper' and g.get('live_trading') is False else 'NECUNOSCUT');print('  agenti online:', sum(1 for a in s['agents'] if a['online'] and a['status']!='error'), '/ 50');print('  Kraken:', 'ok' if m.get('ok') else 'eroare/fara date');print('  alerte:', len(s['alerts']))"
    }
    if ((Get-DashboardHost) -eq "0.0.0.0") { Show-PhoneLink }
}

switch ($Command.ToLower()) {
    "start"   { Start-System }
    "stop"    { Stop-System }
    "status"  { Show-Status }
    "telefon" { Set-Content -Path $HostFile -Value "0.0.0.0"; Restart-Dashboard }
    "phone"   { Set-Content -Path $HostFile -Value "0.0.0.0"; Restart-Dashboard }
    "local"   { Remove-Item -Force -ErrorAction SilentlyContinue $HostFile; Restart-Dashboard }
    default   { Write-Host "Utilizare: system.ps1 start|stop|status|telefon|local"; exit 2 }
}
