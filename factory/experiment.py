"""Reviewer improvement loop: dataset -> experiment -> judge -> promote.

1. `dataset`  Turn every reference.review span in AX into a dataset example: the diff,
              spec and head commit the Claude reviewers saw, plus the review gate's
              verdict and findings (the ground truth).
2. `run`      Re-run one reviewer lane on every example with a given prompt (the live
              Prompt Hub `production` version, or a candidate file), and log the outputs
              as an AX experiment. The reviewer is the real agent: Opus, same tools,
              same read-only checkout of the PR at the commit it originally reviewed.
3. `score`    Score experiments with the same LLM judge the continuous eval task uses
              (as an AX evaluation task), plus a code eval for false alarms.
4. `compare`  Print the miss / false-alarm table per experiment.

    python -m factory.experiment dataset
    python -m factory.experiment run --lane correctness --variant v1
    python -m factory.experiment run --lane correctness --variant v2 \
        --prompt-file factory/prompts/candidates/review_correctness_v2.md
    python -m factory.experiment score --lane correctness
    python -m factory.experiment compare --lane correctness

Experiment agents are not traced into the production project, so they don't show up
as factory runs or get picked up by the continuous eval task.
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

from . import axcli, config, gh, prompts
from .agent import run_agent
from .stations import review as review_station

log = logging.getLogger("factory.experiment")

DATASET = "factory-reviewer-gate-disagreements"
OUT = config.ROOT / "runs" / "experiments"
MISS_JUDGES = {  # same evaluators the continuous task runs (judges/task.json)
    "correctness": "RXZhbHVhdG9yOjE3NzYyOmN2T0c=",
    "security": "RXZhbHVhdG9yOjE3NzYzOjFldW8=",
    "tests": "RXZhbHVhdG9yOjE3NzY0OjVVNzU=",
}


SUFFIX = ""  # set by --suffix so a second dataset's experiments get distinct names/files


def exp_name(lane: str, variant: str, rep: int = 1) -> str:
    """Repeat 1 keeps the bare name (the first experiment predates repeats)."""
    base = f"review-{lane}-{variant}{SUFFIX}"
    return base if rep == 1 else f"{base}-r{rep}"


def rep_names(lane: str, variant: str, repeats: int) -> list[str]:
    return [exp_name(lane, variant, r) for r in range(1, repeats + 1)]


# ---------- 1. dataset ----------

def reference_spans() -> list[dict]:
    with tempfile.TemporaryDirectory() as td:
        axcli.run("spans", "export", config.ARIZE_PROJECT_ID, "--filter", "name = 'reference.review'",
                  "-l", "500", "--output-dir", td, timeout=180)
        files = list(Path(td).rglob("*.json"))
        spans = json.loads(files[0].read_text()) if files else []
    latest: dict[int, dict] = {}
    for s in spans:  # one example per PR: the first commit the reviewers judged
        a = s["attributes"]
        pr = int(a["factory.pr"])
        if pr not in latest or s["start_time"] < latest[pr]["start_time"]:
            latest[pr] = s
    return [latest[k] for k in sorted(latest)]


def build_dataset(prs: list[int] | None = None) -> None:
    examples = []
    for s in reference_spans():
        if prs and int(s["attributes"]["factory.pr"]) not in prs:
            continue
        a = s["attributes"]
        examples.append({
            "pr": int(a["factory.pr"]),
            "issue": int(a["factory.issue"]),
            "head_sha": a["factory.head_sha"],
            "spec": a["factory.spec"],
            "diff": a["input.value"],
            "reference": a["output.value"],
            "reference_verdict": a["factory.reference_verdict"],
            "live_correctness": a.get("factory.reviewer.correctness", ""),
            "live_security": a.get("factory.reviewer.security", ""),
            "live_tests": a.get("factory.reviewer.tests", ""),
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


# ---------- 2. run ----------

def checkout(ex: dict) -> Path:
    clone = gh.ensure_clone()
    try:  # planted-bug examples are local commits: no PR ref to fetch
        gh.sh("git", "cat-file", "-e", f"{ex['head_sha']}^{{commit}}", cwd=clone)
    except Exception:  # noqa: BLE001
        gh.sh("git", "fetch", "-q", "origin", f"pull/{ex['pr']}/head", cwd=clone)
    return gh.detached_worktree(f"exp-pr-{ex['pr']}", ex["head_sha"])


async def review_one(ex: dict, wt: Path, lane: str, system_prompt: str) -> dict:
    title = ex["spec"].splitlines()[2] if ex["spec"].count("\n") > 2 else ""
    prompt = (
        f"Review PR #{ex.get('display_pr') or ex['pr']} for issue #{ex['issue']}. Head commit {ex['head_sha'][:7]}. "
        f"CI is green.\n\n## Spec\n\n{ex['spec']}\n\n## Diff against origin/main\n\n```diff\n{ex['diff']}\n```"
    )
    t0 = time.time()
    res = await run_agent(
        prompt=prompt, system_prompt=system_prompt, cwd=wt, model=config.REVIEW_MODEL,
        allowed_tools=review_station.TOOLS, session_id=f"experiment-{lane}-{ex['pr']}",
        metadata={"experiment": True, "pr": ex["pr"]}, output_schema=review_station.SCHEMA,
        max_turns=config.REVIEW_MAX_TURNS, max_budget_usd=config.REVIEW_BUDGET_USD,
    )
    if not isinstance(res.structured, dict):
        raise RuntimeError(f"PR #{ex['pr']}: no verdict ({res.subtype}) {title}")
    r = dict(res.structured)
    serious = [f for f in r["findings"] if f["severity"] in ("blocker", "major")]
    r["verdict"] = "request_changes" if serious else "approve"
    r["kind"], r["head_sha"] = lane, ex["head_sha"]
    return {"example_id": ex["id"], "pr": ex["pr"], "output": review_station.render(r),
            "verdict": r["verdict"], "cost_usd": res.cost_usd,
            "latency_ms": round((time.time() - t0) * 1000)}


async def run_all(examples: list[dict], lane: str, system_prompt: str, parallel: int) -> list[dict]:
    wts = {ex["pr"]: checkout(ex) for ex in examples}  # one clone: create serially
    sem = asyncio.Semaphore(parallel)

    async def one(ex):
        async with sem:
            for attempt in (1, 2):
                try:
                    r = await review_one(ex, wts[ex["pr"]], lane, system_prompt)
                    print(f"  PR #{r['pr']}: {r['verdict']} ${r['cost_usd']:.2f}", flush=True)
                    return r
                except Exception as e:  # noqa: BLE001 - retry once, then report
                    log.warning("PR #%s attempt %s failed: %s", ex["pr"], attempt, e)
            return None

    return [r for r in await asyncio.gather(*(one(ex) for ex in examples)) if r]


def run(lane: str, variant: str, prompt_file: str | None, parallel: int, prs: list[int] | None,
        rep: int = 1) -> None:
    key = f"review_{lane}"
    if prompt_file:
        text = config.prompt("review_common") + "\n" + Path(prompt_file).read_text()
        source = prompt_file
    else:
        p = prompts.get(key)
        text, source = p.text, f"{p.name}@production ({p.source} {p.version_id})"
    examples = [e for e in dataset_examples() if not prs or e["pr"] in prs]
    name = exp_name(lane, variant, rep)
    print(f"{name}: {len(examples)} examples, prompt {source} sha {prompts.sha(text)}")
    runs = asyncio.run(run_all(examples, lane, text, parallel))
    OUT.mkdir(parents=True, exist_ok=True)
    meta = {"prompt_source": source, "prompt_sha": prompts.sha(text), "model": config.REVIEW_MODEL,
            "variant": variant, "repeat": rep}
    for r in runs:
        r["metadata"] = {**meta, "pr": r["pr"], "verdict": r["verdict"],
                         "cost_usd": r["cost_usd"], "latency_ms": r["latency_ms"]}
    path = OUT / f"{name}.json"
    path.write_text(json.dumps(runs, indent=1))
    print(axcli.run("experiments", "create", "--name", name, "--dataset", DATASET,
                    "--file", str(path), timeout=180))
    print(f"total ${sum(r['cost_usd'] for r in runs):.2f}, {len(runs)}/{len(examples)} runs")


# ---------- 3. score ----------

def experiment_runs(name: str) -> list[dict]:
    rows = axcli.run_json("experiments", "export", name, "--dataset", DATASET, "--stdout", timeout=180)
    return rows.get("runs", rows) if isinstance(rows, dict) else rows


def experiment_id(name: str) -> str:
    return axcli.run_json("experiments", "get", name, "--dataset", DATASET)["id"]


def score(lane: str, variants: list[str], repeats: int = 1, skip: set[str] | None = None) -> None:
    """Score every repeat of every variant. `skip` = experiment names already judged."""
    examples = {e["id"]: e for e in dataset_examples()}
    ids = []
    names = [n for v in variants for n in rep_names(lane, v, repeats) if n not in (skip or set())]
    for name in names:
        ids.append(experiment_id(name))
        # code eval: false alarm = this lane asked for changes on a PR the gate shipped
        ann = []
        for r in experiment_runs(name):
            ex = examples[r["example_id"]]
            blocked = "request changes" in r["output"].splitlines()[0]
            fa = blocked and ex["reference_verdict"] == "ship"
            ann.append({"record_id": r["id"], "values": [
                {"name": "false_alarm", "label": "false_alarm" if fa else "ok"},
                {"name": "reviewer_verdict", "label": "request_changes" if blocked else "approve"},
            ]})
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(ann, f)
        print(axcli.run("experiments", "annotate-runs", name, "--dataset", DATASET, "--file", f.name))
    task = f"Reviewer miss judge: {lane} ({'/'.join(variants)} x{repeats}, {time.strftime('%m-%d %H:%M')})"
    evaluators = [{"evaluator_id": MISS_JUDGES[lane],
                   "column_mappings": {"reference": "reference", "review": "output"}}]
    out = axcli.run_json("tasks", "create", "--name", task, "--task-type", "TEMPLATE_EVALUATION",
                         "--dataset", DATASET, "--experiment-ids", ",".join(ids),
                         "--evaluators", json.dumps(evaluators))
    print(json.dumps(out, indent=1)[:800])
    # creating a task does not run it: trigger it on these experiments and wait
    run = axcli.run_json("tasks", "trigger-run", out["id"], "--experiment-ids", ",".join(ids),
                         "--wait", "--timeout", "900", space=False, timeout=960)
    print(f"judge run {run.get('status')}: {run.get('num_successes')} ok, {run.get('num_errors')} errors")


# ---------- 4. compare ----------

def _evals(r: dict) -> dict:
    """Flatten a run's annotations and eval.* columns into {name: label}."""
    out = {a["name"]: a.get("label") for a in r.get("annotations") or []}
    for k, v in (r.get("additional_properties") or {}).items():
        if k.startswith("eval.") and k.endswith(".label"):
            out[k[5:-6]] = v
    return out


