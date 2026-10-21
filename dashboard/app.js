// Software Factory live dashboard.
// Tails /events (SSE over runs/events.jsonl) and animates each PR through the stations.
const SVGNS = "http://www.w3.org/2000/svg";
const W = 168, H = 92;  // node size
const VB_X = 0, VB_Y = 40, VB_W = 1720, VB_H = 570;

const NODES = {
  issue:              { x: 112,  y: 300, label: "Issue",       sub: "GitHub",              model: "" },
  spec:               { x: 326,  y: 300, label: "Spec",        sub: "acceptance criteria", model: "sonnet" },
  build:              { x: 540,  y: 300, label: "Build",       sub: "code + tests + PR",   model: "sonnet" },
  ci:                 { x: 754,  y: 300, label: "CI",          sub: "GitHub Actions",      model: "" },
  babysit:            { x: 754,  y: 500, label: "Babysit",     sub: "fix until green",     model: "sonnet" },
  review_correctness: { x: 968,  y: 150, label: "Correctness", sub: "reviewer",            model: "opus" },
  review_security:    { x: 968,  y: 300, label: "Security",    sub: "reviewer",            model: "opus" },
  review_tests:       { x: 968,  y: 450, label: "Tests",       sub: "reviewer",            model: "opus" },
  router:             { x: 1182, y: 300, label: "Risk router", sub: "agent + rules",       model: "sonnet" },
  automerge:          { x: 1396, y: 205, label: "Auto-merge",  sub: "squash",              model: "" },
  human:              { x: 1396, y: 395, label: "Human",       sub: "/ship or /changes",   model: "" },
  release:            { x: 1608, y: 300, label: "Release",     sub: "smoke + tag",         model: "sonnet" },
};
const REVIEWERS = ["review_correctness", "review_security", "review_tests"];
const EDGES = [
  ["issue","spec"],["spec","build"],["build","ci"],
  ["ci","babysit","loopL"],["babysit","ci","loopR"],
  ["ci","review_correctness"],["ci","review_security"],["ci","review_tests"],
  ["review_correctness","router"],["review_security","router"],["review_tests","router"],
  ["router","automerge"],["router","human"],["automerge","release"],["human","release"],
  ["automerge","human","down"],["human","babysit","rework"],
];
const PALETTE = ["#0075B1","#4827C1","#C16400","#677300","#D2007A","#454554"];
const C = { lime: "#677300", danger: "#CF222E", amber: "#C16400", pink: "#FF3CA8", muted: "#73738C" };

const svg = document.getElementById("flow");
const el = (tag, attrs = {}, parent = svg) => {
  const e = document.createElementNS(SVGNS, tag);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  parent.appendChild(e); return e;
};
const clip = (s, n) => !s ? "" : s.length > n ? s.slice(0, n - 1) + "…" : s;

function edgePath(a, b, kind) {
  const A = NODES[a], B = NODES[b];
  if (kind === "loopL") return `M${A.x - 36},${A.y + H/2} C${A.x - 80},${A.y + 100} ${B.x - 80},${B.y - 100} ${B.x - 36},${B.y - H/2}`;
  if (kind === "loopR") return `M${A.x + 36},${A.y - H/2} C${A.x + 80},${A.y - 100} ${B.x + 80},${B.y + 100} ${B.x + 36},${B.y + H/2}`;
  if (kind === "down") return `M${A.x},${A.y + H/2} L${B.x},${B.y - H/2}`;
  if (kind === "rework") return `M${A.x},${A.y + H/2} C${A.x},${B.y + 70} ${B.x + W/2 + 220},${B.y + 70} ${B.x + W/2},${B.y + 14}`;
  const x1 = A.x + W/2, y1 = A.y, x2 = B.x - W/2, y2 = B.y, mx = (x1 + x2) / 2;
  return `M${x1},${y1} C${mx},${y1} ${mx},${y2} ${x2},${y2}`;
}

// ---------- static drawing ----------
el("defs").innerHTML = `<marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="rgba(19,22,64,.4)"/></marker>`;
svg.setAttribute("viewBox", `${VB_X} ${VB_Y} ${VB_W} ${VB_H}`);
{ const r = NODES.review_correctness, t = NODES.review_tests;
  el("rect", { class: "group-box", x: r.x - W/2 - 18, y: r.y - H/2 - 18, width: W + 36, height: t.y - r.y + H + 36, rx: 14 }); }
