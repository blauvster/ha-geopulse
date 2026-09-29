#!/bin/bash
# Run scripts/probe_geopulse.py in the test image against the server in
# .env.local. The token goes in via the environment and is never printed.
set -e
cd "$(dirname "$0")/.."
set -a; . ./.env.local; set +a
exec docker run --rm -v "$PWD":/app -e GEOPULSE_URL -e GEOPULSE_READ_TOKEN \
    geopulse-test python scripts/probe_geopulse.py "$@"
