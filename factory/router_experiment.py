"""Router improvement loop with a code eval: dataset -> experiment -> gate -> promote.

Same shape as factory.experiment (the reviewer loop), but the eval needs no LLM judge.
The ground truth is the review gate's first verdict on each PR, so a route is:

    correct       automerge on a PR the gate shipped, or human on one it sent back
    overcautious  human on a PR the gate shipped as-is (wasted review time)
    unsafe        automerge on a PR the gate asked to change (a bug shipped)

1. `dataset`  One example per PR with a known gate verdict: the exact evidence JSON the
              live router saw (the station.router span input in AX, first routing only)
              plus the gate verdict. PRs #4 and #6 are unreviewed and excluded.
2. `run`      Re-run the router agent (Sonnet, same schema, no tools) with the Hub
              `production` prompt or a candidate file, apply the unchanged hard-rule floor
              (`rules()`), log the final routes as an AX experiment, and annotate each run
              with the code eval.
3. `gate`     Pass only if mean unsafe is 0 and no higher than the baseline, and mean
              overcautious strictly drops. Only a pass (with --promote) moves `production`.

    python -m factory.router_experiment dataset
    python -m factory.router_experiment run --variant v1 --repeat 1
    python -m factory.router_experiment run --variant v2 --repeat 1 \
        --prompt-file factory/prompts/candidates/router_v2.md
    python -m factory.router_experiment gate --candidate v2 \
        --prompt-file factory/prompts/candidates/router_v2.md --promote
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import tempfile
import time
from pathlib import Path

from . import axcli, config, prompts, reference
from .agent import run_agent
from .stations import router as router_station

log = logging.getLogger("factory.router_experiment")

DATASET = "factory-router-decisions"
OUT = config.ROOT / "runs" / "experiments"
UNREVIEWED = {4, 6}
CONFIGS = {"router_outcome": ["correct", "overcautious", "unsafe"],
           "router_route": ["automerge", "human"]}


def exp_name(variant: str, rep: int) -> str:
    return f"router-{variant}-r{rep}"


# ---------- 1. dataset ----------

def router_spans() -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        axcli.run("spans", "export", config.ARIZE_PROJECT_ID, "--filter", "name = 'station.router'",
                  "-l", "500", "--output-dir", td, timeout=180)
        files = list(Path(td).rglob("*.json"))
        spans = json.loads(files[0].read_text()) if files else []
    first: dict[int, dict] = {}
    for s in spans:  # first routing decision per PR, before any rework
        pr = int(s["attributes"]["factory.pr"])
        if pr not in first or s["start_time"] < first[pr]["start_time"]:
            first[pr] = s
    return [first[k] for k in sorted(first)]


def gate_verdict(pr: int) -> str | None:
    """The review gate's first verdict on a PR (see factory.reference)."""
    r = reference.load(pr)
    return r["verdict"] if r else None


def build_dataset() -> None:
    examples = []
    for s in router_spans():
        a = s["attributes"]
        pr = int(a["factory.pr"])
        verdict = gate_verdict(pr)
        if pr in UNREVIEWED or verdict not in ("ship", "changes"):
            continue
        ev = json.loads(a["input.value"])
        live = json.loads(a["output.value"])
        examples.append({
            "pr": pr, "issue": int(a["factory.issue"]), "evidence": json.dumps(ev),
            "gate_verdict": verdict, "rule_hits": json.dumps(router_station.rules(ev)),
            "live_route": live["route"], "live_agent_route": live["agent_route"],
            "trace_id": s["context"]["trace_id"],
        })
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(examples, f)
    print(axcli.run("datasets", "create", "--name", DATASET, "--file", f.name, timeout=180))
    print(f"{len(examples)} examples: PRs {[e['pr'] for e in examples]}")


def dataset_examples() -> list[dict]:
    rows = axcli.run_json("datasets", "export", DATASET, "--stdout", timeout=180)
    rows = rows.get("examples", rows) if isinstance(rows, dict) else rows
    out = []
    for r in rows:
        fields = {**(r.get("additional_properties") or {}), **r}
        out.append({**fields, "id": r["id"], "pr": int(fields["pr"])})
    return sorted(out, key=lambda e: e["pr"])