const edgeEls = {};
for (const [a, b, kind] of EDGES) {
  const dashed = kind === "down" || kind === "rework";
  edgeEls[`${a}>${b}`] = el("path", { class: "edge", d: edgePath(a, b, kind), "marker-end": "url(#arrow)",
    ...(dashed ? { "stroke-dasharray": "3 4" } : {}) });
}
const nodeEls = {};
for (const [id, n] of Object.entries(NODES)) {
  const g = el("g", { class: "node", transform: `translate(${n.x},${n.y})` });
  el("rect", { class: "ring", x: -W/2, y: -H/2, width: W, height: H, rx: 12 }, g);
  el("rect", { x: -W/2, y: -H/2, width: W, height: H, rx: 12 }, g);
  el("text", { class: "label", y: n.model ? -6 : 0 }, g).textContent = n.label;
  el("text", { class: "sub", y: n.model ? 15 : 22 }, g).textContent = n.sub;
  if (n.model) el("text", { class: "model", y: 34 }, g).textContent = n.model;
  const timer = el("text", { class: "timer", x: -W/2 + 9, y: -H/2 + 18 }, g);
  const ticker = el("text", { class: "ticker", y: -H/2 - 10 }, g);
  const badge = el("g", { transform: `translate(${W/2 - 6},${-H/2 + 6})`, visibility: "hidden" }, g);
  el("circle", { class: "badge", r: 13 }, badge);
  const count = el("text", { class: "count", y: 5 }, badge);
  nodeEls[id] = { g, badge, count, timer, ticker, since: null };
}
const tokenLayer = el("g"), fxLayer = el("g");

// ---------- state ----------
const runs = new Map();
const stats = { spans: 0, merged: 0, released: 0, ciFirst: 0, ciRuns: 0 };
let replaying = false;

function colorFor(run) {
  let h = 0; for (const c of run) h = (h * 31 + c.charCodeAt(0)) >>> 0;
  return PALETTE[h % PALETTE.length];
}

// ---------- animation helpers ----------
const ease = t => t < .5 ? 2*t*t : 1 - Math.pow(-2*t + 2, 2) / 2;

// Move a <g> through a list of points at constant speed.
function travel(g, points, dur, done) {
  const seg = []; let total = 0;
  for (let i = 1; i < points.length; i++) {
    const d = Math.hypot(points[i][0] - points[i-1][0], points[i][1] - points[i-1][1]);
    seg.push(d); total += d;
  }
  if (!total || replaying) { g.setAttribute("transform", `translate(${points.at(-1)})`); done && done(); return; }
  const t0 = performance.now();
  const step = now => {
    const t = Math.min(1, (now - t0) / dur);
    let d = ease(t) * total, i = 0;
    while (i < seg.length - 1 && d > seg[i]) { d -= seg[i]; i++; }
    const f = seg[i] ? d / seg[i] : 1, a = points[i], b = points[i + 1];
    g.setAttribute("transform", `translate(${a[0] + (b[0] - a[0]) * f},${a[1] + (b[1] - a[1]) * f})`);
    if (t < 1) requestAnimationFrame(step); else done && done();
  };
  requestAnimationFrame(step);
}

// Points along the drawn edge a→b (or a gentle arc if there is no edge).
function routePoints(a, b, from, to) {
  const e = edgeEls[`${a}>${b}`];
  if (!e) {
    const mid = [(from[0] + to[0]) / 2, Math.min(from[1], to[1]) - 50];
    return [from, mid, to];
  }
  const L = e.getTotalLength(), pts = [from];
  for (let i = 0; i <= 24; i++) { const p = e.getPointAtLength(L * i / 24); pts.push([p.x, p.y]); }
  pts.push(to);
  return pts;
}

function slot(run, station) {
  const here = [...runs.values()].filter(r => r.token && r.at === station && !r.finished);
  const i = Math.max(0, here.findIndex(r => r.run === run));
  const n = NODES[station];
  const col = i % 3, row = Math.floor(i / 3);
  return [n.x + (col - (Math.min(here.length, 3) - 1) / 2) * 56, n.y + H/2 + 22 + row * 32];
}

