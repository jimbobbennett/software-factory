#!/bin/zsh
# Held-out test: v1 (old production) vs v3 (current production) on planted-bug PRs.
cd ~/github/software-factory
export PYTHONUNBUFFERED=1
PY=.venv/bin/python
X=(--dataset factory-reviewer-planted-bugs --suffix=-planted)
P=factory/prompts/candidates
for r in 1 2 3; do
  $PY -m factory.experiment $X run --lane correctness --variant v1 --repeat $r --prompt-file $P/review_correctness_v1.md --parallel 5 || exit 1
  $PY -m factory.experiment $X run --lane correctness --variant v3 --repeat $r --prompt-file $P/review_correctness_v3.md --parallel 5 || exit 1
done
$PY -m factory.experiment $X score --lane correctness --variants v1,v3 --repeats 3
$PY -m factory.experiment $X compare --lane correctness --variants v1,v3 --repeats 3
echo "DONE exit=$?"
