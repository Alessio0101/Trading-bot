#!/usr/bin/env bash
# Start / stop the whole AI trading system (paper trading only) on Linux / macOS.
#
#   ai_team/system.sh start | stop | restart | status | logs | verify | telefon | local
#
# One supervisor process keeps three components alive and restarts them if they stop:
#   risk gateway  127.0.0.1:8790  independent, paper-only, own Kraken prices, stop/target exits
#   runner                        market feed every 10 s + coordinator cycle (all 50 agents) every 5 min
#   dashboard     :8800           live web UI (read-only); "telefon" opens it to the local Wi-Fi
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
STATE="$ROOT/ai_team/state"
PIDS="$STATE/pids"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
DATA="${AI_TEAM_DATA:-kraken}"
INTERVAL="${AI_TEAM_INTERVAL:-300}"
DASHBOARD_PORT="${AI_TEAM_DASHBOARD_PORT:-8800}"
HOST_FILE="$STATE/dashboard.host"
mkdir -p "$PIDS"

ensure_venv() {
    if [ ! -x "$PY" ]; then
        echo "Creating virtualenv in .venv ..."
        python3 -m venv "$VENV"
    fi
    "$PY" -m pip install -q --disable-pip-version-check --no-cache-dir -r ai_team/requirements.txt
}

running() { [ -f "$PIDS/supervisor.pid" ] && kill -0 "$(cat "$PIDS/supervisor.pid")" 2>/dev/null; }

lan_ip() {
    local ip=""
    ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
    [ -z "$ip" ] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "${ip:-IP-ul-calculatorului}"
}

wait_http() {
    local url="$1" i
    for i in $(seq 1 60); do
        "$PY" -c "import urllib.request; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('$url', timeout=2)" 2>/dev/null && return 0
        sleep 1
    done
    return 1
}

start() {
    ensure_venv
    local host="127.0.0.1"
    [ -f "$HOST_FILE" ] && host="$(cat "$HOST_FILE")"
    if running; then
        echo "  already running (supervisor pid $(cat "$PIDS/supervisor.pid"))"
    else
        echo "Starting AI trading system (PAPER TRADING ONLY, data=$DATA)"
        nohup "$PY" -m ai_team.supervisor --data "$DATA" --interval "$INTERVAL" \
            --dashboard-host "$host" --dashboard-port "$DASHBOARD_PORT" >> "$STATE/supervisor.out" 2>&1 &
        echo $! > "$PIDS/supervisor.pid"
        echo "  supervisor started (pid $!)"
    fi
    wait_http "http://127.0.0.1:$DASHBOARD_PORT/api/state" || { echo "dashboard did not start; see $STATE/dashboard.out"; exit 1; }
    echo
    echo "Dashboard: http://127.0.0.1:$DASHBOARD_PORT"
    if [ "$host" = "0.0.0.0" ]; then
        echo "Pe telefon (acelasi Wi-Fi): http://$(lan_ip):$DASHBOARD_PORT"
    fi
}

stop() {
    if ! running; then echo "  not running"; rm -f "$PIDS/supervisor.pid"; return; fi
    touch "$STATE/supervisor.stop"
    local i
    for i in $(seq 1 30); do running || break; sleep 1; done
    if running; then kill "$(cat "$PIDS/supervisor.pid")" 2>/dev/null || true; fi
    rm -f "$PIDS/supervisor.pid"
    echo "  stopped"
}

status() {
    if running; then echo "  supervisor: running (pid $(cat "$PIDS/supervisor.pid"))"; else echo "  supervisor: stopped"; return; fi
    "$PY" - "$DASHBOARD_PORT" "$STATE" <<'PY'
import json, sys, urllib.request
sup = json.load(open(f"{sys.argv[2]}/supervisor.json"))
for name, c in sup["children"].items():
    print(f"  {name}: {'running' if c['running'] else 'RESTARTING'} (restarts {c['restarts']})")
o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
try:
    s = json.load(o.open(f"http://127.0.0.1:{sys.argv[1]}/api/state", timeout=10))
except OSError as e:
    print(f"  dashboard not answering: {e}"); sys.exit(0)
r, m, g = s["runner"] or {}, s["market"] or {}, s["gateway"]["health"]
online = sum(1 for a in s["agents"] if a["online"] and a["status"] != "error")
lc = r.get("last_cycle") or {}
print(f"  mode: {'PAPER' if g.get('mode') == 'paper' and g.get('live_trading') is False else 'UNKNOWN'}")
print(f"  agents online: {online}/50; last cycle invoked {lc.get('agents_invoked', '-')}/50")
print(f"  kraken feed: {'ok' if m.get('ok') else 'ERROR'}, {int(m.get('age_s') or 0)} s old")
print(f"  alerts: {len(s['alerts'])}")
for a in s["alerts"]:
    print(f"    [{a['level']}] {a['text']}")
PY
}

phone() {
    echo "0.0.0.0" > "$HOST_FILE"
    stop; start
    echo
    echo "Deschide pe telefon (conectat la ACELASI Wi-Fi): http://$(lan_ip):$DASHBOARD_PORT"
    echo "Fara parola: oricine din reteaua ta Wi-Fi poate VEDEA dashboard-ul (nu poate da ordine)."
    echo "Nu deschide portul $DASHBOARD_PORT in router. Revii la 'doar pe calculator' cu: $0 local"
}

local_only() {
    rm -f "$HOST_FILE"
    stop; start
    echo "Dashboard-ul este din nou doar pe acest calculator."
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop; start ;;
    status) status ;;
    logs) tail -n 100 -f "$STATE/runner.log" ;;
    verify) ensure_venv; "$PY" -m pytest -c ai_team/pytest.ini --rootdir . ai_team/tests -q -p no:cacheprovider; "$PY" -m ai_team.cli verify ;;
    telefon|phone) phone ;;
    local) local_only ;;
    *) echo "Usage: $0 start|stop|restart|status|logs|verify|telefon|local"; exit 2 ;;
esac
