// Midterms 2026 Live — dashboard. No build step; ES module.
// All labels from the API are inserted as text (never innerHTML).

const app = document.getElementById("app");
const PARTY_NAME = { DEM: "Democrats", REP: "Republicans", OTHER: "Other" };
const PARTY_LETTER = { DEM: "D", REP: "R", OTHER: "I" };
const STATUS_LABEL = {
  no_results: "Awaiting results", prior_only: "Pre-results estimate", tossup: "Toss-up",
  lean: "Lean", likely: "Likely", projected: "Model projection", final_count: "Certified count",
};
const STATE_NAMES = {AL:"Alabama",AK:"Alaska",AZ:"Arizona",AR:"Arkansas",CA:"California",CO:"Colorado",CT:"Connecticut",DE:"Delaware",DC:"District of Columbia",FL:"Florida",GA:"Georgia",HI:"Hawaii",ID:"Idaho",IL:"Illinois",IN:"Indiana",IA:"Iowa",KS:"Kansas",KY:"Kentucky",LA:"Louisiana",ME:"Maine",MD:"Maryland",MA:"Massachusetts",MI:"Michigan",MN:"Minnesota",MS:"Mississippi",MO:"Missouri",MT:"Montana",NE:"Nebraska",NV:"Nevada",NH:"New Hampshire",NJ:"New Jersey",NM:"New Mexico",NY:"New York",NC:"North Carolina",ND:"North Dakota",OH:"Ohio",OK:"Oklahoma",OR:"Oregon",PA:"Pennsylvania",RI:"Rhode Island",SC:"South Carolina",SD:"South Dakota",TN:"Tennessee",TX:"Texas",UT:"Utah",VT:"Vermont",VA:"Virginia",WA:"Washington",WV:"West Virginia",WI:"Wisconsin",WY:"Wyoming"};
// Square tile grid: [column, row]
const TILES = {AK:[0,0],ME:[11,0],VT:[10,1],NH:[11,1],WA:[1,2],ID:[2,2],MT:[3,2],ND:[4,2],MN:[5,2],IL:[6,2],WI:[7,2],MI:[8,2],NY:[9,2],RI:[10,2],MA:[11,2],OR:[1,3],NV:[2,3],WY:[3,3],SD:[4,3],IA:[5,3],IN:[6,3],OH:[7,3],PA:[8,3],NJ:[9,3],CT:[10,3],CA:[1,4],UT:[2,4],CO:[3,4],NE:[4,4],MO:[5,4],KY:[6,4],WV:[7,4],VA:[8,4],MD:[9,4],DE:[10,4],AZ:[2,5],NM:[3,5],KS:[4,5],AR:[5,5],TN:[6,5],NC:[7,5],SC:[8,5],DC:[9,5],OK:[4,6],LA:[5,6],MS:[6,6],AL:[7,6],GA:[8,6],HI:[0,7],TX:[4,7],FL:[9,7]};

const fmtInt = new Intl.NumberFormat("en-US");
const fmtCompact = new Intl.NumberFormat("en-US", { notation: "compact", maximumFractionDigits: 1 });
const pct = (x, d = 0) => (x == null ? "—" : `${(x * 100).toFixed(d)}%`);
const probText = (p) => (p == null ? "—" : p > 0.99 ? ">99%" : p < 0.01 ? "<1%" : `${Math.round(p * 100)}%`);
const timeText = (iso) => (iso ? new Date(iso).toLocaleTimeString([], { hour: "numeric", minute: "2-digit", second: "2-digit" }) : "");

// ---------- tiny DOM helper (text-safe) ----------
function h(tag, attrs = {}, ...kids) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k === "style" && typeof v === "object") Object.assign(el.style, v);
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const kid of kids.flat()) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  }
  return el;
}
const svg = (tag, attrs = {}, ...kids) => {
  const el = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) el.setAttribute(k, v);
  for (const kid of kids.flat()) if (kid != null) el.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
  return el;
};
const cssVar = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim();
const partyColor = (p) => cssVar(p === "DEM" ? "--dem" : p === "REP" ? "--rep" : "--oth");

