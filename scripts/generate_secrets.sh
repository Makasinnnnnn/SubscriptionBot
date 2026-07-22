#!/usr/bin/env sh
set -eu
echo "Generated secret (store it in .env, never commit it):"
openssl rand -base64 48