function tokenLabel(r) { return r.pr ? `PR ${r.pr}` : `#${r.issue ?? "?"}`; }

function ensureToken(r) {
  if (r.token) return;
  const g = el("g", { class: "token" }, tokenLayer);
  const bob = el("g", { class: "bob" }, g);
  r.tokRect = el("rect", { height: 28, y: -14, rx: 14, fill: r.color }, bob);
  r.tokText = el("text", {}, bob);
  r.token = g; r.bob = bob;
  r.at = "issue"; r.pos = slot(r.run, "issue");
  g.setAttribute("transform", `translate(${r.pos})`);
  relabel(r);
}

function relabel(r) {
  if (!r.token) return;
  const t = tokenLabel(r), w = 18 + t.length * 8.6;
  r.tokText.textContent = t;
  r.tokRect.setAttribute("width", w); r.tokRect.setAttribute("x", -w / 2);
}

function moveToken(r, station) {
  ensureToken(r);
  if (!NODES[station]) return;
  const from = r.pos, prev = r.at;
  r.at = station;
  const to = slot(r.run, station);
  r.pos = to;
  r.token.classList.toggle("waiting", station === "human" && r.status === "wait");
  if (prev === station) { travel(r.token, [from, to], 300); return; }
  travel(r.token, routePoints(prev, station, from, to), 1100);
}

// Reviewer fan-out: three mini tokens, one per reviewer.
function spawnMini(r, station) {
  r.minis = r.minis || {};
  if (r.minis[station]) return;
  const g = el("g", { class: "mini" }, tokenLayer);
  const c = el("circle", { r: 12, fill: r.color }, g);
  const t = el("text", {}, g); t.textContent = "";
  const n = NODES[station];
  const from = r.pos || [NODES.ci.x, NODES.ci.y + H/2 + 22];
  const to = [n.x + W/2 - 18, n.y + H/2 - 4];
  r.minis[station] = { g, c, t, pos: to };
  travel(g, routePoints("ci", station, from, to), 1000);
}

function settleMini(r, station, ok) {
  const m = r.minis && r.minis[station]; if (!m) return;
  m.c.setAttribute("fill", ok ? C.lime : C.danger);
  m.t.textContent = ok ? "✓" : "✗";
}

function collectMinis(r) {
  if (!r.minis) return;
  const to = slot(r.run, "router");
  for (const [station, m] of Object.entries(r.minis)) {
    travel(m.g, routePoints(station, "router", m.pos, to), 900, () => m.g.remove());
  }
  r.minis = null;
}

function flashEdge(a, b, cls = "flow", ms = 1300) {
  const e = edgeEls[`${a}>${b}`]; if (!e || replaying) return;
  e.classList.add(cls); setTimeout(() => e.classList.remove(cls), ms);
}

function flashNode(id, cls) {
  const n = nodeEls[id]; if (!n || replaying) return;
  n.g.classList.add(cls); setTimeout(() => n.g.classList.remove(cls), 900);
}

function toast(station, text, color, big = false) {
  if (replaying || !NODES[station] || !text) return;
  const n = NODES[station];
  const t = el("text", { class: big ? "toast big" : "toast", x: n.x, y: n.y - H/2 - 30, fill: color }, fxLayer);
  t.textContent = clip(text, big ? 18 : 26);
  const t0 = performance.now(), dur = big ? 2600 : 1900;
  const step = now => {
    const k = Math.min(1, (now - t0) / dur);
    t.setAttribute("y", n.y - H/2 - 30 - 34 * k);
    t.setAttribute("opacity", k < .6 ? 1 : 1 - (k - .6) / .4);
    if (k < 1) requestAnimationFrame(step); else t.remove();
  };
  requestAnimationFrame(step);
}

