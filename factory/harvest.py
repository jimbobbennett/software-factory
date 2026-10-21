"""Outcome harvester: turn what happened to each factory PR on GitHub into labels on
the run's trace in Arize AX.

Every gate in the factory already passes a verdict on the agents' work. This script
collects the ones that land after (or outside) the run, and writes them as span
annotations, so evals and datasets can filter on them:

  on the factory.run root span
    factory_human_verdict   ship | changes | closed | unreviewed | automerged | pending
    factory_merged          yes | no
    factory_ci_first_pass   pass | fail
    factory_reverted        reverted | clean_7d | watching | n/a
    factory_babysit_loops   CI fixes + human reworks (score)
    factory_hours_to_merge  PR opened -> merged (score)
    factory_human_feedback  the gate's comments on the PR (text)

  on each station.review_<kind> span
    factory_reviewer_agreement   agree | miss | false_alarm
        miss        = reviewer approved, but the gate asked for changes / closed it, or it was reverted
        false_alarm = reviewer requested changes, but the gate shipped it as-is

  on the station.router span
    factory_router_outcome   correct | overcautious | unsafe

The human verdict is the gate's *first* verdict, because that is what the reviewers were
judging (reworks after /changes are not re-reviewed). A bare manual merge with no
/ship is "unreviewed", and labels/overrides.json can mark any PR unreviewed, so
release-testing merges don't pollute the agreement numbers.

Annotations are upserts, so this is safe to run repeatedly:

    python -m factory.harvest                 # all runs in runs/state, write to AX
    python -m factory.harvest --dry-run       # print the labels, write nothing
    python -m factory.harvest --every 900     # keep harvesting every 15 minutes
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import axcli, config, gh, reference
from .stations import human

log = logging.getLogger("factory.harvest")

REVERT_WINDOW = timedelta(days=7)
OVERRIDES = config.ROOT / "labels" / "overrides.json"
SNAPSHOT = config.ROOT / "runs" / "harvest.json"

CONFIGS = {
    "factory_human_verdict": ("categorical", ["ship", "changes", "closed", "unreviewed", "automerged", "pending"]),
    "factory_merged": ("categorical", ["yes", "no"]),
    "factory_ci_first_pass": ("categorical", ["pass", "fail"]),
    "factory_reverted": ("categorical", ["reverted", "clean_7d", "watching", "n/a"]),
    "factory_reviewer_agreement": ("categorical", ["agree", "miss", "false_alarm"]),
    "factory_router_outcome": ("categorical", ["correct", "overcautious", "unsafe"]),
    "factory_babysit_loops": ("continuous", (0, 10, "MINIMIZE")),
    "factory_hours_to_merge": ("continuous", (0, 720, "MINIMIZE")),
    "factory_human_feedback": ("freeform", None),
}


# ---------- GitHub side ----------

def _ts(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def is_factory_text(body: str) -> bool:
    """The factory posts as Jim, so tell its comments apart from his by their shape."""
    b = body.lstrip()
    return b.startswith("### ") or "Built by the software factory" in b


def human_items(pr_number: int) -> list[dict]:
    me = human.me()
    return [c for c in gh.pr_conversation(pr_number)
            if c["author"] == me and c["body"].strip() and not is_factory_text(c["body"])]


def first_verdict_comment(items: list[dict]) -> str:
    """The text of the gate's first /ship or /changes comment."""
    for c in items:
        b = c["body"].strip()
        if b.startswith(("/changes", "/ship")):
            return b
    return ""


def first_verdict(pr: dict, items: list[dict], route: str | None) -> str:
    for c in items:
        b = c["body"].strip()
        if b.startswith("/changes"):
            return "changes"
        if b.startswith("/ship"):
            return "ship"
    if pr["state"] == "CLOSED":
        return "closed"
    if pr["state"] == "MERGED":
        return "automerged" if route == "automerge" else "unreviewed"
    return "pending"


def reverted(pr: dict) -> str:
    if pr["state"] != "MERGED" or not pr.get("mergedAt"):
        return "n/a"
    merged_at = _ts(pr["mergedAt"])
    sha = (pr.get("mergeCommit") or {}).get("oid", "")
    commits = json.loads(gh.sh("gh", "api", f"repos/{config.TARGET_REPO}/commits?sha=main&per_page=100"
                               f"&since={pr['mergedAt']}"))
    for c in commits:
        msg = c["commit"]["message"]
        if msg.lower().startswith("revert") and ((sha and sha in msg) or f"#{pr['number']}" in msg):
            return "reverted"
    return "clean_7d" if datetime.now(timezone.utc) - merged_at > REVERT_WINDOW else "watching"


