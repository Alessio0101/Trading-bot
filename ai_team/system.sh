#!/usr/bin/env bash
# Start / stop the whole AI trading system (paper trading only).
#
#   ai_team/system.sh start | stop | restart | status | logs | verify | telefon | local
#
# Components (all bound to 127.0.0.1):
#   risk gateway  :8790  independent, paper-only, own Kraken price feed, stop/target exits
#   runner              market feed every 10 s + coordinator cycle (all 50 agents) every 5 min
#   dashboard     :8800  live web UI (Server-Sent Events)
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
STATE="$ROOT/ai_team/state"
PIDS="$STATE/pids"
VENV="$ROOT/.venv"
PY="$VENV/bin/python"
DATA="${AI_TEAM_DATA:-kraken}"
INTERVAL="${AI_TEAM_INTERVAL:-300}"
GATEWAY_PORT="${AI_TEAM_GATEWAY_PORT:-8790}"
DASHBOARD_PORT="${AI_TEAM_DASHBOARD_PORT:-8800}"
# "telefon" stores 0.0.0.0 here so the dashboard is reachable from phones on the same Wi-Fi.
# Only the read-only dashboard is opened; the risk gateway always stays on 127.0.0.1.
HOST_FILE="$STATE/dashboard.host"
mkdir -p "$PIDS"

ensure_venv() {
    if [ ! -x "$PY" ]; then
        echo "Creating virtualenv in .venv ..."
        python3 -m venv "$VENV"
    fi
    "$PY" -m pip install -q -r ai_team/requirements.txt
}

running() { [ -f "$PIDS/$1.pid" ] && kill -0 "$(cat "$PIDS/$1.pid")" 2>/dev/null; }

start_one() {
    local name="$1"; shift
    if running "$name"; then echo "  $name already running (pid $(cat "$PIDS/$name.pid"))"; return; fi
    nohup "$@" >> "$STATE/$name.out" 2>&1 &
    echo $! > "$PIDS/$name.pid"
    echo "  $name started (pid $!)"
}

wait_http() {
    local url="$1" i
    for i in $(seq 1 30); do
        "$PY" -c "import urllib.request,sys; urllib.request.build_opener(urllib.request.ProxyHandler({})).open('$url', timeout=2)" 2>/dev/null && return 0
        sleep 1
    done
    return 1
}

start() {
    ensure_venv
    echo "Starting AI trading system (PAPER TRADING ONLY, data=$DATA)"
    start_one gateway "$PY" -m risk_gateway.server --port "$GATEWAY_PORT" --data "$DATA"
    wait_http "http://127.0.0.1:$GATEWAY_PORT/health" || { echo "risk gateway did not start; see $STATE/gateway.out"; exit 1; }
    start_one runner "$PY" -m ai_team.runner --data "$DATA" --interval "$INTERVAL" \
        --gateway "http://127.0.0.1:$GATEWAY_PORT"
    local host="127.0.0.1"
    [ -f "$HOST_FILE" ] && host="$(cat "$HOST_FILE")"
    start_one dashboard "$PY" -m ai_team.dashboard.server --host "$host" --port "$DASHBOARD_PORT" \
        --gateway "http://127.0.0.1:$GATEWAY_PORT"
    wait_http "http://127.0.0.1:$DASHBOARD_PORT/api/state" || { echo "dashboard did not start; see $STATE/dashboard.out"; exit 1; }
    echo
    echo "Dashboard: http://127.0.0.1:$DASHBOARD_PORT"
    if [ "$host" = "0.0.0.0" ]; then
        echo "Pe telefon (acelasi Wi-Fi): http://$(lan_ip):$DASHBOARD_PORT"
    fi
}

stop() {
    local name
    for name in dashboard runner gateway; do
        if running "$name"; then
            kill "$(cat "$PIDS/$name.pid")" && echo "  $name stopped"
        else
            echo "  $name not running"
        fi
        rm -f "$PIDS/$name.pid"
    done
}

status() {
    local name
    for name in gateway runner dashboard; do
        if running "$name"; then echo "  $name: running (pid $(cat "$PIDS/$name.pid"))"; else echo "  $name: stopped"; fi
    done
    if running dashboard; then
        "$PY" - "$DASHBOARD_PORT" <<'PY'
import json, sys, urllib.request
o = urllib.request.build_opener(urllib.request.ProxyHandler({}))
s = json.load(o.open(f"http://127.0.0.1:{sys.argv[1]}/api/state", timeout=10))
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
    fi
}

lan_ip() {
    local ip=""
    ip="$(ipconfig getifaddr en0 2>/dev/null || true)"
    [ -z "$ip" ] && ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
    echo "${ip:-IP-ul-calculatorului}"
}

restart_dashboard() {
    if running dashboard; then kill "$(cat "$PIDS/dashboard.pid")"; rm -f "$PIDS/dashboard.pid"; sleep 1; fi
    start
}

phone() {
    echo "0.0.0.0" > "$HOST_FILE"
    restart_dashboard
    echo
    echo "Deschide pe telefon (conectat la ACELASI Wi-Fi): http://$(lan_ip):$DASHBOARD_PORT"
    echo "Fara parola: oricine din reteaua ta Wi-Fi poate VEDEA dashboard-ul (nu poate da ordine)."
    echo "Nu deschide portul $DASHBOARD_PORT in router. Revii la 'doar pe calculator' cu: $0 local"
}

local_only() {
    rm -f "$HOST_FILE"
    restart_dashboard
    echo "Dashboard-ul este din nou doar pe acest calculator."
}

case "${1:-}" in
    start) start ;;
    stop) stop ;;
    restart) stop; sleep 1; start ;;
    status) status ;;
    telefon|phone) phone ;;
    local) local_only ;;
    logs) tail -n 100 -f "$STATE/runner.log" ;;
    verify) ensure_venv; "$PY" -m pytest -c ai_team/pytest.ini --rootdir . ai_team/tests -q -p no:cacheprovider; "$PY" -m ai_team.cli verify ;;
    *) echo "Usage: $0 start|stop|restart|status|logs|verify|telefon|local"; exit 2 ;;
esac
