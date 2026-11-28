// The film is a pure function of time: `seek(t)` puts every element where it
// belongs at second t, and nothing animates on its own. tools/render.mjs calls
// it once per frame; opened in a browser, the page plays itself (?t=12 holds a
// frame). Scene lengths come from the narration: audio/timings.json when the
// voice has been generated, the estimates in script.json until then.

const $ = (selector) => document.querySelector(selector);
const clamp = (x, a = 0, b = 1) => Math.min(b, Math.max(a, x));
const lerp = (a, b, k) => a + (b - a) * k;
const inOut = (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2);
const out = (k) => 1 - Math.pow(1 - k, 3);
const linear = (k) => k;
/** 0 before a, 1 after b, eased between. */
const prog = (t, a, b, ease = inOut) => ease(clamp((t - a) / (b - a)));
const show = (el, k, rise = 24) => {
  el.style.opacity = k;
  el.style.transform = `translateY(${(1 - k) * rise}px)`;
};

// ---- the timeline ----------------------------------------------------------

const FADE = 0.4;
const script = await (await fetch("../script.json")).json();
const LEAD = script.lead; // the picture starts this long before its line is spoken
const voiced = await fetch("../audio/timings.json").then((r) => (r.ok ? r.json() : {})).catch(() => ({}));
const T = {};
let total = 0;
for (const segment of script.segments) {
  const voice = voiced[segment.id]?.duration ?? segment.est;
  const dur = LEAD + voice + segment.pad;
  T[segment.id] = { start: total, dur, end: total + dur, voiceAt: total + LEAD, voice };
  total += dur;
}
/**
 * When, in seconds into its scene, the narration of `id` reaches `phrase`, by
 * its share of the line's characters: close enough to put a picture on a word.
 */
function said(id, phrase) {
  const text = script.segments.find((s) => s.id === id).text;
  const at = text.indexOf(phrase);
  if (at < 0) throw new Error(`"${phrase}" is not in the ${id} line of script.json`);
  return LEAD + T[id].voice * (at / text.length);
}
/** Scenes with nothing behind them but the sea fade out; the rest are covered by the next one. */
const FADES_OUT = new Set(["hook", "scale"]);

// ---- pieces ----------------------------------------------------------------

const markSvg = await (await fetch("../assets/exuvia-mark.svg")).text();
for (const el of document.querySelectorAll("[data-mark]")) el.innerHTML = markSvg;

/** A repeatable random number in [0, 1) for index i. */
const rand = (i) => {
  const x = Math.sin(i * 127.1 + 311.7) * 43758.5453;
  return x - Math.floor(x);
};

const bubbles = Array.from({ length: 22 }, (_, i) => {
  const el = document.createElement("i");
  $("#bubbles").append(el);
  return { el, x: rand(i) * 1920, y: rand(i + 50) * 1300, size: 0.5 + rand(i + 90) * 1.1, speed: 26 + rand(i + 130) * 44 };
});

const KEYWORDS = new Set(
  "def return import from as select join using group by order with lambda and on where avg count sum desc".split(" "),
);
const escape = (text) => text.replace(/&/g, "&amp;").replace(/</g, "&lt;");
/** Colours code. \x01 and \x02 in the text open and close a highlighted name. */
function highlight(code) {
  const pattern = /("""[\s\S]*?(?:"""|$)|"[^"\n]*"?|'[^'\n]*'?)|(#.*|--.*)|(@[\w.]+)|(\b\d[\d.]*\b)|([A-Za-z_]\w*)|([\s\S])/g;
  let html = "";
  let afterDef = false;
  for (const m of code.matchAll(pattern)) {
    if (m[1]) html += `<span class="tok-s">${escape(m[1])}</span>`;
    else if (m[2]) html += `<span class="tok-c">${escape(m[2])}</span>`;
    else if (m[3]) html += `<span class="tok-d">${escape(m[3])}</span>`;
    else if (m[4]) html += `<span class="tok-n">${m[4]}</span>`;
    else if (m[5]) {
      if (KEYWORDS.has(m[5])) html += `<span class="tok-k">${m[5]}</span>`;
      else if (afterDef) html += `<span class="tok-f">${m[5]}</span>`;
      else html += m[5];
      afterDef = m[5] === "def";
    } else html += escape(m[6]);
  }
  return html.replace(/\x01/g, "<mark>").replace(/\x02/g, "</mark>");
}

/**
 * Code being typed: the first `k` of it, with a caret. `marks` are names to
 * highlight once they have been typed in full: [text before the name, name].
 * Returns the html and, per mark, whether it is lit.
 */
