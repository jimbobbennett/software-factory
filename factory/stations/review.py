"""Review station: three Opus reviewers (correctness, security, tests) in parallel.

Each returns a structured verdict. These verdicts are the thing the blog post
evaluates: later, the outcome harvester compares them to Jim's verdict on the same PR.
"""
from __future__ import annotations

from .. import config, prompts
from ..agent import run_agent

KINDS = ["correctness", "security", "tests"]

SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["approve", "request_changes"]},
        "confidence": {"type": "number"},
        "summary": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "severity": {"type": "string", "enum": ["blocker", "major", "minor", "nit"]},
                    "file": {"type": "string"},
                    "line": {"type": "integer"},
                    "title": {"type": "string"},
                    "body": {"type": "string"},
                },
                "required": ["severity", "file", "title", "body"],
            },
        },
    },
    "required": ["verdict", "confidence", "summary", "findings"],
}

TOOLS = ["Read", "Grep", "Glob", "Bash(git diff:*)", "Bash(git log:*)", "Bash(git show:*)"]
ICONS = {"blocker": "🛑", "major": "🔴", "minor": "🟡", "nit": "⚪"}


def system_prompt(kind: str) -> str:
    """Local fallback text; the live prompt comes from Prompt Hub (factory.prompts)."""
    return prompts.local_text(f"review_{kind}")


async def run_one(ctx, kind: str, diff: str, *, metadata: dict, on_tool=None) -> dict:
    prompt = (
        f"Review PR #{ctx.pr['number']} ({ctx.pr.get('title') or ctx.issue['title']}) for issue "
        f"#{ctx.number}. Head commit {ctx.pr['head_sha'][:7]}. CI is green.\n\n"
        f"## Spec\n\n{ctx.spec['markdown']}\n\n## Diff against origin/main\n\n```diff\n{diff}\n```"
    )
    prompt_ = prompts.use(f"review_{kind}")
    res = await run_agent(
        prompt=prompt,
        system_prompt=prompt_.text,
        cwd=ctx.wt,
        model=config.REVIEW_MODEL,
        allowed_tools=TOOLS,
        session_id=ctx.session_id,
        metadata={**metadata, **prompt_.meta},
        output_schema=SCHEMA,
        max_turns=config.REVIEW_MAX_TURNS,
        max_budget_usd=config.REVIEW_BUDGET_USD,
        on_tool=on_tool,
    )
    if not isinstance(res.structured, dict):
        raise RuntimeError(f"{kind} reviewer returned no verdict ({res.subtype}): {res.text[:200]}")
    out = dict(res.structured)
    # The verdict rule is mechanical, so enforce it rather than trust it.
    serious = [f for f in out["findings"] if f["severity"] in ("blocker", "major")]
    out["model_verdict"] = out["verdict"]
    out["verdict"] = "request_changes" if serious else "approve"
    out["kind"] = kind
    out["cost_usd"] = res.cost_usd
    out["head_sha"] = ctx.pr["head_sha"]
    return out


def render(r: dict) -> str:
    head = "✅ approve" if r["verdict"] == "approve" else "❌ request changes"
    lines = [f"### 🤖 {r['kind'].title()} reviewer: {head}",
             f"<sub>confidence {r['confidence']:.2f} · {config.REVIEW_MODEL} · "
             f"commit {r['head_sha'][:7]}</sub>", "", r["summary"], ""]
    for f in r["findings"]:
        loc = f"`{f['file']}" + (f":{f['line']}`" if f.get("line") else "`")
        lines += [f"- {ICONS.get(f['severity'], '')} **{f['severity']}** {loc}: **{f['title']}**",
                  f"  {f['body'].strip()}"]
    if not r["findings"]:
        lines.append("_No findings._")
    return "\n".join(lines)