function burst(station) {
  if (replaying) return;
  const n = NODES[station];
  for (let i = 0; i < 14; i++) {
    const a = (i / 14) * Math.PI * 2, c = el("circle", { r: 5, fill: PALETTE[i % PALETTE.length], cx: n.x, cy: n.y }, fxLayer);
    const t0 = performance.now(), dur = 900 + Math.random() * 300, R = 85 + Math.random() * 35;
    const step = now => {
      const k = Math.min(1, (now - t0) / dur), e = 1 - Math.pow(1 - k, 3);
      c.setAttribute("cx", n.x + Math.cos(a) * R * e); c.setAttribute("cy", n.y + Math.sin(a) * R * e + 30 * k * k);
      c.setAttribute("opacity", 1 - k);
      if (k < 1) requestAnimationFrame(step); else c.remove();
    };
    requestAnimationFrame(step);
  }
}

function dropSpan(station, small = false) {
  stats.spans++;
  if (replaying) return;
  const n = NODES[station]; if (!n) return;
  pulseStat();
  const x = n.x + (Math.random() - .5) * (small ? W * .7 : 10);
  const c = el("circle", { class: "span-drop", r: small ? 3.5 : 6, cx: x, cy: n.y - H/2 }, fxLayer);
  const t0 = performance.now(), dur = small ? 800 : 1100, y0 = n.y - H/2, y1 = y0 - (small ? 50 : 80);
  const step = now => {
    const t = Math.min(1, (now - t0) / dur);
    c.setAttribute("cy", y0 + (y1 - y0) * (1 - Math.pow(1 - t, 2)));
    c.setAttribute("opacity", 1 - t);
    if (t < 1) requestAnimationFrame(step); else c.remove();
  };
  requestAnimationFrame(step);
}

function pulseStat() {
  const b = document.getElementById("s-spans");
  b.textContent = stats.spans;
  b.classList.remove("pop"); void b.offsetWidth; b.classList.add("pop");
}

// ---------- node + panel state ----------
function refreshNodes() {
  const active = {}, waiting = {}, ticker = {}, since = {};
  for (const r of runs.values()) {
    if (r.finished) continue;
    for (const [s, t0] of r.active) {
      const bucket = r.status === "wait" && s === "human" ? waiting : active;
      bucket[s] = (bucket[s] || 0) + 1;
      since[s] = Math.min(since[s] ?? Infinity, t0);
      if (r.tick && r.tick[s]) ticker[s] = r.tick[s];
    }
  }
  for (const [id, n] of Object.entries(nodeEls)) {
    const a = active[id] || 0, w = waiting[id] || 0;
    n.g.classList.toggle("active", a > 0);
    n.g.classList.toggle("wait", w > 0);
    const c = a + w;
    n.badge.setAttribute("visibility", c > 1 ? "visible" : "hidden");
    n.count.textContent = c;
    n.since = c ? since[id] : null;
    n.ticker.textContent = w && !a ? "waiting for Jim…" : clip(ticker[id] || (a ? "working…" : ""), 26);
    if (!c) n.timer.textContent = "";
  }
  tickTimers();
}

function tickTimers() {
  const now = Date.now();
  for (const n of Object.values(nodeEls)) {
    if (n.since == null) continue;
    const s = Math.max(0, Math.round((now - n.since) / 1000));
    n.timer.textContent = s >= 3600 ? `${Math.floor(s / 3600)}h${String(Math.floor(s / 60) % 60).padStart(2, "0")}`
                                    : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }
}
setInterval(tickTimers, 1000);

function refreshPanel() {
  const list = document.getElementById("runs");
  const items = [...runs.values()].sort((a, b) => (a.finished - b.finished) || b.updated - a.updated);
  list.innerHTML = "";
  for (const r of items) {
    const li = document.createElement("li");
    const cls = r.finished ? (r.failed ? "fail" : "done") : r.status === "wait" ? "wait" : r.failed ? "fail" : "";
    const ver = (r.outcome || "").match(/released (v[\d.]+)/);
    const label = r.finished ? (ver ? `shipped ${ver[1]}` : clip(r.outcome || "done", 18)) : NODES[r.at]?.label || r.at;
    li.innerHTML = `<span class="num" style="color:${r.color}">#${r.issue ?? "?"}</span><span class="title"></span><span class="pill ${cls}"></span><span class="now"></span>`;
    li.querySelector(".title").textContent = (r.pr ? `PR ${r.pr} · ` : "") + (r.title || r.run);
    li.querySelector(".pill").textContent = label;
    li.querySelector(".now").textContent = r.finished ? "" : (r.lastTool || "");
    const href = r.pr_url || r.trace_url;
    if (href) li.onclick = () => window.open(href, "_blank");
    list.appendChild(li);
  }
  document.getElementById("s-inflight").textContent = items.filter(r => !r.finished).length;
  document.getElementById("s-human").textContent = items.filter(r => !r.finished && r.status === "wait").length;
  document.getElementById("s-merged").textContent = stats.merged;
  document.getElementById("s-ci").textContent = stats.ciRuns ? Math.round(100 * stats.ciFirst / stats.ciRuns) + "%" : "–";
  document.getElementById("s-spans").textContent = stats.spans;
}

