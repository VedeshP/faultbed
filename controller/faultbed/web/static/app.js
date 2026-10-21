"use strict";
// FaultBed web UI. No build step: plain JS + Chart.js.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmt = (v, d = 1) => (v === null || v === undefined || Number.isNaN(v) ? "–" : Number(v).toFixed(d));
const pct = (v, d = 1) => (v === null || v === undefined ? "–" : (v * 100).toFixed(d) + "%");
const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const SERIES = ["--s1", "--s2", "--s3", "--s4", "--s5", "--s6"];
const color = (i) => css(SERIES[i % SERIES.length]);

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined });
  const txt = await r.text();
  const data = txt ? JSON.parse(txt) : null;
  if (!r.ok) {
    const d = data && data.detail;
    const msg = Array.isArray(d) ? d.map((e) => `${e.loc}: ${e.msg}`).join("\n") : (d || r.statusText);
    throw new Error(msg);
  }
  return data;
}

// ---------------------------------------------------------------- state
const RULE_DEFAULT = { model: "none", p_loss: 0, ge_p_gb: 0.05, ge_p_bg: 0.3, ge_loss_good: 0, ge_loss_bad: 0.8,
  p_corrupt: 0, p_duplicate: 0, delay_ms: 0, jitter_ms: 0, blackout: false };
const S = {
  server: null,
  sc: {
    name: "my-experiment", description: "", seed: 1, nodes: [1, 2, 3, 4].map((id) => ({ id })),
    transport: { name: "espnow", params: { channel: 1, unicast: true } },
    topology: { preset: "line", center: null, edges: [], enforce: true }, ttl: 8,
    faults: { default: { ...RULE_DEFAULT }, links: {} },
    flows: [{ id: 1, src: 1, dst: 4, interval_ms: 100, size: 32, echo: true, pattern: "periodic", count: 0, start_ms: 0 }],
    timeline: [], duration_s: 30, warmup_s: 2, drain_s: 3, repeats: 1, vary_seed: false, sweep: {},
  },
  validated: null, pos: {}, selLink: null, pick: null, charts: {}, results: [], camp: null,
};

// ---------------------------------------------------------------- tabs
$$("#tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
function showTab(t) {
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.tab === t));
  $$(".tab").forEach((s) => s.classList.toggle("on", s.id === "tab-" + t));
  if (t === "results") loadResults();
  if (t === "experiment") validate();
}

// ---------------------------------------------------------------- testbed
async function loadState() {
  S.server = await api("/api/state");
  $("#mode").textContent = S.server.mode;
  renderBoards(S.server.boards);
}
function renderBoards(boards) {
  const rows = boards.map((b) => `<tr>
    <td><span class="dot ${b.online ? "ok" : "bad"}"></span> ${esc(b.port)}</td><td>${esc(b.mac)}</td><td>${esc(b.chip)}</td>
    <td>${esc(b.fw)}</td><td>${esc((b.transports || []).join(", "))}</td>
    <td class="num">${b.node_id || "–"}</td><td>${b.node_id ? (b.link_up ? "up" : "down") : "–"} ${esc(b.ip || "")}</td>
    <td><button class="btn tiny" data-identify="${esc(b.port)}">blink LED</button></td></tr>`).join("");
  $("#boards").innerHTML = `<tr><th>Port</th><th>MAC</th><th>Chip</th><th>Firmware</th><th>Protocols</th><th class="num">Node</th><th>Link</th><th></th></tr>` +
    (rows || `<tr><td colspan="8" class="muted">No boards found. Flash the firmware and press Rescan.</td></tr>`);
}
$("#boards").addEventListener("click", async (e) => {
  const p = e.target.dataset.identify;
  if (p) await api("/api/boards/identify", { method: "POST", body: { port: p } });
});
$("#scan").addEventListener("click", async () => {
  $("#scan").disabled = true;
  try { renderBoards(await api("/api/boards/scan", { method: "POST" })); } catch (err) { alert(err.message); }
  $("#scan").disabled = false;
});

// ---------------------------------------------------------------- generic binding
function getPath(obj, path) { return path.split(".").reduce((o, k) => (o == null ? undefined : o[k]), obj); }
function setPath(obj, path, val) {
  const ks = path.split(".");
  let o = obj;
  ks.slice(0, -1).forEach((k) => { if (o[k] == null || typeof o[k] !== "object") o[k] = {}; o = o[k]; });
  o[ks[ks.length - 1]] = val;
}
function input(path, type = "num", attrs = "", root = "sc") {
  const v = getPath(S[root], path);
  if (type === "bool") return `<input type="checkbox" data-bind="${path}" data-type="bool" ${v ? "checked" : ""} ${attrs}>`;
  const t = type === "str" ? "text" : type === "pass" ? "password" : "number";
  return `<input type="${t}" data-bind="${path}" data-type="${type}" value="${esc(v ?? "")}" ${attrs}>`;
}
function select(path, options, attrs = "") {
  const v = getPath(S.sc, path);
  return `<select data-bind="${path}" data-type="str" ${attrs}>` +
    options.map((o) => { const [val, lab] = Array.isArray(o) ? o : [o, o];
      return `<option value="${esc(val)}" ${String(val) === String(v) ? "selected" : ""}>${esc(lab)}</option>`; }).join("") + `</select>`;
}
const L = (label, html, cls = "") => `<label class="${cls}">${label}${html}</label>`;
const C = (label, html) => `<label class="check">${html}${label}</label>`;