# ---------- labels ----------

def agreement(reviewer_verdict: str, human_verdict: str, revert: str) -> str | None:
    approved = reviewer_verdict == "approve"
    if revert == "reverted":
        return "miss" if approved else "agree"
    if human_verdict in ("changes", "closed"):
        return "miss" if approved else "agree"
    if human_verdict == "ship":
        return "agree" if approved else "false_alarm"
    return None  # automerged / unreviewed / pending: no human ground truth (yet)


def router_outcome(route: str | None, human_verdict: str, revert: str) -> str | None:
    if route == "automerge":
        return {"reverted": "unsafe", "clean_7d": "correct"}.get(revert)
    if route == "human":
        if human_verdict in ("changes", "closed") or revert == "reverted":
            return "correct"
        if human_verdict == "ship":
            return "overcautious"
    return None


def harvest_one(state: dict, overrides: dict) -> dict:
    n = state["pr"]["number"]
    pr = json.loads(gh.gh("pr", "view", str(n), "--json",
                          "number,state,createdAt,mergedAt,closedAt,mergeCommit,url"))
    items = human_items(n)
    route = (state.get("route") or {}).get("route")
    verdict = first_verdict(pr, items, route)
    override = overrides.get(str(n)) or {}
    verdict = override.get("human_verdict", verdict)
    revert = reverted(pr)
    feedback = "\n\n".join(f"[{c['kind']}] {c['body'].strip()}" for c in items)
    if override.get("reason"):
        feedback = (feedback + "\n\n" if feedback else "") + f"[override] {override['reason']}"

    root = {
        "factory_human_verdict": verdict,
        "factory_merged": "yes" if pr["state"] == "MERGED" else "no",
        "factory_ci_first_pass": "pass" if state.get("ci_runs", 0) >= 1 and not state.get("ci_fixes") else "fail",
        "factory_reverted": revert,
        "factory_babysit_loops": state.get("ci_fixes", 0) + state.get("reworks", 0),
    }
    if pr.get("mergedAt"):
        root["factory_hours_to_merge"] = round(
            (_ts(pr["mergedAt"]) - _ts(pr["createdAt"])).total_seconds() / 3600, 2)
    if feedback:
        root["factory_human_feedback"] = feedback[:1490]

    reviewers = {k: agreement(r["verdict"], verdict, revert) for k, r in (state.get("reviews") or {}).items()}
    return {"issue": state["issue"]["number"], "pr": n, "url": pr["url"], "trace_id": state["trace_id"],
            "root_span_id": state["root_span_id"], "started": state.get("started"), "root": root,
            "reviewer_verdicts": {k: r["verdict"] for k, r in (state.get("reviews") or {}).items()},
            "reviewers": reviewers, "route": route, "router": router_outcome(route, verdict, revert),
            "gate_feedback": first_verdict_comment(items)}


# ---------- AX side ----------

def ensure_configs() -> None:
    found = axcli.run_json("annotation-configs", "list", "--name", "factory_", "--limit", "100")
    have = {c["name"] for c in (found.get("annotation_configs", []) if isinstance(found, dict) else found)}
    for name, (kind, spec) in CONFIGS.items():
        if name in have:
            continue
        args = ["annotation-configs", "create", kind, "--name", name]
        if kind == "categorical":
            args += [x for v in spec for x in ("--value", v)]
        elif kind == "continuous":
            args += ["--min-score", str(spec[0]), "--max-score", str(spec[1]),
                     "--optimization-direction", spec[2]]
        axcli.run(*args)
        log.info("created annotation config %s", name)


def station_spans(trace_id: str) -> dict[str, str]:
    """Latest span id per station name (station.review_tests, station.router, ...)."""
    spans = axcli.run_json("spans", "export", config.ARIZE_PROJECT_ID or config.ARIZE_PROJECT_NAME,
                           "--trace-id", trace_id, "--stdout", "-l", "500", "--days", "31", timeout=120)
    out: dict[str, tuple[str, str]] = {}
    for s in spans:
        name = s.get("name", "")
        if name.startswith("station."):
            if name not in out or s["start_time"] > out[name][1]:
                out[name] = (s["context"]["span_id"], s["start_time"])
    return {k: v[0] for k, v in out.items()}


