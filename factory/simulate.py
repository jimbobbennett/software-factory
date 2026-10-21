"""Fake factory traffic for the dashboard, matching the real event flow in factory/run.py.

    python factory/simulate.py --reset --issues 6 --speed 1.5

Plays out runs with tool-call ticks, CI failures (babysit loops), parallel reviewers,
router decisions, the human gate (/ship and /changes rework) and releases.
"""
from __future__ import annotations

import argparse
import random
import threading
import time

from events import EVENTS_PATH, emit

ISSUES = [
    "Repo cards: a card for a whole repository",
    "Badge endpoint: embed your ship score in a README",
    "Show AI co-authors with a robot crest",
    "Open Graph tags so cards unfurl on social",
    "More varied art styles per card type",
    "CLI: --json output",
    "Card dates use a timezone-naive fallback",
    "Cache GitHub API responses",
]
TOOLS = {
    "spec": ["Read README.md", "Glob shipit/**/*.py", "Read shipit/stats.py", "Grep ship_score", "Read tests/test_stats.py"],
    "build": ["Read shipit/card.py", "Edit shipit/card.py", "Edit tests/test_card.py", "Bash uv run ruff check .",
              "Bash uv run ruff format .", "Bash uv run pytest -q", "Bash git diff --stat"],
    "babysit": ["Read tests/test_card.py", "Grep assert", "Edit shipit/card.py", "Bash uv run pytest -q"],
    "review_correctness": ["Bash git diff origin/main...HEAD", "Read shipit/card.py", "Grep def render", "Read shipit/core.py"],
    "review_security": ["Read shipit/github.py", "Grep requests.get", "Read shipit/web.py", "Grep escape"],
    "review_tests": ["Bash git diff origin/main...HEAD -- tests", "Read tests/test_card.py", "Grep def test_"],
    "router": [],
    "release": ["Bash uv run pytest -q", "Bash uv run python -m shipit https://github.com/Arize-ai/phoenix/pull/17062 --no-gemini",
                "Bash uv run python -c \"import shipit.web\"", "Bash git log --oneline -5"],
}
_version = [0, 1, 0]
_vlock = threading.Lock()


def run_issue(n: int, title: str, speed: float, rng: random.Random):
    run = f"issue-{n}"
    pr = 100 + n
    pr_url = f"https://github.com/jimbobbennett/ship-it-card/pull/{pr}"
    sleep = lambda lo, hi: time.sleep(rng.uniform(lo, hi) / speed)
    has_pr = False

    def ev(station, status, detail="", **kw):
        if has_pr:
            kw.setdefault("pr", pr)
            kw.setdefault("pr_url", pr_url)
        emit(run, station, status, detail, issue=n, **kw)

    def work(station, lo, hi):
        tools = TOOLS.get(station, [])
        k = rng.randint(max(1, len(tools) - 2), len(tools)) if tools else 0
        steps = rng.sample(tools, k) if tools else []
        total = rng.uniform(lo, hi)
        for t in steps:
            time.sleep(total / max(1, len(steps)) / speed)
            ev(station, "info", t, tool=t.split(" ")[0])
        if not steps:
            time.sleep(total / speed)

    ev("issue", "ok", f"#{n} picked up", title=title)
    sleep(0.5, 1)
    ev("spec", "start", "writing acceptance criteria")
    work("spec", 3, 5)
    risk = rng.choice(["low", "low", "medium", "high"])
    ev("spec", "ok", f"{rng.randint(3, 7)} criteria, risk {risk}")
    ev("build", "start", "implementing in a worktree")
    work("build", 6, 10)
    if rng.random() < 0.05:
        ev("build", "fail", "error: builder hit its budget")
        ev("done", "info", "failed: builder hit its budget")
        return
    has_pr = True
    ev("build", "ok", f"PR #{pr} opened")

    def ci_loop() -> bool:
        for attempt in range(1, 5):
            ev("ci", "start", f"run {attempt}")
            sleep(3, 5)
            if rng.random() < (0.35 if attempt == 1 else 0.15) and attempt < 4:
                ev("ci", "fail", rng.choice(["test: failure", "ruff: E501", "pytest: 2 failed"]))
                ev("babysit", "start", "reading CI logs")
                work("babysit", 3, 6)
                ev("babysit", "ok", f"pushed {rng.randbytes(4).hex()[:7]}: fixed the failing assert")
                continue
            ev("ci", "ok", "1 check green")
            return True
        ev("done", "info", "CI still red after 3 fixes")
        return False

    if not ci_loop():
        return

    reviewers = ["review_correctness", "review_security", "review_tests"]
    verdicts = {}
    for rv in reviewers:
        ev(rv, "start", rv.split("_")[1] + " review")

    def review(rv):
        work(rv, 4, 9)
        bad = rng.random() < 0.2
        verdicts[rv] = not bad
        nf = rng.randint(1, 3) if bad else rng.randint(0, 2)
        ev(rv, "fail" if bad else "ok", f"{'request changes' if bad else 'approve'} · {nf} findings",
           verdict="request_changes" if bad else "approve")

    threads = [threading.Thread(target=review, args=(rv,)) for rv in reviewers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    ev("router", "start", "weighing the evidence")
    sleep(1.5, 3)
    human = not all(verdicts.values()) or risk == "high" or rng.random() < 0.15
    ev("router", "ok", "→ human: reviewer requested changes" if human else "→ auto-merge: all clear",
       route="human" if human else "automerge")

    if not human:
        ev("automerge", "start", "squash-merging")
        sleep(1, 2)
        ev("automerge", "ok", f"merged as {rng.randbytes(4).hex()[:7]}")
    else:
        for rework in range(3):
            ev("human", "wait", "waiting for Jim: /ship or /changes")
            sleep(6, 12)
            if rework < 2 and rng.random() < 0.4:
                ev("human", "fail", "/changes: escape the PR title before rendering")
                ev("babysit", "start", "addressing Jim's review")
                work("babysit", 3, 6)
                ev("babysit", "ok", f"pushed {rng.randbytes(4).hex()[:7]}: escaped title")
                if not ci_loop():
                    return
                continue
            if rng.random() < 0.1:
                ev("human", "fail", "closed by Jim")
                ev("done", "info", "closed by human")
                return
            ev("human", "ok", "/ship from Jim")
            break

    ev("release", "start", "smoke test + release notes")
    work("release", 3, 5)
    with _vlock:
        _version[1] += 1
        v = f"v{_version[0]}.{_version[1]}.{_version[2]}"
    ev("release", "ok", f"{v} published", version=v,
       release_url=f"https://github.com/jimbobbennett/ship-it-card/releases/tag/{v}")
    ev("done", "info", f"merged + released {v}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--issues", type=int, default=6)
    ap.add_argument("--speed", type=float, default=1.0, help=">1 is faster")
    ap.add_argument("--stagger", type=float, default=5.0, help="seconds between new issues")
    ap.add_argument("--reset", action="store_true", help="truncate the events file first")
    ap.add_argument("--seed", type=int)
    args = ap.parse_args()
    if args.reset and EVENTS_PATH.exists():
        EVENTS_PATH.write_text("")
    rng = random.Random(args.seed)
    threads = []
    for i, title in enumerate(ISSUES[: args.issues]):
        t = threading.Thread(target=run_issue, args=(i + 1, title, args.speed, random.Random(rng.random())))
        t.start()
        threads.append(t)
        time.sleep(args.stagger / args.speed)
    for t in threads:
        t.join()


if __name__ == "__main__":
    main()
