"""Replay a run with compressed pacing: long waits removed, tool ticks thinned."""
import json, re, sys, time, datetime as d
src, dst, run = sys.argv[1], sys.argv[2], sys.argv[3]
evs = [json.loads(l) for l in open(src) if f'"run": "{run}"' in l]
def clean(e):
    det = str(e.get("detail", ""))
    if e["station"] == "human" and e["status"] == "fail":
        det = "/changes: token echoed in an error; explicit token ignored"
    det = det.replace("Jim's review", "the review").replace("waiting for Jim", "waiting for review").replace("from Jim", "from the review gate")
    e["detail"] = det
    return e
# thin tool ticks: keep at most 4 per station visit (reviewers keep all, they run in parallel)
out, visit, cur = [], [], None
def flush():
    infos = [x for x in visit if x["status"] == "info"]
    keep = infos if (cur or "").startswith("review_") or False else infos[:: max(1, -(-len(infos) // 3))]
    out.extend(x for x in visit if x["status"] != "info" or x in keep)
for e in evs:
    key = e["station"]
    if e["status"] == "start" and not key.startswith("review_") or key != cur and not (key.startswith("review_") and (cur or "").startswith("review_")):
        flush(); visit = []; cur = key
    visit.append(e)
flush()
PACE = {"info": 0.35, "start": 0.6, "wait": 1.0, "ok": 1.0, "fail": 1.3}
open(dst, "w").close()
for e in out:
    e = clean(e); e["ts"] = d.datetime.now(d.timezone.utc).isoformat(timespec="milliseconds")
    with open(dst, "a") as f: f.write(json.dumps(e) + "\n")
    p = PACE.get(e["status"], 0.6)
    if e["station"].startswith("review_") and e["status"] in ("start", "info"): p = 0.3
    if e["station"] == "ci" and e["status"] == "start": p = 1.5   # CI pulse, minus the real 55 s
    time.sleep(p)
print("replay done", len(out), "events")
