#!/bin/sh
# Pre-registered sequence: trials 1..N, each followed by its dev-eval.
# A further trial starts only if the previous trial and dev-eval exited cleanly;
# this rule never inspects scores.
set -eu
cd "$(dirname "$0")/../.."
N=${1:-3}
BUFFER=${2:-full}
DIR=${3:-runs/comparative}
ROLE_TIMEOUT=${4:-90}
PY=".venv/bin/python"
export PYTHONPATH=src:.
mkdir -p "$DIR"
$PY -m experiments.comparative.runner validate > "$DIR/validate.json"
t=1
while [ "$t" -le "$N" ]; do
  echo "$(date -u +%FT%TZ) trial $t start"
  $PY -m experiments.comparative.runner trial --trial "$t" --buffer "$BUFFER" --role-timeout "$ROLE_TIMEOUT" --out "$DIR/trial-$t"
  echo "$(date -u +%FT%TZ) trial $t deveval start"
  $PY -m experiments.comparative.runner deveval --trial-dir "$DIR/trial-$t" \
      --out "$DIR/deveval-$t"
  echo "$(date -u +%FT%TZ) trial $t done"
  t=$((t + 1))
done