document.addEventListener("change", (e) => {
  const el = e.target;
  if (!el.dataset.bind) return;
  let v;
  const t = el.dataset.type;
  if (t === "bool") v = el.checked;
  else if (t === "num") v = el.value === "" ? 0 : Number(el.value);
  else if (t === "int") v = el.value === "" ? 0 : parseInt(el.value, 10);
  else if (t === "pass") v = el.value;
  else if (t === "nullint") v = el.value === "" ? null : parseInt(el.value, 10);
  else v = el.value;
  setPath(S.sc, el.dataset.bind, v);
  onScenarioChanged(el.dataset.bind);
});

function onScenarioChanged(path) {
  if (path === "transport.name") {
    const defs = (S.server.transport_params[S.sc.transport.name] || []);
    const keep = S.sc.transport.params.wifi;
    S.sc.transport.params = Object.fromEntries(defs.map((d) => [d.key, d.default]));
    if (keep) S.sc.transport.params.wifi = keep;
  }
  if (path === "nodecount") return;
  if (path.startsWith("topology")) { S.pos = {}; }
  renderBuilder();
  validate();
}

// ---------------------------------------------------------------- rule editor
function ruleEditor(path) {
  const r = getPath(S.sc, path) || {};
  const m = r.model || "none";
  let h = L("Loss model", select(path + ".model", [["none", "none"], ["bernoulli", "random (Bernoulli)"], ["gilbert_elliott", "bursty (Gilbert-Elliott)"]]));
  if (m === "bernoulli") h += L("Loss probability", input(path + ".p_loss", "num", 'step="0.01" min="0" max="1"'));
  if (m === "gilbert_elliott") {
    h += L("P(good→bad)", input(path + ".ge_p_gb", "num", 'step="0.01" min="0" max="1"'));
    h += L("P(bad→good)", input(path + ".ge_p_bg", "num", 'step="0.01" min="0" max="1"'));
    h += L("Loss in good", input(path + ".ge_loss_good", "num", 'step="0.01" min="0" max="1"'));
    h += L("Loss in bad", input(path + ".ge_loss_bad", "num", 'step="0.01" min="0" max="1"'));
  }
  h += L("Delay (ms)", input(path + ".delay_ms", "num", 'step="1" min="0"'));
  h += L("Jitter (ms)", input(path + ".jitter_ms", "num", 'step="1" min="0"'));
  h += L("Corrupt prob.", input(path + ".p_corrupt", "num", 'step="0.01" min="0" max="1"'));
  h += L("Duplicate prob.", input(path + ".p_duplicate", "num", 'step="0.01" min="0" max="1"'));
  h += C("Link cut (blackout)", input(path + ".blackout", "bool"));
  return `<div class="form">${h}</div>`;
}
function ruleSummary(r) {
  if (!r) return "";
  const p = [];
  if (r.blackout) p.push("cut");
  if (r.model === "bernoulli" && r.p_loss) p.push(`${Math.round(r.p_loss * 100)}% loss`);
  if (r.model === "gilbert_elliott") p.push("bursty");
  if (r.delay_ms || r.jitter_ms) p.push(`${r.delay_ms}±${r.jitter_ms}ms`);
  if (r.p_corrupt) p.push("corrupt");
  if (r.p_duplicate) p.push("dup");
  return p.join(", ");
}

// ---------------------------------------------------------------- builder
function renderBuilder() {
  const sc = S.sc;
  const maxNodes = Math.max(2, Math.min(31, (S.server.boards || []).length || 5));
  $("#f-general").innerHTML = `<div class="form">
    ${L("Name", input("name", "str"), "full")}
    ${L("Description", input("description", "str"), "full")}
    ${L("Nodes", `<input type="number" id="nodecount" min="2" max="31" value="${sc.nodes.length}">`)}
    ${L("Duration (s)", input("duration_s", "num", 'min="1"'))}
    ${L("Warm-up (s)", input("warmup_s", "num", 'min="0"'))}
    ${L("Drain (s)", input("drain_s", "num", 'min="0"'))}
    ${L("Repeats", input("repeats", "int", 'min="1" max="100"'))}
    ${L("Seed", input("seed", "int", 'min="0"'))}
    ${L("TTL", input("ttl", "int", 'min="1" max="32"'))}
    ${C("New seed per repeat", input("vary_seed", "bool"))}
  </div><p class="hint small">${maxNodes < sc.nodes.length ? `⚠ only ${maxNodes} boards connected` : `Same seed ⇒ every link drops exactly the same frames in every repeat.`}</p>`;
  $("#nodecount").addEventListener("change", (e) => {
    const n = Math.max(2, Math.min(31, parseInt(e.target.value, 10) || 2));
    sc.nodes = Array.from({ length: n }, (_, i) => ({ id: i + 1 }));
    const ids = new Set(sc.nodes.map((x) => x.id));
    sc.flows = sc.flows.filter((f) => ids.has(f.src) && ids.has(f.dst));
    sc.timeline = sc.timeline.filter((t) => !t.node || ids.has(t.node));
    for (const k of Object.keys(sc.faults.links)) { const [a, b] = k.split("->").map(Number); if (!ids.has(a) || !ids.has(b)) delete sc.faults.links[k]; }
    sc.topology.edges = sc.topology.edges.filter(([a, b]) => ids.has(a) && ids.has(b));
    S.pos = {};
    onScenarioChanged("nodes");
  });

  const tnames = S.server.transports;
  const defs = S.server.transport_params[sc.transport.name];
  let th = L("Protocol", select("transport.name", tnames.map((t) => [t, t.toUpperCase()])));
  if (defs) {
    for (const d of defs) {
      const p = "transport.params." + d.key;
      if (getPath(sc, p) === undefined) setPath(sc, p, d.default);
      th += d.type === "bool" ? C(d.key, input(p, "bool")) : L(d.key + (d.help ? ` <span class="small">(${esc(d.help)})</span>` : ""), input(p, d.type === "str" ? "str" : "num"));
    }
  } else {
    th += L("Parameters (JSON)", `<input type="text" id="rawparams" value='${esc(JSON.stringify(sc.transport.params))}'>`, "full");
  }
  if (S.server.wifi_transports.includes(sc.transport.name) || Object.keys(sc.sweep).length) {
    if (!sc.transport.params.wifi) sc.transport.params.wifi = { ssid: "", pass: "" };
    th += L("Wi-Fi SSID", input("transport.params.wifi.ssid", "str"));
    th += L("Wi-Fi password", input("transport.params.wifi.pass", "pass"));
  }
  $("#f-transport").innerHTML = `<div class="form">${th}</div>`;
  const raw = $("#rawparams");
  if (raw) raw.addEventListener("change", () => { try { sc.transport.params = JSON.parse(raw.value); validate(); } catch { alert("invalid JSON"); } });

  $("#f-topology").innerHTML = `<div class="form">
    ${L("Preset", select("topology.preset", S.server.presets))}
    ${sc.topology.preset === "star" ? L("Center node", select("topology.center", sc.nodes.map((n) => [n.id, "node " + n.id]))) : ""}
    ${C("Enforce topology", input("topology.enforce", "bool"))}</div>`;

  $("#f-default").innerHTML = ruleEditor("faults.default");
  renderFlows();
  renderTimeline();
  renderSweep();
  renderLinkPanel();
}

