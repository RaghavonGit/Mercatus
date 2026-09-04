#!/usr/bin/env bash
# One-command Mercatus autopay demo. See Mercator/AUTOPAY.md.
#   ./demo.sh            set up + launch + open the browser
#   ./demo.sh --verify   headless: run the flow, print a PASS/FAIL checklist
set -e
command -v uv >/dev/null 2>&1 || { echo "uv is not installed - see https://docs.astral.sh/uv/"; exit 1; }
cd "$(dirname "$0")/Forum"
exec uv run demo "$@"
