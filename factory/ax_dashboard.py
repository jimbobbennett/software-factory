"""Create the "Software factory health" dashboard in Arize AX via GraphQL.

The ax CLI has no dashboards command, so this talks to the GraphQL API with the
factory's API key. It is create-only: if a dashboard with the same name already
exists it stops (pass --name to make another). It never edits or deletes anything.

Every dimension and value ID is looked up by name from the project's tracing schema,
so the script is re-runnable against a fresh project once the factory has sent spans.

    python -m factory.ax_dashboard
    python -m factory.ax_dashboard --name "Software factory health (v2)"
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

from . import config

GQL = "https://app.arize.com/graphql"
NAME = "Software factory health"
WINDOW = {"startTime": "2026-09-01T00:00:00Z", "endTime": "2027-12-31T00:00:00Z"}

# Arize 2026 palette (see blog-diagrams/arize_brand.py)
COLORS = ["#5B4BFF", "#FF6B4A", "#1F9D73", "#E5A200", "#14141F"]


def q(query: str, variables: dict | None = None) -> dict:
    req = urllib.request.Request(GQL, data=json.dumps({"query": query, "variables": variables or {}}).encode(),
                                 headers={"Content-Type": "application/json", "x-api-key": config.ARIZE_API_KEY})
    try:
        r = json.load(urllib.request.urlopen(req, timeout=60))
    except urllib.error.HTTPError as e:
        raise RuntimeError(e.read().decode()[:2000]) from None
    if r.get("errors"):
        raise RuntimeError(json.dumps(r["errors"])[:3000])
    return r["data"]


# ---------- schema lookups ----------

_SCHEMA_KIND = {"spanProperty": "spanProperties", "llmEval": "llmEvals", "annotation": "annotations"}


def dim(name: str, category: str = "spanProperty") -> dict:
    kind = _SCHEMA_KIND[category]
    data = q("""query($id:ID!,$t:String,$s:DateTime,$e:DateTime){node(id:$id){... on Model{
        tracingSchema(startTime:$s,endTime:$e){ %s(first:20,searchTerm:$t){edges{node{dimension{id name dataType category}}}}}}}}""" % kind,
             {"id": config.ARIZE_PROJECT_ID, "t": name, "s": WINDOW["startTime"], "e": WINDOW["endTime"]})
    for e in data["node"]["tracingSchema"][kind]["edges"]:
        d = e["node"]["dimension"]
        if d["name"] == name:
            return {**d, "category": category}
    raise KeyError(f"dimension {name} ({category}) not in project schema yet")


def values(d: dict) -> dict[str, str]:
    data = q("""query($id:ID!,$d:DimensionInput!,$ft:FilterRowType){node(id:$id){... on Model{
        dimensionValues(first:20,dimension:$d,modelEnvironmentName:tracing,filterType:$ft){edges{node{id value}}}}}}""",
             {"id": config.ARIZE_PROJECT_ID, "d": d, "ft": d["category"]})
    return {e["node"]["value"]: e["node"]["id"] for e in data["node"]["dimensionValues"]["edges"]}


def eq(d: dict, value: str) -> dict:
    """Filter row: dimension == value."""
    vals = values(d)
    if value not in vals:
        raise KeyError(f"{d['name']} has no value {value!r} (has {sorted(vals)})")
    return {"filterType": d["category"], "operator": "equals", "dimension": d,
            "dimensionValues": [{"id": vals[value], "value": value}]}


# ---------- widgets ----------

def plot(title: str, metric: str, d: dict, filters: list[dict], pos: int) -> dict:
    return {"modelId": config.ARIZE_PROJECT_ID, "modelVersionIds": [], "modelEnvironmentName": "tracing",
            "dimensionCategory": d["category"], "metric": metric, "dimension": d, "title": title,
            "position": pos, "filters": filters, "colors": [COLORS[pos % len(COLORS)]]}


def line(dash: str, title: str, grid: list[int], plots: list[dict]) -> str:
    out = q("""mutation($i:CreateLineChartWidgetMutationInput!){createLineChartWidget(input:$i){lineChartWidget{id}}}""",
            {"i": {"dashboardId": dash, "title": title, "gridPosition": grid, "timeSeriesMetricType": "modelDataMetric",
                   "creationStatus": "published", "widgetType": "lineChartWidget", "plots": plots, "showLegend": True,
                   "timeSeriesDataGranularity": "hour", "timeRangeKey": "7d", "overwriteGlobalTime": False}})
    return out["createLineChartWidget"]["lineChartWidget"]["id"]


def text(dash: str, title: str, grid: list[int], content: str) -> None:
    q("""mutation($i:CreateTextWidgetMutationInput!){createTextWidget(input:$i){clientMutationId}}""",
      {"i": {"dashboardId": dash, "title": title, "gridPosition": grid, "content": content, "creationStatus": "published"}})


def existing(name: str) -> str | None:
    data = q("""query($id:ID!){node(id:$id){... on Space{dashboards(first:100){edges{node{id name}}}}}}""",
             {"id": config.ARIZE_SPACE_ID})
    for e in data["node"]["dashboards"]["edges"]:
        if e["node"]["name"] == name:
            return e["node"]["id"]
    return None


def url(dash_id: str) -> str:
    return (f"https://app.arize.com/organizations/{config.ARIZE_ORG_ID}/spaces/{config.ARIZE_SPACE_ID}"
            f"/dashboards/{dash_id}") if getattr(config, "ARIZE_ORG_ID", "") else dash_id


def build(name: str, into: str | None = None) -> str:
    """Create the dashboard and its widgets. `into` = fill an existing (empty, ours) dashboard
    instead, e.g. after a partial run; it only ever adds widgets."""
    if not into and (found := existing(name)):
        print(f"dashboard {name!r} already exists ({found}); not touching it. Use --name for a new one.")
        return found

    span_name = dim("name")
    run = eq(span_name, "factory.run")
    ref = eq(span_name, "reference.review")
    route = dim("attributes.factory.route")
    cost = dim("attributes.factory.cost_usd")
    ci_fixes = dim("attributes.factory.ci_fixes")
    ci_first = dim("annotation.factory_ci_first_pass.label", "annotation")
    agree = dim("annotation.factory_reviewer_agreement.label", "annotation")
    kind = dim("attributes.openinference.span.kind")
    llm_cost = dim("attributes.llm.cost.total")
    spec = dim("eval.factory_spec_adherence.label", "llmEval")

    if into:
        dash = into
    else:
        dash = q("""mutation($i:CreateDashboardMutationInput!){createDashboard(input:$i){dashboard{id}}}""",
                 {"i": {"name": name, "spaceId": config.ARIZE_SPACE_ID}})["createDashboard"]["dashboard"]["id"]
        print(f"created dashboard {dash}")

    def row(r: int, left: bool) -> list[int]:  # [rowStart, colStart, rowEnd, colEnd], 12 columns
        return [r, 1, r + 4, 7] if left else [r, 7, r + 4, 13]

    if not into:
        text(dash, "How to read this", [1, 1, 3, 13],
         "One factory.run trace per GitHub issue. Gate verdicts (CI, the review gate, merges, reverts) are written "
         "back as span annotations by factory/harvest.py; LLM judges score every reference.review span. "
         "Misses = the AI reviewer approved what the review gate sent back. False alarms = it blocked what the gate shipped.")

    made = []
    made.append(line(dash, "Runs by route: auto-merge vs review gate", row(3, True), [
        plot("auto-merge", "count", route, [run, eq(route, "automerge")], 0),
        plot("review gate", "count", route, [run, eq(route, "human")], 1),
    ]))
    # harvester labels are categorical with no numeric score on the span, so count passes against all runs
    made.append(line(dash, "CI: first-pass vs all runs, babysit fixes", row(3, False), [
        plot("all runs", "count", span_name, [run], 0),
        plot("CI green first time", "count", ci_first, [run, eq(ci_first, "pass")], 1),
        plot("babysit CI fixes (sum)", "sum", ci_fixes, [run], 2),
    ]))
    # miss judges score missed = 1, no_miss = 0, so the average is the miss rate (works at zero misses)
    lanes = [plot(f"{lane} miss rate", "avg", dim(f"eval.factory_miss_{lane}.score", "llmEval"), [ref], i)
             for i, lane in enumerate(("correctness", "security", "tests"))]
    made.append(line(dash, "Reviewer miss rate by lane (LLM judge)", row(7, True), lanes))
    have = values(agree)
    made.append(line(dash, "Reviewer verdicts vs the review gate", row(7, False), [
        plot(v.replace("_", " "), "count", agree, [eq(agree, v)], i)
        for i, v in enumerate(("miss", "false_alarm", "agree")) if v in have
    ]))
    made.append(line(dash, "Cost (USD)", row(11, True), [
        plot("factory cost per run (sum)", "sum", cost, [run], 0),
        plot("agent LLM cost (sum)", "sum", llm_cost, [eq(kind, "AGENT")], 1),
    ]))
    spec_vals = [v for v in values(spec) if v not in ("No Value", "NULL", '"" (empty string)')]
    made.append(line(dash, "Spec adherence (LLM judge)", row(11, False), [
        plot(v, "count", spec, [ref, eq(spec, v)], i) for i, v in enumerate(sorted(spec_vals))
    ]))
    print(f"{len(made)} line widgets + 1 text widget")
    return dash


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--name", default=NAME)
    ap.add_argument("--into", help="add the line widgets to this existing dashboard ID (skips the text widget)")
    a = ap.parse_args(argv)
    dash = build(a.name, a.into)
    print(url(dash))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
