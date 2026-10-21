"""One helper to run a Claude agent for a station.

Two things are load-bearing (same as blog-agent-bot):
- call claude_agent_sdk.query through the module, so the instrumentor's rebinding
  is picked up and AGENT/TOOL spans are emitted;
- wrap the stream in aclosing(), so the instrumentor's span ends in the right
  OTel context when we break on ResultMessage.
"""
from __future__ import annotations

import shutil
from contextlib import aclosing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import claude_agent_sdk
from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultMessage,
    TextBlock,
    ToolUseBlock,
)
from openinference.instrumentation import using_attributes


@dataclass
class AgentResult:
    text: str
    structured: Any = None
    cost_usd: float = 0.0
    turns: int = 0
    is_error: bool = False
    subtype: str = ""
    usage: dict = field(default_factory=dict)


async def run_agent(
    *,
    prompt: str,
    system_prompt: str,
    cwd: Path,
    model: str,
    allowed_tools: list[str],
    disallowed_tools: list[str] | None = None,
    session_id: str,
    metadata: dict,
    output_schema: dict | None = None,
    max_turns: int = 30,
    max_budget_usd: float = 2.0,
    on_tool: Callable[[str], None] | None = None,
) -> AgentResult:
    """Run one agent to completion. on_tool gets a one-line summary of each tool call
    (for the live dashboard), e.g. "Read shipit/stats.py"."""
    options = ClaudeAgentOptions(
        cwd=str(cwd),
        system_prompt=system_prompt,
        model=model,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools or [],
        permission_mode="dontAsk",  # anything not allowlisted is denied, not prompted
        setting_sources=[],  # don't inherit Jim's user/project Claude settings
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        output_format=(
            {"type": "json_schema", "schema": output_schema} if output_schema else None
        ),
        max_buffer_size=10 * 1024 * 1024,
        cli_path=shutil.which("claude"),
    )
    parts: list[str] = []
    result = AgentResult(text="")
    with using_attributes(session_id=session_id, metadata=metadata):
        async with aclosing(claude_agent_sdk.query(prompt=prompt, options=options)) as stream:
            async for message in stream:
                if isinstance(message, AssistantMessage):
                    for b in message.content:
                        if isinstance(b, TextBlock):
                            parts.append(b.text)
                        elif isinstance(b, ToolUseBlock) and on_tool and b.name != "StructuredOutput":
                            on_tool(describe_tool(b.name, b.input or {}, cwd))
                elif isinstance(message, ResultMessage):
                    result.structured = message.structured_output
                    result.cost_usd = message.total_cost_usd or 0.0
                    result.turns = message.num_turns
                    result.is_error = message.is_error
                    result.subtype = message.subtype
                    result.usage = message.usage or {}
                    if message.result:
                        parts.append(message.result)
                    break
    result.text = "\n".join(p for p in parts if p).strip()
    return result


def describe_tool(name: str, args: dict, cwd: Path) -> str:
    """Short, human-readable label for a tool call."""
    def rel(p: str) -> str:
        try:
            return str(Path(p).resolve().relative_to(Path(cwd).resolve()))
        except (ValueError, OSError):
            return Path(p).name
    if "file_path" in args:
        target = rel(args["file_path"])
    elif name == "Bash":
        target = " ".join(str(args.get("command", "")).split())
    elif "pattern" in args:
        target = str(args["pattern"])
    else:
        target = ""
    label = f"{name} {target}".strip()
    return label if len(label) <= 80 else label[:77] + "..."
