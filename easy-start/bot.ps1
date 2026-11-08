# Trading bot usor - one script for everything (Windows PowerShell).
param([string]$Comanda = "")
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$Url = "http://127.0.0.1:8080"
$Settings = "user_data/bot_settings.json"

function Need-Docker {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
        Write-Host "Docker nu este instalat. Instaleaza Docker Desktop: https://docs.docker.com/get-docker/"
        exit 1
    }
    docker info *> $null
    if ($LASTEXITCODE -ne 0) {
        Write-Host "Docker nu ruleaza. Porneste Docker Desktop si incearca din nou."
        exit 1
    }
}

function Run-Settings([string]$What) {
    New-Item -ItemType Directory -Force -Path user_data/logs, user_data/data, user_data/backtest_results | Out-Null
    if ($What -in "keys", "live", "demo") {
        docker compose run --rm -it --entrypoint python bot /easy-tools/settings.py $What
    } else {
        docker compose run --rm --entrypoint python bot /easy-tools/settings.py $What
    }
}

function Ensure-Settings {
    if (-not (Test-Path $Settings)) { Run-Settings "init" }
}

function Show-Usage {
    Write-Host @"
Utilizare: .\bot.ps1 <comanda>

  start      Porneste botul si platforma web ($Url)
  stop       Opreste botul
  restart    Reporneste botul (dupa ce schimbi setarile)
  status     Arata modul (DEMO/REAL), exchange-ul si perechile
  loguri     Arata ce face botul, in timp real (Ctrl+C pentru iesire)
  parola     Arata utilizatorul si parola pentru platforma web
  backtest   Testeaza strategia pe ultimele 90 de zile (fara bani)
  chei       Adauga cheile API de la exchange
  real       Trece pe bani REALI (cere confirmare)
  demo       Revine la bani virtuali
  update     Descarca ultima versiune a botului
"@
}

switch ($Comanda.ToLower()) {
    "start" {
        Need-Docker; Ensure-Settings
        docker compose up -d
        Write-Host "`nBotul a pornit. Deschide platforma: $Url"
        Run-Settings "login"
        Start-Process $Url
    }
    "stop"    { Need-Docker; docker compose down }
    "restart" { Need-Docker; docker compose restart }
    "status"  { Need-Docker; Ensure-Settings; Run-Settings "status"; docker compose ps }
    { $_ -in "loguri", "logs" } { Need-Docker; docker compose logs -f --tail 100 }
    { $_ -in "parola", "login" } { Need-Docker; Ensure-Settings; Run-Settings "login" }
    "backtest" {
        Need-Docker; Ensure-Settings
        Write-Host "Descarc preturile istorice..."
        docker compose run --rm bot download-data --config /freqtrade/user_data/bot_settings.json --timeframes 15m --days 120
        $from = (Get-Date).ToUniversalTime().AddDays(-90).ToString("yyyyMMdd")
        docker compose run --rm bot backtesting --config /freqtrade/user_data/bot_settings.json --strategy StrategieUsoara --timerange "$from-"
        Write-Host "Rezultatele apar si in platforma web, la sectiunea Backtest."
    }
    { $_ -in "chei", "keys" } {
        Need-Docker; Ensure-Settings; Run-Settings "keys"
        Write-Host "Ruleaza '.\bot.ps1 restart' ca botul sa foloseasca noile chei."
    }
    { $_ -in "real", "live" } { Need-Docker; Ensure-Settings; Run-Settings "live"; docker compose restart }
    "demo"   { Need-Docker; Ensure-Settings; Run-Settings "demo"; docker compose restart }
    "update" { Need-Docker; docker compose pull; docker compose up -d }
    default  { Show-Usage }
}
