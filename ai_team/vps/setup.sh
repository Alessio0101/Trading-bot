#!/usr/bin/env bash
# One-command install of the AI trading desk (paper trading only) on an Ubuntu 22.04/24.04 VPS.
#
#   curl -fsSL https://raw.githubusercontent.com/Alessio0101/Trading-bot/refs/heads/claude/trading-bot-platform-w9ip9u/ai_team/vps/setup.sh | sudo bash
#
# What it does (safe to run again; it updates instead of reinstalling):
#   1. installs Python, git and the firewall;
#   2. creates the unprivileged user "aibot" and checks out the bot in /opt/ai-trading-desk;
#   3. installs the Python packages in /opt/ai-trading-desk/.venv;
#   4. runs the supervisor as the systemd service "ai-trading-desk" (starts at boot, restarts on failure);
#   5. firewall: SSH open, the read-only dashboard (port 8800) reachable ONLY from Tailscale
#      (100.64.0.0/10), everything else closed; the risk gateway stays on 127.0.0.1;
#   6. installs Tailscale and prints the login link and the dashboard address.
# No exchange keys are used or asked for. Messages are in Romanian for the owner.
set -euo pipefail

REPO="${AI_REPO:-https://github.com/Alessio0101/Trading-bot.git}"
BRANCH="${AI_BRANCH:-claude/trading-bot-platform-w9ip9u}"
DIR="${AI_DIR:-/opt/ai-trading-desk}"
BOT_USER="${AI_USER:-aibot}"
PORT=8800
SERVICE=ai-trading-desk
# For testing in containers: AI_SKIP="systemd ufw tailscale" skips those steps.
SKIP=" ${AI_SKIP:-} "

say() { printf '\n==> %s\n' "$*"; }
skip() { [[ "$SKIP" == *" $1 "* ]]; }

[ "$(id -u)" -eq 0 ] || { echo "Ruleaza cu sudo (sau ca root)."; exit 1; }

say "1/6 Instalez Python, git si firewall-ul"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq python3 python3-venv python3-pip git ca-certificates curl ufw >/dev/null

say "2/6 Utilizatorul '$BOT_USER' si codul botului in $DIR"
id "$BOT_USER" >/dev/null 2>&1 || useradd --system --create-home --shell /usr/sbin/nologin "$BOT_USER"
if [ -d "$DIR/.git" ]; then
    sudo -u "$BOT_USER" git -C "$DIR" fetch -q origin "$BRANCH"
    sudo -u "$BOT_USER" git -C "$DIR" checkout -q -B "$BRANCH" "origin/$BRANCH"
else
    mkdir -p "$DIR"
    chown "$BOT_USER:$BOT_USER" "$DIR"
    sudo -u "$BOT_USER" git clone -q --depth 50 --branch "$BRANCH" "$REPO" "$DIR"
fi

say "3/6 Pachetele Python (prima data dureaza cateva minute)"
[ -x "$DIR/.venv/bin/python" ] || sudo -u "$BOT_USER" python3 -m venv "$DIR/.venv"
sudo -u "$BOT_USER" "$DIR/.venv/bin/python" -m pip install -q --disable-pip-version-check --no-cache-dir \
    -r "$DIR/ai_team/requirements.txt"
sudo -u "$BOT_USER" mkdir -p "$DIR/ai_team/state" "$DIR/risk_gateway/state"

say "4/6 Serviciul '$SERVICE' (porneste singur si la restart)"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=AI trading desk (paper trading only)
After=network-online.target
Wants=network-online.target

[Service]
User=$BOT_USER
WorkingDirectory=$DIR
ExecStart=$DIR/.venv/bin/python -m ai_team.supervisor --data kraken --interval 300 --dashboard-host 0.0.0.0 --dashboard-port $PORT
Restart=always
RestartSec=10
KillSignal=SIGTERM
TimeoutStopSec=60

[Install]
WantedBy=multi-user.target
EOF
if ! skip systemd; then
    systemctl daemon-reload
    systemctl enable -q "$SERVICE"
    systemctl restart "$SERVICE"
fi

say "5/6 Firewall: SSH deschis, dashboard-ul doar prin Tailscale"
if ! skip ufw; then
    ufw allow OpenSSH >/dev/null
    ufw allow from 100.64.0.0/10 to any port "$PORT" proto tcp >/dev/null
    ufw --force enable >/dev/null
fi

say "6/6 Tailscale (acces privat de pe telefon)"
if ! skip tailscale; then
    command -v tailscale >/dev/null || curl -fsSL https://tailscale.com/install.sh | sh
    if ! tailscale ip -4 >/dev/null 2>&1; then
        echo "Deschide linkul de mai jos in browser si logheaza-te cu ACELASI cont ca pe telefon:"
        tailscale up
    fi
    TS_IP="$(tailscale ip -4 | head -n1)"
else
    TS_IP="100.x.y.z"
fi

cat <<EOF

==============================================================
  GATA. Botul ruleaza pe VPS, non-stop (DOAR PAPER TRADING).

  Dashboard (iPhone/PC cu Tailscale pornit):
      http://$TS_IP:$PORT

  Stare:        sudo systemctl status $SERVICE
  Jurnal:       sudo journalctl -u $SERVICE -f
  Actualizare:  sudo bash $DIR/ai_team/vps/setup.sh
  Chei/Telegram: sudo -u $BOT_USER $DIR/.venv/bin/python -m ai_team.keys setup
                 sudo -u $BOT_USER $DIR/.venv/bin/python -m ai_team.notifier setup
==============================================================
EOF
