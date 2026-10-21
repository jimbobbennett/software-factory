"""Spec station: issue -> acceptance criteria, posted back to the issue."""
from __future__ import annotations

from .. import config, gh, prompts
from ..agent import run_agent

SCHEMA = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "acceptance_criteria": {"type": "array", "items": {"type": "string"}},
        "assumptions": {"type": "array", "items": {"type": "string"}},
        "out_of_scope": {"type": "array", "items": {"type": "string"}},
        "files_likely_touched": {"type": "array", "items": {"type": "string"}},
        "questions": {"type": "array", "items": {"type": "string"}},
        "risk": {"type": "string", "enum": ["low", "medium", "high"]},
        "risk_reason": {"type": "string"},
    },
    "required": ["summary", "acceptance_criteria", "risk", "risk_reason"],
}


def render(spec: dict) -> str:
    def section(title: str, items: list[str] | None) -> list[str]:
        return [f"**{title}**", *[f"- {i}" for i in items], ""] if items else []

    lines = ["### 🏭 Factory spec", "", spec["summary"], ""]
    lines += section("Acceptance criteria", [f"[ ] {c}" for c in spec["acceptance_criteria"]])
    lines += section("Assumptions", spec.get("assumptions"))
    lines += section("Out of scope", spec.get("out_of_scope"))
    lines += section("Likely files", [f"`{f}`" for f in spec.get("files_likely_touched", [])])
    lines += section("Questions for a human", spec.get("questions"))
    lines += [f"**Risk:** {spec['risk']} ({spec['risk_reason']})"]
    return "\n".join(lines)


async def run(issue: dict, *, session_id: str, metadata: dict, on_tool=None) -> dict:
    clone = gh.ensure_clone()
    prompt = (
        f"Issue #{issue['number']}: {issue['title']}\n\n{issue.get('body') or '(no body)'}\n\n"
        "Write the spec for this issue."
    )
    prompt_ = prompts.use("spec")
    res = await run_agent(
        prompt=prompt,
        system_prompt=prompt_.text,
        cwd=clone,
        model=config.BUILD_MODEL,
        allowed_tools=["Read", "Grep", "Glob"],
        session_id=session_id,
        metadata={**metadata, **prompt_.meta},
        output_schema=SCHEMA,
        max_turns=config.SPEC_MAX_TURNS,
        max_budget_usd=config.SPEC_BUDGET_USD,
        on_tool=on_tool,
    )
    if not isinstance(res.structured, dict):
        raise RuntimeError(f"spec agent returned no structured spec ({res.subtype}): {res.text[:300]}")
    spec = res.structured
    spec["markdown"] = render(spec)
    spec["cost_usd"] = res.cost_usd
    gh.comment_issue(issue["number"], spec["markdown"])
    return spec
