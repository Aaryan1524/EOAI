#!/bin/sh
# Pre-registered sequence: trials 1..N, each followed by its dev-eval.
# A further trial starts only if the previous trial and dev-eval exited cleanly;
# this rule never inspects scores.
set -eu
cd "$(dirname "$0")/../.."
N=${1:-3}
PY=".venv/bin/python"
export PYTHONPATH=src:.
mkdir -p runs/comparative
$PY -m experiments.comparative.runner validate > runs/comparative/validate.json
t=1
while [ "$t" -le "$N" ]; do
  echo "$(date -u +%FT%TZ) trial $t start"
  $PY -m experiments.comparative.runner trial --trial "$t" --out "runs/comparative/trial-$t"
  echo "$(date -u +%FT%TZ) trial $t deveval start"
  $PY -m experiments.comparative.runner deveval --trial-dir "runs/comparative/trial-$t" \
      --out "runs/comparative/deveval-$t"
  echo "$(date -u +%FT%TZ) trial $t done"
  t=$((t + 1))
done
