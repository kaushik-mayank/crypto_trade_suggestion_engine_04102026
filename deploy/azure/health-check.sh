#!/usr/bin/env bash
# One command that answers "is this host healthy?" — run it after setup and during the 72-hour soak.
#
#   /opt/crypto-trader/deploy/azure/health-check.sh
#
# Prints a line per check. Exits non-zero if anything is wrong, so it can be used in a cron job.
set -uo pipefail

APP_DIR=/opt/crypto-trader
PY="$APP_DIR/.venv/bin/python"
FAIL=0

check() {
  local label="$1"; shift
  if "$@" >/dev/null 2>&1; then
    printf '  OK    %s\n' "$label"
  else
    printf '  FAIL  %s\n' "$label"
    FAIL=1
  fi
}

echo "services"
check "crypto-ingest running" systemctl is-active --quiet crypto-ingest
check "crypto-app running"    systemctl is-active --quiet crypto-app
check "crypto-tunnel running" systemctl is-active --quiet crypto-tunnel

echo "the API answers on loopback"
# No token here: an unauthenticated request should be REFUSED with 401, which proves the token is
# actually enforced. A 200 would mean the API is open.
CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 5 http://127.0.0.1:8000/api/health || echo 000)
if [ "$CODE" = "401" ]; then
  printf '  OK    unauthenticated request refused (401)\n'
else
  printf '  FAIL  unauthenticated request returned %s — expected 401\n' "$CODE"
  FAIL=1
fi

echo "data freshness"
cd "$APP_DIR" || exit 1
# --check makes no requests; it reports whether the stored history is complete for the live profile.
if "$PY" -m bootstrap --check >/tmp/bootstrap-check.txt 2>&1; then
  printf '  OK    price history complete\n'
else
  printf '  WARN  price history incomplete — see /tmp/bootstrap-check.txt\n'
  tail -3 /tmp/bootstrap-check.txt | sed 's/^/        /'
fi

echo "disk"
USED=$(df --output=pcent "$APP_DIR" | tail -1 | tr -dc '0-9')
if [ "${USED:-100}" -lt 85 ]; then
  printf '  OK    disk %s%% used\n' "$USED"
else
  printf '  FAIL  disk %s%% used — the database grows about 2 GB a month\n' "$USED"
  FAIL=1
fi

echo "recent errors in the journal (last hour)"
ERRS=$(journalctl -u crypto-app -u crypto-ingest --since '1 hour ago' 2>/dev/null \
       | grep -c '"level": *"error"' || true)
printf '        %s error-level log lines\n' "${ERRS:-0}"

exit $FAIL
