"""Tiny Arize GraphQL client (API key from the factory .env; never printed)."""
import json, sys, urllib.request
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[3]))
from factory import config

def q(query, variables=None):
    req = urllib.request.Request("https://app.arize.com/graphql",
        data=json.dumps({"query": query, "variables": variables or {}}).encode(),
        headers={"Content-Type": "application/json", "x-api-key": config.ARIZE_API_KEY})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:2000])
    if r.get("errors"):
        raise RuntimeError(json.dumps(r["errors"])[:3000])
    return r["data"]

def type_info(name):
    return q("""query($n:String!){ __type(name:$n){ name kind
      inputFields{ name type{ kind name ofType{ kind name ofType{ kind name ofType{kind name}}}}}
      fields{ name args{name type{kind name ofType{kind name ofType{kind name}}}} type{ kind name ofType{ kind name ofType{kind name}}}}
      enumValues{name} } }""", {"n": name})["__type"]

def tname(t):
    s = ""
    while t:
        if t["kind"] == "NON_NULL": s += "!"
        elif t["kind"] == "LIST": s += "[]"
        else: return s + (t["name"] or "")
        t = t.get("ofType")
    return s

def show(name):
    t = type_info(name)
    if not t: print(name, "MISSING"); return
    print(f"== {name} ({t['kind']})")
    for f in t.get("inputFields") or []: print("  ", f["name"], tname(f["type"]))
    for f in t.get("fields") or []: print("  ", f["name"], tname(f["type"]), [a["name"] for a in f["args"]] or "")
    for e in t.get("enumValues") or []: print("   =", e["name"])

if __name__ == "__main__":
    for n in sys.argv[1:]: show(n)
