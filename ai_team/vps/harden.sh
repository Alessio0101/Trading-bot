#!/usr/bin/env bash
# Lock down SSH on the VPS once the owner can log in with an SSH key.
#
#   sudo bash /opt/ai-trading-desk/ai_team/vps/harden.sh
#
# 1. refuses to run unless root has at least one SSH key in ~/.ssh/authorized_keys
#    (otherwise turning passwords off would lock the owner out);
# 2. turns off password logins: keys only (root may still log in with a key);
# 3. installs fail2ban (bans addresses that keep failing to log in) and automatic
#    security updates.
# If anything goes wrong, the Hetzner Cloud Console (server -> Console) still works.
set -euo pipefail
SKIP=" ${AI_SKIP:-} "
skip() { [[ "$SKIP" == *" $1 "* ]]; }
KEYS="${AI_AUTHORIZED_KEYS:-/root/.ssh/authorized_keys}"
# sshd keeps the FIRST value it reads and includes sshd_config.d/*.conf in name order,
# so this file must sort before cloud-init's 50-cloud-init.conf (PasswordAuthentication yes).
CONF="${AI_SSHD_CONF:-/etc/ssh/sshd_config.d/00-ai-trading-desk.conf}"

[ "$(id -u)" -eq 0 ] || { echo "Ruleaza cu sudo (sau ca root)."; exit 1; }
if ! grep -qE '^(ssh-(ed25519|rsa)|ecdsa-sha2-)' "$KEYS" 2>/dev/null; then
    echo "STOP: nu exista nicio cheie SSH in $KEYS."
    echo "Adauga intai cheia de pe calculator (vezi ghidul), altfel te-ai bloca singur afara."
    exit 1
fi

echo "==> Doar logare cu cheie SSH (parolele sunt dezactivate)"
cat > "$CONF" <<'EOF'
# Written by ai_team/vps/harden.sh
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin prohibit-password
PubkeyAuthentication yes
EOF
if ! skip ssh; then
    sshd -t
    systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || systemctl restart ssh
fi

echo "==> fail2ban si actualizari automate de securitate"
if ! skip apt; then
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y -qq fail2ban unattended-upgrades >/dev/null
    systemctl enable --now fail2ban >/dev/null 2>&1 || true
    echo 'APT::Periodic::Update-Package-Lists "1";' > /etc/apt/apt.conf.d/20auto-upgrades
    echo 'APT::Periodic::Unattended-Upgrade "1";' >> /etc/apt/apt.conf.d/20auto-upgrades
fi

echo ""
echo "GATA. Serverul accepta acum doar cheia SSH de pe calculatorul tau."
echo "Testeaza INAINTE sa inchizi fereastra: deschide un cmd nou si scrie  ssh root@<IP>"
echo "Trebuie sa intre fara sa ceara parola."
