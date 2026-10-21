"""Review gate span: the gate's verdict on a PR, and the span the AX judges score.

When the review gate gives its first verdict on a PR (a `/ship` or `/changes` comment,
picked up by factory.harvest), two things happen:

1. `record()` saves the verdict to `labels/gate/<pr>.json`. The router experiment reads
   these as ground truth.
2. `emit_span()` writes a `reference.review` span into the PR's original trace. It holds
   everything an LLM judge needs to grade the Claude reviewers in one place:

     input.value                       the PR diff
     output.value                      the gate's verdict + feedback
     factory.spec                      the spec agent's acceptance criteria
     factory.reviewer.correctness      the Claude correctness reviewer's verdict + findings
     factory.reviewer.security         ... security
     factory.reviewer.tests            ... tests
     factory.reference_verdict         ship | changes

   Continuous evaluation tasks in AX filter on name = 'reference.review' and score it with
   the miss and spec-adherence judges (see judges/).

Only the first verdict counts: it is the one the reviewers' verdicts are compared with.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from . import config, gh, tracing
from .stations import review as review_station

log = logging.getLogger("factory.reference")

OUT_DIR = config.ROOT / "labels" / "gate"
EMITTED = OUT_DIR / "_spans.json"  # PR numbers whose reference span has been written
KINDS = ["correctness", "security", "tests"]


def verdict_text(r: dict) -> str:
    return f"Verdict: {r['verdict']}\n\n{r.get('feedback') or '(no comments)'}"


def reviewer_text(r: dict | None) -> str:
    if not r:
        return "(this reviewer did not run)"
    return review_station.render(r)


def _emitted() -> set[int]:
    return set(json.loads(EMITTED.read_text())) if EMITTED.exists() else set()


def _diff(n: int, limit: int = 60_000) -> str:
    d = gh.gh("pr", "diff", str(n))
    return d if len(d) <= limit else d[:limit] + "\n\n[diff truncated]"


def load(pr: int) -> dict | None:
    p = OUT_DIR / f"{pr}.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def record(state: dict, verdict: str, feedback: str) -> dict:
    """Save the gate's first verdict on a PR. Later calls leave the first one alone."""
    n = state["pr"]["number"]
    existing = load(n)
    if existing:
        return existing
    r = {"pr": n, "issue": state["issue"]["number"], "head_sha": state["pr"].get("head_sha"),
         "verdict": verdict, "feedback": feedback,
         "recorded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
         "claude_reviews": {k: v["verdict"] for k, v in (state.get("reviews") or {}).items()},
         "route": (state.get("route") or {}).get("route")}
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"{n}.json").write_text(json.dumps(r, indent=2), encoding="utf-8")
    return r


def emit_span(state: dict, r: dict, diff: str | None = None) -> bool:
    """Write the reference.review span into the run's trace. Once per PR."""
    n = state["pr"]["number"]
    done = _emitted()
    if n in done or not state.get("trace_id") or not state.get("root_span_id"):
        return False
    diff = diff if diff is not None else _diff(n)
    session = f"{config.TARGET_REPO}#{state['issue']['number']}"
    meta = {"issue": state["issue"]["number"], "repo": config.TARGET_REPO, "pr": n,
            "run": f"issue-{state['issue']['number']}", "station": "reference",
            "head_sha": r.get("head_sha")}
    with tracing.span("reference.review", session_id=session, metadata=meta, input=diff[:40_000],
                      parent=(state["trace_id"], state["root_span_id"])) as s:
        tracing.set_output(s, verdict_text(r))
        s.set_attribute("factory.spec", (state.get("spec") or {}).get("markdown") or "(none)")
        s.set_attribute("factory.reference_verdict", r["verdict"])
        reviews = state.get("reviews") or {}
        for kind in KINDS:
            s.set_attribute(f"factory.reviewer.{kind}", reviewer_text(reviews.get(kind)))
            if reviews.get(kind):
                s.set_attribute(f"factory.review_{kind}", reviews[kind]["verdict"])
    done.add(n)
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    EMITTED.write_text(json.dumps(sorted(done)))
    return True


def sync(states: dict[int, dict], harvested: list[dict]) -> int:
    """For every PR with a first gate verdict, save it and emit its span (once each)."""
    todo = [h for h in harvested if h["root"]["factory_human_verdict"] in ("ship", "changes")
            and h["pr"] not in _emitted() and h["pr"] in states]
    if not todo:
        return 0
    tracing.init()
    for h in todo:
        state = states[h["pr"]]
        r = record(state, h["root"]["factory_human_verdict"], h.get("gate_feedback", ""))
        emit_span(state, r)
    if tracing._provider is not None:
        tracing._provider.force_flush()
    return len(todo)
