# Start / stop the AI trading system on Windows (paper trading only).
#
#   powershell -ExecutionPolicy Bypass -File ai_team\system.ps1 start|stop|status|telefon|local|autostart|fara-autostart|telegram|raport|chei|actualizeaza
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

function Write-AutostartScript {
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
}

function Install-Autostart {
    Initialize-Venv
    Write-AutostartScript
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

# ---------------- one-click update (ACTUALIZEAZA.bat) ----------------
$UpdateRepo = "Alessio0101/Trading-bot"
$UpdateBranch = "claude/trading-bot-platform-w9ip9u"
$VersionFile = Join-Path $Root "VERSIUNE.txt"

function Get-CurrentVersion {
    if (Test-Path $VersionFile) { return (Get-Content $VersionFile -Raw).Trim() }
    if ((Split-Path -Leaf $Root) -match "^Trading-bot-([0-9a-f]{40})$") { return $Matches[1] }
    return ""
}

function Update-System {
    # Downloads the newest version next to this folder, moves the Python environment and the
    # paper account (state) into it, and starts it there. Keys and the Telegram token live in
    # %USERPROFILE%\.ai-trading-desk and are kept. This folder stays as a backup.
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    $ProgressPreference = "SilentlyContinue"
    $parent = Split-Path -Parent $Root
    Write-Host "Caut versiunea noua ..."
    $branch = [uri]::EscapeDataString($UpdateBranch)
    $info = Invoke-RestMethod -Uri "https://api.github.com/repos/$UpdateRepo/commits/$branch" -UseBasicParsing -TimeoutSec 60
    $sha = "$($info.sha)"
    if ($sha -notmatch "^[0-9a-f]{40}$") { throw "Nu am putut afla versiunea noua de pe GitHub." }
    if ($sha -eq (Get-CurrentVersion)) {
        Write-Host "Ai deja ultima versiune ($($sha.Substring(0, 7)))." -ForegroundColor Green
        return
    }
    $newRoot = Join-Path $parent "Trading-bot-$sha"

    # Older backups (not this folder, not the new one) only take space.
    $old = @(Get-ChildItem -Path $parent -Directory -Filter "Trading-bot-*" | Where-Object { $_.FullName -ne $Root -and $_.FullName -ne $newRoot })
    if ($old.Count -gt 0) {
        Write-Host "Versiuni vechi gasite (copii de rezerva): $($old.Name -join ', ')"
        $answer = Read-Host "Le sterg ca sa fac loc pe disc? (D/N)"
        if ($answer -match "^[DdYy]") { $old | ForEach-Object { Remove-Item -Recurse -Force $_.FullName -ErrorAction SilentlyContinue } }
    }
    $drive = Get-PSDrive -Name $Root.Substring(0, 1)
    if ($drive.Free -lt 500MB) { throw "Prea putin spatiu pe disc (sub 0.5 GB liber). Elibereaza spatiu si incearca din nou." }

    if (-not (Test-Path (Join-Path $newRoot "ai_team\system.ps1"))) {
        $zip = Join-Path $env:TEMP "trading-bot-update.zip"
        Write-Host "Descarc versiunea $($sha.Substring(0, 7)) (circa 20 MB) ..."
        Invoke-WebRequest -Uri "https://github.com/$UpdateRepo/archive/$sha.zip" -OutFile $zip -UseBasicParsing -TimeoutSec 600
        Write-Host "Dezarhivez ..."
        if (Get-Command tar.exe -ErrorAction SilentlyContinue) {
            # tar reports one harmless error for a symbolic link in the archive; the check below
            # decides. It runs through cmd so its error text cannot stop this script (PS 5.1).
            & cmd.exe /c "tar -xf ""$zip"" -C ""$parent"" 2>nul"
        } else {
            Expand-Archive -Path $zip -DestinationPath $parent -Force
        }
        Remove-Item -Force -ErrorAction SilentlyContinue $zip
        if (-not (Test-Path (Join-Path $newRoot "ai_team\system.ps1"))) { throw "Dezarhivarea a esuat: $newRoot nu este complet." }
    }

    Write-Host "Opresc versiunea curenta ..."
    Stop-System
    $moved = $false
    Start-Sleep -Seconds 2  # let Windows release the stopped processes' files
    try {
        if ((Test-Path $Venv) -and -not (Test-Path (Join-Path $newRoot ".venv"))) {
            Move-Item -Path $Venv -Destination (Join-Path $newRoot ".venv")
            $moved = $true
        }
        Write-Host "Mut contul de test si istoricul ..."
        foreach ($dir in @("ai_team\state", "risk_gateway\state")) {
            $src = Join-Path $Root $dir
            if (Test-Path $src) {
                & robocopy.exe $src (Join-Path $newRoot $dir) /E /XD pids /XF supervisor.lock supervisor.stop /NFL /NDL /NJH /NJS /NP | Out-Null
                if ($LASTEXITCODE -ge 8) { throw "Copierea din $dir a esuat (robocopy $LASTEXITCODE)." }
            }
        }
        Set-Content -Path (Join-Path $newRoot "VERSIUNE.txt") -Value $sha -Encoding ASCII
        Write-Host "Pornesc versiunea noua ..."
        & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $newRoot "ai_team\system.ps1") dupa-actualizare
        if ($LASTEXITCODE -ne 0) { throw "Versiunea noua nu a pornit." }
    } catch {
        Write-Host ""
        Write-Host "Actualizarea a esuat: $($_.Exception.Message)" -ForegroundColor Red
        Write-Host "Revin la versiunea veche ..."
        if ($moved -and -not (Test-Path $Venv)) { Move-Item -Path (Join-Path $newRoot ".venv") -Destination $Venv }
        Start-System
        throw
    }
    Write-Host ""
    Write-Host "Actualizare terminata: versiunea $($sha.Substring(0, 7)) ruleaza din $newRoot" -ForegroundColor Green
    Write-Host "Folderul vechi a ramas ca rezerva: $Root"
    Write-Host "(La urmatoarea actualizare te intreb daca il sterg.)"
}

function Complete-Update {
    # Run inside the new folder by Update-System: install new packages, point the Windows
    # start-up script here, and start. No questions are asked.
    Initialize-Venv
    if (Test-Path $StartupLink) { Write-AutostartScript }  # start at logon from the new folder
    Start-System
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
    "telegram"       { Initialize-Venv; & $Py -m ai_team.notifier setup; if ($LASTEXITCODE -ne 0) { exit 1 } }
    "raport"         { Initialize-Venv; & $Py -m ai_team.notifier report; if ($LASTEXITCODE -ne 0) { exit 1 } }
    "chei"           { Initialize-Venv; & $Py -m ai_team.keys setup; if ($LASTEXITCODE -ne 0) { exit 1 } }
    "actualizeaza"   { Update-System }
    "dupa-actualizare" { Complete-Update }
    default          { Write-Host "Utilizare: system.ps1 start|stop|status|telefon|local|autostart|fara-autostart|telegram|raport|chei|actualizeaza"; exit 2 }
}