function typed(code, k, marks = [], caret = true) {
  const n = Math.floor(code.length * clamp(k));
  let text = code.slice(0, n);
  const lit = [];
  const found = marks
    .map(([before, name]) => code.indexOf(before) + before.length)
    .map((at, i) => ({ at, name: marks[i][1] }))
    .sort((a, b) => b.at - a.at);
  for (const { at, name } of found) {
    const done = n >= at + name.length;
    lit.unshift(done);
    if (done) text = `${text.slice(0, at)}\x01${name}\x02${text.slice(at + name.length)}`;
  }
  return { html: highlight(text) + (caret && k < 1 ? '<span class="caret"></span>' : ""), lit, n };
}
/** When, between t0 and t1, the name of a mark has been typed. */
const typedAt = (code, [before, name], t0, t1) => lerp(t0, t1, (code.indexOf(before) + before.length + name.length) / code.length);

/** Points a 1600-wide window at (fx, fy) of its picture, zoomed z, with that point at (cx, cy) on the stage. */
function camera(el, lt, keys, { cx = 960, cy = 470, h = 1000 } = {}) {
  let i = 0;
  while (i < keys.length - 2 && lt > keys[i + 1][0]) i++;
  const [t0, ...a] = keys[i];
  const [t1, ...b] = keys[i + 1];
  const k = prog(lt, t0, t1);
  const [fx, fy, z] = a.map((v, j) => lerp(v, b[j], k));
  el.style.transform = `translate(${cx - fx * 1600 * z}px, ${cy - fy * h * z}px) scale(${z})`;
}

// ---- the graph -------------------------------------------------------------

const NODES = {
  universe: { x: 280, y: 0, kind: "py", desc: "The tickers this project tracks.", rows: "8 rows" },
  prices: { x: 60, y: 188, kind: "py", desc: "Daily closes per ticker.", rows: "2,080 rows" },
  returns: { x: 60, y: 376, kind: "py", desc: "Daily log returns per ticker.", rows: "2,072 rows" },
  rolling_vol: { x: 0, y: 564, kind: "py", desc: "20-day annualized volatility.", rows: "1,920 rows" },
  sector_summary: { x: 280, y: 752, kind: "sql", desc: "Vol and return, by sector.", rows: "6 rows" },
};
const EDGES = {
  "universe>prices": [400, 126, 230, 188],
  "prices>returns": [230, 314, 230, 376],
  "returns>rolling_vol": [230, 502, 170, 564],
  "rolling_vol>sector_summary": [170, 690, 400, 752],
  "universe>sector_summary": [540, 126, 540, 752],
};
const STATE_COLOUR = { fresh: "var(--fresh)", stale: "var(--stale)", running: "var(--running)" };

function makeGraph(slot) {
  const root = document.createElement("div");
  root.className = "graph";
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  root.append(svg);
  const edges = {};
  for (const [key, [x1, y1, x2, y2]] of Object.entries(EDGES)) {
    const path = document.createElementNS(ns, "path");
    const ym = (y1 + y2) / 2;
    path.setAttribute("d", `M${x1} ${y1}C${x1} ${ym} ${x2} ${ym} ${x2} ${y2}`);
    path.setAttribute("pathLength", "1");
    path.style.strokeDasharray = "1";
    const dot = document.createElementNS(ns, "circle");
    dot.setAttribute("r", "7");
    svg.append(path, dot);
    edges[key] = { path, dot };
  }
  const nodes = {};
  for (const [name, spec] of Object.entries(NODES)) {
    const el = document.createElement("div");
    el.className = "node";
    el.style.left = `${spec.x}px`;
    el.style.top = `${spec.y}px`;
    el.innerHTML =
      `<div class="top"><i class="dot"></i><span class="name">${name}</span><span class="kind">${spec.kind}</span></div>` +
      `<div class="desc">${spec.desc}</div><div class="meta"></div><i class="bar"></i>`;
    root.append(el);
    nodes[name] = { el, meta: el.querySelector(".meta"), bar: el.querySelector(".bar"), spec };
  }
  slot.append(root);

  /** spec.nodes[name] = {show, state, note, run}; spec.edges[key] = {draw, pulse}. Anything left out is shown, fresh and drawn. */
  return function apply(spec = {}) {
    for (const [name, node] of Object.entries(nodes)) {
      const { show: k = 1, state = "fresh", note, run = 0 } = spec.nodes?.[name] ?? {};
      node.el.style.opacity = k;
      node.el.style.transform = `scale(${0.9 + 0.1 * k})`;
      node.el.style.setProperty("--state", STATE_COLOUR[state]);
      node.el.classList.toggle("running", state === "running");
      node.meta.innerHTML = `<b>${state}</b>${note ?? (state === "fresh" ? node.spec.rows : "")}`;
      node.bar.style.width = `${state === "running" ? run * 100 : 0}%`;
    }
    for (const [key, edge] of Object.entries(edges)) {
      const { draw = 1, pulse = -1 } = spec.edges?.[key] ?? {};
      edge.path.style.strokeDashoffset = 1 - draw;
      edge.path.style.opacity = draw > 0 ? 1 : 0;
      const live = pulse > 0 && pulse < 1;
      edge.path.classList.toggle("hot", live);
      edge.dot.style.opacity = live ? 1 : 0;
      if (live) {
        const at = edge.path.getPointAtLength(edge.path.getTotalLength() * pulse);
        edge.dot.setAttribute("cx", at.x);
        edge.dot.setAttribute("cy", at.y);
      }
    }
  };
}

