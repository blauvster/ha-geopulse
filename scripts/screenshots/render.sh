#!/bin/sh
# Regenerate docs/images/card-*.png from demo.html (made-up data) in headless
# Chromium, in the test image. Run from anywhere: scripts/screenshots/render.sh
set -e
cd "$(dirname "$0")/../.."
exec docker run --rm \
  -v "$PWD/scripts/screenshots":/src:ro \
  -v "$PWD/custom_components/geopulse/frontend":/frontend:ro \
  -v "$PWD/docs/images":/out \
  geopulse-test sh -c '
set -e
apt-get update -qq >/dev/null
apt-get install -y -qq --no-install-recommends chromium fonts-roboto fonts-noto-core pngquant >/dev/null 2>&1
mkdir -p /page && cp /src/demo.html /page/ && ln -s /frontend /page/geopulse_frontend
cd /page && (python3 -m http.server 8765 >/dev/null 2>&1 &) && sleep 1
shot() { chromium --headless=new --no-sandbox --disable-gpu --hide-scrollbars --lang=en-GB \
  --force-device-scale-factor=2 --window-size=$2,$3 --virtual-time-budget=15000 \
  --screenshot=/out/$1.png "http://127.0.0.1:8765/demo.html#$4" 2>/dev/null; }
shot hero 1240 790 "{\"width\":1240,\"config\":{\"title\":\"Family timeline\",\"layout\":\"columns\",\"map_height\":480}}"
shot dark  480 1010 "{\"width\":480,\"dark\":true,\"config\":{\"title\":\"Today\",\"layout\":\"stacked\",\"map_height\":300}}"
python /src/crop.py
for f in card-light card-dark; do
  pngquant --quality 70-90 --speed 1 --force --output /out/$f.q.png /out/$f.png && mv /out/$f.q.png /out/$f.png
done
rm -f /out/hero.png /out/dark.png'
