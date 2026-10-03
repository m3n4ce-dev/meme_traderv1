#!/usr/bin/env bash
# Start the bot (Mac or Ubuntu). Run scripts/setup_mac.sh or scripts/setup_ubuntu.sh first.
#   scripts/start.sh demo                 simulated market + dashboard (no keys needed)
#   scripts/start.sh paper                real pump.fun market, fake money, records data
#   scripts/start.sh paper --desk         ...with the AI trading desk (needs ANTHROPIC_API_KEY)
#   scripts/start.sh live                 REAL money (needs everything in docs/SETUP.md step 5)
#   scripts/start.sh train | backtest | sweep | compare | leaders    (use your recordings in data/ by default)
#   scripts/start.sh report | review | doctor                        (extra args are passed through)
set -euo pipefail
cd "$(dirname "$0")/.."
[ -x .venv/bin/python ] || { echo "Run the setup script first (scripts/setup_mac.sh or scripts/setup_ubuntu.sh)"; exit 1; }
PY=.venv/bin/python
MODE="${1:-demo}"; shift || true
PORT=8787
prev=""; for a in "$@"; do [ "$prev" = "--port" ] && PORT="$a"; prev="$a"; done   # bash 3.2-safe (macOS)
URL="http://127.0.0.1:$PORT"

open_dashboard() {
  [ "${NO_BROWSER:-}" = 1 ] && return
  ( sleep 3
    if command -v open >/dev/null 2>&1; then open "$URL"
    elif command -v xdg-open >/dev/null 2>&1 && [ -n "${DISPLAY:-}${WAYLAND_DISPLAY:-}" ]; then xdg-open "$URL" >/dev/null 2>&1
    fi ) &
}
# keep a Mac awake while the bot runs (no-op elsewhere)
awake() { if command -v caffeinate >/dev/null 2>&1; then caffeinate -i "$@"; else "$@"; fi; }

case "$MODE" in
  demo)   echo "Dashboard: $URL  (Ctrl-C to stop)"; open_dashboard
          awake "$PY" -m meme_trader.sniper run --synthetic --speed 3 "$@" ;;
  paper)  echo "Dashboard: $URL  (Ctrl-C to stop)"; open_dashboard
          awake "$PY" -m meme_trader.sniper run "$@" ;;
  live)   grep -q '^MEME_TRADER_CONFIRM_LIVE=yes' .env 2>/dev/null || [ "${MEME_TRADER_CONFIRM_LIVE:-}" = yes ] \
            || { echo "Live trading is locked. Read docs/SETUP.md step 5, then add MEME_TRADER_CONFIRM_LIVE=yes to .env"; exit 1; }
          echo "LIVE TRADING - real money. Dashboard: $URL  (KILL button sells everything)"; open_dashboard
          awake "$PY" -m meme_trader.sniper run --live "$@" ;;
  train|backtest|leaders|sweep|compare)
          # default to the recorded market (data/feed-*) unless a source was given
          src=0; for a in "$@"; do case "$a" in --file|--synthetic) src=1 ;; esac; done
          if [ "$src" = 0 ] && ls data/feed-* >/dev/null 2>&1; then
            echo "Using your recordings: $(ls data/feed-* | wc -l | tr -d ' ') file(s) in data/"
            "$PY" -m meme_trader.sniper "$MODE" "$@" --file data/feed-*
          else
            [ "$src" = 0 ] && echo "No recordings in data/ yet (run: scripts/start.sh paper) - using the SIMULATED market."
            "$PY" -m meme_trader.sniper "$MODE" "$@"
          fi ;;
  doctor|review|report)
          "$PY" -m meme_trader.sniper "$MODE" "$@" ;;
  *)      sed -n '2,8p' "$0"; exit 1 ;;
esac
