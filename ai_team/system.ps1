# Start / stop the AI trading system on Windows (paper trading only).
#
#   powershell -ExecutionPolicy Bypass -File ai_team\system.ps1 start|stop|status|telefon|local|autostart|fara-autostart
#
# One supervisor process (ai_team.supervisor) keeps the risk gateway (127.0.0.1:8790), the runner
# and the dashboard (:8800) running and restarts any of them that stops. "telefon" opens only the
# read-only dashboard to the local Wi-Fi; the gateway always stays on 127.0.0.1.
# Written for Windows PowerShell 5.1 (no PowerShell 7-only syntax).
param([string]$Command = "")

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root
$State = Join-Path $Root "ai_team\state"
$Pids = Join-Path $State "pids"
$HostFile = Join-Path $State "dashboard.host"
$StopFile = Join-Path $State "supervisor.stop"
$SupPid = Join-Path $Pids "supervisor.pid"
$Venv = Join-Path $Root ".venv"
$Py = Join-Path $Venv "Scripts\python.exe"
$Pyw = Join-Path $Venv "Scripts\pythonw.exe"
$DashboardPort = 8800
$StartupLink = Join-Path ([Environment]::GetFolderPath("Startup")) "AI-Trading-Desk.vbs"
New-Item -ItemType Directory -Force -Path $Pids | Out-Null

function Get-BasePython {
    # Returns the arguments that run a suitable Python. Versions with ready-made packages for
    # pandas, numpy and scikit-learn (3.13, 3.12) are preferred over a newer default, which may
    # still lack them. "python" may be the Microsoft Store placeholder, so it is checked last.
    $candidates = @(@("py", "-3.13"), @("py", "-3.12"), @("py", "-3"), @("python"))
    foreach ($c in $candidates) {
        if (-not (Get-Command $c[0] -ErrorAction SilentlyContinue)) { continue }
        try {
            if ($c.Count -eq 2) { $v = & $c[0] $c[1] --version 2>&1 } else { $v = & $c[0] --version 2>&1 }
        } catch { continue }
        if ("$v" -match "Python 3\.(1[1-9]|[2-9][0-9])") { return ,$c }
    }
    return $null
}

function Get-VenvVersion {
    if (-not (Test-Path $Py)) { return $null }
    try { return ("" + (& $Py -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>&1)).Trim() } catch { return $null }
}

function Get-FreeGb {
    try {
        $drive = Get-PSDrive -Name $Root.Substring(0, 1) -ErrorAction Stop
        return [math]::Round($drive.Free / 1GB, 1)
    } catch { return $null }
}