/** A node that runs from a to b: `before` until a, running between, fresh after. */
function runs(lt, a, b, before) {
  if (lt < a) return before;
  if (lt < b) return { state: "running", note: "running…", run: prog(lt, a, b, linear) };
  return { state: "fresh" };
}

const GRAPH_AT = "translate(1180px, 96px) scale(1)";
const graphs = {};
for (const id of ["nodes", "run", "schedule"]) {
  const slot = $(`#${id}-graph`);
  slot.style.transform = GRAPH_AT;
  graphs[id] = makeGraph(slot);
}

// ---- the scenes ------------------------------------------------------------

const CODE_PY = `"""20-day annualized vol per ticker."""
import exuvia

@exuvia.node()
def rolling_vol(returns):
    out = returns.sort_values("date")
    out["vol_20d"] = annualized(out)
    return out.dropna()
`;
const CODE_SQL = `-- Latest vol, by sector.
select u.sector, avg(v.vol_20d) as avg_vol
from universe u
join rolling_vol v using (ticker)
group by 1
`;
const MARK_PY = ["def rolling_vol(", "returns"];
const MARKS_SQL = [
  ["from ", "universe"],
  ["join ", "rolling_vol"],
];
const CODE_CRON = `@exuvia.node(schedule="0 17 * * 1-5")
def prices(universe):
    ...`;
const TITLE_WORDS = ["Notebooks ", "for ", "<em>the </em>", "<em>AI </em>", "<em>era.</em>"];
$("#title-line").innerHTML = TITLE_WORDS.map((word) => `<span>${word}</span>`).join("");

const ARTIFACTS = [
  ["universe", "5b0e9d2c", 0.26, false],
  ["prices", "c77a01f4", 0.3, false],
  ["returns", "9c41e07b", 0.45, true],
  ["rolling_vol", "3fa9c1d8", 0.6, true],
  ["sector_summary", "e20b6a95", 0.75, true],
];
$("#run-artifacts").innerHTML = ARTIFACTS.map(
  ([node, sha, , fresh]) =>
    `<div class="artifact"><span class="path">${node}/${sha}<span>….parquet</span></span><span class="tag${fresh ? " new" : ""}">${fresh ? "new output" : "cache hit"}</span></div>`,
).join("");

const TRIGGERS = [
  ["schedule", "prices", 0.3],
  ["cascade", "returns", 0.45],
  ["cascade", "rolling_vol", 0.57],
  ["cascade", "sector_summary", 0.69],
];
$("#schedule-triggers").innerHTML = TRIGGERS.map(
  ([kind, node]) => `<div class="trigger"><b class="${kind}">${kind}</b>${node}<span>17:00</span></div>`,
).join("");
$("#schedule-code").innerHTML = highlight(CODE_CRON.replace('schedule="0 17 * * 1-5"', '\x01schedule="0 17 * * 1-5"\x02'));

const GIT = ["git add -A", 'git commit -m "wip"', "git checkout -b agent/try-2", "git push"];
const SAVES = [
  ["v3", "you", "11-19", "e20b6a"],
  ["v2", "agent", "11-17", "7c1e09"],
  ["v1", "you", "11-12", "3fa9c1"],
];
$("#save-rows").innerHTML = SAVES.map(
  ([version, who, when, sha]) =>
    `<div class="artifact"><span class="who ${who}">${who}</span><span class="path">${version} <span>saved ${when}</span> <small>→ output ${sha}…</small></span></div>`,
).join("");

const PROMPT = "Add a drawdown node per ticker, with a chart";
const TOOLS = [
  ["list_nodes", ""],
  ["read_node", "prices"],
  ["write_node", "drawdown"],
  ["run_node", "drawdown"],
  ["query_data", "drawdown"],
];
$("#agent-tools").innerHTML = TOOLS.map(([tool, arg]) => `<div class="tool"><i></i>${tool}<span>${arg}</span></div>`).join("");

const DATA_PROMPT = "AAPL, MSFT and NVDA since July 2026";
/** What the Data tab does, each shown as the narration names it. */
const DATA_TOOLS = [
  ["filter", "a column", "Filter it"],
  ["sort", "any column", "sort it"],
  ["query", "in SQL", "query it"],
  ["ask", "in words", "or just say"],
  ["brush", "a chart", "drag across"],
];
$("#data-tools").innerHTML = DATA_TOOLS.map(([tool, arg]) => `<div class="tool"><i></i>${tool}<span>${arg}</span></div>`).join("");

