#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"
if [[ ! -f .env ]]; then
  echo "Missing .env. Copy .env.example to .env and configure ORBIT_MODEL / ORBIT_BASE_URL."
  exit 1
fi
if [[ ! -x .venv/bin/python ]]; then
  echo "Missing .venv. Run: python -m venv .venv && .venv/bin/pip install -r requirements.txt"
  exit 1
fi
set -a
source .env
set +a
exec .venv/bin/python -m orbit