async function getJSON(url) {
  const res = await fetch(url, { headers: { Accept: "application/json" } });
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

// Two ways to run: against the live API (server mode) or from exported JSON files (snapshot mode,
// used when the dashboard is published as a static page). Routes are plain #names in both.
const STATIC = document.querySelector('meta[name="dashboard-mode"]')?.content === "static";
const DATASETS = { sim: "Simulated night", real: "Real data" };
let dataset = (() => { try { return localStorage.getItem("dataset") || "sim"; } catch { return "sim"; } })();
if (!DATASETS[dataset]) dataset = "sim";
const cache = new Map();
const cached = (url) => { if (!cache.has(url)) cache.set(url, getJSON(url).catch((e) => { cache.delete(url); throw e; })); return cache.get(url); };
const slug = (key) => key.toLowerCase().replace(/[^a-z0-9]+/g, "-");
const raceHref = (key) => `#race-${slug(key)}`;
const officeOfSlug = (sl) => (sl.includes("us-senate") ? "senate" : sl.includes("us-house") ? "house" : "governor");
const api = {
  summary: () => (STATIC ? cached(`data/${dataset}/summary.json`) : getJSON("/live/summary")),
  races: (office) => (STATIC ? cached(`data/${dataset}/races-${office}.json`) : getJSON(`/live/races?office=${office}`)),
  race: async (sl) => {
    if (!STATIC) return getJSON(`/live/races/${encodeURIComponent(sl)}`);
    const bundle = await cached(`data/${dataset}/details-${officeOfSlug(sl)}.json`);
    if (!bundle[sl]) throw new Error("Race not found in this snapshot");
    return bundle[sl];
  },
};

// ---------- classification ----------
function tier(r) {
  if (!r || !r.leader_party) return "none";
  if (r.status === "projected" || r.status === "final_count") return "proj";
  const p = r.leader_prob ?? 0.5;
  if (p >= 0.95) return "likely";
  if (p >= 0.75) return "lean";
  return "tossup";
}
function tierFill(r) {
  const t = tier(r);
  if (t === "none") return cssVar("--none");
  if (t === "tossup") return cssVar("--tossup");
  const party = r.leader_party === "DEM" ? "dem" : r.leader_party === "REP" ? "rep" : null;
  if (!party) return cssVar("--oth");
  return cssVar(`--${party}-${t === "proj" ? "proj" : t}`);
}
function isDarkFill(color) {
  const m = color.match(/^#([0-9a-f]{6})$/i);
  if (!m) return false;
  const n = parseInt(m[1], 16), r = (n >> 16) & 255, g = (n >> 8) & 255, b = n & 255;
  return 0.2126 * r + 0.7152 * g + 0.0722 * b < 140;
}
const raceTitle = (r) => {
  const state = STATE_NAMES[r.state] || r.state;
  if (r.office === "house") {
    const d = r.contest_key.split("-").pop();
    return `${r.state}-${d === "00" ? "AL" : String(parseInt(d, 10))}`;
  }
  return `${state}`;
};
const officeLabel = (r) => (r.office === "senate" ? (r.contest_key.includes("special") ? "Senate (special)" : "Senate")
  : r.office === "governor" ? "Governor" : "House");

function statusChip(r) {
  let label = STATUS_LABEL[r.status] || r.status;
  if ((r.status === "lean" || r.status === "likely") && r.leader_party) label += ` ${PARTY_LETTER[r.leader_party]}`;
  return h("span", { class: `status ${r.status}` }, label);
}
const partyTag = (p) => h("span", { class: `party-tag ${p || "OTHER"}`, "aria-label": PARTY_NAME[p] || "Other" }, PARTY_LETTER[p] || "I");

// ---------- header / mode ----------
function setMode(mode, last) {
  const el = document.getElementById("mode");
  el.className = `mode ${STATIC && mode === "live" ? "" : mode}`;
  document.getElementById("mode-text").textContent =
    mode === "simulation" ? "Simulated data" : mode === "live" ? (STATIC ? "Snapshot" : "Live") : "Awaiting results";
  if (last) document.getElementById("updated").textContent = `Model updated ${timeText(last)}`;
}
function modeBanner(mode) {
  if (mode === "simulation") {
    return h("div", { class: "banner", role: "alert" },
      "SIMULATED DATA — synthetic results for testing the dashboard. Candidates are generic party placeholders. Nothing here is a real result.");
  }
  if (mode === "awaiting_results") {
    return h("div", { class: "banner info" },
      "No live results loaded yet. Probabilities shown are pre-results estimates from each seat's last election (see a race for its baseline).");
  }
  return null;
}

// ---------- charts ----------
function tooltipEl(container) {
  const tip = h("div", { class: "tooltip", hidden: true });
  container.append(tip);
  return tip;
}
function placeTip(tip, container, x, y) {
  const box = container.getBoundingClientRect();
  tip.hidden = false;
  const w = tip.offsetWidth, hgt = tip.offsetHeight;
  let left = x + 14, top = y - hgt - 8;
  if (left + w > box.width) left = x - w - 14;
  if (top < 0) top = y + 14;
  tip.style.left = `${Math.max(0, left)}px`;
  tip.style.top = `${top}px`;
}

/** Line chart with crosshair. series: [{name, party, color, points:[{t: Date, y}]}] (shared timestamps). */
function lineChart({ series, yMin = 0, yMax = 1, yFmt = (v) => pct(v), height = 220, title }) {
  const W = 640, H = height, m = { l: 40, r: 64, t: 12, b: 26 };
  const wrap = h("div", { class: "chart", role: "img", "aria-label": title || "Line chart" });
  const all = series.flatMap((s) => s.points);
  if (!all.length) return h("p", { class: "empty" }, "No history yet.");
  let t0 = Math.min(...all.map((p) => +p.t)), t1 = Math.max(...all.map((p) => +p.t));
  if (t0 === t1) { t0 -= 5 * 60e3; t1 += 5 * 60e3; }
  const x = (t) => m.l + ((+t - t0) / (t1 - t0)) * (W - m.l - m.r);
  const y = (v) => m.t + (1 - (v - yMin) / (yMax - yMin)) * (H - m.t - m.b);
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });
  const span = yMax - yMin;
  const step = [0.05, 0.1, 0.2, 0.25, 0.5].find((st) => span / st <= 5) || span / 4;
  for (let v = Math.ceil(yMin / step) * step; v <= yMax + 1e-9; v += step) {
    root.append(svg("line", { class: "gridline", x1: m.l, x2: W - m.r, y1: y(v), y2: y(v) }),
      svg("text", { class: "axis-label", x: m.l - 6, y: y(v) + 4, "text-anchor": "end" }, yFmt(v)));
  }
  const short = t1 - t0 < 15 * 60e3; // under 15 minutes of history: show seconds so ticks differ
  for (let i = 0; i < 4; i++) {
    const t = t0 + ((t1 - t0) * i) / 3;
    root.append(svg("text", { class: "axis-label", x: x(t), y: H - 6, "text-anchor": i === 0 ? "start" : i === 3 ? "end" : "middle" },
      new Date(t).toLocaleTimeString([], short ? { hour: "numeric", minute: "2-digit", second: "2-digit" } : { hour: "numeric", minute: "2-digit" })));
  }
  for (const s of series) {
    if (!s.points.length) continue;
    const d = s.points.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.y).toFixed(1)}`).join("");
    root.append(svg("path", { class: "line", d, stroke: s.color }));
    const last = s.points[s.points.length - 1];
    root.append(svg("circle", { cx: x(last.t), cy: y(last.y), r: 4, fill: s.color, stroke: cssVar("--surface"), "stroke-width": 2 }));
  }
  // end labels (<= 4 series), nudged apart only by leader-free spacing check
  if (series.length <= 4) {
    const ends = series.filter((s) => s.points.length).map((s) => ({ s, yy: y(s.points[s.points.length - 1].y) }))
      .sort((a, b) => a.yy - b.yy);
    let prev = -Infinity;
    for (const e of ends) {
      if (e.yy - prev < 13) continue; // collision: leave it to the legend + tooltip
      prev = e.yy;
      root.append(svg("text", { class: "axis-label", x: W - m.r + 8, y: e.yy + 4, style: `fill:${cssVar("--ink-2")}` },
        yFmt(e.s.points[e.s.points.length - 1].y)));
    }
  }
  const cross = svg("line", { class: "crosshair", y1: m.t, y2: H - m.b, visibility: "hidden" });
  root.append(cross);
  const hit = svg("rect", { x: m.l, y: m.t, width: W - m.l - m.r, height: H - m.t - m.b, fill: "transparent" });
  root.append(hit);
  wrap.append(root);
  const tip = tooltipEl(wrap);
  const times = series[0].points.map((p) => +p.t);
  const show = (clientX) => {
    const box = root.getBoundingClientRect();
    const sx = ((clientX - box.left) / box.width) * W;
    let idx = 0, best = Infinity;
    times.forEach((t, i) => { const dd = Math.abs(x(t) - sx); if (dd < best) { best = dd; idx = i; } });
    cross.setAttribute("x1", x(times[idx])); cross.setAttribute("x2", x(times[idx])); cross.setAttribute("visibility", "visible");
    tip.replaceChildren(h("div", { class: "t" }, new Date(times[idx]).toLocaleTimeString()),
      ...series.map((s) => ({ s, p: s.points[idx] })).filter((o) => o.p).sort((a, b) => b.p.y - a.p.y).map(({ s, p }) =>
        h("div", { class: "row" }, h("span", { class: "k" }, h("i", { style: { background: s.color } }), s.name), h("b", {}, yFmt(p.y)))));
    placeTip(tip, wrap, (x(times[idx]) / W) * box.width, (m.t / H) * box.height + 20);
  };
  hit.addEventListener("pointermove", (e) => show(e.clientX));
  hit.addEventListener("pointerleave", () => { tip.hidden = true; cross.setAttribute("visibility", "hidden"); });
  const legend = h("div", { class: "legend" }, series.map((s) =>
    h("span", {}, h("i", { style: { width: "14px", height: "2px", background: s.color, display: "inline-block", borderRadius: "1px" } }), s.name)));
  return h("div", {}, legend, wrap);
}

/** Seat distribution columns; columns at or above the majority line use the controlling party's color. */
function seatHistogram(chamber, party = "DEM") {
  const dist = chamber.seat_distribution?.[party] || {};
  const entries = Object.entries(dist).map(([k, v]) => [Number(k), v]).sort((a, b) => a[0] - b[0]);
  if (!entries.length) return h("p", { class: "empty" }, "No simulation yet.");
  const lo = entries[0][0], hi = entries[entries.length - 1][0];
  const W = 640, H = 140, m = { l: 8, r: 8, t: 8, b: 22 };
  const n = hi - lo + 1, slot = (W - m.l - m.r) / n, bw = Math.max(1, Math.min(24, slot - 2));
  const maxP = Math.max(...entries.map((e) => e[1]));
  const need = chamber.seats_needed;
  const root = svg("svg", { viewBox: `0 0 ${W} ${H}`, "aria-hidden": "true" });
  root.append(svg("line", { class: "gridline", x1: m.l, x2: W - m.r, y1: H - m.b, y2: H - m.b }));
  const wrap = h("div", { class: "chart", role: "img", "aria-label": `Distribution of ${PARTY_NAME[party]} seats` });
  const tip = tooltipEl(wrap);
  for (const [seats, p] of entries) {
    const cx = m.l + (seats - lo) * slot + slot / 2;
    const hgt = Math.max(1, (p / maxP) * (H - m.t - m.b));
    const demControl = seats >= need; // a Senate 50-50 tie falls below 51 and goes to the tie-breaking party
    const color = demControl ? cssVar("--dem") : cssVar("--rep");
    const y0 = H - m.b - hgt, r = Math.min(4, bw / 2, hgt);
    const d = `M${cx - bw / 2},${H - m.b}V${y0 + r}Q${cx - bw / 2},${y0} ${cx - bw / 2 + r},${y0}H${cx + bw / 2 - r}Q${cx + bw / 2},${y0} ${cx + bw / 2},${y0 + r}V${H - m.b}Z`;
    const col = svg("path", { class: "col", d, fill: color });
    const hitbox = svg("rect", { x: cx - slot / 2, y: m.t, width: slot, height: H - m.t - m.b, fill: "transparent" });
    const showTip = () => {
      const box = root.getBoundingClientRect();
      tip.replaceChildren(h("div", { class: "row" }, h("b", {}, pct(p, 1)), h("span", { class: "k" }, `${seats} ${PARTY_NAME[party]} seats`)));
      placeTip(tip, wrap, (cx / W) * box.width, (y0 / H) * box.height);
    };
    hitbox.addEventListener("pointermove", showTip);
    hitbox.addEventListener("pointerleave", () => (tip.hidden = true));
    root.append(col, hitbox);
  }
  const mx = m.l + (need - lo) * slot;
  if (need >= lo && need <= hi + 1) {
    const flip = mx > W * 0.7; // keep the label inside the plot near the right edge
    root.append(svg("line", { x1: mx, x2: mx, y1: m.t, y2: H - m.b + 4, stroke: cssVar("--ink"), "stroke-width": 1 }),
      svg("text", { class: "axis-label", x: flip ? mx - 4 : mx + 4, y: m.t + 10, "text-anchor": flip ? "end" : "start" }, `${need} for majority`));
  }
  root.append(svg("text", { class: "axis-label", x: m.l, y: H - 6 }, `${lo}`),
    svg("text", { class: "axis-label", x: W - m.r, y: H - 6, "text-anchor": "end" }, `${hi} ${PARTY_LETTER[party]} seats`));
  wrap.prepend(root);
  const table = h("details", { class: "table-view" }, h("summary", {}, "View as table"),
    h("table", { class: "data" }, h("thead", {}, h("tr", {}, h("th", {}, `${PARTY_NAME[party]} seats`), h("th", { class: "r" }, "Probability"))),
      h("tbody", {}, entries.map(([s, p]) => h("tr", {}, h("td", { class: "num" }, s), h("td", { class: "r num" }, pct(p, 1)))))));
  return h("div", {}, wrap, table);
}

// ---------- chamber card ----------
function chamberCard(name, chamber, races) {
  if (!chamber) return h("section", { class: "card" }, h("h2", {}, `${name} control`), h("p", { class: "empty" }, "No projection yet."));
  const total = chamber.total_seats, need = chamber.seats_needed;
  const hold = chamber.holdovers || {};
  const counts = { DEM: { proj: hold.DEM || 0, likely: 0, lean: 0 }, REP: { proj: hold.REP || 0, likely: 0, lean: 0 }, tossup: 0, OTHER: 0 };
  for (const r of races) {
    const t = tier(r);
    if (t === "tossup" || t === "none") counts.tossup++;
    else if (r.leader_party === "DEM" || r.leader_party === "REP") counts[r.leader_party][t]++;
    else counts.OTHER++;
  }
  const segs = [
    ["DEM", "proj", "--dem-proj"], ["DEM", "likely", "--dem-likely"], ["DEM", "lean", "--dem-lean"],
    ["tossup", null, "--tossup"], ["OTHER", null, "--oth"],
    ["REP", "lean", "--rep-lean"], ["REP", "likely", "--rep-likely"], ["REP", "proj", "--rep-proj"],
  ].map(([p, t, color]) => ({ p, t, color, n: t ? counts[p][t] : counts[p] })).filter((s) => s.n > 0);
  const bar = h("div", { class: "seatbar", role: "img",
    "aria-label": `${name}: Democrats ${counts.DEM.proj + counts.DEM.likely + counts.DEM.lean} safe or leaning, Republicans ${counts.REP.proj + counts.REP.likely + counts.REP.lean}, ${counts.tossup} toss-ups. ${need} needed.` });
  for (const s of segs) {
    const fill = cssVar(s.color);
    const label = s.p === "tossup" ? `${s.n}` : s.t === "proj" ? `${s.n}` : "";
    const seg = h("div", { class: `seg ${s.p === "REP" ? "end" : ""}`, style: { flex: `${s.n} 1 0`, background: fill },
      title: s.p === "tossup" ? `${s.n} toss-ups` : `${PARTY_NAME[s.p] || "Other"} — ${s.t === "proj" ? "projected or not up" : s.t}: ${s.n}` },
      label && s.n / total > 0.06 ? h("span", { style: { color: isDarkFill(fill) ? "#fff" : "#0b0b0b" } }, label) : null);
    bar.append(seg);
  }
  bar.append(h("div", { class: "majority", style: { left: `${(need / total) * 100}%` } }, h("b", {}, `${need}`)));
  const cp = chamber.control_prob || {}, es = chamber.expected_seats || {};
  return h("section", { class: "card" },
    h("h2", {}, `${name} control`),
    h("p", { class: "sub" }, `Chance of winning the majority · ${fmtInt.format(chamber.simulations)} simulations`),
    h("div", { class: "odds" },
      h("div", { class: "side" }, h("span", { class: "pct" }, probText(cp.DEM)), h("span", { class: "who" }, h("i", { class: "swatch DEM" }), "Democrats")),
      h("div", { class: "side right" }, h("span", { class: "pct" }, probText(cp.REP)), h("span", { class: "who" }, "Republicans", h("i", { class: "swatch REP" })))),
    bar,
    h("div", { class: "legend" },
      h("span", {}, h("i", { class: "swatch", style: { background: cssVar("--dem-proj") } }), "Projected / not up"),
      h("span", {}, h("i", { class: "swatch", style: { background: cssVar("--dem-likely") } }), "Likely"),
      h("span", {}, h("i", { class: "swatch", style: { background: cssVar("--dem-lean") } }), "Lean"),
      h("span", {}, h("i", { class: "swatch", style: { background: cssVar("--tossup") } }), "Toss-up")),
    h("p", { class: "expect num" }, `Expected seats: D ${(es.DEM ?? 0).toFixed(1)} · R ${(es.REP ?? 0).toFixed(1)}`),
    h("div", { class: "section-title" }, "Simulated Democratic seat totals"),
    seatHistogram(chamber, "DEM"));
}

// ---------- tile map ----------
function tileMap(racesByOffice, initial = "senate") {
  const card = h("section", { class: "card" });
  let current = initial;
  const control = h("div", { class: "seg-control", role: "group", "aria-label": "Map layer" });
  const map = h("div", { class: "tilemap" });
  const draw = () => {
    control.querySelectorAll("button").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.v === current)));
    const byState = {};
    for (const r of racesByOffice[current] || []) byState[r.state] = r;
    map.replaceChildren();
    for (const [code, [c, row]] of Object.entries(TILES)) {
      const r = byState[code];
      const fill = r ? tierFill(r) : cssVar("--none");
      const style = { gridColumn: `${c + 1}`, gridRow: `${row + 1}`, background: fill };
      const label = r ? `${STATE_NAMES[code]}: ${r.leader_party ? `${PARTY_NAME[r.leader_party]} ${probText(r.leader_prob)}` : "awaiting"} (${STATUS_LABEL[r.status]})` : `${STATE_NAMES[code]}: no ${current} race`;
      const tile = r
        ? h("a", { class: `tile race${isDarkFill(fill) ? " dark-fill" : ""}`, href: raceHref(r.contest_key), style, title: label, "aria-label": label },
            code, h("small", { class: "num" }, r.leader_party ? `${PARTY_LETTER[r.leader_party]} ${probText(r.leader_prob)}` : "—"))
        : h("div", { class: "tile", style, title: label, "aria-label": label }, code);
      map.append(tile);
    }
  };
  for (const [v, label] of [["senate", "Senate"], ["governor", "Governor"]]) {
    control.append(h("button", { type: "button", "data-v": v, onclick: () => { current = v; draw(); } }, label));
  }
  const legend = h("div", { class: "tier-legend" },
    ...[["--dem-proj", "D projected"], ["--dem-likely", "D likely"], ["--dem-lean", "D lean"], ["--tossup", "Toss-up"],
       ["--rep-lean", "R lean"], ["--rep-likely", "R likely"], ["--rep-proj", "R projected"], ["--none", "No race"]]
      .map(([v, t]) => h("span", {}, h("i", { class: "sw", style: { background: cssVar(v) } }), t)));
  card.append(h("div", { class: "map-head" }, h("div", {}, h("h2", {}, "Map"), h("p", { class: "sub" }, "Shade = leader and model confidence")), control), map, legend);
  draw();
  return card;
}

// ---------- race rows ----------
function raceRow(r) {
  const cands = (r.candidates || []).filter((c) => c.label).slice(0, 2);
  const total = r.votes_counted || 0;
  const body = cands.length
    ? cands.map((c) => h("div", {},
        h("div", { class: "cand-line" }, h("span", { class: "name" }, partyTag(c.party), h("span", { class: "n" }, c.label)),
          h("span", { class: "val num" }, total ? pct(c.share, 1) : "—")),
        h("div", { class: "bar" }, h("i", { style: { width: `${(c.share || 0) * 100}%`, background: partyColor(c.party) } }))))
    : [h("div", { class: "cand-line" }, h("span", { class: "name" }, h("span", { class: "n" }, "Awaiting results")), h("span", {}))];
  return h("a", { class: "race-row", href: raceHref(r.contest_key) },
    h("div", { class: "where" }, raceTitle(r), h("small", {}, officeLabel(r))),
    h("div", {}, body, h("div", { style: { fontSize: "12px", color: "var(--muted)" } },
      r.pct_reporting != null ? `${Math.round(r.pct_reporting)}% reporting · ${fmtCompact.format(total)} votes` : total ? `${fmtCompact.format(total)} votes` : "")),
    h("div", { class: "prob-chip" }, h("b", { class: "num" }, r.leader_party ? `${PARTY_LETTER[r.leader_party]} ${probText(r.leader_prob)}` : "—"), statusChip(r)));
}

// ---------- views ----------
async function viewOverview() {
  const [summary, senate, governor, house] = await Promise.all([
    api.summary(), api.races("senate"), api.races("governor"), api.races("house"),
  ]);
  setMode(summary.data_mode, summary.last_projection_at);
  const feeds = summary.feeds.length
    ? summary.feeds.map((f) => h("div", { class: "feed-row" }, h("span", {}, f.job_name), h("span", { class: "num", style: { color: "var(--ink-2)" } }, `${f.status} · ${timeText(f.started_at)}`)))
    : [h("p", { class: "empty" }, "No live feed has run yet.")];
  return [
    modeBanner(summary.data_mode),
    h("div", { class: "grid two" }, chamberCard("Senate", summary.chambers.senate, senate.races), chamberCard("House", summary.chambers.house, house.races)),
    h("div", { class: "section-title" }, "Races"),
    h("div", { class: "grid main-side" },
      tileMap({ senate: senate.races, governor: governor.races }),
      h("section", { class: "card" }, h("h2", {}, "Races to watch"),
        h("p", { class: "sub" }, summary.races_reporting ? "Closest contests with votes counted" : "Closest contests by pre-results estimate"),
        h("div", { class: "races" }, summary.watch.length ? summary.watch.map(raceRow) : h("p", { class: "empty" }, "Nothing close right now.")))),
    h("div", { class: "grid two", style: { marginTop: "16px" } },
      h("section", { class: "card" }, h("h2", {}, "Feeds"), h("p", { class: "sub" }, "Latest run of each live job"), ...feeds),
      h("section", { class: "card" }, h("h2", {}, "How to read this"),
        h("p", { class: "sub" }, `Model ${summary.model_version}`),
        h("p", { style: { fontSize: "13px", margin: 0, color: "var(--ink-2)" } },
          "Win probabilities come from simulating how the uncounted vote could break, starting from each seat's last result and updating as votes are counted. ",
          "“Model projection” means the model is ≥99.5% confident — it is not an official result or a media race call. Results stay preliminary until the state certifies them."))),
  ];
}

async function viewOffice(office) {
  const data = await api.races(office);
  setMode(data.data_mode, null);
  const states = [...new Set(data.races.map((r) => r.state))].sort();
  const q = h("input", { type: "search", placeholder: "Search race or candidate", "aria-label": "Search" });
  const st = h("select", { "aria-label": "State" }, h("option", { value: "" }, "All states"), states.map((s) => h("option", { value: s }, STATE_NAMES[s] || s)));
  const status = h("select", { "aria-label": "Status" }, h("option", { value: "" }, "Any status"),
    Object.entries(STATUS_LABEL).map(([k, v]) => h("option", { value: k }, v)));
  const tbody = h("tbody");
  const render = () => {
    const term = q.value.trim().toLowerCase();
    const rows = data.races.filter((r) => (!st.value || r.state === st.value) && (!status.value || r.status === status.value) &&
      (!term || raceTitle(r).toLowerCase().includes(term) || (STATE_NAMES[r.state] || "").toLowerCase().includes(term) ||
        (r.candidates || []).some((c) => (c.label || "").toLowerCase().includes(term))));
    tbody.replaceChildren(...rows.map((r) => {
      const [a, b] = (r.candidates || []).filter((c) => c.label);
      const margin = a && b ? (a.votes - b.votes) : null;
      return h("tr", { onclick: () => (location.hash = raceHref(r.contest_key)) },
        h("td", {}, h("a", { href: raceHref(r.contest_key) }, raceTitle(r)), " ", h("span", { style: { color: "var(--muted)", fontSize: "12px" } }, officeLabel(r))),
        h("td", {}, a ? h("span", { class: "name", style: { display: "inline-flex", gap: "6px", alignItems: "center" } }, partyTag(a.party), a.label)
          : r.leader_party ? h("span", {}, partyTag(r.leader_party), " favored") : "—"),
        h("td", { class: "r num" }, r.leader_party ? `${PARTY_LETTER[r.leader_party]} ${probText(r.leader_prob)}` : "—"),
        h("td", { class: "r num" }, r.pct_reporting != null ? `${Math.round(r.pct_reporting)}%` : "—"),
        h("td", { class: "r num" }, margin != null ? `${margin >= 0 ? "+" : ""}${fmtInt.format(margin)}` : "—"),
        h("td", {}, statusChip(r)));
    }));
  };
  [q, st, status].forEach((el) => el.addEventListener("input", render));
  render();
  const title = office === "senate" ? "Senate races" : office === "house" ? "House races" : "Governor races";
  return [
    modeBanner(data.data_mode),
    h("section", { class: "card" }, h("h2", {}, title), h("p", { class: "sub" }, `${data.races.length} contests`),
      h("div", { class: "filters" }, q, st, status),
      h("div", { class: "table-wrap" }, h("table", { class: "data" },
        h("thead", {}, h("tr", {}, h("th", {}, "Race"), h("th", {}, "Leading"), h("th", { class: "r" }, "Win prob."),
          h("th", { class: "r" }, "Reporting"), h("th", { class: "r" }, "Vote margin"), h("th", {}, "Status"))), tbody))),
  ];
}

function candidateCard(c, r, maxShare) {
  const scale = (v) => `${Math.min(100, (v / maxShare) * 100)}%`;
  const color = partyColor(c.party);
  return h("div", { class: "cand-card" },
    h("div", { class: "nm" }, partyTag(c.party), h("span", {}, c.label), c.party_label ? h("span", { style: { color: "var(--muted)", fontWeight: 400, fontSize: "13px" } }, c.party_label) : null),
    h("div", { class: "votes" }, h("b", { class: "num" }, r.votes_counted ? fmtInt.format(c.votes) : "—"), h("small", { class: "num" }, r.votes_counted ? pct(c.share, 1) : "")),
    h("div", { class: "range", role: "img", "aria-label": `Projected final share ${pct(c.proj_share, 1)}, 80% range ${pct(c.proj_low, 1)} to ${pct(c.proj_high, 1)}` },
      h("div", { class: "track" }),
      maxShare > 0.5 ? h("div", { class: "half", style: { left: scale(0.5) } }) : null,
      h("div", { class: "band", style: { left: scale(c.proj_low), width: `calc(${scale(c.proj_high)} - ${scale(c.proj_low)})`, background: color } }),
      r.votes_counted ? h("div", { class: "now", style: { left: scale(c.share || 0), background: cssVar("--ink-2") } }) : null,
      h("div", { class: "mean", style: { left: scale(c.proj_share), background: color } })),
    h("div", { class: "range-cap num" },
      h("span", {}, `Projected final ${pct(c.proj_share, 1)} (80% range ${pct(c.proj_low, 1)}–${pct(c.proj_high, 1)})`),
      h("span", {}, `Win ${probText(c.win_prob)}`, c.majority_prob != null ? ` · Majority (avoids runoff) ${probText(c.majority_prob)}` : "")));
}

async function viewRace(key) {
  const d = await api.race(key);
  setMode(d.data_mode, d.race.computed_at);
  const r = d.race;
  const cands = (r.candidates || []).filter((c) => c.label).sort((a, b) => (b.votes - a.votes) || (b.win_prob - a.win_prob));
  const maxShare = Math.min(1, Math.max(0.6, ...cands.map((c) => c.proj_high || 0)) + 0.05);
  const head = h("div", { class: "race-head" },
    h("div", {}, h("h1", {}, `${STATE_NAMES[r.state] || r.state} ${officeLabel(r)}${r.office === "house" ? ` · ${raceTitle(r)}` : ""}`),
      h("div", { class: "meta" }, statusChip(r), h("span", {}, `Certification: ${r.certification_status.replace("_", " ")}`), h("span", { class: "num" }, `Updated ${timeText(r.computed_at)}`))),
    h("a", { href: "#overview", class: "icon-btn", style: { textDecoration: "none" } }, "← Overview"));

  const exp = r.expected_total || 0;
  const reporting = h("section", { class: "card" }, h("h2", {}, "Votes counted"),
    r.votes_counted
      ? [h("div", { class: "progress", role: "img", "aria-label": `${fmtInt.format(r.votes_counted)} of about ${fmtInt.format(exp)} expected votes counted` },
          h("i", { style: { width: `${Math.min(100, (r.votes_counted / Math.max(exp, 1)) * 100)}%` } })),
         h("p", { class: "sub num", style: { margin: 0 } },
           `${fmtInt.format(r.votes_counted)} counted · about ${fmtCompact.format(exp)} expected`,
           r.pct_reporting != null ? ` · source reports ${Math.round(r.pct_reporting)}%` : "")]
      : h("p", { class: "empty" }, "No votes reported yet."));

  const probBlock = h("section", { class: "card" }, h("h2", {}, "Chance of winning"),
    r.leader_party ? h("div", { style: { display: "flex", alignItems: "baseline", gap: "10px", margin: "6px 0" } },
      h("span", { class: "big-prob" }, probText(r.leader_prob)),
      h("span", { class: "who", style: { color: "var(--ink-2)", display: "inline-flex", gap: "6px", alignItems: "center" } }, partyTag(r.leader_party), r.leader || PARTY_NAME[r.leader_party])) : null,
    h("p", { class: "note" }, STATUS_LABEL[r.status], r.status === "prior_only" && d.prior ? ` — based on ${d.prior.basis}` : ""));

  const parties = [...new Set(d.history.flatMap((p) => Object.keys(p.prob)))].filter((p) => p);
  const times = d.history.map((p) => new Date(p.t));
  const probSeries = parties.map((p) => ({ name: PARTY_NAME[p] || p, color: partyColor(p), points: d.history.map((s, i) => ({ t: times[i], y: s.prob[p] ?? 0 })) }));
  const shareNames = cands.slice(0, 4).map((c) => c.label);
  const shareSeries = shareNames.map((n) => {
    const c = cands.find((x) => x.label === n);
    return { name: n, color: partyColor(c.party), points: d.history.filter((s) => s.share[n] != null).map((s) => ({ t: new Date(s.t), y: s.share[n] })) };
  }).filter((s) => s.points.length);
  const historyTable = h("details", { class: "table-view" }, h("summary", {}, "View history as table"),
    h("div", { class: "table-wrap" }, h("table", { class: "data" },
      h("thead", {}, h("tr", {}, h("th", {}, "Time"), h("th", { class: "r" }, "Votes"), h("th", { class: "r" }, "Reporting"), parties.map((p) => h("th", { class: "r" }, `${PARTY_LETTER[p]} win`)))),
      h("tbody", {}, d.history.slice().reverse().map((s) => h("tr", {}, h("td", { class: "num" }, timeText(s.t)), h("td", { class: "r num" }, fmtInt.format(s.votes)),
        h("td", { class: "r num" }, `${Math.round(s.pct)}%`), parties.map((p) => h("td", { class: "r num" }, probText(s.prob[p])))))))));

  const side = h("div", { class: "grid" },
    probBlock, reporting,
    h("section", { class: "card" }, h("h2", {}, "Context"),
      h("dl", { class: "kv" },
        h("dt", {}, "Baseline"), h("dd", {}, d.prior ? `${d.prior.basis}${d.prior.basis !== "none" ? ` (${d.prior.prior_margin >= 0 ? "D" : "R"}+${Math.abs(d.prior.prior_margin * 100).toFixed(1)})` : ""}` : "—"),
        h("dt", {}, "Results source"), h("dd", {}, d.results_source_rows[0] ? `${d.results_source_rows[0].source} · ${d.results_source_rows[0].result_stage}` : "—"),
        h("dt", {}, "Race calls"), h("dd", {}, d.race_calls.length ? d.race_calls.map((c) => `${c.caller}: ${c.winner_label} (${timeText(c.called_at)})`).join("; ") : "None recorded")),
      d.notes.map((n) => h("p", { class: "note" }, n))));

  return [
    modeBanner(d.data_mode), head,
    h("div", { class: "grid main-side" },
      h("div", { class: "grid" },
        h("section", { class: "card" }, h("h2", {}, "Candidates"), h("p", { class: "sub" }, "Votes counted, and the model's projected final share (dot, with 80% range)"),
          cands.length ? cands.map((c) => candidateCard(c, r, maxShare)) : h("p", { class: "empty" }, "Candidate list arrives with the first results.")),
        h("section", { class: "card" }, h("h2", {}, "Win probability over time"), lineChart({ series: probSeries, title: "Win probability over time" })),
        shareSeries.length ? h("section", { class: "card" }, h("h2", {}, "Vote share as counted"),
          lineChart({ series: shareSeries, yMin: 0, yMax: Math.min(1, Math.ceil((Math.max(...shareSeries.flatMap((s) => s.points.map((p) => p.y))) + 0.05) * 10) / 10), yFmt: (v) => pct(v), title: "Vote share as counted" }), historyTable) : null),
      side),
  ];
}

// ---------- snapshot mode ----------
function snapshotBar() {
  const group = h("div", { class: "seg-control", role: "group", "aria-label": "Dataset" },
    Object.entries(DATASETS).map(([k, label]) => h("button", { type: "button", "aria-pressed": String(k === dataset),
      onclick: () => { dataset = k; try { localStorage.setItem("dataset", k); } catch { /* storage unavailable */ } render(); } }, label)));
  return h("div", { class: "snapshot-bar" },
    h("span", {}, "Static snapshot of the dashboard. Live updates need the server (", h("code", {}, "python -m scripts.run_live"), ")."), group);
}

// ---------- router + live updates ----------
let rendering = false, pending = false;
async function render() {
  if (rendering) { pending = true; return; }
  rendering = true;
  const route = location.hash.replace(/^#\/?/, "");
  const isRace = route.startsWith("race-");
  const name = isRace ? "race" : route;
  document.querySelectorAll(".tabs a").forEach((a) => {
    if (a.dataset.route === (["senate", "house", "governor"].includes(name) ? name : isRace ? "" : "overview")) a.setAttribute("aria-current", "page");
    else a.removeAttribute("aria-current");
  });
  try {
    const nodes = isRace ? await viewRace(route.slice(5))
      : ["senate", "house", "governor"].includes(name) ? await viewOffice(name) : await viewOverview();
    if (STATIC) nodes.unshift(snapshotBar());
    const y = window.scrollY;
    app.replaceChildren(...nodes.filter(Boolean));
    window.scrollTo(0, y);
  } catch (err) {
    app.replaceChildren(h("div", { class: "banner info" }, `Could not load data (${err.message}). Retrying on the next update.`));
  } finally {
    rendering = false;
    if (pending) { pending = false; render(); }
  }
}

let debounce;
function scheduleRefresh() { clearTimeout(debounce); debounce = setTimeout(render, 800); }
function connectStream() {
  if (STATIC) return; // a snapshot has no server to stream from
  if (!("EventSource" in window)) { setInterval(render, 30000); return; }
  const es = new EventSource("/live/stream");
  let first = true;
  es.addEventListener("update", () => { if (first) { first = false; return; } scheduleRefresh(); });
  es.onerror = () => { /* EventSource reconnects automatically (retry: 5000) */ };
}

// theme toggle: OS default, viewer override wins both ways
const themeBtn = document.getElementById("theme");
if (STATIC) themeBtn.hidden = true; // the hosting viewer controls the theme
const savedTheme = (() => { try { return localStorage.getItem("theme"); } catch { return null; } })();
if (savedTheme) document.documentElement.dataset.theme = savedTheme;
themeBtn.addEventListener("click", () => {
  const dark = document.documentElement.dataset.theme
    ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("theme", document.documentElement.dataset.theme); } catch { /* storage unavailable */ }
  render();
});

window.addEventListener("hashchange", () => { window.scrollTo(0, 0); render(); });
render();
connectStream();