// Where outputs land in a bucket: <prefix>/<project>/<node>/<digest>.parquet (README, "Storage").
const BUCKET = [
  ["rolling_vol", "3fa9c1d8", "current"],
  ["rolling_vol", "8e21b04c", "kept"],
  ["returns", "9c41e07b", "current"],
  ["sector_summary", "e20b6a95", "current"],
  ["sector_summary", "71c0d3aa", "kept"],
];
$("#storage-files").innerHTML = BUCKET.map(
  ([node, sha, tag]) =>
    `<div class="artifact"><span class="path">${node}/${sha}<span>….parquet</span></span><span class="tag${tag === "current" ? " new" : ""}">${tag}</span></div>`,
).join("");
const CODE_STORAGE = `select ticker, max(vol_20d) as peak
from rolling_vol
group by 1 order by 2 desc`;

const MCP_TOOLS = "list_nodes read_node write_node run_node query_data write_view list_revisions restore_node request_packages docs".split(" ");
$("#mcp-tools").innerHTML = MCP_TOOLS.map((tool) => `<span>${tool}</span>`).join("");
const MCP_CMD = "uvx exuvia mcp --url http://127.0.0.1:7420";
const wires = [198, 328, 458, 588].map((y) => {
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", `M550 ${y}C680 ${y} 660 360 780 360`);
  path.setAttribute("pathLength", "1");
  path.style.strokeDasharray = "1";
  $("#mcp-wires").append(path);
  return path;
});
const trunk = document.createElementNS("http://www.w3.org/2000/svg", "path");
trunk.setAttribute("d", "M1180 360L1330 360");
trunk.setAttribute("pathLength", "1");
trunk.style.strokeDasharray = "1";
$("#mcp-wires").append(trunk);