def _label(ev, name):
    return (ev or {}).get(name)


def tally(lane: str, variants: list[str], repeats: int) -> dict:
    """Per variant: misses and false alarms per repeat, and per-PR miss counts."""
    examples = {e["id"]: e for e in dataset_examples()}
    judge = f"factory_miss_{lane}"
    out = {}
    for v in variants:
        reps, per_pr = [], {}
        for name in rep_names(lane, v, repeats):
            rs = experiment_runs(name)
            m = f = scored = 0
            for r in rs:
                ev = _evals(r)
                pr = examples[r["example_id"]]["pr"]
                row = per_pr.setdefault(pr, {"gate": examples[r["example_id"]]["reference_verdict"],
                                             "miss": 0, "fa": 0, "n": 0})
                row["n"] += 1
                if ev.get(judge) == "missed":
                    m += 1; row["miss"] += 1
                if ev.get("false_alarm") == "false_alarm":
                    f += 1; row["fa"] += 1
                scored += ev.get(judge) is not None
            reps.append({"experiment": name, "misses": m, "false_alarms": f,
                         "judged": scored, "runs": len(rs)})
        n = len(reps) or 1
        out[v] = {"repeats": reps, "per_pr": per_pr,
                  "mean_misses": sum(r["misses"] for r in reps) / n,
                  "mean_false_alarms": sum(r["false_alarms"] for r in reps) / n}
    return out


