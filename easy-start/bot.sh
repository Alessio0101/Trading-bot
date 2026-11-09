#!/usr/bin/env bash
# Trading bot usor - one script for everything (Linux / macOS).
set -euo pipefail
cd "$(dirname "$0")"

URL="http://127.0.0.1:8080"
SETTINGS="user_data/bot_settings.json"

need_docker() {
    if ! command -v docker >/dev/null 2>&1; then
        echo "Docker nu este instalat. Instaleaza Docker Desktop: https://docs.docker.com/get-docker/"
        exit 1
    fi
    if ! docker info >/dev/null 2>&1; then
        echo "Docker nu ruleaza. Porneste Docker Desktop si incearca din nou."
        exit 1
    fi
}

prepare_dirs() {
    mkdir -p user_data/logs user_data/data user_data/backtest_results
    # The container runs as uid 1000; on Linux hosts with another uid the
    # folders must be writable for it. Only directories are touched, so the
    # settings file with passwords keeps its private permissions.
    if [ "$(uname -s)" = "Linux" ] && [ "$(id -u)" != "1000" ]; then
        find user_data -type d -exec chmod a+rwx {} +
    fi
}

settings() {
    prepare_dirs
    local tty=()
    [ -t 0 ] && tty=(-it)
    docker compose run --rm "${tty[@]}" --entrypoint python bot /easy-tools/settings.py "$1"
}

ensure_settings() {
    [ -f "$SETTINGS" ] || settings init
}

ensure_data() {
    local days="${1:-90}"
    echo "Descarc preturile istorice pe ultimele ${days} zile..."
    docker compose run --rm bot download-data \
        --config /freqtrade/user_data/bot_settings.json \
        --timeframes 15m --days "$days"
}

usage() {
    cat <<USAGE
Utilizare: ./bot.sh <comanda>

  start      Porneste botul si platforma web ($URL)
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
  telefon    Deschide platforma pe telefon (aceeasi retea Wi-Fi)
  local      Platforma doar pe acest calculator (implicit)
  telegram   Controleaza botul din Telegram, de oriunde
USAGE
}

cmd="${1:-}"
case "$cmd" in
    start)
        need_docker
        ensure_settings
        prepare_dirs
        docker compose up -d
        echo
        echo "Botul a pornit. Deschide platforma: $URL"
        settings login
        ;;
    stop)
        need_docker
        docker compose down
        ;;
    restart)
        need_docker
        docker compose restart
        ;;
    status)
        need_docker
        ensure_settings
        settings status
        docker compose ps
        ;;
    loguri|logs)
        need_docker
        docker compose logs -f --tail 100
        ;;
    parola|login)
        need_docker
        ensure_settings
        settings login
        ;;
    backtest)
        need_docker
        ensure_settings
        ensure_data 120
        docker compose run --rm bot backtesting \
            --config /freqtrade/user_data/bot_settings.json \
            --strategy StrategieUsoara --timerange "$(date -u -d '90 days ago' +%Y%m%d 2>/dev/null || date -u -v-90d +%Y%m%d)-"
        echo "Rezultatele apar si in platforma web, la sectiunea Backtest."
        ;;
    chei|keys)
        need_docker
        ensure_settings
        settings keys
        echo "Ruleaza './bot.sh restart' ca botul sa foloseasca noile chei."
        ;;
    real|live)
        need_docker
        ensure_settings
        settings live
        docker compose restart 2>/dev/null || true
        ;;
    demo)
        need_docker
        ensure_settings
        settings demo
        docker compose restart 2>/dev/null || true
        ;;
    telefon|phone)
        need_docker
        ensure_settings
        echo "BIND_IP=0.0.0.0" > .env
        prepare_dirs
        docker compose up -d
        ip="$(ipconfig getifaddr en0 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}')"
        echo
        echo "Pe telefon (conectat la ACELASI Wi-Fi) deschide: http://${ip:-IP-ul-calculatorului}:8080"
        echo "Nu deschide portul 8080 in router - platforma trebuie sa ramana doar in reteaua ta."
        settings login
        ;;
    local)
        need_docker
        rm -f .env
        docker compose up -d
        echo "Platforma este din nou doar pe acest calculator: $URL"
        ;;
    telegram)
        need_docker
        ensure_settings
        settings telegram
        docker compose restart 2>/dev/null || true
        ;;
    update)
        need_docker
        docker compose pull
        docker compose up -d
        ;;
    *)
        usage
        ;;
esac
