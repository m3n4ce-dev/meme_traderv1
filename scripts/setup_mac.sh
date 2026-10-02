#!/usr/bin/env bash
# One-time setup on macOS (Apple Silicon or Intel).
#   bash scripts/setup_mac.sh
set -euo pipefail
cd "$(dirname "$0")/.."
say()  { printf '\n\033[1;36m==> %s\033[0m\n' "$*"; }
ok()   { printf '    \033[32m✓\033[0m %s\n' "$*"; }
die()  { printf '\n\033[1;31m%s\033[0m\n' "$*"; exit 1; }

say "Checking developer tools (git)"
if ! xcode-select -p >/dev/null 2>&1; then
  xcode-select --install || true
  die "A window opened to install Apple's Command Line Tools. Click Install, wait for it to finish, then run this script again."
fi
ok "$(git --version)"

say "Finding Python 3.10+"
PY=""
for c in python3.13 python3.12 python3.11 python3.10 python3 /opt/homebrew/bin/python3 /usr/local/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null; then
    PY="$(command -v "$c")"; break
  fi
done
if [ -z "$PY" ]; then
  if command -v brew >/dev/null 2>&1; then
    brew install python@3.12
    PY="$(brew --prefix)/bin/python3.12"
  else
    die "Python 3.10+ not found. Install it from https://www.python.org/downloads/macos/ (the macOS 64-bit universal2 installer), then run this script again."
  fi
fi
ok "using $PY ($("$PY" --version))"

say "Creating Python environment in .venv"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/python -m pip install --upgrade pip -q
.venv/bin/python -m pip install -r requirements.txt -q
ok "packages installed"

say "Preparing config"
if [ ! -f .env ]; then
  cp .env.example .env
  ok "created .env (put your API keys in it: open -e .env)"
else
  ok ".env already exists - left untouched"
fi
chmod 600 .env
mkdir -p data
chmod +x scripts/*.sh scripts/mac/*.command

say "Running setup checks (doctor)"
.venv/bin/python -m meme_trader.sniper doctor || true

say "Done. Next:"
cat <<EOF
    Double-click in Finder (meme_traderv1/scripts/mac):
      "Start Demo.command"            simulated market, opens the dashboard
      "Start Paper Trading.command"   real market, fake money (needs PUMPPORTAL_API_KEY in .env)
      "Check Setup.command"           re-run the checks
    Or in Terminal: scripts/start.sh demo | paper | doctor
EOF