def compare(lane: str, variants: list[str], repeats: int = 1) -> dict:
    t = tally(lane, variants, repeats)
    prs = sorted({pr for v in t.values() for pr in v["per_pr"]})
    print(f"{'PR':>4}  {'gate':8}" + "".join(f"{v + ' miss/FA':>18}" for v in variants))
    for pr in prs:
        cells = []
        for v in variants:
            row = t[v]["per_pr"].get(pr)
            cells.append(f"{row['miss']}/{row['fa']} of {row['n']}" if row else "-")
        gate = next(t[v]["per_pr"][pr]["gate"] for v in variants if pr in t[v]["per_pr"])
        print(f"{pr:>4}  {gate:8}" + "".join(f"{c:>18}" for c in cells))
    for v in variants:
        reps = ", ".join(f"{r['misses']}m/{r['false_alarms']}fa ({r['judged']}/{r['runs']} judged)"
                         for r in t[v]["repeats"])
        print(f"{v}: mean misses {t[v]['mean_misses']:.2f}, mean false alarms "
              f"{t[v]['mean_false_alarms']:.2f}  [{reps}]")
    return t


# ---------- 5. optimize: candidate prompt from judge explanations ----------

OPTIMIZER_SYSTEM = """You improve the system prompt of an AI code-review agent, using evaluation
evidence. You get the reviewer's current lane instructions, and for every pull request where
an LLM judge said the reviewer MISSED a serious problem that the review gate found: the gate's
finding, the reviewer's own output, and the judge's explanation. You may also get earlier
candidate prompts that were tested and did not help, with their results.

Diagnose the failure pattern across the misses, then write replacement lane instructions.
Rules:
- Fix the pattern, not the examples. Never mention specific files, functions, PR numbers or
  this codebase. The prompt must generalise to new pull requests.
- Diagnose from the reviewer outputs. If a reviewer noticed a problem and still approved,
  that is a verdict-calibration failure, and asking it to "look harder" will not help.
- Keep false alarms down: the reviewer must still approve correct PRs. Don't make it
  block on style, speculation or out-of-lane issues.
- Keep the lane boundary: the other reviewers own security and test quality.
- Learn from failed candidates: say why they failed and avoid repeating that approach.
- Stay short. Under 350 words of instructions. Plain language.
Return the new lane instructions (they are appended after a shared preamble that defines
severities and the verdict rule), plus a short diagnosis."""