const SCENES = {
  hook(lt, d) {
    // The lobster slips out of its shell, tail first, and is bigger for it.
    const away = prog(lt, 1.6, 4.2);
    const drift = Math.max(0, lt - 4.2) * 10;
    const bob = Math.sin(lt * 1.7) * 6;
    $("#hook-shell").style.transform = `translate(110px, 130px) rotate(${away * 3}deg) scale(0.84)`;
    $("#hook-shell").style.opacity = prog(lt, 1.5, 2.5) * 0.95;
    $("#hook-lobster").style.transform =
      `translate(${110 + away * 800 + drift}px, ${130 - away * 40 + bob}px) rotate(${-away * 5}deg) scale(${0.84 + away * 0.26})`;
    $("#hook-lobster").style.opacity = prog(lt, 0.1, 0.9);
    const line = LEAD + T.hook.voice * 0.55;
    show($("#outgrow"), prog(lt, line, line + 0.7, out));
  },

  title(lt) {
    const lockup = $("#title .lockup");
    const k = prog(lt, 0.15, 0.9, out);
    lockup.style.opacity = k;
    lockup.style.transform = `scale(${0.94 + 0.06 * k})`;
    lockup.querySelectorAll("path").forEach((facet, i) => (facet.style.opacity = prog(lt, 0.15 + i * 0.06, 0.5 + i * 0.06)));
    $("#title-line").childNodes.forEach((word, i) => show(word, prog(lt, 1.0 + i * 0.13, 1.5 + i * 0.13, out), 18));
    // The two halves, as the narration names them.
    const [graph, rule, views] = $("#title-split").children;
    const [at, viewsAt] = [said("title", "a graph"), said("title", "and views")];
    show(graph, prog(lt, at, at + 0.6, out));
    rule.style.opacity = prog(lt, at + 0.2, at + 0.8);
    show(views, prog(lt, viewsAt, viewsAt + 0.6, out));
  },

  nodes(lt, d) {
    const editor = $("#nodes-editor");
    editor.style.cssText = "left:96px;top:96px;width:1010px;height:640px";
    const [a0, a1, swap, b0, b1] = [0.6, 0.36 * d, 0.5 * d, 0.54 * d, 0.82 * d];
    const sql = lt >= swap;
    $("#nodes-file").textContent = sql ? "nodes/sector_summary.sql" : "nodes/rolling_vol.py";
    const code = sql
      ? typed(CODE_SQL, prog(lt, b0, b1, linear), MARKS_SQL)
      : typed(CODE_PY, prog(lt, a0, a1, linear), [MARK_PY]);
    $("#nodes-code").innerHTML = code.html;
    $("#nodes-code").style.opacity = 1 - prog(lt, swap - 0.25, swap) + prog(lt, swap, swap + 0.25);
    show(editor, prog(lt, 0, 0.6, out));

    const py = typedAt(CODE_PY, MARK_PY, a0, a1);
    const [fromAt, joinAt] = MARKS_SQL.map((mark) => typedAt(CODE_SQL, mark, b0, b1));
    const pop = (at) => prog(lt, at, at + 0.5, out);
    graphs.nodes({
      nodes: {
        universe: { show: pop(0.2) },
        prices: { show: pop(0.45) },
        returns: { show: pop(0.7) },
        rolling_vol: { show: pop(py) },
        sector_summary: { show: pop(fromAt) },
      },
      edges: {
        "universe>prices": { draw: prog(lt, 0.6, 1.0) },
        "prices>returns": { draw: prog(lt, 0.85, 1.25) },
        "returns>rolling_vol": { draw: prog(lt, py + 0.1, py + 0.7), pulse: prog(lt, py + 0.1, py + 0.7) },
        "universe>sector_summary": { draw: prog(lt, fromAt + 0.1, fromAt + 0.8), pulse: prog(lt, fromAt + 0.1, fromAt + 0.8) },
        "rolling_vol>sector_summary": { draw: prog(lt, joinAt + 0.1, joinAt + 0.7), pulse: prog(lt, joinAt + 0.1, joinAt + 0.7) },
      },
    });
  },

  run(lt, d) {
    const cut = 0.47 * d;
    const b = prog(lt, cut - 0.25, cut + 0.25, linear);
    $("#run-a").style.opacity = 1 - b;
    $("#run-b").style.opacity = b;
    $("#run .scrim").style.opacity = 1 - b;
    show($("#run-cap-a"), 1 - b, 0);
    show($("#run-cap-b"), prog(lt, cut, cut + 0.5, out));

    camera($("#run-shot"), lt, [
      [0, 0.5, 0.47, 1.02],
      [0.14 * d, 0.6, 0.3, 1.5],
      [0.26 * d, 0.62, 0.3, 1.5],
      [cut, 0.6, 0.36, 1.62],
    ]);
    const ring = $("#run-ring");
    ring.style.left = "68.3%";
    ring.style.top = "12.3%";
    const click = (lt - 0.15 * d) / 0.8;
    ring.style.opacity = click > 0 && click < 1 ? 1 - click : 0;
    ring.style.transform = `scale(${0.3 + clamp(click) * 0.9})`;

    // The second half: `returns` was edited, so it and what reads it are stale.
    const u = (lt - cut) / (d - cut);
    const edited = u > 0.1;
    const stale = (note) => (edited ? { state: "stale", note } : { state: "fresh" });
    graphs.run({
      nodes: {
        returns: runs(u, 0.3, 0.45, stale("code changed")),
        rolling_vol: runs(u, 0.45, 0.6, stale("upstream is stale")),
        sector_summary: runs(u, 0.6, 0.75, stale("upstream is stale")),
      },
      edges: {
        "returns>rolling_vol": { pulse: prog(u, 0.43, 0.5, linear) },
        "rolling_vol>sector_summary": { pulse: prog(u, 0.58, 0.65, linear) },
      },
    });
    show($("#run-side .side-title"), prog(u, 0.05, 0.15, out));
    document.querySelectorAll("#run-artifacts .artifact").forEach((row, i) => show(row, prog(u, ARTIFACTS[i][2], ARTIFACTS[i][2] + 0.07, out)));
  },

  schedule(lt, d) {
    const fire = 0.3 * d;
    const seconds = lt < fire ? 57 + Math.floor((3 * lt) / fire) : Math.floor(lt - fire);
    const clock = $("#schedule-clock");
    clock.textContent = lt < fire ? `16:59:${seconds}` : `17:00:${String(seconds).padStart(2, "0")}`;
    clock.style.color = lt >= fire ? "var(--accent-hover)" : "";
    clock.style.textShadow = `0 0 ${60 * (1 - prog(lt, fire, fire + 1.2, out)) * (lt >= fire)}px #6f9bff`;
    show($("#schedule .cron-card"), prog(lt, 0, 0.6, out));
    const k = lt / d;
    graphs.schedule({
      nodes: {
        prices: runs(k, 0.3, 0.45, { state: "fresh", note: "2,080 rows · next 17:00" }),
        returns: runs(k, 0.45, 0.57, { state: "fresh" }),
        rolling_vol: runs(k, 0.57, 0.69, { state: "fresh" }),
        sector_summary: runs(k, 0.69, 0.81, { state: "fresh" }),
      },
      edges: {
        "prices>returns": { pulse: prog(k, 0.42, 0.48, linear) },
        "returns>rolling_vol": { pulse: prog(k, 0.54, 0.6, linear) },
        "rolling_vol>sector_summary": { pulse: prog(k, 0.66, 0.72, linear) },
      },
    });
    document.querySelectorAll("#schedule-triggers .trigger").forEach((row, i) => show(row, prog(k, TRIGGERS[i][2], TRIGGERS[i][2] + 0.05, out)));
  },

  data(lt, d) {
    // The unfiltered table, a filter asked for in words, the rows it leaves, then a drag across the chart.
    const ask = said("data", "or just say");
    const brush = said("data", "drag across");
    const typing = said("data", "query it");
    const swap = Math.min(ask + 0.6, brush - 1.2);
    camera($("#data-shot"), lt, [
      [0, 0.5, 0.47, 1.02],
      [typing, 0.64, 0.5, 1.3],
      [swap + 0.3, 0.63, 0.58, 1.3],
      [brush, 0.62, 0.38, 1.42],
      [d, 0.62, 0.37, 1.48],
    ]);
    show($("#data-prompt"), prog(lt, typing - 0.4, typing, out) * (1 - prog(lt, swap + 0.2, swap + 0.5)));
    const k = prog(lt, typing, swap - 0.2, linear);
    $("#data-typed").innerHTML = escape(DATA_PROMPT.slice(0, Math.floor(DATA_PROMPT.length * k))) + (k < 1 ? '<span class="caret"></span>' : "");
    $("#data-after").style.opacity = prog(lt, swap - 0.2, swap + 0.2, linear);
    // The condition the model wrote, under the box (data.png).
    const ring = $("#data-ring");
    ring.style.cssText = "left:45%;top:62.4%;width:30.5%;height:5.2%";
    ring.style.opacity = prog(lt, swap + 0.2, swap + 0.5) * (1 - prog(lt, brush - 0.3, brush)) * (0.65 + 0.35 * Math.sin((lt - swap) * 5));
    // August to October, dragged across the chart.
    const band = $("#data-brush");
    const b = prog(lt, brush, brush + 1.1, out);
    band.style.cssText = `left:53.5%;top:27.6%;height:18.8%;width:${8.75 * b}%`;
    band.style.opacity = prog(lt, brush - 0.1, brush + 0.1);
    $("#data-tools").childNodes.forEach((chip, i) => {
      const at = said("data", DATA_TOOLS[i][2]);
      show(chip, prog(lt, at, at + 0.4, out));
    });
  },

  notebook(lt, d) {
    // Canvas, the toggle, and the same graph as a notebook: down the contents, into rolling_vol's cell.
    const swap = 0.3 * d;
    camera($("#notebook-shot"), lt, [
      [0, 0.5, 0.47, 1.02],
      [0.18 * d, 0.32, 0.22, 1.38],
      [swap + 0.5, 0.32, 0.26, 1.34],
      [0.66 * d, 0.32, 0.4, 1.3],
      [d, 0.3, 0.46, 1.36],
    ]);
    $("#notebook-after").style.opacity = prog(lt, swap - 0.2, swap + 0.25, linear);
    const ring = $("#notebook-ring");
    ring.style.cssText = "left:12.2%;top:5.4%;width:12.6%;height:3.6%";
    ring.style.opacity = prog(lt, 0.12 * d, 0.2 * d) * (1 - prog(lt, swap + 0.6, swap + 1.1));
  },

  agent(lt, d) {
    // First the built-in agent at work on the canvas, then everything else that can use its tools.
    const cut = said("agent", "Use the built-in") - 0.2;
    const b = prog(lt, cut - 0.25, cut + 0.25, linear);
    $("#agent-a").style.opacity = 1 - b;
    $("#agent-b").style.opacity = b;
    show($("#agent-cap-a"), 1 - b, 0);
    show($("#agent-cap-b"), prog(lt, cut, cut + 0.5, out));

    const a = cut + 0.3; // how long the first part has
    camera($("#agent-shot"), lt, [
      [0, 0.5, 0.47, 1.02],
      [0.3 * a, 0.66, 0.52, 1.16],
      [0.42 * a, 0.66, 0.52, 1.16],
      [0.62 * a, 0.36, 0.5, 1.62],
      [a, 0.37, 0.51, 1.72],
    ]);
    $("#agent-after").style.opacity = prog(lt, 0.4 * a, 0.47 * a, linear);
    show($("#agent-prompt"), prog(lt, 0.04 * a, 0.04 * a + 0.5, out) * (1 - prog(lt, 0.52 * a, 0.57 * a)));
    const k = prog(lt, 0.08 * a, 0.3 * a, linear);
    $("#agent-typed").innerHTML = escape(PROMPT.slice(0, Math.floor(PROMPT.length * k))) + (k < 1 ? '<span class="caret"></span>' : "");
    $("#agent-tools").childNodes.forEach((chip, i) => show(chip, prog(lt, (0.33 + i * 0.045) * a, (0.33 + i * 0.045) * a + 0.4, out)));
    // The drawdown node the agent made, on the canvas (agent.png: x 223-504, y 845-946 of 2880x1800).
    const ring = $("#agent-ring");
    ring.style.cssText = "left:7.3%;top:46.3%;width:10.6%;height:6.9%";
    ring.style.opacity = prog(lt, 0.64 * a, 0.7 * a) * (0.65 + 0.35 * Math.sin((lt - 0.64 * a) * 5));

    const u = lt - cut;
    const at = ["built-in", "Claude Code", "Cursor", "Codex"].map((name) => said("agent", name) - cut - 0.15);
    [...$("#mcp-clients").children].forEach((pill, i) => show(pill, prog(u, at[i], at[i] + 0.45, out)));
    wires.forEach((wire, i) => (wire.style.strokeDashoffset = 1 - prog(u, at[i] + 0.25, at[i] + 0.85)));
    show($("#mcp-server"), prog(u, 0.1, 0.7, out));
    const tools = at[2] + 0.3;
    trunk.style.strokeDashoffset = 1 - prog(u, tools, tools + 0.6);
    $("#mcp-tools").childNodes.forEach((tool, i) => show(tool, prog(u, tools + 0.3 + i * 0.1, tools + 0.65 + i * 0.1, out), 12));
    show($("#mcp-term"), prog(u, at[1], at[1] + 0.5, out));
    const typedK = prog(u, at[1] + 0.2, d - cut - 1.2, linear);
    $("#mcp-cmd").innerHTML = `<span class="dollar">$</span> ${MCP_CMD.slice(0, Math.floor(MCP_CMD.length * typedK))}${typedK < 1 ? '<span class="caret"></span>' : ""}`;
  },

  views(lt, d) {
    camera($("#views-shot"), lt, [
      [0, 0.5, 0.47, 1.02],
      [0.28 * d, 0.47, 0.36, 1.3],
      [0.58 * d, 0.47, 0.68, 1.3],
      [0.72 * d, 0.5, 0.42, 1.14],
      [d, 0.48, 0.4, 1.22],
    ]);
    $("#views-code").style.opacity = prog(lt, 0.6 * d, 0.67 * d, linear);
  },

  storage(lt, d) {
    // The Storage page, then where the files are and the SQL that reads them.
    camera($("#storage-shot"), lt, [
      [0, 0.5, 0.47, 1.02],
      [0.4 * d, 0.3, 0.3, 1.2],
      [d, 0.28, 0.36, 1.26],
    ], { cx: 640 });
    const bucket = said("storage", "on your disk");
    show($("#storage-side"), prog(lt, bucket - 0.3, bucket + 0.3, out));
    document.querySelectorAll("#storage-files .artifact").forEach((row, i) => show(row, prog(lt, bucket + i * 0.22, bucket + i * 0.22 + 0.4, out), 14));
    const sql = said("storage", "and all of it");
    show($("#storage-term"), prog(lt, sql - 0.3, sql + 0.2, out));
    const k = prog(lt, sql, sql + 1.8, linear);
    $("#storage-sql").innerHTML = typed(CODE_STORAGE, k).html + (lt > sql + 2.1 ? `\n<span class="outp">→ 8 rows</span>` : "");
  },

  history(lt, d) {
    // First what there is no longer to do, then the History tab itself.
    const cut = 0.52 * d;
    const b = prog(lt, cut - 0.25, cut + 0.25, linear);
    $("#history-a").style.opacity = 1 - b;
    $("#history-b").style.opacity = b;
    show($("#history-cap-a"), 1 - b, 0);
    show($("#history-cap-b"), prog(lt, cut, cut + 0.5, out));

    show($("#git-term"), prog(lt, 0, 0.6, out));
    const caret = '<span class="caret"></span>';
    $("#git-cmds").innerHTML = GIT.map((line, i) => {
      const k = prog(lt, (0.03 + i * 0.055) * d, (0.075 + i * 0.055) * d, linear);
      if (k <= 0) return "";
      const gone = lt > (0.27 + i * 0.03) * d;
      return `<span class="${gone ? "gone" : ""}"><span class="dollar">$</span> ${line.slice(0, Math.floor(line.length * k))}</span>${k < 1 ? caret : ""}`;
    }).filter(Boolean).join("\n");
    show($("#saves .side-title"), prog(lt, 0.22 * d, 0.22 * d + 0.5, out));
    document.querySelectorAll("#save-rows .artifact").forEach((row, i) => show(row, prog(lt, (0.26 + i * 0.05) * d, (0.26 + i * 0.05) * d + 0.5, out)));

    camera($("#history-shot"), lt - cut, [
      [0, 0.5, 0.47, 1.02],
      [0.5 * (d - cut), 0.6, 0.4, 1.55],
      [d - cut, 0.62, 0.42, 1.66],
    ]);
  },

  scale(lt, d) {
    const hubAt = said("scale", "Move to a hub") - 0.2;
    const keysAt = said("scale", "and give your own apps") - 0.2;
    const keys = prog(lt, keysAt, keysAt + 0.6, out);
    const hub = prog(lt, hubAt, hubAt + 0.8, out);
    // The terminal steps aside for the hub, which needs the height of the stage.
    show($("#scale-term"), prog(lt, 0, 0.6, out) * (1 - prog(lt, hubAt - 0.1, hubAt + 0.4)));
    const lines = ["uvx exuvia init my-research", "uvx exuvia serve my-research"];
    const [one, two] = [prog(lt, 0.1 * hubAt, 0.45 * hubAt, linear), prog(lt, 0.5 * hubAt, 0.85 * hubAt, linear)];
    const caret = '<span class="caret"></span>';
    let html = `<span class="dollar">$</span> ${lines[0].slice(0, Math.floor(lines[0].length * one))}${one < 1 ? caret : ""}`;
    if (one >= 1) html += `\n<span class="dollar">$</span> ${lines[1].slice(0, Math.floor(lines[1].length * two))}${two < 1 ? caret : ""}`;
    if (lt > 0.9 * hubAt) html += `\n<span class="outp">→ http://127.0.0.1:7420</span>`;
    $("#scale-cmd").innerHTML = html;

    $("#scale-hub").style.transform = `translate(80px, ${64 + (1 - hub) * 60}px) scale(1.1)`;
    $("#scale-hub").style.opacity = hub * (1 - keys);
    $("#scale-chips").childNodes.forEach((chip, i) => chip.nodeType === 1 && show(chip, prog(lt, hubAt + 0.6 + i * 0.12, hubAt + 1 + i * 0.12, out) * (1 - keys), 14));

    // A data API key, with the prompt that hands it to a coding agent.
    $("#scale-keys").style.opacity = keys;
    $("#scale-scrim").style.opacity = keys;
    camera($("#scale-keys"), lt - keysAt, [
      [0, 0.5, 0.42, 1.2],
      [d - keysAt, 0.5, 0.48, 1.32],
    ]);

    const stage = lt < hubAt ? 0 : lt < keysAt ? 1 : 2;
    $("#scale-kicker").textContent = ["from a laptop", "to a hub for the team", "the data API"][stage];
    $("#scale .head").textContent = ["Start with one command", "Grow into a hub", "A key for your own apps"][stage];
    const swapK = (at) => 1 - prog(lt, at - 0.3, at) + prog(lt, at, at + 0.4);
    show($("#scale .cap"), Math.min(swapK(hubAt), swapK(keysAt)), 0);
  },

  end(lt, d) {
    const lockup = $("#end .lockup");
    const k = prog(lt, 0.3, 1.1, out);
    lockup.style.opacity = k;
    lockup.style.transform = `scale(${0.94 + 0.06 * k})`;
    show($("#end-line"), prog(lt, 0.8, 1.5, out));
    const cta = prog(lt, 1.5, 2.2, out);
    $("#end-cta").style.opacity = cta;
    $("#end-cta").style.transform = `translateX(-50%) translateY(${(1 - cta) * 24}px)`;
    show($("#end-links"), prog(lt, 2.0, 2.7, out));
    $("#end-shell").style.transform = "translate(70px, 790px) rotate(7deg)";
    $("#end-shell").style.opacity = 0.45 * prog(lt, 0.2, 1.4);
    $("#end-lobster").style.transform = `translate(${1440 + lt * 5}px, ${800 + Math.sin(lt * 1.5) * 6}px) rotate(-4deg)`;
    $("#end-lobster").style.opacity = prog(lt, 0.2, 1.4);
  },
};