function Initialize-Venv {
    # A .venv made with a Python that pandas/numpy/scikit-learn ship no packages for yet
    # (e.g. 3.15) cannot install them on Windows without a compiler: rebuild it when a
    # supported Python (3.12 or 3.13) is available.
    $venvVersion = Get-VenvVersion
    if ($venvVersion -and $venvVersion -notin @("3.12", "3.13")) {
        $base = Get-BasePython
        if ($base -and ($base -join " ") -match "-3\.1[23]$") {
            Write-Host "Mediul .venv foloseste Python $venvVersion (prea nou pentru unele pachete). Il refac cu $($base -join ' ') ..."
            Remove-Item -Recurse -Force $Venv
        }
    }
    if (-not (Test-Path $Py)) {
        $base = Get-BasePython
        if (-not $base) {
            Write-Host ""
            Write-Host "Python 3.11 sau mai nou nu este instalat." -ForegroundColor Red
            Write-Host "1. Deschide https://www.python.org/downloads/ si instaleaza Python."
            Write-Host "2. Apoi, intr-o fereastra cmd, scrie: py install 3.13"
            Write-Host "3. Porneste din nou acest fisier."
            exit 1
        }
        Write-Host "Creez mediul Python (.venv) cu: $($base -join ' ') ..."
        if ($base.Count -eq 2) { & $base[0] $base[1] -m venv $Venv } else { & $base[0] -m venv $Venv }
        if ($LASTEXITCODE -ne 0) { throw "Nu am putut crea .venv" }
    }
    $freeGb = Get-FreeGb
    if ($null -ne $freeGb -and $freeGb -lt 2) {
        Write-Host ""
        Write-Host "Nu este destul spatiu pe disc: liber $freeGb GB, trebuie cel putin 2 GB." -ForegroundColor Red
        Write-Host "Elibereaza spatiu (Cos de gunoi, Descarcari, foldere Trading-bot vechi) si porneste din nou fisierul."
        throw "Spatiu insuficient pe disc"
    }
    Write-Host "Verific/instalez pachetele (prima data dureaza cateva minute) ..."
    # --no-cache-dir: do not keep a second copy of every package on a disk that may be nearly full.
    & $Py -m pip install -q --disable-pip-version-check --no-cache-dir -r (Join-Path $Root "ai_team\requirements.txt")
    if ($LASTEXITCODE -ne 0) {
        $ver = Get-VenvVersion
        Write-Host ""
        Write-Host "Instalarea pachetelor a esuat (Python $ver, liber pe disc: $(Get-FreeGb) GB)." -ForegroundColor Red
        if ($ver -in @("3.12", "3.13")) {
            Write-Host "Python este bun. Cauza este de obicei spatiul pe disc (No space left on device) sau internetul."
            Write-Host "Elibereaza cel putin 2 GB, sterge folderul .venv din acest folder si porneste din nou fisierul."
        } else {
            Write-Host "Instaleaza Python 3.13: deschide cmd si scrie  py install 3.13"
            Write-Host "Apoi sterge folderul .venv din acest folder si porneste din nou fisierul."
        }
        throw "Instalarea pachetelor a esuat"
    }
}

function Test-Running {
    if (-not (Test-Path $SupPid)) { return $false }
    $procId = [int](Get-Content $SupPid -Raw)
    return [bool](Get-Process -Id $procId -ErrorAction SilentlyContinue)
}