def value(name: str, v) -> dict:
    kind = CONFIGS[name][0]
    if kind == "continuous":
        return {"name": name, "score": float(v)}
    if kind == "freeform":
        return {"name": name, "text": str(v)}
    return {"name": name, "label": v}  # categorical scores live on the config, not here


def records_for(h: dict) -> list[dict]:
    recs = [{"record_id": h["root_span_id"], "values": [value(k, v) for k, v in h["root"].items()]}]
    wanted = {f"station.review_{k}": a for k, a in h["reviewers"].items() if a}
    if h["router"]:
        wanted["station.router"] = h["router"]
    if wanted:
        ids = station_spans(h["trace_id"])
        for span_name, label in wanted.items():
            if span_name not in ids:
                log.warning("issue #%s: no %s span found in AX", h["issue"], span_name)
                continue
            name = "factory_router_outcome" if span_name == "station.router" else "factory_reviewer_agreement"
            recs.append({"record_id": ids[span_name], "values": [value(name, label)]})
    return recs


def write(harvested: list[dict]) -> int:
    records = [r for h in harvested for r in records_for(h)]
    if not records:
        return 0
    earliest = min(h["started"] or time.time() for h in harvested) - 3600
    start = datetime.fromtimestamp(earliest, timezone.utc).isoformat(timespec="seconds")
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(records, f)
    try:
        axcli.run("spans", "annotate", config.ARIZE_PROJECT_ID or config.ARIZE_PROJECT_NAME,
                  "--file", f.name, "--start-time", start, timeout=120)
    finally:
        Path(f.name).unlink(missing_ok=True)
    return len(records)


# ---------- CLI ----------

def load_states() -> list[dict]:
    out = []
    for p in sorted(config.STATE_DIR.glob("*.json"), key=lambda p: int(p.stem)):
        s = json.loads(p.read_text(encoding="utf-8"))
        if s.get("pr") and s.get("trace_id") and s.get("root_span_id"):
            out.append(s)
    return out


def summary(harvested: list[dict]) -> str:
    rows = [f"{'issue':>5} {'PR':>4}  {'human':11} {'merged':6} {'ci 1st':6} {'revert':9} "
            f"{'correct.':12} {'security':12} {'tests':12} router"]
    for h in harvested:
        r = h["root"]
        rv = [f"{h['reviewer_verdicts'].get(k, '-')[:7]}/{h['reviewers'].get(k) or '-'}"
              for k in ("correctness", "security", "tests")]
        rows.append(f"{h['issue']:>5} {h['pr']:>4}  {r['factory_human_verdict']:11} {r['factory_merged']:6} "
                    f"{r['factory_ci_first_pass']:6} {r['factory_reverted']:9} "
                    + " ".join(f"{x:12}" for x in rv) + f" {h['route']}/{h['router'] or '-'}")
    return "\n".join(rows)


def once(dry_run: bool) -> list[dict]:
    overrides = json.loads(OVERRIDES.read_text(encoding="utf-8")) if OVERRIDES.exists() else {}
    harvested = []
    states = load_states()
    for s in states:
        try:
            harvested.append(harvest_one(s, overrides))
        except Exception:
            log.exception("harvest of issue #%s failed", s["issue"]["number"])
    print(summary(harvested))
    SNAPSHOT.write_text(json.dumps(harvested, indent=2), encoding="utf-8")
    if not dry_run:
        ensure_configs()
        n = write(harvested)
        log.info("wrote %d annotated spans for %d runs to AX", n, len(harvested))
        k = reference.sync({s["pr"]["number"]: s for s in states}, harvested)
        if k:
            log.info("recorded %d new gate verdicts and their reference.review spans", k)
    return harvested


def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--dry-run", action="store_true", help="print labels, don't write to AX")
    p.add_argument("--every", type=int, default=0, help="repeat every N seconds")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    if not args.dry_run and not axcli.available():
        p.error("needs the ax CLI and ARIZE_SPACE_ID (or use --dry-run)")
    while True:
        try:
            once(args.dry_run)
        except Exception:
            if not args.every:
                raise
            log.exception("harvest pass failed")
        if not args.every:
            return 0
        time.sleep(args.every)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
