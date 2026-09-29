#!/bin/sh
# Build (cached) and run the test suite in Docker. Extra args go to pytest,
# e.g. docker/run-tests.sh tests/test_api.py -k friends
set -e
cd "$(dirname "$0")/.."
docker build -q -f docker/Dockerfile.test -t geopulse-test . >/dev/null
exec docker run --rm -v "$PWD":/app geopulse-test pytest -p no:cacheprovider "$@"
