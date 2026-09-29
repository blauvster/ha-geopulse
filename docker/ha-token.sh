#!/bin/sh
# Print a fresh access token for the dev HA (tokens expire after 30 min),
# using the refresh token in .ha-config/dev-credentials.
cd "$(dirname "$0")/.."
RT=$(grep ^HA_REFRESH_TOKEN= .ha-config/dev-credentials | cut -d= -f2-)
curl -s -X POST http://127.0.0.1:8123/auth/token \
  -d "grant_type=refresh_token&refresh_token=$RT&client_id=http://127.0.0.1:8123/" |
  python3 -c 'import sys,json;print(json.load(sys.stdin)["access_token"])'