function Wait-Http([string]$Url) {
    for ($i = 0; $i -lt 90; $i++) {
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

function Start-System {
    Initialize-Venv
    $dashHost = Get-DashboardHost
    if (Test-Running) {
        Write-Host "  Sistemul ruleaza deja."
    } else {
        Remove-Item -Force -ErrorAction SilentlyContinue $StopFile
        Write-Host ""
        Write-Host "Pornesc sistemul AI (DOAR PAPER TRADING, date Kraken) ..." -ForegroundColor Green
        $exe = $Pyw
        if (-not (Test-Path $exe)) { $exe = $Py }
        $proc = Start-Process -FilePath $exe -WorkingDirectory $Root -WindowStyle Hidden -PassThru `
            -ArgumentList @("-m", "ai_team.supervisor", "--data", "kraken", "--dashboard-host", $dashHost,
                "--dashboard-port", "$DashboardPort")
        Set-Content -Path $SupPid -Value $proc.Id
        Write-Host "  supraveghetor pornit (pid $($proc.Id)) - reporneste automat orice componenta care se opreste"
    }
    if (-not (Wait-Http "http://127.0.0.1:$DashboardPort/api/state")) {
        throw "Dashboard-ul nu a pornit. Vezi ai_team\state\supervisor.log si ai_team\state\dashboard.out"
    }
    Write-Host ""
    Write-Host "Dashboard pe acest calculator: http://127.0.0.1:$DashboardPort"
    if ($dashHost -eq "0.0.0.0") { Show-PhoneLink }
}

function Stop-System {
    if (-not (Test-Running)) {
        Write-Host "  Sistemul nu rula."
        Remove-Item -Force -ErrorAction SilentlyContinue $SupPid
        return
    }
    Set-Content -Path $StopFile -Value "stop"
    for ($i = 0; $i -lt 30; $i++) {
        if (-not (Test-Running)) { break }
        Start-Sleep -Seconds 1
    }
    if (Test-Running) {
        # Fallback: stop the supervisor and any child it reported.
        $status = Join-Path $State "supervisor.json"
        if (Test-Path $status) {
            $children = (Get-Content $status -Raw | ConvertFrom-Json).children
            foreach ($c in $children.PSObject.Properties) {
                if ($c.Value.pid) { Stop-Process -Id $c.Value.pid -Force -ErrorAction SilentlyContinue }
            }
        }
        Stop-Process -Id ([int](Get-Content $SupPid -Raw)) -Force -ErrorAction SilentlyContinue
    }
    Remove-Item -Force -ErrorAction SilentlyContinue $SupPid
    Write-Host "  Sistemul a fost oprit."
}

function Show-Status {
    if (Test-Running) { Write-Host "  supraveghetor: ruleaza" } else { Write-Host "  supraveghetor: oprit"; return }
    & $Py -c "import json,urllib.request as u;sup=json.load(open(r'$State\supervisor.json'));[print('  '+n+':', 'ruleaza' if c['running'] else 'SE REPORNESTE', '(reporniri', c['restarts'], ')') for n,c in sup['children'].items()];o=u.build_opener(u.ProxyHandler({}));s=json.load(o.open('http://127.0.0.1:$DashboardPort/api/state',timeout=10));g=s['gateway']['health'];m=s['market'] or {};print('  mod:', 'PAPER' if g.get('mode')=='paper' and g.get('live_trading') is False else 'NECUNOSCUT');print('  agenti online:', sum(1 for a in s['agents'] if a['online'] and a['status']!='error'), '/ 50');print('  Kraken:', 'ok' if m.get('ok') else 'eroare/fara date');print('  alerte:', len(s['alerts']))"
    if ((Get-DashboardHost) -eq "0.0.0.0") { Show-PhoneLink }
}

function Install-Autostart {
    Initialize-Venv
    $script = Join-Path $Root "ai_team\system.ps1"
    # VBScript runs PowerShell with no window at all; quotes are doubled inside the VBS string.
    $cmd = "powershell -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File """"$script"""" start"
    $vbs = @(
        'Set sh = CreateObject("WScript.Shell")',
        "sh.CurrentDirectory = ""$Root""",
        "sh.Run ""$cmd"", 0, False"
    )
    Set-Content -Path $StartupLink -Value $vbs -Encoding ASCII
    Write-Host "Pornire automata activata: botul porneste singur cand intri in Windows." -ForegroundColor Green
    Write-Host "  ($StartupLink)"
    Write-Host ""
    Write-Host "Ca sa mearga non-stop, calculatorul nu trebuie sa intre in sleep."
    $answer = Read-Host "Opresc sleep-ul cat timp calculatorul e la priza? (D/N)"
    if ($answer -match "^[DdYy]") {
        & powercfg /change standby-timeout-ac 0
        & powercfg /change hibernate-timeout-ac 0
        Write-Host "Sleep dezactivat cand e la priza. (Il poti reactiva din Setari > Sistem > Alimentare.)"
    } else {
        Write-Host "Am lasat setarile de sleep neschimbate."
    }
    Start-System
}

function Remove-Autostart {
    Remove-Item -Force -ErrorAction SilentlyContinue $StartupLink
    Write-Host "Pornirea automata a fost dezactivata. (Setarile de sleep nu au fost schimbate.)"
}

switch ($Command.ToLower()) {
    "start"          { Start-System }
    "stop"           { Stop-System }
    "status"         { Show-Status }
    "telefon"        { Set-Content -Path $HostFile -Value "0.0.0.0"; Stop-System; Start-System }
    "phone"          { Set-Content -Path $HostFile -Value "0.0.0.0"; Stop-System; Start-System }
    "local"          { Remove-Item -Force -ErrorAction SilentlyContinue $HostFile; Stop-System; Start-System }
    "autostart"      { Set-Content -Path $HostFile -Value "0.0.0.0"; Install-Autostart }
    "fara-autostart" { Remove-Autostart }
    default          { Write-Host "Utilizare: system.ps1 start|stop|status|telefon|local|autostart|fara-autostart"; exit 2 }
}
