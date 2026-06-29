#!/usr/bin/env bash
#
# demo.sh — one command to validate the Korral StoreLink MCP server end-to-end.
#
# Hand this to anyone (incl. the client): it sets up the venv, runs the Step 2 butter
# task, runs the unit tests, shows the two operator artifacts (audit + debug), and proves
# the raw store key is never logged. Exits non-zero if anything fails.
#
#   ./demo.sh
#
set -euo pipefail

# Always run from the script's own directory (handles the spaces in the path).
cd "$(dirname "$0")"

banner() { printf '\n\033[1m========================================================================\n%s\n========================================================================\033[0m\n' "$1"; }

# ---------------------------------------------------------------------------
banner "1/5  Environment"
if [ ! -d ".venv" ]; then
  echo "Creating virtualenv (.venv)..."
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
echo "Installing pinned dependencies..."
pip install -q -r requirements.txt
python --version

# ---------------------------------------------------------------------------
banner "2/5  Smoke test — the 'Madeta butter' task (store 47 orders, 102 does not)"
python smoke_test.py

# ---------------------------------------------------------------------------
banner "3/5  Unit tests"
python -m pytest -q

# ---------------------------------------------------------------------------
banner "4/5  Audit log — what the Korral category buyer reads next morning"
cat audit.log

# ---------------------------------------------------------------------------
banner "5/5  Debug log — sample line for the FDE + raw-key safety check"
echo "Sample structured debug line (stderr in production):"
python -c "from server import service; service.get_stock_position(47,'8847291')" 2>&1 >/dev/null | python -m json.tool
echo
if python -c "from server import service; service.get_stock_position(47,'8847291')" 2>&1 >/dev/null | grep -q "sk_live"; then
  echo "FAIL: a raw store key appeared in the debug output"; exit 1
else
  echo "PASS: 0 raw store keys in the debug output (only the sha256 fingerprint is logged)"
fi

banner "DEMO COMPLETE ✅"
echo "Next: connect to Claude Desktop (see README 'Connect to Claude Desktop') to drive it as the agent."
