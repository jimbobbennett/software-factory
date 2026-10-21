"""Build the planted-bug held-out set: one known correctness bug per merged PR.

Each mutation is applied to the PR's final head in a throwaway worktree of the factory
clone, tests are weakened only where a test would otherwise catch the bug (so CI stays
green, as with the real misses), the full suite must pass, and the result is committed
to a local branch eval/planted-<pr>. Writes runs/planted/planted.json.

    .venv/bin/python runs/planted/plant.py [pr ...]
"""
import json, subprocess, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from factory import config, gh

HERE = Path(__file__).parent
M = json.loads((HERE / "mutations.json").read_text())


def sh(*a, cwd=None, check=True):
    r = subprocess.run(a, cwd=cwd, capture_output=True, text=True)
    if check and r.returncode:
        raise SystemExit(f"{' '.join(a)}\n{r.stdout[-3000:]}\n{r.stderr[-3000:]}")
    return r


def drop_test(src: str, name: str) -> str:
    """Remove a top-level test function (with its decorators) the PR added."""
    lines = src.split("\n")
    i = next(k for k, l in enumerate(lines) if l.startswith(f"def {name}("))
    start = i
    while start > 0 and lines[start - 1].startswith("@"):
        start -= 1
    end = i + 1
    while end < len(lines) and not (lines[end][:1] not in ("", " ", "\t", ")", "]")):
        end += 1
    while start > 0 and lines[start - 1] == "":
        start -= 1
    return "\n".join(lines[:start] + lines[end:]) if end < len(lines) else "\n".join(lines[:start]) + "\n"


def main(only):
    clone = gh.ensure_clone()
    out = json.loads((HERE / "planted.json").read_text()) if (HERE / "planted.json").exists() else {}
    for pr, m in M.items():
        if only and pr not in only:
            continue
        info = json.loads(sh("gh", "pr", "view", pr, "-R", config.TARGET_REPO, "--json",
                             "headRefOid,baseRefOid,mergeCommit").stdout)
        sh("git", "fetch", "-q", "origin", f"pull/{pr}/head", cwd=clone)
        wt = config.WORKTREES / f"plant-{pr}"
        if wt.exists():
            sh("git", "worktree", "remove", "--force", str(wt), cwd=clone)
        sh("git", "worktree", "add", "--detach", str(wt), info["headRefOid"], cwd=clone)
        for e in m["edits"]:
            p = wt / e["path"]
            t = p.read_text()
            if "drop" in e:
                p.write_text(drop_test(t, e["drop"]))
                continue
            if e.get("all") and e["old"] in t:
                p.write_text(t.replace(e["old"], e["new"]))
                continue
            if t.count(e["old"]) != 1:
                raise SystemExit(f"PR {pr}: {e['path']}: old text found {t.count(e['old'])}x:\n{e['old']}")
            p.write_text(t.replace(e["old"], e["new"]))
        r = sh("uv", "run", "--quiet", "pytest", "-q", "-p", "no:cacheprovider", cwd=wt, check=False)
        tail = (r.stdout.strip().splitlines() or ["?"])[-1]
        if r.returncode:
            print(f"PR {pr}: TESTS FAIL -> {tail}\n{r.stdout[-2500:]}")
            continue
        lint = sh("uv", "run", "--quiet", "ruff", "check", ".", cwd=wt, check=False)
        sh("git", "-c", "user.name=Jim Bennett", "-c", "user.email=jim@jimbobbennett.io",
           "commit", "-qam", m["commit"], cwd=wt)
        head = sh("git", "rev-parse", "HEAD", cwd=wt).stdout.strip()
        sh("git", "branch", "-f", f"eval/planted-{pr}", head, cwd=clone)
        diff = sh("git", "diff", f"{info['baseRefOid']}...{head}", cwd=clone).stdout
        out[pr] = {"pr": int(pr), "base": info["baseRefOid"], "orig_head": info["headRefOid"],
                   "head_sha": head, "diff": diff, "kind": m["kind"], "bug": m["bug"]}
        print(f"PR {pr}: planted {head[:7]}, tests {tail}, ruff {'ok' if not lint.returncode else 'WARN'}")
        sh("git", "worktree", "remove", "--force", str(wt), cwd=clone)
    (HERE / "planted.json").write_text(json.dumps(out, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