function log(ev) {
  if (ev.status === "info" && ev.station !== "done") return;  // tool ticks go to the ticker, not the log
  const ul = document.getElementById("log");
  const li = document.createElement("li");
  li.className = ev.status;
  const t = new Date(ev.ts).toLocaleTimeString([], { hour12: false });
  li.innerHTML = `<time>${t}</time><b>#${ev.issue ?? ev.run}</b> ${NODES[ev.station]?.label || ev.station}: <span></span>`;
  li.querySelector("span").textContent = ev.detail || ev.status;
  const link = ev.release_url || ev.trace_url;
  if (link) { const a = document.createElement("a"); a.href = link; a.target = "_blank"; a.textContent = ev.release_url ? " release ↗" : " trace ↗"; li.appendChild(a); }
  ul.prepend(li);
  while (ul.children.length > 200) ul.lastChild.remove();
}

// ---------- event handling ----------
function handle(ev) {
  let r = runs.get(ev.run);
  if (!r) {
    r = { run: ev.run, issue: ev.issue, title: ev.title, at: "issue", status: "start", color: colorFor(ev.run),
          active: new Map(), tick: {}, updated: 0, ciRuns: 0, finished: false, failed: false };
    runs.set(ev.run, r);
  }
  // A re-run of a finished issue starts a fresh journey.
  if (r.finished && ev.station === "issue") {
    Object.assign(r, { finished: false, failed: false, at: "issue", active: new Map(), tick: {}, ciRuns: 0, outcome: "" });
    if (r.token) { r.token.remove(); r.token = null; }
  }
  if (ev.title) r.title = ev.title;
  if (ev.issue != null) r.issue = ev.issue;
  if (ev.trace_url) r.trace_url = ev.trace_url;
  if (ev.pr_url) r.pr_url = ev.pr_url;
  if (ev.pr != null && ev.pr !== r.pr) { r.pr = ev.pr; relabel(r); }
  const ts = Date.parse(ev.ts) || Date.now();
  r.updated = ts;
  const s = ev.station, isReview = REVIEWERS.includes(s);

  if (ev.status === "info" && s !== "done") {
    r.tick[s] = ev.detail; r.lastTool = `${NODES[s]?.label || s}: ${ev.detail}`;
    dropSpan(s, true);
  } else if (ev.status === "start") {
    r.active.set(s, ts); r.tick[s] = ""; r.status = "start"; r.failed = false;
    r.lastTool = ev.detail ? `${NODES[s]?.label || s}: ${ev.detail}` : "";
    if (s === "ci") r.ciRuns++;
    if (isReview && r.at !== "review") r.verdicts = {};
    ensureToken(r);
    if (isReview) {
      if (r.at !== "review") {  // main token dives into the review box, minis take over
        const to = [NODES.review_security.x - W/2 - 23, NODES.review_security.y];
        travel(r.token, routePoints("ci", "review_security", r.pos, to), 900);
        r.bob.style.opacity = 0.0; r.pos = to; r.at = "review";
      }
      spawnMini(r, s);
    } else if (NODES[s]) {
      if (r.at === "review") { collectMinis(r); r.bob.style.opacity = 1; r.at = "review_security"; }
      if (r.at !== s) flashEdge(r.at, s, s === "babysit" ? "loop-hot" : "flow");
      moveToken(r, s);
    }
  } else if (ev.status === "wait") {
    r.active.set(s, ts); r.status = "wait"; r.lastTool = "Human: waiting for /ship or /changes";
    if (r.at === "review") { collectMinis(r); r.bob.style.opacity = 1; r.at = "router"; }
    moveToken(r, s);
    r.token.classList.add("waiting");
    toast(s, "over to you, Jim", C.amber);
  } else if (ev.status === "ok" || ev.status === "fail") {
    const ok = ev.status === "ok";
    r.active.delete(s); r.tick[s] = "";
    if (NODES[s]) dropSpan(s);
    flashNode(s, ok ? "flash-ok" : "flash-fail");
    if (isReview) { (r.verdicts = r.verdicts || {})[s] = ok; settleMini(r, s, ok); toast(s, ok ? "approve" : "changes", ok ? C.lime : C.danger); }
    else if (s === "release" && ok) { toast(s, `🏷 ${ev.version || "released"}`, C.pink, true); burst(s); }
    else if (s === "router") {
      const dest = ev.route === "automerge" ? "automerge" : "human";
      flashEdge("router", dest, "chosen", 2200);
      toast(s, dest === "human" ? "→ human" : "→ auto-merge", dest === "human" ? C.amber : C.lime);
    } else toast(s, clip(ev.detail, 30), ok ? C.lime : C.danger);
    if (s === "ci" && r.ciRuns === 1) { stats.ciRuns++; if (ok) stats.ciFirst++; }
    if (s === "ci" && !ok) flashEdge("ci", "babysit", "loop-hot", 2000);
    if (s === "human") { r.status = "start"; r.token && r.token.classList.remove("waiting"); }
    if (!ok && !isReview && s !== "ci" && s !== "human" && s !== "automerge") r.failed = true;
    if (ok && s === "automerge") { stats.merged++; }
    if (ok && s === "human" && /ship|merged/i.test(ev.detail || "")) stats.merged++;
  }
  if (s === "done") {
    r.finished = true; r.outcome = ev.detail || "done"; r.active.clear();
    r.failed = /fail|red|stopped|closed/i.test(r.outcome);
    if (r.minis) { Object.values(r.minis).forEach(m => m.g.remove()); r.minis = null; }
    if (r.token) {
      const tok = r.token; r.token = null;
      if (r.failed) tok.classList.add("failed");
      if (replaying) tok.remove();
      else { tok.style.transition = "opacity 1.4s"; setTimeout(() => { tok.style.opacity = 0; }, r.failed ? 1800 : 600);
             setTimeout(() => tok.remove(), r.failed ? 3400 : 2200); }
    }
  }
  log(ev);
  if (!replaying) { refreshNodes(); refreshPanel(); }
}