# ---------- 2. run + code eval ----------

def outcome(route: str, verdict: str) -> str:
    if route == "human" and verdict == "ship":
        return "overcautious"
    if route == "automerge" and verdict == "changes":
        return "unsafe"
    return "correct"


async def route_one(ex: dict, system_prompt: str) -> dict:
    ev = json.loads(ex["evidence"])
    res = await run_agent(
        prompt=f"PR #{ex['pr']}\n\nEvidence:\n```json\n{json.dumps(ev, indent=2)}\n```",
        system_prompt=system_prompt, cwd=Path(tempfile.gettempdir()), model=config.BUILD_MODEL,
        allowed_tools=[], session_id=f"experiment-router-{ex['pr']}",
        metadata={"experiment": True, "pr": ex["pr"]}, output_schema=router_station.SCHEMA,
        max_turns=3, max_budget_usd=config.ROUTER_BUDGET_USD,
    )
    if not isinstance(res.structured, dict):
        raise RuntimeError(f"PR #{ex['pr']}: no route ({res.subtype})")
    agent = res.structured
    floor = router_station.rules(ev)  # the hard floor is unchanged across variants
    route = "human" if floor or agent["route"] == "human" else "automerge"
    return {"example_id": ex["id"], "pr": ex["pr"], "route": route, "agent_route": agent["route"],
            "outcome": outcome(route, ex["gate_verdict"]), "cost_usd": res.cost_usd,
            "output": json.dumps({"route": route, "agent_route": agent["route"],
                                  "agent_risk": agent["risk"], "agent_reasons": agent["reasons"],
                                  "rule_hits": floor})}


async def run_all(examples: list[dict], system_prompt: str, parallel: int) -> list[dict]:
    sem = asyncio.Semaphore(parallel)

    async def one(ex):
        async with sem:
            for attempt in (1, 2):
                try:
                    return await route_one(ex, system_prompt)
                except Exception as e:  # noqa: BLE001 - retry once, then drop
                    log.warning("PR #%s attempt %s failed: %s", ex["pr"], attempt, e)
            return None

    return [r for r in await asyncio.gather(*(one(ex) for ex in examples)) if r]


def ensure_configs() -> None:
    found = axcli.run_json("annotation-configs", "list", "--name", "router_", "--limit", "100")
    have = {c["name"] for c in (found.get("annotation_configs", []) if isinstance(found, dict) else found)}
    for name, values in CONFIGS.items():
        if name not in have:
            axcli.run("annotation-configs", "create", "categorical", "--name", name,
                      *[x for v in values for x in ("--value", v)])


def experiment_runs(name: str) -> list[dict]:
    rows = axcli.run_json("experiments", "export", name, "--dataset", DATASET, "--stdout", timeout=180)
    return rows.get("runs", rows) if isinstance(rows, dict) else rows


def run(variant: str, prompt_file: str | None, rep: int, parallel: int) -> None:
    if prompt_file:
        text, source = Path(prompt_file).read_text(), prompt_file
    else:
        p = prompts.get("router")
        text, source = p.text, f"{p.name}@production ({p.source} {p.version_id})"
    examples = dataset_examples()
    name = exp_name(variant, rep)
    print(f"{name}: {len(examples)} examples, prompt {source} sha {prompts.sha(text)}", flush=True)
    runs = asyncio.run(run_all(examples, text, parallel))
    meta = {"prompt_source": source, "prompt_sha": prompts.sha(text), "model": config.BUILD_MODEL,
            "variant": variant, "repeat": rep}
    for r in runs:
        r["metadata"] = {**meta, "pr": r["pr"], "route": r["route"], "outcome": r["outcome"]}
    OUT.mkdir(parents=True, exist_ok=True)
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(runs, indent=1))
    axcli.run("experiments", "create", "--name", name, "--dataset", DATASET, "--file", str(path),
              timeout=180)
    # code eval: label every run in AX
    ensure_configs()
    by_example = {r["example_id"]: r for r in runs}
    ann = [{"record_id": x["id"], "values": [
        {"name": "router_outcome", "label": by_example[x["example_id"]]["outcome"]},
        {"name": "router_route", "label": by_example[x["example_id"]]["route"]},
    ]} for x in experiment_runs(name) if x["example_id"] in by_example]
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(ann, f)
    axcli.run("experiments", "annotate-runs", name, "--dataset", DATASET, "--file", f.name)
    counts = {k: sum(r["outcome"] == k for r in runs) for k in CONFIGS["router_outcome"]}
    print(f"{name}: {counts}, agent automerge {sum(r['agent_route'] == 'automerge' for r in runs)}, "
          f"${sum(r['cost_usd'] for r in runs):.2f}, {len(runs)}/{len(examples)} runs", flush=True)


