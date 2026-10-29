#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x node_modules/.bin/remotion ]]; then echo "BLOCKED: external worker dependencies not installed" >&2; exit 3; fi
mkdir -p out/frames
for frame in 0 72 600 720 1320 1440 2100; do
 ./node_modules/.bin/remotion still src/index.tsx CalmHistorySleeper "out/frames/frame-$frame.png" --frame="$frame" --scale=0.5 --props=data/sample.json
done
echo "STILLS_READY_VISUAL_QA_NOT_YET_PASSED"
