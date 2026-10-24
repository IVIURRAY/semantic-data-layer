// Tiny deterministic SVG timeline engine for the README animations.
//
// Scenes build SVG with the helpers below and register tweens. The renderer
// (render.mjs) calls window.seek(t) for each frame and screenshots, so every
// frame is a pure function of t: no requestAnimationFrame, no timing drift.

const NS = "http://www.w3.org/2000/svg";
const W = 1200, H = 675;

const C = {
  bg: "#0B1220", panel: "#111A2E", panel2: "#16213A", line: "#26324D",
  text: "#E6EAF2", muted: "#93A0B8", dim: "#5B6884",
  pg: "#4F8EF7", lake: "#22C08A", sqlite: "#F2A93B", csv: "#EC5FA6",
  sem: "#9B7BFF", duck: "#FFD166", good: "#3DDC97", bad: "#FF6B6B",
};
const FONT = "Inter, system-ui, sans-serif";
const MONO = "'JetBrains Mono', 'DejaVu Sans Mono', monospace";

const svg = document.createElementNS(NS, "svg");
svg.setAttribute("width", W);
svg.setAttribute("height", H);
svg.setAttribute("viewBox", `0 0 ${W} ${H}`);
document.body.appendChild(svg);
const style = document.createElement("style");
style.textContent = `
  html,body{margin:0;background:${C.bg};}
  svg{display:block;font-family:${FONT};}
  .a{transform-box:fill-box;transform-origin:center;}
  text{white-space:pre;}
`;
document.head.appendChild(style);
el("rect", { x: 0, y: 0, width: W, height: H, fill: C.bg });

// ------------------------------------------------------------------ primitives

function el(tag, attrs = {}, parent = svg) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) n.setAttribute(k, v);
  parent.appendChild(n);
  return n;
}

// A group that can be animated (opacity / x / y / s). Starts hidden by default.
function group(parent = svg, init = { o: 0 }) {
  const g = el("g", { class: "a" }, parent);
  set(g, init);
  return g;
}

function text(parent, x, y, str, o = {}) {
  const t = el("text", {
    x, y,
    fill: o.fill || C.text,
    "font-size": o.size || 16,
    "font-weight": o.weight || 400,
    "font-family": o.mono ? MONO : FONT,
    "text-anchor": o.anchor || "start",
    "dominant-baseline": "middle",
  }, parent);
  if (o.ls) t.setAttribute("letter-spacing", o.ls);
  t.textContent = str;
  t._full = str;
  return t;
}

function box(parent, x, y, w, h, o = {}) {
  return el("rect", {
    x, y, width: w, height: h, rx: o.r ?? 12,
    fill: o.fill || C.panel, stroke: o.stroke || C.line, "stroke-width": o.sw ?? 1.5,
    ...(o.dash ? { "stroke-dasharray": o.dash } : {}),
  }, parent);
}

function chip(parent, x, y, label, color, o = {}) {
  const g = el("g", {}, parent);
  const size = o.size || 13;
  const w = o.w || Math.round(label.length * size * (o.mono ? 0.62 : 0.53) + 22);
  const h = o.h || size + 14;
  el("rect", { x, y, width: w, height: h, rx: h / 2, fill: color + "22", stroke: color, "stroke-width": 1.2 }, g);
  text(g, x + w / 2, y + h / 2 + 1, label, { size, fill: o.fill || color, anchor: "middle", mono: o.mono, weight: o.weight || 500 });
  g._w = w;
  return g;
}

function path(parent, d, color, o = {}) {
  const p = el("path", {
    d, fill: "none", stroke: color, "stroke-width": o.sw || 2,
    "stroke-linecap": "round", ...(o.dash ? { "stroke-dasharray": o.dash } : {}),
    ...(o.marker ? { "marker-end": `url(#${o.marker})` } : {}),
  }, parent);
  return p;
}

function arrowHead(id, color) {
  let defs = svg.querySelector("defs") || el("defs", {}, svg);
  const m = el("marker", { id, viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 7, markerHeight: 7, orient: "auto-start-reverse" }, defs);
  el("path", { d: "M0,0 L10,5 L0,10 z", fill: color }, m);
  return id;
}

// Colours for the four parts of a semantic query, used consistently everywhere.
const KIND = { metrics: "#FFD166", dimensions: "#8AB4FF", filters: "#3DDC97", as_of: "#FF9F5A" };

function charW(size) {
  const t = text(svg, -1000, -1000, "0000000000", { size, mono: true });
  const w = t.getComputedTextLength() / 10;
  t.remove();
  return w;
}

