#!/bin/sh
set -eu
mkdir -p "${DATA_ROOT:-/data}/deployed_bots" "${DATA_ROOT:-/data}/logs"
exec python /app/bot.py
