#!/usr/bin/env bash
# Pull the latest code from GitHub and restart. Run on the VM as the `crypto` user:
#
#   /opt/crypto-trader/deploy/azure/update.sh
#
# Safe to run repeatedly. It stops if anything fails rather than leaving a half-updated service
# running, because a service running half of one version and half of another produces results
# nobody can reproduce.
set -euo pipefail

APP_DIR=/opt/crypto-trader
cd "$APP_DIR"

echo "==> current version"
git log --oneline -1

echo "==> fetching"
git fetch --quiet origin
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse origin/main)
if [ "$LOCAL" = "$REMOTE" ]; then
  echo "    already up to date; nothing to do"
  exit 0
fi

# Refuse to discard local edits silently. If someone hot-fixed a file on the VM, overwriting it
# without a word is how a fix disappears and the bug comes back a week later looking new.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "!!! there are uncommitted changes on the VM:" >&2
  git status --short >&2
  echo "!!! commit them, or `git checkout -- .` to discard, then run this again" >&2
  exit 1
fi

echo "==> updating to $REMOTE"
git merge --ff-only origin/main

echo "==> dependencies"
./.venv/bin/pip install --quiet -e .

echo "==> checks before restarting"
# Preflight proves the new code can start: parameters load, every feature's inputs are present,
# the regime artifacts are readable. Finding that out here beats finding it out from a crash loop.
./.venv/bin/python -m ingest --check
./.venv/bin/python -m app --check

echo "==> restarting"
sudo systemctl restart crypto-ingest
sudo systemctl restart crypto-app
sleep 5
systemctl is-active crypto-ingest crypto-app

echo "==> done"
git log --oneline -1
