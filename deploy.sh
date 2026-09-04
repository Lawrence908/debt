#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

export BUILD_TIME="$(date -u '+%Y-%m-%d %H:%M:%S UTC')"
docker compose up -d --build
