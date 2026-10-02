#!/usr/bin/env bash
# One-time setup on Ubuntu 22.04 / 24.04 (e.g. a mini PC at home).
#   bash scripts/setup_ubuntu.sh             # install everything, run checks
#   bash scripts/setup_ubuntu.sh --service   # ...and run the bot 24/7 as a background service
set -euo pipefail
cd "$(dirname "$0")/.."
ROOT="$(pwd)"
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }

say "Checking system packages"
need=()
command -v git >/dev/null || need+=(git)
python3 -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null || need+=(python3)
python3 -c 'import ensurepip' 2>/dev/null || need+=(python3-venv)
command -v curl >/dev/null || need+=(curl)
if [ ${#need[@]} -gt 0 ]; then
  echo "    installing: ${need[*]} (you may be asked for your password)"
  sudo apt-get update -qq
  sudo apt-get install -y -qq "${need[@]}" python3-pip
fi
ok "$(python3 --version), $(git --version)"

say "Creating Python environment in .venv"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q
ok "packages installed"

say "Preparing config"
if [ ! -f .env ]; then
  cp .env.example .env
  ok "created .env (put your API keys in it: nano .env)"
else
  ok ".env already exists - left untouched"
fi
chmod 600 .env
mkdir -p data
chmod +x scripts/*.sh

say "Running setup checks (doctor)"
.venv/bin/python -m meme_trader.sniper doctor || true

if [[ "${1:-}" == "--service" ]]; then
  say "Installing background service (meme-sniper)"
  sed -e "s|__USER__|$(whoami)|g" -e "s|__ROOT__|$ROOT|g" deploy/meme-sniper.service \
    | sudo tee /etc/systemd/system/meme-sniper.service >/dev/null
  sudo systemctl daemon-reload
  sudo systemctl enable --now meme-sniper
  ok "service running - status: sudo systemctl status meme-sniper | logs: journalctl -u meme-sniper -f"
fi

say "Done. Next:"
cat <<EOF
    Demo market:        scripts/start.sh demo     (then open http://127.0.0.1:8787)
    Real market, paper: scripts/start.sh paper    (needs PUMPPORTAL_API_KEY in .env)
    Re-check setup:     scripts/start.sh doctor
EOF
