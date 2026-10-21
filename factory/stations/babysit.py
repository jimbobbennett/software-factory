"""Babysit station: fix a red CI run, or address a human's /changes, then push."""
from __future__ import annotations

from .. import config, gh, prompts
from ..agent import run_agent
from .build import TRAILER

SCHEMA = {
    "type": "object",
    "properties": {
        "fixed": {"type": "boolean"},
        "commit_message": {"type": "string"},
        "root_cause": {"type": "string"},
        "notes": {"type": "string"},
    },
    "required": ["fixed", "commit_message", "root_cause"],
}

TOOLS = [
    "Read", "Write", "Edit", "Glob", "Grep",
    "Bash(uv run:*)", "Bash(uv sync:*)",
    "Bash(git status:*)", "Bash(git diff:*)", "Bash(git log:*)", "Bash(ls:*)",
]


async def run(ctx, *, reason: str, evidence: str, metadata: dict, on_tool=None) -> dict:
    """reason is "ci", "human" or "conflict". Returns {fixed, pushed, head_sha, root_cause, cost_usd}."""
    branch = ctx.pr["branch"]
    wt = gh.worktree_for(branch)
    ctx.worktree = str(wt)

    if reason == "conflict":
        conflicts = gh.merge_main(wt)
        if not conflicts:
            sha = gh.push_head(wt, branch)
            ctx.pr["head_sha"] = sha
            return {"fixed": True, "pushed": True, "head_sha": sha, "cost_usd": 0.0,
                    "root_cause": "main moved on; merged origin/main cleanly", "notes": ""}
        evidence = (
            "main has moved on since this PR was opened, and `git merge origin/main` is now in "
            "progress in your worktree with conflicts in:\n" + "\n".join(conflicts) +
            "\n\nResolve every conflict marker so that BOTH the PR's change and the changes "
            "already on main survive. Do not drop either side's behaviour or tests. Then run CI "
            "locally. The factory will conclude the merge commit from your result.")

    gh.sh("uv", "sync", "-q", cwd=wt, check=False)

    what = {"ci": "CI failed on this PR. Failed-step log:",
            "human": "The human reviewer asked for changes on this PR:",
            "conflict": "This PR has merge conflicts with main:"}[reason]
    prompt = (
        f"PR #{ctx.pr['number']} for issue #{ctx.number} ({ctx.issue['title']}).\n\n"
        f"{what}\n\n```\n{evidence}\n```\n\nThe spec the PR implements:\n\n{ctx.spec['markdown']}"
    )
    prompt_ = prompts.use("babysit")
    res = await run_agent(
        prompt=prompt,
        system_prompt=prompt_.text,
        cwd=wt,
        model=config.BUILD_MODEL,
        allowed_tools=TOOLS,
        session_id=ctx.session_id,
        metadata={**metadata, **prompt_.meta},
        output_schema=SCHEMA,
        max_turns=config.BABYSIT_MAX_TURNS,
        max_budget_usd=config.BABYSIT_BUDGET_USD,
        on_tool=on_tool,
    )
    out = res.structured if isinstance(res.structured, dict) else {
        "fixed": False, "commit_message": "", "root_cause": f"no structured result ({res.subtype})"}
    out["cost_usd"] = res.cost_usd
    out["pushed"] = False
    if out.get("fixed") and gh.has_changes(wt):
        subject = "\n".join(l for l in out["commit_message"].strip().splitlines()
                            if not l.lower().startswith("co-authored-by:")).strip()
        subject = subject or {"ci": "fix: make CI green", "human": "fix: address review",
                              "conflict": "Merge origin/main"}[reason]
        sha = gh.push(wt, branch, f"{subject}\n\nRefs #{ctx.number}\n\n{TRAILER}\n")
        ctx.pr["head_sha"] = sha
        out["pushed"] = True
        out["head_sha"] = sha
    return out