OPT_SCHEMA = {"type": "object", "required": ["diagnosis", "prompt"], "properties": {
    "diagnosis": {"type": "string"}, "prompt": {"type": "string"}}}


def optimize(lane: str, variants: list[str], repeats: int, failed: list[str], out: str) -> None:
    examples = {e["id"]: e for e in dataset_examples()}
    judge = f"factory_miss_{lane}"
    cases = []
    for v in variants:
        for name in rep_names(lane, v, repeats):
            for r in experiment_runs(name):
                ap = r.get("additional_properties") or {}
                if ap.get(f"eval.{judge}.label") != "missed":
                    continue
                ex = examples[r["example_id"]]
                cases.append(f"### PR (variant {v})\n\n**Review gate findings:**\n{ex['reference'][:3000]}"
                             f"\n\n**Reviewer output:**\n{r['output'][:3000]}"
                             f"\n\n**Judge explanation:**\n{ap.get(f'eval.{judge}.explanation', '')[:2000]}")
    current = prompts.get(f"review_{lane}").text.split("Your focus:", 1)[-1].strip()
    history = ""
    for f in failed:
        history += f"\n\n### Failed candidate {Path(f).name}\n\n{Path(f).read_text()}"
    t = tally(lane, variants, repeats)
    results = "\n".join(f"- {v}: mean misses {t[v]['mean_misses']:.2f}, false alarms "
                        f"{t[v]['mean_false_alarms']:.2f} over {len(t[v]['per_pr'])} PRs" for v in variants)
    prompt = (f"## Current lane instructions (production)\n\n{current}\n\n## Experiment results\n\n{results}"
              f"\n\n## Earlier candidates that did not help{history or ' (none)'}"
              f"\n\n## Misses ({len(cases)})\n\n" + "\n\n---\n\n".join(cases))
    print(f"optimizer input: {len(cases)} misses, {len(prompt)} chars")
    res = asyncio.run(run_agent(
        prompt=prompt, system_prompt=OPTIMIZER_SYSTEM, cwd=Path(tempfile.mkdtemp()),
        model=config.REVIEW_MODEL, allowed_tools=[], session_id=f"optimize-{lane}",
        metadata={"experiment": True, "optimizer": True}, output_schema=OPT_SCHEMA,
        max_turns=4, max_budget_usd=2.0))
    if not isinstance(res.structured, dict):
        raise SystemExit(f"optimizer returned no prompt ({res.subtype})")
    Path(out).write_text(res.structured["prompt"].strip() + "\n")
    Path(out).with_suffix(".diagnosis.md").write_text(res.structured["diagnosis"].strip() + "\n")
    print(f"wrote {out} (${res.cost_usd:.2f})\n\nDIAGNOSIS:\n{res.structured['diagnosis']}")


# ---------- 6. gate: promote only if the experiment passes ----------

