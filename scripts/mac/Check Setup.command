#!/usr/bin/env bash
# Double-click me: checks packages, keys, connections and your wallet.
cd "$(dirname "$0")/../.." && scripts/start.sh doctor; echo; read -n 1 -s -r -p "Press any key to close"
