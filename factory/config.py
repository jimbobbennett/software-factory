"""Factory configuration, read from .env (which wins over the shell, like the bot)."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env", override=True)

ARIZE_SPACE_ID = os.getenv("ARIZE_SPACE_ID", "")
ARIZE_API_KEY = os.getenv("ARIZE_API_KEY", "")
ARIZE_PROJECT_NAME = os.getenv("ARIZE_PROJECT_NAME") or "software-factory"
# Only used to build UI deep links for the dashboard.
ARIZE_ORG_ID = os.getenv("ARIZE_ORG_ID", "")
ARIZE_PROJECT_ID = os.getenv("ARIZE_PROJECT_ID", "")

TARGET_REPO = os.getenv("FACTORY_TARGET_REPO", "jimbobbennett/ship-it-card")
WORKDIR = (ROOT / os.getenv("FACTORY_WORKDIR", "work")).resolve()
CLONE_DIR = WORKDIR / TARGET_REPO.split("/")[-1]
WORKTREES = WORKDIR / "worktrees"

BUILD_MODEL = os.getenv("FACTORY_BUILD_MODEL", "sonnet")
REVIEW_MODEL = os.getenv("FACTORY_REVIEW_MODEL", "opus")

# Hard stops per station so a confused agent can't run up a bill.
SPEC_BUDGET_USD = float(os.getenv("FACTORY_SPEC_BUDGET_USD", "1.0"))
BUILD_BUDGET_USD = float(os.getenv("FACTORY_BUILD_BUDGET_USD", "4.0"))
SPEC_MAX_TURNS = int(os.getenv("FACTORY_SPEC_MAX_TURNS", "25"))
BUILD_MAX_TURNS = int(os.getenv("FACTORY_BUILD_MAX_TURNS", "80"))
BABYSIT_BUDGET_USD = float(os.getenv("FACTORY_BABYSIT_BUDGET_USD", "2.0"))
BABYSIT_MAX_TURNS = int(os.getenv("FACTORY_BABYSIT_MAX_TURNS", "50"))
REVIEW_BUDGET_USD = float(os.getenv("FACTORY_REVIEW_BUDGET_USD", "3.0"))  # per reviewer
REVIEW_MAX_TURNS = int(os.getenv("FACTORY_REVIEW_MAX_TURNS", "30"))
ROUTER_BUDGET_USD = float(os.getenv("FACTORY_ROUTER_BUDGET_USD", "0.5"))
RELEASE_BUDGET_USD = float(os.getenv("FACTORY_RELEASE_BUDGET_USD", "1.0"))
RELEASE_MAX_TURNS = int(os.getenv("FACTORY_RELEASE_MAX_TURNS", "25"))

# Loop limits and gates.
MAX_CI_FIXES = int(os.getenv("FACTORY_MAX_CI_FIXES", "3"))
MAX_REWORKS = int(os.getenv("FACTORY_MAX_REWORKS", "2"))
CI_TIMEOUT_S = int(os.getenv("FACTORY_CI_TIMEOUT_S", "900"))
# How long a run waits in-process for Jim's /ship or /changes before handing off to
# `python -m factory.run --watch`. 0 = hand off immediately.
HUMAN_WAIT_S = int(os.getenv("FACTORY_HUMAN_WAIT_S", "0"))
POLL_S = int(os.getenv("FACTORY_POLL_S", "15"))
# Rules floor for the router: these always go to a human.
BIG_DIFF_LINES = int(os.getenv("FACTORY_BIG_DIFF_LINES", "400"))
SENSITIVE_PATHS = [p for p in os.getenv(
    "FACTORY_SENSITIVE_PATHS", ".github/,pyproject.toml,uv.lock,shipit/github.py").split(",") if p]
# A public PR the release smoke test renders a card for (no Gemini, no token needed).
SMOKE_PR_URL = os.getenv("FACTORY_SMOKE_PR_URL", "https://github.com/Arize-ai/phoenix/pull/17062")
STATE_DIR = ROOT / "runs" / "state"

PROMPTS = ROOT / "factory" / "prompts"


def prompt(name: str) -> str:
    """Local prompt file. Stations use factory.prompts (Prompt Hub, falling back to these)."""
    return (PROMPTS / f"{name}.md").read_text(encoding="utf-8")