def gate(lane: str, baseline: str, candidate: str, repeats: int, prompt_file: str, promote: bool) -> bool:
    """Regression test for a prompt change. Pass = mean misses strictly lower than the
    baseline AND mean false alarms no higher. Only a pass moves `production` in Prompt Hub."""
    t = tally(lane, [baseline, candidate], repeats)
    b, c = t[baseline], t[candidate]
    passed = c["mean_misses"] < b["mean_misses"] and c["mean_false_alarms"] <= b["mean_false_alarms"]
    record = {"lane": lane, "baseline": baseline, "candidate": candidate, "repeats": repeats,
              "baseline_misses": b["mean_misses"], "candidate_misses": c["mean_misses"],
              "baseline_false_alarms": b["mean_false_alarms"],
              "candidate_false_alarms": c["mean_false_alarms"], "passed": passed,
              "prompt_file": prompt_file, "at": time.strftime("%Y-%m-%dT%H:%M:%S%z")}
    print(json.dumps(record, indent=1))
    if passed and promote:
        name = prompts.CATALOG[f"review_{lane}"][0]
        text = config.prompt("review_common") + "\n" + Path(prompt_file).read_text()
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump([{"role": "SYSTEM", "content": text}], f)
        msg = (f"{candidate}: experiment gate passed. misses {b['mean_misses']:.2f} -> "
               f"{c['mean_misses']:.2f}, false alarms {b['mean_false_alarms']:.2f} -> "
               f"{c['mean_false_alarms']:.2f} (x{repeats})")
        v = axcli.run_json("prompts", "create-version", name, "--provider", "ANTHROPIC",
                           "--model", prompts.model_for(f"review_{lane}"),
                           "--input-variable-format", "NONE", "--messages", f.name,
                           "--commit-message", msg)
        v = v.get("version", v)
        axcli.run("prompts", "set-version-labels", v["id"], "--label", prompts.LABEL, space=False)
        record["promoted_version_id"] = v["id"]
        # keep the repo fallback in step with production
        (config.PROMPTS / f"review_{lane}.md").write_text(Path(prompt_file).read_text())
        print(f"promoted {name} {v['id']} -> {prompts.LABEL}")
    elif not passed:
        print("gate FAILED: production unchanged")
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "gate-log.jsonl").open("a") as fh:
        fh.write(json.dumps(record) + "\n")
    return passed


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    ap = argparse.ArgumentParser(prog="factory.experiment")
    ap.add_argument("--dataset", help="AX dataset name (default factory-reviewer-gate-disagreements)")
    ap.add_argument("--suffix", default="", help="experiment name suffix, e.g. -planted")
    sub = ap.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("dataset")
    d.add_argument("--prs", type=lambda s: [int(x) for x in s.split(",")])
    lanes = list(MISS_JUDGES)
    csv = lambda s: s.split(",")  # noqa: E731
    r = sub.add_parser("run")
    r.add_argument("--lane", default="correctness", choices=lanes)
    r.add_argument("--variant", required=True)
    r.add_argument("--prompt-file")
    r.add_argument("--parallel", type=int, default=3)
    r.add_argument("--prs", type=lambda s: [int(x) for x in s.split(",")])
    r.add_argument("--repeat", type=int, default=1, help="repeat index (1 = bare name)")
    for name in ("score", "compare"):
        p = sub.add_parser(name)
        p.add_argument("--lane", default="correctness", choices=lanes)
        p.add_argument("--variants", default="v1,v2", type=csv)
        p.add_argument("--repeats", type=int, default=1)
        p.add_argument("--skip", default="", type=lambda s: set(filter(None, s.split(","))))
    o = sub.add_parser("optimize")
    o.add_argument("--lane", default="correctness", choices=lanes)
    o.add_argument("--variants", default="v1", type=csv)
    o.add_argument("--repeats", type=int, default=1)
    o.add_argument("--failed", default="", type=lambda s: list(filter(None, s.split(","))))
    o.add_argument("--out", required=True)
    g = sub.add_parser("gate")
    g.add_argument("--lane", default="correctness", choices=lanes)
    g.add_argument("--baseline", default="v1")
    g.add_argument("--candidate", required=True)
    g.add_argument("--repeats", type=int, default=3)
    g.add_argument("--prompt-file", required=True)
    g.add_argument("--promote", action="store_true")
    a = ap.parse_args(argv)
    if a.dataset:
        globals()["DATASET"] = a.dataset
    if a.suffix:
        globals()["SUFFIX"] = a.suffix
    if a.cmd == "dataset":
        build_dataset(a.prs)
    elif a.cmd == "run":
        run(a.lane, a.variant, a.prompt_file, a.parallel, a.prs, a.repeat)
    elif a.cmd == "score":
        score(a.lane, a.variants, a.repeats, a.skip)
    elif a.cmd == "compare":
        compare(a.lane, a.variants, a.repeats)
    elif a.cmd == "optimize":
        optimize(a.lane, a.variants, a.repeats, a.failed, a.out)
    else:
        return 0 if gate(a.lane, a.baseline, a.candidate, a.repeats, a.prompt_file, a.promote) else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
