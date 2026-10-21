"""Build station: spec -> code + tests in a worktree -> pushed branch -> PR."""
from __future__ import annotations

from .. import config, gh, prompts
from ..agent import run_agent

SCHEMA = {
    "type": "object",
    "properties": {
        "commit_message": {"type": "string"},
        "pr_title": {"type": "string"},
        "pr_body": {"type": "string"},
        "local_ci_passed": {"type": "boolean"},
        "tests_added": {"type": "array", "items": {"type": "string"}},
        "notes": {"type": "string"},
    },
    "required": ["commit_message", "pr_title", "pr_body", "local_ci_passed"],
}

TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(uv run:*)", "Bash(uv sync:*)", "Bash(uv add:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(ls:*)",
]

TRAILER = "Co-Authored-By: Claude <noreply@anthropic.com>"


async def run(issue: dict, spec: dict, *, run_id: str, trace_id: str,
              session_id: str, metadata: dict,
              on_tool=None) -> dict:
    branch = f"factory/issue-{issue['number']}"
    wt = gh.worktree(branch)
    gh.sh("uv", "sync", "-q", cwd=wt)

    prompt = (
        f"Implement this spec for issue #{issue['number']} ({issue['title']}).\n\n"
        f"{spec['markdown']}\n\nOriginal issue text:\n{issue.get('body') or '(none)'}"
    )
    prompt_ = prompts.use("build")
    res = await run_agent(
        prompt=prompt,
        system_prompt=prompt_.text,
        cwd=wt,
        model=config.BUILD_MODEL,
        allowed_tools=TOOLS,
        session_id=session_id,
        metadata={**metadata, **prompt_.meta},
        output_schema=SCHEMA,
        max_turns=config.BUILD_MAX_TURNS,
        max_budget_usd=config.BUILD_BUDGET_USD,
        on_tool=on_tool,
    )
    out = res.structured
    if not isinstance(out, dict):
        raise RuntimeError(f"builder returned no structured result ({res.subtype}): {res.text[:300]}")

    stat = gh.diff_stat(wt)
    if not stat:
        raise RuntimeError("builder made no changes")

    subject_body = "\n".join(
        line for line in out["commit_message"].strip().splitlines()
        if not line.lower().startswith("co-authored-by:")
    ).strip()
    message = f"{subject_body}\n\nRefs #{issue['number']}\n\n{TRAILER}\n"
    sha = gh.commit_and_push(wt, branch, message)

    body = (
        f"{out['pr_body'].strip()}\n\nCloses #{issue['number']}\n\n"
        f"<details><summary>Factory spec</summary>\n\n{spec['markdown']}\n\n</details>\n\n"
        f"<details><summary>Builder notes</summary>\n\n{out.get('notes') or '(none)'}\n\n"
        f"Local CI passed: {out['local_ci_passed']}\n\n</details>\n\n"
        f"<!-- factory run={run_id} trace_id={trace_id} -->\n"
        "🏭 Built by the software factory."
    )
    pr = gh.open_pr(branch, out["pr_title"], body, labels=["factory"])
    return {**out, **pr, "head_sha": sha, "diff_stat": stat, "branch": branch,
            "cost_usd": res.cost_usd, "turns": res.turns}