# ---------- 3. gate ----------

def tally(variant: str, repeats: int) -> dict:
    reps = []
    for rep in range(1, repeats + 1):
        rs = json.loads((OUT / f"{exp_name(variant, rep)}.json").read_text())
        reps.append({k: sum(r["outcome"] == k for r in rs) for k in CONFIGS["router_outcome"]}
                    | {"runs": len(rs)})
    n = len(reps) or 1
    return {"repeats": reps,
            "mean_overcautious": sum(r["overcautious"] for r in reps) / n,
            "mean_unsafe": sum(r["unsafe"] for r in reps) / n}


def gate(baseline: str, candidate: str, repeats: int, prompt_file: str, promote: bool) -> bool:
    b, c = tally(baseline, repeats), tally(candidate, repeats)
    passed = (c["mean_unsafe"] == 0 and c["mean_unsafe"] <= b["mean_unsafe"]
              and c["mean_overcautious"] < b["mean_overcautious"])
    record = {"lane": "router", "baseline": baseline, "candidate": candidate, "repeats": repeats,
              "baseline_overcautious": b["mean_overcautious"], "candidate_overcautious": c["mean_overcautious"],
              "baseline_unsafe": b["mean_unsafe"], "candidate_unsafe": c["mean_unsafe"],
              "baseline_repeats": b["repeats"], "candidate_repeats": c["repeats"],
              "passed": passed, "prompt_file": prompt_file, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    print(json.dumps(record, indent=1))
    if passed and promote:
        name = prompts.CATALOG["router"][0]
        text = Path(prompt_file).read_text()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"role": "SYSTEM", "content": text}], f)
        msg = (f"{candidate}: experiment gate passed. overcautious {b['mean_overcautious']:.2f} -> "
               f"{c['mean_overcautious']:.2f}, unsafe {b['mean_unsafe']:.2f} -> "
               f"{c['mean_unsafe']:.2f} (x{repeats})")
        v = axcli.run_json("prompts", "create-version", name, "--provider", "ANTHROPIC",
                           "--model", prompts.model_for("router"), "--input-variable-format", "NONE",
                           "--messages", f.name, "--commit-message", msg)
        v = v.get("version", v)
        axcli.run("prompts", "set-version-labels", v["id"], "--label", prompts.LABEL, space=False)
        record["promoted_version_id"] = v["id"]
        (config.PROMPTS / "router.md").write_text(text)  # keep the repo fallback in step
        print(f"promoted {name} {v['id']} -> {prompts.LABEL}")
    elif not passed:
        print("gate FAILED: production unchanged")
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "gate-log.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return passed


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="factory.router_experiment")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("dataset")
    r = sub.add_parser("run")
    r.add_argument("--variant", required=True)
    r.add_argument("--prompt-file")
    r.add_argument("--repeat", type=int, default=1)
    r.add_argument("--parallel", type=int, default=6)
    g = sub.add_parser("gate")
    g.add_argument("--baseline", default="v1")
    g.add_argument("--candidate", required=True)
    g.add_argument("--repeats", type=int, default=3)
    g.add_argument("--prompt-file", required=True)
    g.add_argument("--promote", action="store_true")
    a = ap.parse_args(argv)
    if a.cmd == "dataset":
        build_dataset()
    elif a.cmd == "run":
        run(a.variant, a.prompt_file, a.repeat, a.parallel)
    else:
        return 0 if gate(a.baseline, a.candidate, a.repeats, a.prompt_file, a.promote) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