function seek(t) {
  t = clamp(t, 0, total);
  for (const [id, scene] of Object.entries(T)) {
    const el = document.getElementById(id);
    let opacity = prog(t, scene.start - FADE / 2, scene.start + FADE / 2, linear);
    if (scene.start === 0) opacity = 1;
    if (FADES_OUT.has(id)) opacity *= 1 - prog(t, scene.end - FADE / 2, scene.end + FADE / 2, linear);
    else if (t > scene.end + FADE / 2 && scene.end < total) opacity = 0;
    el.style.opacity = opacity;
    el.style.visibility = opacity > 0 ? "visible" : "hidden";
    if (opacity > 0) SCENES[id](t - scene.start, scene.dur);
  }

  const sea = t < T.title.end + FADE || t > T.scale.end - FADE;
  $("#sea").style.opacity = sea ? 1 : 0;
  if (sea) {
    const dive = t < T.title.end + FADE ? t / T.title.end : 1 + (t - T.end.start) / T.end.dur;
    $("#sea-img").style.transform = `scale(${1.06 + dive * 0.05}) translateY(${-dive * 14}px)`;
    $("#sea-shade").style.opacity = t < T.hook.end ? 0.45 + 0.4 * prog(t, T.hook.end - 1, T.hook.end + 0.5) : 0.9;
    for (const b of bubbles) {
      const y = 1130 - ((b.y + t * b.speed) % 1300);
      b.el.style.transform = `translate(${b.x + Math.sin(t * 0.8 + b.y) * 14}px, ${y}px) scale(${b.size})`;
      b.el.style.opacity = 0.75 * clamp(y / 300) * clamp((1130 - y) / 200);
    }
  }
  $("#black").style.opacity = Math.max(1 - prog(t, 0, 0.7, linear), prog(t, total - 0.9, total - 0.1, linear));
}

await document.fonts.ready;
await Promise.all([...document.images].map((image) => image.decode().catch(() => {})));

window.FILM = { total, scenes: T, lead: LEAD };
window.seek = seek;

// In a browser: fit the stage to the window, and play unless ?t= holds a frame.
const query = new URLSearchParams(location.search);
if (!query.has("render")) {
  const fit = () => ($("#stage").style.transform = `scale(${Math.min(innerWidth / 1920, innerHeight / 1080)})`);
  addEventListener("resize", fit);
  fit();
  if (query.has("t")) seek(+query.get("t"));
  else {
    const from = performance.now() - (+query.get("from") || 0) * 1000;
    const tick = (now) => {
      seek(((now - from) / 1000) % total);
      requestAnimationFrame(tick);
    };
    requestAnimationFrame(tick);
  }
} else seek(0);
window.READY = true;
