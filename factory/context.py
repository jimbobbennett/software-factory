"""Per-issue run state, shared by every station.

A Ctx carries what a station needs (issue, spec, PR, worktree), accumulates cost and
loop counts, and gives each station the same three things: a dashboard event stream,
an Arize station span, and a tool-call ticker. It is saved to runs/state/<issue>.json
so a run parked at the human gate can be resumed later by `factory.run --watch`, in
the same Arize trace.
"""
from __future__ import annotations

import json
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterator

from . import config, tracing
from .events import STATIONS, emit


@dataclass
class Ctx:
    issue: dict
    attempt: int = 1
    trace_id: str = ""
    root_span_id: str = ""
    trace_url: str | None = None
    spec: dict | None = None
    pr: dict | None = None  # number, url, branch, head_sha
    worktree: str | None = None
    cost_usd: float = 0.0
    ci_runs: int = 0
    ci_fixes: int = 0
    reworks: int = 0
    tests_delta: int = 0
    reviews: dict = field(default_factory=dict)  # kind -> review result
    route: dict | None = None
    status: str = "running"  # running | awaiting_human | merged | released | failed | stopped
    gate_opened_at: str = ""
    outcome: str = ""
    started: float = field(default_factory=time.time)

    # ---------- identity ----------
    @property
    def number(self) -> int:
        return self.issue["number"]

    @property
    def run_id(self) -> str:
        return f"issue-{self.number}"

    @property
    def session_id(self) -> str:
        return f"{config.TARGET_REPO}#{self.number}"

    @property
    def wt(self) -> Path:
        if not self.worktree:
            raise RuntimeError("no worktree for this run yet")
        return Path(self.worktree)

    def base(self, station: str, **extra) -> dict:
        meta = {"issue": self.number, "repo": config.TARGET_REPO, "attempt": self.attempt,
                "run": self.run_id, "station": station}
        if self.pr:
            meta["pr"] = self.pr["number"]
            meta["head_sha"] = self.pr.get("head_sha")
        meta.update({k: v for k, v in extra.items() if v is not None})
        return meta

    # ---------- dashboard ----------
    def emit(self, station: str, status: str, detail: str = "", **extra) -> None:
        if self.pr:
            extra.setdefault("pr", self.pr["number"])
            extra.setdefault("pr_url", self.pr.get("url"))
        emit(self.run_id, station, status, detail, issue=self.number,
             trace_id=self.trace_id or None, **extra)

    def ticker(self, station: str) -> Callable[[str], None]:
        """Callback for run_agent(on_tool=...): streams each tool call to the dashboard."""
        return lambda label: self.emit(station, "info", label, tool=label.split(" ", 1)[0])

    # ---------- tracing ----------
    @contextmanager
    def station(self, name: str, *, detail: str = "", input: Any = None,
                model: str | None = None, emit_start: bool = True, **meta) -> Iterator[Any]:
        """Station span + start event. Emits a fail event if the body raises."""
        if emit_start:
            self.emit(name, "start", detail)
        md = self.base(name, model=model, **meta)
        with tracing.span(f"station.{name}", session_id=self.session_id, metadata=md,
                          input=input) as s:
            try:
                yield s
            except Exception as e:
                s.set_attribute("factory.error", str(e)[:500])
                if name in STATIONS:
                    self.emit(name, "fail", f"error: {str(e)[:160]}")
                raise

    def agent_meta(self, station: str, model: str) -> dict:
        return self.base(station, model=model)

    # ---------- persistence ----------
    def save(self) -> None:
        config.STATE_DIR.mkdir(parents=True, exist_ok=True)
        path = config.STATE_DIR / f"{self.number}.json"
        path.write_text(json.dumps(asdict(self), indent=2, default=str), encoding="utf-8")

    @classmethod
    def load(cls, number: int) -> "Ctx":
        data = json.loads((config.STATE_DIR / f"{number}.json").read_text(encoding="utf-8"))
        return cls(**data)

    @staticmethod
    def parked() -> list[int]:
        if not config.STATE_DIR.exists():
            return []
        out = []
        for p in sorted(config.STATE_DIR.glob("*.json")):
            if json.loads(p.read_text(encoding="utf-8")).get("status") == "awaiting_human":
                out.append(int(p.stem))
        return out


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
