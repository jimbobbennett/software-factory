#!/bin/zsh
# Router loop: candidate v2 x3 against the existing v1 baseline (x3), then the gate.
# The gate only moves Prompt Hub `production` if unsafe stays 0 and overcautious drops.
cd ~/github/software-factory
export PYTHONUNBUFFERED=1
PY=.venv/bin/python
C=factory/prompts/candidates/router_v2.md
for r in 1 2 3; do $PY -m factory.router_experiment run --variant v2 --repeat $r --prompt-file $C || exit 1; done
$PY -m factory.router_experiment gate --candidate v2 --prompt-file $C --promote
echo "DONE exit=$?"