// Monospace code block. A line is a string, or {t, hl, c, tag}: `hl` is a
// substring to highlight in colour `c`, `tag` a label drawn at column tagX.
// Returns the highlight groups (hidden) so a scene can reveal them in turn.
function code(parent, x, y, lines, o = {}) {
  const size = o.size || 15, lh = o.lh || Math.round(size * 1.6), cw = charW(size);
  const hls = [];
  lines.forEach((ln, i) => {
    const L = typeof ln === "string" ? { t: ln } : ln;
    const yy = y + i * lh;
    if (L.hl) {
      const g = group(parent);
      const idx = L.t.indexOf(L.hl);
      el("rect", { x: x + idx * cw - 4, y: yy - size * 0.78, width: L.hl.length * cw + 8, height: size * 1.56, rx: 4,
                   fill: L.c + "2E", stroke: L.c, "stroke-width": 1.3 }, g);
      if (L.tag) text(g, o.tagX, yy, "← " + L.tag, { size: o.tagSize || 13.5, fill: L.c, weight: 600 });
      hls.push(g);
    }
    text(parent, x, yy, L.t, { size, mono: true, fill: L.fill || o.fill || C.text });
  });
  return hls;
}

// ------------------------------------------------------------------ timeline

const nodes = new Set();
let DURATION = 0;

function set(n, props) {
  n._init = { o: 1, x: 0, y: 0, s: 1, draw: 1, along: 0, type: 1, ...(n._init || {}), ...props };
  n._tw = n._tw || [];
  nodes.add(n);
  return n;
}

const EASE = {
  lin: (p) => p,
  out: (p) => 1 - Math.pow(1 - p, 3),
  inOut: (p) => (p < 0.5 ? 4 * p * p * p : 1 - Math.pow(-2 * p + 2, 3) / 2),
  back: (p) => { const c = 1.6; return 1 + (c + 1) * Math.pow(p - 1, 3) + c * Math.pow(p - 1, 2); },
};

// tween node from its current state to `to` over [start, start + dur]
function tw(n, start, dur, to, ease = "inOut") {
  if (!n._init) set(n, {});
  n._tw.push({ start, dur, to, ease: EASE[ease] });
  n._tw.sort((a, b) => a.start - b.start);
  DURATION = Math.max(DURATION, start + dur);
  return n;
}
const show = (n, t, d = 0.5, extra = {}) => tw(n, t, d, { o: 1, ...extra }, "out");
const hide = (n, t, d = 0.4) => tw(n, t, d, { o: 0 });

// draw a path in (needs set(path, {draw: 0}) first)
function drawIn(p, t, d = 0.8) {
  set(p, { draw: 0, o: 1 });
  p._len = p.getTotalLength();
  return tw(p, t, d, { draw: 1 }, "inOut");
}

// a dot that travels along a path
function packet(parent, p, color, t, d = 1, r = 6) {
  const c = el("circle", { cx: 0, cy: 0, r, fill: color }, parent);
  c._path = p;
  c._len = p.getTotalLength();
  set(c, { o: 0, along: 0 });
  tw(c, t, 0.15, { o: 1 }, "lin");
  tw(c, t, d, { along: 1 }, "inOut");
  tw(c, t + d - 0.05, 0.2, { o: 0 }, "lin");
  return c;
}

function typeIn(t_node, t, d) {
  set(t_node, { type: 0 });
  return tw(t_node, t, d, { type: 1 }, "lin");
}

function hold(seconds) { DURATION += seconds; }

function apply(n, st) {
  n.style.opacity = st.o;
  if (n._path) {
    const pt = n._path.getPointAtLength(st.along * n._len);
    n.setAttribute("cx", pt.x);
    n.setAttribute("cy", pt.y);
  } else if (st.x || st.y || st.s !== 1) {
    n.style.transform = `translate(${st.x}px, ${st.y}px) scale(${st.s})`;
  } else {
    n.style.transform = "";
  }
  if (n._len && !n._path) {
    n.style.strokeDasharray = `${n._len}`;
    n.style.strokeDashoffset = `${n._len * (1 - st.draw)}`;
  }
  if (n._full !== undefined) n.textContent = n._full.slice(0, Math.round(n._full.length * st.type));
}

window.seek = (t) => {
  for (const n of nodes) {
    const st = { ...n._init };
    for (const w of n._tw) {
      if (t < w.start) break;
      const p = w.dur === 0 ? 1 : Math.min(1, (t - w.start) / w.dur);
      // Tweens are applied in start order, so `st` holds the value reached by
      // earlier tweens: each one interpolates from wherever the last left off.
      for (const [k, v] of Object.entries(w.to)) {
        const a = st[k];
        st[k] = a + (v - a) * w.ease(p);
      }
    }
    apply(n, st);
  }
};
window.getDuration = () => DURATION;

// Opened directly in a browser: play on a loop. Under Playwright
// (navigator.webdriver) the renderer drives seek() itself.
window.addEventListener("load", () => {
  if (navigator.webdriver) return;
  const t0 = performance.now();
  const loop = (now) => { window.seek(((now - t0) / 1000) % DURATION); requestAnimationFrame(loop); };
  requestAnimationFrame(loop);
});