function reset() {
  for (const r of runs.values()) { r.token && r.token.remove(); r.minis && Object.values(r.minis).forEach(m => m.g.remove()); }
  runs.clear(); fxLayer.innerHTML = "";
  Object.assign(stats, { spans: 0, merged: 0, released: 0, ciFirst: 0, ciRuns: 0 });
  document.getElementById("log").innerHTML = "";
  refreshNodes(); refreshPanel();
}

function connect() {
  const es = new EventSource("/events");
  const live = document.getElementById("live");
  es.onopen = () => live.classList.add("on");
  es.onerror = () => live.classList.remove("on");
  es.addEventListener("replay-start", () => { reset(); replaying = true; });
  es.addEventListener("replay-end", () => {
    replaying = false;
    for (const r of runs.values()) {
      if (!r.token) continue;
      if (r.at === "review") {  // rebuild the fan-out for runs mid-review
        r.bob.style.opacity = 0;
        for (const s of REVIEWERS) if (r.active.has(s) || r.verdicts?.[s] != null) { spawnMini(r, s); if (r.verdicts?.[s] != null) settleMini(r, s, r.verdicts[s]); }
      } else { r.pos = slot(r.run, r.at); r.token.setAttribute("transform", `translate(${r.pos})`); }
      r.token.classList.toggle("waiting", r.status === "wait");
    }
    refreshNodes(); refreshPanel();
  });
  es.addEventListener("reset", reset);
  es.onmessage = m => { try { handle(JSON.parse(m.data)); } catch (e) { console.error(e, m.data); } };
}
connect();

fetch("/config").then(r => r.json()).then(cfg => {
  const a = document.getElementById("ax-link");
  if (cfg.ax_project_url) { a.href = cfg.ax_project_url; a.hidden = false; }
}).catch(() => {});