function nodeOptions(path, v) {
  return `<select data-bind="${path}" data-type="int">` + S.sc.nodes.map((n) =>
    `<option value="${n.id}" ${n.id === v ? "selected" : ""}>${n.id}</option>`).join("") + `</select>`;
}
function renderFlows() {
  const paths = (S.validated && S.validated.paths) || {};
  const rows = S.sc.flows.map((f, i) => `<tr>
    <td>${input(`flows.${i}.id`, "int", 'min="1" max="255"')}</td>
    <td>${nodeOptions(`flows.${i}.src`, f.src)}</td><td>${nodeOptions(`flows.${i}.dst`, f.dst)}</td>
    <td>${input(`flows.${i}.interval_ms`, "int", 'min="5"')}</td><td>${input(`flows.${i}.size`, "int", 'min="0" max="226"')}</td>
    <td>${select(`flows.${i}.pattern`, ["periodic", "poisson"])}</td>
    <td>${input(`flows.${i}.count`, "int", 'min="0"')}</td><td>${input(`flows.${i}.start_ms`, "int", 'min="0"')}</td>
    <td>${input(`flows.${i}.echo`, "bool")}</td>
    <td class="muted small">${esc((paths[f.id] || []).join(" → "))}</td>
    <td><button class="btn tiny" data-delflow="${i}">✕</button></td></tr>`).join("");
  $("#flows").innerHTML = `<tr><th>Id</th><th>From</th><th>To</th><th>Interval ms</th><th>Payload B</th><th>Pattern</th>
    <th>Count (0=∞)</th><th>Start ms</th><th>Echo/RTT</th><th>Route</th><th></th></tr>` + rows;
}
$("#flows").addEventListener("click", (e) => {
  const i = e.target.dataset.delflow;
  if (i !== undefined) { S.sc.flows.splice(Number(i), 1); renderBuilder(); validate(); }
});
$("#addflow").addEventListener("click", () => {
  const ids = S.sc.nodes.map((n) => n.id);
  const id = Math.max(0, ...S.sc.flows.map((f) => f.id)) + 1;
  S.sc.flows.push({ id, src: ids[0], dst: ids[ids.length - 1], interval_ms: 200, size: 32, echo: true, pattern: "periodic", count: 0, start_ms: 0 });
  renderBuilder(); validate();
});

function linkKeys() {
  const out = [["default", "all links"]];
  for (const [a, b] of (S.validated && S.validated.edges) || []) { out.push([`${a}->${b}`, `${a}→${b}`]); out.push([`${b}->${a}`, `${b}→${a}`]); }
  return out;
}
function renderTimeline() {
  const rows = S.sc.timeline.map((t, i) => {
    let target = "";
    if (t.action === "kill" || t.action === "revive") target = nodeOptions(`timeline.${i}.node`, t.node);
    else target = select(`timeline.${i}.link`, linkKeys());
    let extra = "";
    if (t.action === "kill") extra = C("radio off (hard)", input(`timeline.${i}.hard`, "bool"));
    if (t.action === "fault") extra = `<details><summary class="small">${esc(ruleSummary(t.rule) || "rule…")}</summary>${ruleEditor(`timeline.${i}.rule`)}</details>`;
    return `<tr><td style="width:90px">${input(`timeline.${i}.at_s`, "num", 'min="0" step="0.5"')}</td>
      <td style="width:150px">${select(`timeline.${i}.action`, [["kill", "kill node"], ["revive", "revive node"], ["fault", "set link fault"], ["clear_fault", "clear link fault"]])}</td>
      <td style="width:150px">${target}</td><td>${extra}</td><td style="width:40px"><button class="btn tiny" data-delev="${i}">✕</button></td></tr>`;
  }).join("");
  $("#timeline").innerHTML = `<tr><th>At (s)</th><th>Action</th><th>Target</th><th>Details</th><th></th></tr>` +
    (rows || `<tr><td colspan="5" class="muted">No scheduled events. Add e.g. "kill node 2 at 10 s" + "revive at 20 s" to measure recovery.</td></tr>`);
}
document.addEventListener("change", (e) => {
  const m = /^timeline\.(\d+)\.action$/.exec(e.target.dataset.bind || "");
  if (!m) return;
  const t = S.sc.timeline[Number(m[1])];
  if (t.action === "kill" || t.action === "revive") { t.node = t.node || S.sc.nodes[0].id; delete t.link; delete t.rule; }
  else { t.link = t.link || "default"; delete t.node; if (t.action === "fault") t.rule = t.rule || { ...RULE_DEFAULT, blackout: true }; else delete t.rule; }
  renderTimeline();
}, true);
$("#timeline").addEventListener("click", (e) => {
  const i = e.target.dataset.delev;
  if (i !== undefined) { S.sc.timeline.splice(Number(i), 1); renderTimeline(); validate(); }
});
$("#addevent").addEventListener("click", () => {
  S.sc.timeline.push({ at_s: Math.round(S.sc.duration_s / 3), action: "kill", node: S.sc.nodes[Math.min(1, S.sc.nodes.length - 1)].id, hard: false });
  renderTimeline(); validate();
});

