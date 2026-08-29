#!/usr/bin/env bash
# Prove the isolation actually bought something. Run AFTER validate passes.
#   ./scripts/jitter-test.sh [duration_seconds]
# Needs rt-tests (cyclictest) and, ideally, an idle window.
set -euo pipefail
source /etc/lowlatency/cores.env
DUR=${1:-300}

command -v cyclictest >/dev/null || { echo "install rt-tests (cyclictest)" >&2; exit 1; }

echo "=== hardware latency detector (SMI / firmware steals) ==="
if command -v hwlatdetect >/dev/null; then
  hwlatdetect --duration=30 --threshold=10 || true
else
  echo "hwlatdetect not installed - skipping. Firmware SMIs show up as unexplained outliers."
fi

echo
echo "=== cyclictest on exclusive cores: $EXCLUSIVE_CORES (${DUR}s) ==="
# --smp would spawn on every cpu; -a pins to exactly the isolated set instead.
cyclictest -a "$EXCLUSIVE_CORES" -t "$EXCLUSIVE_CORE_COUNT" -p 99 -i 200 -m -q -D "$DUR" -h 400 \
  > /tmp/cyclictest-exclusive.txt
awk '/# Max Latencies/{print} /^# Min Latencies/{print} /^# Avg Latencies/{print}' /tmp/cyclictest-exclusive.txt

echo
echo "=== same test on shared cores (expect materially worse; that is the point) ==="
cyclictest -a "$SHARED_CORES" -t "$SHARED_CORE_COUNT" -p 99 -i 200 -m -q -D 60 \
  > /tmp/cyclictest-shared.txt
awk '/# Max Latencies/{print}' /tmp/cyclictest-shared.txt

echo
echo "full histograms: /tmp/cyclictest-exclusive.txt /tmp/cyclictest-shared.txt"
echo "Acceptance gate for a tuned metal node: p99.99 < 20us, max < 50us on exclusive cores."
