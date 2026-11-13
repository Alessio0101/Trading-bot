#!/usr/bin/env bash
# Move the paper account, history and keys from the Windows PC to the VPS.
#
# On the PC (cmd), after stopping the bot there, copy three folders to /tmp on the VPS:
#   scp -r "<bot folder>\risk_gateway\state" root@<VPS-IP>:/tmp/import-gateway
#   scp -r "<bot folder>\ai_team\state"      root@<VPS-IP>:/tmp/import-ai
#   scp -r "%USERPROFILE%\.ai-trading-desk"  root@<VPS-IP>:/tmp/import-keys
# then on the VPS:
#   sudo bash /opt/ai-trading-desk/ai_team/vps/import.sh
#
# Missing folders are skipped. Machine-specific files (pids, locks, the dashboard address)
# are not copied. The import folders are deleted afterwards because they hold the keys.
set -euo pipefail
DIR="${AI_DIR:-/opt/ai-trading-desk}"
BOT_USER="${AI_USER:-aibot}"
SERVICE=ai-trading-desk
SRC="${AI_IMPORT:-/tmp}"
HOME_DIR="$(getent passwd "$BOT_USER" | cut -d: -f6)"
SKIP=" ${AI_SKIP:-} "
skip() { [[ "$SKIP" == *" $1 "* ]]; }

[ "$(id -u)" -eq 0 ] || { echo "Ruleaza cu sudo (sau ca root)."; exit 1; }
[ -d "$DIR/ai_team" ] || { echo "Botul nu este instalat in $DIR. Ruleaza intai setup.sh."; exit 1; }

skip systemd || systemctl stop "$SERVICE" || true
copied=0
if [ -d "$SRC/import-gateway" ]; then
    cp -a "$SRC/import-gateway/." "$DIR/risk_gateway/state/"
    echo "Contul de test (portofoliul) a fost mutat."; copied=1
fi
if [ -d "$SRC/import-ai" ]; then
    rsync -a --exclude pids --exclude supervisor.lock --exclude supervisor.stop --exclude supervisor.json \
        --exclude dashboard.host --exclude remote_url.txt --exclude '*.out' \
        "$SRC/import-ai/" "$DIR/ai_team/state/" 2>/dev/null \
        || cp -a "$SRC/import-ai/." "$DIR/ai_team/state/"
    rm -rf "$DIR/ai_team/state/pids"
    rm -f "$DIR/ai_team/state/supervisor.lock" "$DIR/ai_team/state/supervisor.stop" \
        "$DIR/ai_team/state/dashboard.host" "$DIR/ai_team/state/remote_url.txt"
    echo "Istoricul, invatarea agentilor si setarile au fost mutate."; copied=1
fi
if [ -d "$SRC/import-keys" ]; then
    mkdir -p "$HOME_DIR/.ai-trading-desk"
    cp -a "$SRC/import-keys/." "$HOME_DIR/.ai-trading-desk/"
    chown -R "$BOT_USER:$BOT_USER" "$HOME_DIR/.ai-trading-desk"
    chmod 700 "$HOME_DIR/.ai-trading-desk"
    chmod 600 "$HOME_DIR/.ai-trading-desk/"*.json 2>/dev/null || true
    echo "Cheile si Telegram au fost mutate."; copied=1
fi
chown -R "$BOT_USER:$BOT_USER" "$DIR/ai_team/state" "$DIR/risk_gateway/state"
if command -v tailscale >/dev/null && tailscale ip -4 >/dev/null 2>&1; then
    echo "http://$(tailscale ip -4 | head -n1):8800" > "$DIR/ai_team/state/remote_url.txt"
    chown "$BOT_USER:$BOT_USER" "$DIR/ai_team/state/remote_url.txt"
fi
rm -rf "$SRC/import-gateway" "$SRC/import-ai" "$SRC/import-keys"
skip systemd || systemctl start "$SERVICE"
[ "$copied" = 1 ] || echo "Nu am gasit nimic de mutat in $SRC (import-gateway, import-ai, import-keys)."
echo "Gata. Botul a pornit pe VPS cu datele de pe calculator."