const SWEEP_KEYS = ["transport.name", "faults.default.p_loss", "faults.default.delay_ms", "faults.default.jitter_ms",
  "flows.0.interval_ms", "flows.0.size", "topology.preset", "ttl"];
function renderSweep() {
  const rows = Object.entries(S.sc.sweep).map(([k, vals], i) => `<tr>
    <td><input list="sweepkeys" value="${esc(k)}" data-sweepkey="${esc(k)}"></td>
    <td><input value="${esc(vals.join(", "))}" data-sweepvals="${esc(k)}"></td>
    <td style="width:40px"><button class="btn tiny" data-delsweep="${esc(k)}">✕</button></td></tr>`).join("");
  $("#sweep").innerHTML = `<datalist id="sweepkeys">${SWEEP_KEYS.map((k) => `<option value="${k}">`).join("")}</datalist>
    <tr><th>Parameter (dotted path)</th><th>Values (comma separated)</th><th></th></tr>` +
    (rows || `<tr><td colspan="3" class="muted">No sweep: one configuration. Tip: sweep transport.name = espnow, udp, mqtt, coap to compare protocols.</td></tr>`);
}
const parseVal = (s) => { const t = s.trim(); if (t === "true") return true; if (t === "false") return false; return t !== "" && !Number.isNaN(Number(t)) ? Number(t) : t; };
$("#sweep").addEventListener("change", (e) => {
  const el = e.target;
  if (el.dataset.sweepkey !== undefined) {
    const old = el.dataset.sweepkey; const vals = S.sc.sweep[old]; delete S.sc.sweep[old];
    if (el.value.trim()) S.sc.sweep[el.value.trim()] = vals;
  } else if (el.dataset.sweepvals !== undefined) {
    S.sc.sweep[el.dataset.sweepvals] = el.value.split(",").map(parseVal).filter((v) => v !== "");
  }
  renderBuilder(); validate();
});
$("#sweep").addEventListener("click", (e) => {
  const k = e.target.dataset.delsweep;
  if (k !== undefined) { delete S.sc.sweep[k]; renderBuilder(); validate(); }
});
$("#addsweep").addEventListener("click", () => {
  if (!S.sc.sweep["transport.name"]) S.sc.sweep["transport.name"] = ["espnow", "udp", "mqtt", "coap"];
  else S.sc.sweep["faults.default.p_loss"] = [0, 0.1, 0.3];
  renderBuilder(); validate();
});

