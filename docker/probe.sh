#!/bin/bash
# Run scripts/probe_geopulse.py in the test image against the server in
# .env.local. --env-file passes values literally (tokens can contain shell
# metacharacters), and the token is never printed.
set -e
cd "$(dirname "$0")/.."
exec docker run --rm -v "$PWD":/app --env-file .env.local \
    geopulse-test python scripts/probe_geopulse.py "$@"
