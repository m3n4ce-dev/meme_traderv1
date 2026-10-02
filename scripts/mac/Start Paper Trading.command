#!/usr/bin/env bash
# Double-click me: real pump.fun market with fake money (needs PUMPPORTAL_API_KEY in .env).
cd "$(dirname "$0")/../.." && exec scripts/start.sh paper