// ---------------------------------------------------------------- topology editor
const W = 600, H = 340;
function nodePos(id) {
  if (S.pos[id]) return S.pos[id];
  const l = S.validated && S.validated.layout && S.validated.layout[id];
  return l ? [40 + l[0] * (W - 80), 30 + l[1] * (H - 60)] : [W / 2, H / 2];
}
function drawTopology() {
  const svg = $("#topo");
  const edges = (S.validated && S.validated.edges) || [];
  const links = S.sc.faults.links;
  let h = "";
  for (const [a, b] of edges) {
    const [x1, y1] = nodePos(a), [x2, y2] = nodePos(b);
    const fa = links[`${a}->${b}`], fb = links[`${b}->${a}`];
    const sel = S.selLink && S.selLink[0] === a && S.selLink[1] === b;
    h += `<line class="edge ${fa || fb ? "fault" : ""} ${sel ? "sel" : ""}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
    h += `<line class="hit" data-edge="${a},${b}" x1="${x1}" y1="${y1}" x2="${x2}" y2="${y2}"/>`;
    const lab = [fa && `${a}→${b}: ${ruleSummary(fa)}`, fb && `${b}→${a}: ${ruleSummary(fb)}`].filter(Boolean).join(" · ");
    if (lab) h += `<text class="elabel" x="${(x1 + x2) / 2}" y="${(y1 + y2) / 2 - 8}">${esc(lab)}</text>`;
  }
  for (const n of S.sc.nodes) {
    const [x, y] = nodePos(n.id);
    h += `<g class="node ${S.pick === n.id ? "pick" : ""}" data-node="${n.id}" transform="translate(${x},${y})"><circle r="20"/><text>${n.id}</text></g>`;
  }
  svg.innerHTML = h;
}
(function topoInteractions() {
  const svg = $("#topo");
  let drag = null, moved = false;
  const pt = (e) => { const r = svg.getBoundingClientRect(); return [(e.clientX - r.left) * W / r.width, (e.clientY - r.top) * H / r.height]; };
  svg.addEventListener("pointerdown", (e) => {
    const g = e.target.closest(".node");
    if (g) { drag = Number(g.dataset.node); moved = false; svg.setPointerCapture(e.pointerId); }
  });
  svg.addEventListener("pointermove", (e) => {
    if (drag == null) return;
    const [x, y] = pt(e);
    S.pos[drag] = [Math.max(22, Math.min(W - 22, x)), Math.max(22, Math.min(H - 22, y))];
    moved = true;
    drawTopology();
  });
  svg.addEventListener("pointerup", (e) => {
    const id = drag; drag = null;
    if (id != null && !moved && $("#editlinks").checked) {
      if (S.pick == null) { S.pick = id; drawTopology(); return; }
      if (S.pick !== id) toggleEdge(S.pick, id);
      S.pick = null; drawTopology();
      return;
    }
    if (id == null) {
      const hit = e.target.closest(".hit");
      if (hit) { S.selLink = hit.dataset.edge.split(",").map(Number); renderLinkPanel(); drawTopology(); }
    }
  });
})();
function toggleEdge(a, b) {
  const [x, y] = [Math.min(a, b), Math.max(a, b)];
  const edges = ((S.validated && S.validated.edges) || []).map((e) => [...e]);
  const i = edges.findIndex(([p, q]) => p === x && q === y);
  if (i >= 0) edges.splice(i, 1); else edges.push([x, y]);
  S.sc.topology.preset = "custom";
  S.sc.topology.edges = edges;
  renderBuilder(); validate();
}
function renderLinkPanel() {
  const el = $("#linkpanel");
  if (!S.selLink) { el.innerHTML = ""; return; }
  const [a, b] = S.selLink;
  const side = (x, y) => {
    const k = `${x}->${y}`;
    const on = !!S.sc.faults.links[k];
    return `<div><div class="row between"><b>${x} → ${y}</b>
      <label class="small"><input type="checkbox" data-linktoggle="${k}" ${on ? "checked" : ""}> override</label></div>
      ${on ? ruleEditor(`faults.links.${k}`) : `<p class="muted small">uses default faults</p>`}</div>`;
  };
  el.innerHTML = `<div class="row between"><h3>Link ${a} ↔ ${b}</h3><button class="btn tiny" id="closelink">close</button></div>
    <div class="linkpanel">${side(a, b)}${side(b, a)}</div>`;
  $("#closelink").onclick = () => { S.selLink = null; renderLinkPanel(); drawTopology(); };
  $$("[data-linktoggle]", el).forEach((cb) => cb.addEventListener("change", () => {
    const k = cb.dataset.linktoggle;
    if (cb.checked) S.sc.faults.links[k] = { ...RULE_DEFAULT, model: "bernoulli", p_loss: 0.2 };
    else delete S.sc.faults.links[k];
    renderLinkPanel(); drawTopology(); validate();
  }));
}

// ---------------------------------------------------------------- validate / save / run
let vtimer = null;
function validate() {
  clearTimeout(vtimer);
  vtimer = setTimeout(async () => {
    try {
      S.validated = await api("/api/validate", { method: "POST", body: S.sc });
      $("#warnings").innerHTML = S.validated.warnings.map((w) => `<div class="warn">⚠ ${esc(w)}</div>`).join("");
      const est = S.validated.estimated_s;
      $("#estimate").textContent = `${S.validated.runs} run${S.validated.runs > 1 ? "s" : ""} · ~${est > 90 ? Math.round(est / 60) + " min" : Math.round(est) + " s"}`;
      $("#yaml").textContent = S.validated.yaml + "\n# ---- config sent to each node ----\n" + JSON.stringify(S.validated.node_configs, null, 1);
      $("#runbtn").disabled = false;
      renderFlows();
      if ($$("#timeline select[data-bind$='.link']").length) renderTimeline();
    } catch (err) {
      $("#warnings").innerHTML = `<div class="warn err">${esc(err.message)}</div>`;
      $("#runbtn").disabled = true;
    }
    drawTopology();
  }, 150);
}
async function loadScenarioList() {
  const list = await api("/api/scenarios");
  $("#scnfile").innerHTML = list.map((s) => `<option value="${esc(s.file)}">${esc(s.file)} — ${esc(s.name)}</option>`).join("");
}
$("#load").addEventListener("click", async () => {
  const f = $("#scnfile").value;
  if (!f) return;
  try {
    S.sc = await api("/api/scenarios/" + encodeURIComponent(f));
    $("#savename").value = f;
    S.pos = {}; S.selLink = null;
    renderBuilder(); validate();
  } catch (err) { alert(err.message); }
});
$("#save").addEventListener("click", async () => {
  const f = $("#savename").value.trim() || S.sc.name;
  try { const r = await api("/api/scenarios/" + encodeURIComponent(f), { method: "PUT", body: S.sc }); await loadScenarioList(); $("#scnfile").value = r.file; }
  catch (err) { alert(err.message); }
});
$("#runbtn").addEventListener("click", async () => {
  try { await api("/api/run", { method: "POST", body: S.sc }); showTab("live"); }
  catch (err) { alert(err.message); }
});
$("#stopbtn").addEventListener("click", () => api("/api/stop", { method: "POST" }));

// ---------------------------------------------------------------- live
function chart(id, cfg) {
  if (!window.Chart) return null;
  if (S.charts[id]) S.charts[id].destroy();
  const ink = css("--muted"), grid = css("--line");
  cfg.options = Object.assign({ responsive: true, maintainAspectRatio: false, animation: false,
    plugins: { legend: { labels: { color: ink } } },
    scales: { x: { ticks: { color: ink }, grid: { color: grid } }, y: { ticks: { color: ink }, grid: { color: grid } } } }, cfg.options || {});
  S.charts[id] = new Chart(document.getElementById(id), cfg);
  return S.charts[id];
}
let liveT0 = null, lastRunDir = null;
function connectWs() {
  const ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
  ws.onmessage = (m) => onLive(JSON.parse(m.data));
  ws.onclose = () => setTimeout(connectWs, 1500);
}
function onLive(d) {
  const p = d.progress;
  $("#phase").textContent = p.phase;
  $("#pvariant").textContent = p.run_total ? `run ${p.run_index}/${p.run_total} · ${p.variant} · rep ${p.rep}/${p.reps}` : "";
  const frac = p.run_total ? ((p.run_index - 1) + (p.duration_s ? Math.min(1, p.elapsed_s / p.duration_s) : 0)) / p.run_total : 0;
  $("#pbar").style.width = (p.phase === "done" ? 100 : frac * 100) + "%";
  $("#perror").textContent = p.phase === "error" ? p.error : "";
  $("#runchip").textContent = d.busy ? `● ${p.phase} ${p.run_total ? p.run_index + "/" + p.run_total : ""}` : "";
  $("#stopbtn").disabled = !d.busy;
  if ($("#tab-testbed").classList.contains("on")) renderBoards(d.boards);
  if (p.run_dir !== lastRunDir) { lastRunDir = p.run_dir; liveT0 = null; $("#log").innerHTML = ""; }

  $("#livenodes").innerHTML = d.nodes.sort((a, b) => a.id - b.id).map((n) =>
    `<span class="chip"><span class="dot ${n.down ? "bad" : n.link ? "ok" : "warn"}"></span>node ${n.id}
     <span class="muted small">${n.down ? "FAILED" : n.link ? "link up" : "link down"} · ${n.inj}/${n.arr} injected</span></span>`).join("");
  $("#liveflows").innerHTML = d.flows.map((f) => `<div class="tile"><div class="k">flow ${f.id} · ${f.src} → ${f.dst}</div>
    <div class="v">${pct(f.pdr)}</div><div class="s">${f.delivered}/${f.sent} delivered · RTT ${fmt(f.last_rtt)} ms</div></div>`).join("");
  const pts = d.flows.flatMap((f) => f.rtt.map((x) => x[0]));
  if (pts.length && liveT0 === null) liveT0 = Math.min(...pts);
  if ($("#tab-live").classList.contains("on") && window.Chart) {
    const ds = d.flows.map((f, i) => ({ label: `flow ${f.id}`, data: f.rtt.map(([t, v]) => ({ x: t - liveT0, y: v })),
      borderColor: color(i), backgroundColor: color(i), pointRadius: 1.5, showLine: false }));
    const c = S.charts.livertt;
    if (c && c.data.datasets.length === ds.length) { c.data.datasets.forEach((x, i) => (x.data = ds[i].data)); c.update("none"); }
    else chart("livertt", { type: "scatter", data: { datasets: ds }, options: { scales: {
      x: { title: { display: true, text: "s", color: css("--muted") }, ticks: { color: css("--muted") }, grid: { color: css("--line") } },
      y: { title: { display: true, text: "ms", color: css("--muted") }, ticks: { color: css("--muted") }, grid: { color: css("--line") } } } } });
  }
  if (d.log.length) {
    const log = $("#log");
    for (const e of d.log) {
      const { k, node, e: ev, t, ...rest } = e;
      const div = document.createElement("div");
      div.textContent = `[node ${node}] ${ev} ${JSON.stringify(rest)}`;
      log.appendChild(div);
    }
    while (log.childNodes.length > 300) log.removeChild(log.firstChild);
    log.scrollTop = log.scrollHeight;
  }
  if (p.phase === "done" && S.lastPhase !== "done") loadResults();
  S.lastPhase = p.phase;
}

// ---------------------------------------------------------------- results
async function loadResults() {
  S.results = await api("/api/results");
  $("#camps").innerHTML = S.results.map((c) => `<div class="camp ${S.camp === c.id ? "on" : ""}" data-camp="${esc(c.id)}">
    <div class="t">${esc(c.name)}</div><div class="d">${esc(c.id.slice(0, 15))} · ${c.runs.length} run(s)${c.complete ? "" : " · incomplete"}</div></div>`).join("")
    || `<p class="muted">No results yet.</p>`;
  if (S.camp) renderCampaign(S.camp);
}
$("#camps").addEventListener("click", (e) => {
  const el = e.target.closest("[data-camp]");
  if (el) { S.camp = el.dataset.camp; $$(".camp").forEach((x) => x.classList.toggle("on", x === el)); renderCampaign(S.camp); }
});
$("#refresh").addEventListener("click", loadResults);

const ms = (o) => (o ? `${fmt(o.mean)} ± ${fmt(o.std)}` : "–");
function renderCampaign(id) {
  const c = S.results.find((x) => x.id === id);
  if (!c) return;
  const agg = c.aggregate || [];
  const variants = [...new Set(agg.map((a) => a.variant))];
  const vname = (v) => { const a = agg.find((x) => x.variant === v); const n = (a && a.variant_name) || v; const m = /\[(.*)\]$/.exec(n); return m ? m[1] : n; };
  const rows = agg.map((a) => `<tr><td>${esc(vname(a.variant))}</td><td>${esc(a.transport)}</td><td class="num">${a.flow}</td><td class="num">${a.reps}</td>
    <td class="num">${a.pdr ? pct(a.pdr.mean, 2) + " ± " + pct(a.pdr.std, 2) : "–"}</td>
    <td class="num">${ms(a.rtt_mean_ms)}</td><td class="num">${ms(a.rtt_p95_ms)}</td>
    <td class="num">${a.goodput_bps ? fmt(a.goodput_bps.mean / 1000, 2) : "–"}</td>
    <td class="num">${a.recovery_s ? fmt(a.recovery_s.mean, 2) : "–"}</td>
    <td class="num">${a.pdr && a.pdr.cv != null ? fmt(a.pdr.cv * 100, 2) + "%" : "–"}</td>
    <td class="num ${a.conformance_min === 1 ? "good" : "bad"}">${pct(a.conformance_min, 2)}</td></tr>`).join("");
  $("#resdetail").innerHTML = `
    <div class="card"><div class="row between wrap"><h3>${esc(c.name)}</h3>
      <div class="row"><a class="btn ghost" href="/api/download/${encodeURIComponent(c.id)}/campaign.csv">CSV</a>
      <a class="btn ghost" href="/api/download/${encodeURIComponent(c.id)}/campaign.yaml">Scenario</a></div></div>
      <div class="scroll"><table class="tbl"><tr><th>Variant</th><th>Protocol</th><th class="num">Flow</th><th class="num">Reps</th><th class="num">PDR (mean ± sd)</th>
      <th class="num">RTT ms</th><th class="num">RTT p95 ms</th><th class="num">Goodput kb/s</th><th class="num">Recovery s</th>
      <th class="num">PDR CV</th><th class="num">Fault audit</th></tr>${rows}</table></div>
      <p class="hint small">CV = coefficient of variation across repeats (lower = more reproducible). Fault audit = share of arrivals whose injected fate matched the model exactly.</p></div>
    ${variants.length > 1 ? `<div class="charts2"><div class="card"><h3>Delivery ratio by variant</h3><div class="chartbox"><canvas id="c-pdr"></canvas></div></div>
      <div class="card"><h3>Mean RTT by variant</h3><div class="chartbox"><canvas id="c-rtt"></canvas></div></div></div>` : ""}
    <div class="card"><div class="row between wrap"><h3>Run detail</h3>
      <select id="runsel">${c.runs.map((r) => `<option value="${esc(r)}">${esc(r)}</option>`).join("")}</select></div><div id="rundetail"></div></div>`;
  if (variants.length > 1) {
    const flows = [...new Set(agg.map((a) => a.flow))];
    const mk = (key, scale) => flows.map((f, i) => ({ label: `flow ${f}`, backgroundColor: color(i),
      data: variants.map((v) => { const a = agg.find((x) => x.variant === v && x.flow === f); return a && a[key] ? a[key].mean * scale : null; }) }));
    chart("c-pdr", { type: "bar", data: { labels: variants.map(vname), datasets: mk("pdr", 100) }, options: { scales: { y: { min: 0, max: 100, ticks: { color: css("--muted") }, grid: { color: css("--line") } }, x: { ticks: { color: css("--muted") }, grid: { display: false } } } } });
    chart("c-rtt", { type: "bar", data: { labels: variants.map(vname), datasets: mk("rtt_mean_ms", 1) } });
  }
  $("#runsel").addEventListener("change", (e) => renderRun(id, e.target.value));
  if (c.runs.length) renderRun(id, c.runs[0]);
}

async function renderRun(cid, rel) {
  const [v, r] = rel.split("/");
  const m = await api(`/api/results/${encodeURIComponent(cid)}/${encodeURIComponent(v)}/${encodeURIComponent(r)}`);
  const s = m.summary;
  const tile = (k, val, sub = "") => `<div class="tile"><div class="k">${k}</div><div class="v">${val}</div><div class="s">${sub}</div></div>`;
  const causes = [...new Set(m.flows.flatMap((f) => Object.keys(f.loss_causes)))];
  const flowRows = m.flows.map((f) => `<tr><td>${f.id}</td><td>${f.src} → ${f.dst}</td><td class="num">${f.hops}</td><td class="num">${f.sent}</td>
    <td class="num">${pct(f.pdr, 2)}</td><td class="num">${fmt(f.rtt_ms.mean, 2)}</td><td class="num">${fmt(f.rtt_ms.p95, 2)}</td><td class="num">${fmt(f.rtt_ms.jitter, 2)}</td>
    <td class="num">${fmt(f.owd_ms.mean, 2)}</td><td class="num">${fmt(f.goodput_bps / 1000, 2)}</td><td class="num">${f.duplicates}</td>
    <td>${esc(Object.entries(f.loss_causes).map(([k, n]) => `${k}: ${n}`).join(", ") || "none")}</td></tr>`).join("");
  const auditRows = m.audit.links.map((L) => `<tr><td>${esc(L.link)}</td><td class="num">${L.arrivals}</td><td class="num">${L.injected_loss}</td>
    <td class="num">${pct(L.measured_loss, 2)}</td><td class="num">${pct(L.expected_loss, 2)}</td>
    <td class="num">[${pct(L.ci95[0], 1)}, ${pct(L.ci95[1], 1)}]</td><td class="num">${fmt(L.p_value, 3)}</td>
    <td class="${L.within_ci ? "good" : "bad"}">${L.within_ci ? "consistent" : "inconsistent"}</td>
    <td class="num">${L.injected_corrupt}</td><td class="num">${L.injected_dup}</td>
    <td class="num">${L.injected_delay_ms.n ? fmt(L.injected_delay_ms.mean, 1) : "–"}</td></tr>`).join("");
  const linkRows = m.links.map((L) => `<tr><td>${esc(L.link)}</td><td class="num">${L.frames_sent}</td><td class="num">${L.frames_arrived}</td>
    <td class="num">${pct(L.channel_delivery, 2)}</td><td class="num">${L.retransmission_surplus}</td></tr>`).join("");
  const recRows = m.recovery.map((x) => `<tr><td>${esc(x.disruption)} ${esc(x.target)}${x.hard ? " (radio off)" : ""}</td><td class="num">${x.flow}</td>
    <td class="num">${fmt(x.start_s, 1)} – ${fmt(x.end_s, 1)}</td><td class="num">${fmt(x.recovery_s, 3)}</td><td class="num">${fmt(x.outage_s, 3)}</td>
    <td class="num">${fmt(x.relink_s, 3)}</td></tr>`).join("");
  $("#rundetail").innerHTML = `
    <div class="tiles">
      ${tile("Delivery ratio (mean)", pct(s.pdr_mean, 2), `min ${pct(s.pdr_min, 2)}`)}
      ${tile("Mean RTT", fmt(s.rtt_mean_ms, 2) + " ms", s.transport.toUpperCase() + " · " + s.topology)}
      ${tile("Goodput", fmt(s.goodput_bps_total / 1000, 2) + " kb/s", `${s.flows} flow(s)`)}
      ${tile("Fault audit", pct(m.audit.conformance, 2), `${m.audit.matching}/${m.audit.arrivals_checked} arrivals`)}
      ${tile("Recovery", s.recovery_s_mean != null ? fmt(s.recovery_s_mean, 2) + " s" : "–", "after disruptions")}
    </div>
    <h3>Flows</h3><div class="scroll"><table class="tbl"><tr><th>Flow</th><th>Path</th><th class="num">Hops</th><th class="num">Sent</th><th class="num">PDR</th>
      <th class="num">RTT ms</th><th class="num">p95</th><th class="num">Jitter</th><th class="num">One-way ms</th><th class="num">Goodput kb/s</th><th class="num">Dups</th><th>Why packets were lost</th></tr>${flowRows}</table></div>
    <div class="charts2" style="margin-top:14px">
      <div><h3>Delivery ratio over time</h3><div class="chartbox"><canvas id="c-pdrt"></canvas></div></div>
      <div><h3>RTT per packet</h3><div class="chartbox"><canvas id="c-rttp"></canvas></div></div>
      <div><h3>Loss attribution</h3><div class="chartbox"><canvas id="c-cause"></canvas></div></div>
      <div><h3>RTT distribution (CDF)</h3><div class="chartbox"><canvas id="c-cdf"></canvas></div></div>
    </div>
    <h3 style="margin-top:14px">Fault audit — configured vs. injected, per directed link</h3>
    <div class="scroll"><table class="tbl"><tr><th>Link</th><th class="num">Arrivals</th><th class="num">Dropped</th><th class="num">Measured</th><th class="num">Configured</th>
      <th class="num">95% CI</th><th class="num">p</th><th>Verdict</th><th class="num">Corrupted</th><th class="num">Duplicated</th><th class="num">Mean delay ms</th></tr>${auditRows}</table></div>
    <h3 style="margin-top:14px">Real channel, per hop <span class="muted small">(frames handed to the radio vs. frames that arrived — loss here is <i>not</i> injected)</span></h3>
    <div class="scroll"><table class="tbl"><tr><th>Hop</th><th class="num">Sent</th><th class="num">Arrived</th><th class="num">Channel delivery</th><th class="num">Retransmission surplus</th></tr>${linkRows}</table></div>
    ${recRows ? `<h3 style="margin-top:14px">Recovery</h3><div class="scroll"><table class="tbl"><tr><th>Disruption</th><th class="num">Flow</th><th class="num">Window s</th>
      <th class="num">Back after s</th><th class="num">Outage s</th><th class="num">Re-link s</th></tr>${recRows}</table></div>` : ""}`;

  const fl = m.flows;
  const seconds = Math.max(...fl.map((f) => m.series[f.id].pdr_per_s.length));
  chart("c-pdrt", { type: "line", data: { labels: Array.from({ length: seconds }, (_, i) => i),
    datasets: fl.map((f, i) => ({ label: `flow ${f.id}`, data: m.series[f.id].pdr_per_s.map((x) => (x == null ? null : x * 100)),
      borderColor: color(i), backgroundColor: color(i), pointRadius: 0, tension: 0.2, spanGaps: false })) },
    options: { scales: { y: { min: 0, max: 100, ticks: { color: css("--muted") }, grid: { color: css("--line") } }, x: { ticks: { color: css("--muted") }, grid: { color: css("--line") } } } } });
  chart("c-rttp", { type: "scatter", data: { datasets: fl.map((f, i) => ({ label: `flow ${f.id}`, data: m.series[f.id].rtt_points.map(([x, y]) => ({ x, y })),
    borderColor: color(i), backgroundColor: color(i), pointRadius: 1.5 })) } });
  chart("c-cause", { type: "bar", data: { labels: fl.map((f) => `flow ${f.id}`),
    datasets: causes.map((c, i) => ({ label: c, backgroundColor: color(i), data: fl.map((f) => f.loss_causes[c] || 0) })) },
    options: { scales: { x: { stacked: true, ticks: { color: css("--muted") }, grid: { display: false } }, y: { stacked: true, ticks: { color: css("--muted") }, grid: { color: css("--line") } } } } });
  chart("c-cdf", { type: "line", data: { datasets: fl.map((f, i) => {
    const xs = m.series[f.id].rtt_points.map((p) => p[1]).sort((a, b) => a - b);
    return { label: `flow ${f.id}`, data: xs.map((x, k) => ({ x, y: ((k + 1) / xs.length) * 100 })), borderColor: color(i), pointRadius: 0, showLine: true };
  }) }, options: { scales: { x: { type: "linear", title: { display: true, text: "RTT ms", color: css("--muted") }, ticks: { color: css("--muted") }, grid: { color: css("--line") } },
    y: { min: 0, max: 100, ticks: { color: css("--muted") }, grid: { color: css("--line") } } } } });
}

// ---------------------------------------------------------------- boot
(async function boot() {
  await loadState();
  await loadScenarioList();
  renderBuilder();
  validate();
  connectWs();
})();
