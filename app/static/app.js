"use strict";

// Kolory klas indeksu jakości powietrza (bardzo dobry → bardzo zły) + szary dla podejrzanych.
const INDEX_COLORS = ["#2e9e44", "#9ccc3a", "#f2c80f", "#ef8a17", "#d7301f", "#8c1d40"];
const INDEX_LABELS = ["bardzo dobry", "dobry", "umiarkowany", "dostateczny", "zły", "bardzo zły"];
const SUSPECT_COLOR = "#98a2b3";
const WHO = { pm25: 15, pm10: 45 }; // normy dobowe WHO 2021, µg/m³

const METRICS = {
  pm25: { label: "PM2.5", unit: "µg/m³", kind: "index" },
  pm10: { label: "PM10", unit: "µg/m³", kind: "index" },
  pm1: { label: "PM1", unit: "µg/m³", kind: "index" },
  pressure_sl: { label: "Ciśnienie", unit: "hPa", kind: "ramp", digits: 0,
                 ramp: ["#3b4cc0", "#8db0fe", "#f2f2f2", "#f49a7b", "#b40426"],
                 note: "Zredukowane do poziomu morza — porównywalne między miastami." },
  humidity: { label: "Wilgotność", unit: "%", kind: "ramp", digits: 0, fixed: [20, 100],
              ramp: ["#a6611a", "#dfc27d", "#f5f5f5", "#80cdc1", "#018571"],
              note: "Mierzona w obudowie paczkomatu." },
  temperature: { label: "Temperatura", unit: "°C", kind: "ramp", digits: 1,
                 ramp: ["#313695", "#74add1", "#ffffbf", "#f46d43", "#a50026"],
                 note: "Uwaga: mierzona w obudowie paczkomatu — w słońcu mocno zawyżona. To nie jest temperatura powietrza." },
};

const state = {
  cfg: null, data: null, metric: "pm25", view: "points",
  showSuspect: false, showGios: true, domains: {}, hexRes: null, searchMarker: null, origin: null,
};

const map = new maplibregl.Map({
  container: "map",
  // OpenFreeMap: darmowe kafelki wektorowe OSM bez klucza API
  style: "https://tiles.openfreemap.org/styles/positron",
  attributionControl: { customAttribution: "dane: InPost, GIOŚ" },
  center: [19.4, 52.0], zoom: 5.6, maxZoom: 17, minZoom: 4,
});
map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
map.addControl(new maplibregl.GeolocateControl({ trackUserLocation: false }), "top-right");
map.addControl(new maplibregl.ScaleControl({ unit: "metric" }), "bottom-right");

// ------------------------------------------------------------------ pomocnicze

const $ = s => document.querySelector(s);
// Wszystko, co przychodzi z ShipX / GIOŚ / Nominatim, jest obce — escapujemy przed wstawieniem do HTML-a.
const esc = v => String(v ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const safeUrl = u => (/^https:\/\/(www\.)?inpost\.pl\//.test(u || "") ? u : null);
const fmt = (v, d = 1) => (v === null || v === undefined ? "–" : Number(v).toLocaleString("pl-PL", { maximumFractionDigits: d, minimumFractionDigits: d }));

function thresholds(metric) {
  return state.cfg[`${metric}_thresholds`];
}

function indexClass(metric, v) {
  const t = thresholds(metric);
  if (v === null || v === undefined || !t) return null;
  let i = 0;
  while (i < t.length && v > t[i]) i++;
  return i;
}

function rampColor(metric, v) {
  const m = METRICS[metric];
  const [lo, hi] = state.domains[metric] || [0, 1];
  const t = Math.max(0, Math.min(1, (v - lo) / (hi - lo || 1)));
  const pos = t * (m.ramp.length - 1);
  const i = Math.min(Math.floor(pos), m.ramp.length - 2);
  return mix(m.ramp[i], m.ramp[i + 1], pos - i);
}

function mix(a, b, t) {
  const pa = [1, 3, 5].map(i => parseInt(a.slice(i, i + 2), 16));
  const pb = [1, 3, 5].map(i => parseInt(b.slice(i, i + 2), 16));
  return "#" + pa.map((x, i) => Math.round(x + (pb[i] - x) * t).toString(16).padStart(2, "0")).join("");
}

function colorFor(metric, v) {
  if (v === null || v === undefined) return SUSPECT_COLOR;
  return METRICS[metric].kind === "index" ? INDEX_COLORS[indexClass(metric, v)] : rampColor(metric, v);
}

// Wyrażenie MapLibre kolorujące po wartości (dla warstw kropek, grup i sześciokątów).
function colorExpr(metric, valueExpr) {
  const m = METRICS[metric];
  if (m.kind === "index") {
    const t = thresholds(metric);
    const step = ["step", valueExpr, INDEX_COLORS[0]];
    t.forEach((x, i) => step.push(x + 0.0001, INDEX_COLORS[i + 1]));
    return step;
  }
  const [lo, hi] = state.domains[metric];
  const expr = ["interpolate", ["linear"], valueExpr];
  m.ramp.forEach((c, i) => expr.push(lo + (hi - lo) * i / (m.ramp.length - 1), c));
  return expr;
}

function quantile(sorted, q) {
  if (!sorted.length) return 0;
  const pos = (sorted.length - 1) * q;
  const lo = Math.floor(pos);
  return sorted[lo] + (sorted[Math.min(lo + 1, sorted.length - 1)] - sorted[lo]) * (pos - lo);
}

function computeDomains() {
  for (const [k, m] of Object.entries(METRICS)) {
    if (m.kind !== "ramp") continue;
    if (m.fixed) { state.domains[k] = m.fixed; continue; }
    const vals = state.data.features.filter(f => !f.properties.suspect && f.properties[k] != null)
      .map(f => f.properties[k]).sort((a, b) => a - b);
    let lo = quantile(vals, 0.03), hi = quantile(vals, 0.97);
    if (hi - lo < 1) { lo -= 1; hi += 1; }
    state.domains[k] = [Math.floor(lo), Math.ceil(hi)];
  }
}

function distanceKm(a, b) {
  const R = 6371, r = Math.PI / 180;
  const dLat = (b[1] - a[1]) * r, dLon = (b[0] - a[0]) * r;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(a[1] * r) * Math.cos(b[1] * r) * Math.sin(dLon / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(h));
}

function ago(ts) {
  if (!ts) return "–";
  const min = Math.round((Date.now() / 1000 - ts) / 60);
  if (min < 1) return "przed chwilą";
  if (min < 60) return `${min} min temu`;
  const h = Math.round(min / 60);
  return h < 48 ? `${h} h temu` : `${Math.round(h / 24)} dni temu`;
}

function visibleFeatures() {
  return state.data.features.filter(f => state.showSuspect || !f.properties.suspect);
}

// ------------------------------------------------------------------ warstwy

function clusterProperties() {
  const props = {};
  // podejrzane czujniki nie wpływają na kolor grupy, nawet gdy są pokazane
  for (const k of Object.keys(METRICS)) {
    const missing = ["any", ["get", "suspect"], ["==", ["get", k], null]];
    props[`${k}_max`] = ["max", ["case", missing, -9999, ["get", k]]];
    props[`${k}_sum`] = ["+", ["case", missing, 0, ["get", k]]];
    props[`${k}_n`] = ["+", ["case", missing, 0, 1]];
  }
  return props;
}

// Nazwy na mapie po polsku (styl OpenFreeMap domyślnie bierze name:latin / angielskie)
function polishLabels() {
  for (const l of map.getStyle().layers) {
    if (l.type !== "symbol" || !map.getLayoutProperty(l.id, "text-field")) continue;
    map.setLayoutProperty(l.id, "text-field", ["coalesce", ["get", "name:pl"], ["get", "name"]]);
  }
}

function setupLayers() {
  polishLabels();
  map.addSource("sensors", {
    type: "geojson", data: { type: "FeatureCollection", features: [] },
    cluster: true, clusterRadius: 38, clusterMaxZoom: 9, clusterProperties: clusterProperties(),
  });
  map.addSource("sensors-flat", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addSource("hex", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addSource("gios", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

  // sześciokąty i plama pod nazwami miejscowości, kropki nad nimi
  const firstLabel = map.getStyle().layers.find(l => l.type === "symbol")?.id;
  map.addLayer({ id: "hex-fill", type: "fill", source: "hex", paint: { "fill-opacity": 0.6, "fill-color": "#ccc" } }, firstLabel);
  map.addLayer({ id: "hex-line", type: "line", source: "hex", paint: { "line-color": "#fff", "line-width": 0.6 } }, firstLabel);

  map.addLayer({
    id: "heat", type: "heatmap", source: "sensors-flat", maxzoom: 13,
    paint: {
      "heatmap-radius": ["interpolate", ["linear"], ["zoom"], 5, 14, 9, 30, 13, 60],
      "heatmap-intensity": ["interpolate", ["linear"], ["zoom"], 5, 0.6, 12, 1.4],
      "heatmap-opacity": 0.75,
    },
  }, firstLabel);

  map.addLayer({
    id: "clusters", type: "circle", source: "sensors", filter: ["has", "point_count"],
    paint: {
      "circle-radius": ["step", ["get", "point_count"], 13, 10, 17, 50, 22, 200, 28],
      "circle-stroke-width": 2, "circle-stroke-color": "#fff", "circle-opacity": 0.9,
    },
  });
  map.addLayer({
    id: "cluster-count", type: "symbol", source: "sensors", filter: ["has", "point_count"],
    layout: { "text-field": ["get", "point_count_abbreviated"], "text-size": 11, "text-font": ["Noto Sans Bold"] },
    paint: { "text-color": "#fff", "text-halo-color": "rgba(0,0,0,.35)", "text-halo-width": 1 },
  });
  map.addLayer({
    id: "points", type: "circle", source: "sensors", filter: ["!", ["has", "point_count"]],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 5, 12, 8, 16, 11],
      "circle-stroke-width": 1.5, "circle-stroke-color": "#fff",
    },
  });

  map.addLayer({
    id: "gios", type: "circle", source: "gios",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 5, 12, 9],
      "circle-stroke-width": 2.5, "circle-stroke-color": "#1d2330",
    },
  });

  for (const id of ["points", "gios", "hex-fill", "clusters"]) {
    map.on("mouseenter", id, () => { map.getCanvas().style.cursor = "pointer"; });
    map.on("mouseleave", id, () => { map.getCanvas().style.cursor = ""; });
  }
  map.on("click", "clusters", async e => {
    const f = e.features[0];
    const zoom = await map.getSource("sensors").getClusterExpansionZoom(f.properties.cluster_id);
    map.easeTo({ center: f.geometry.coordinates, zoom });
  });
  map.on("click", "points", e => openSensor(e.features[0].properties.name));
  map.on("click", "gios", e => openGios(e.features[0]));
  map.on("click", "hex-fill", e => {
    if (map.queryRenderedFeatures(e.point, { layers: ["points", "gios"] }).length) return;
    const p = e.features[0].properties, m = METRICS[state.metric];
    new maplibregl.Popup().setLngLat(e.lngLat)
      .setHTML(`<div class="pop"><b>${m.label}: ${fmt(p.value, m.digits ?? 1)} ${m.unit}</b><div class="meta">mediana z ${p.count} czujn.</div></div>`)
      .addTo(map);
  });
  map.on("zoomend", () => { if (state.view === "hex") refreshHex(); });
}

function render() {
  const metric = state.metric, m = METRICS[metric];
  const feats = visibleFeatures();
  const fc = { type: "FeatureCollection", features: feats };
  map.getSource("sensors").setData(fc);
  map.getSource("sensors-flat").setData({ type: "FeatureCollection", features: feats.filter(f => !f.properties.suspect) });

  const val = ["get", metric];
  map.setPaintProperty("points", "circle-color",
    ["case", ["any", ["get", "suspect"], ["==", val, null]], SUSPECT_COLOR, colorExpr(metric, val)]);
  map.setPaintProperty("points", "circle-opacity", ["case", ["==", val, null], 0.25, 1]);
  // grupy: dla pyłów najgorszy czujnik w grupie, dla reszty średnia
  const clusterVal = m.kind === "index" ? ["get", `${metric}_max`]
    : ["/", ["get", `${metric}_sum`], ["max", ["get", `${metric}_n`], 1]];
  map.setPaintProperty("clusters", "circle-color",
    ["case", ["==", ["get", `${metric}_n`], 0], SUSPECT_COLOR, colorExpr(metric, clusterVal)]);

  // plama: tylko dla pyłów (dla ciśnienia czy wilgotności gęstość nie ma sensu)
  const heatOk = m.kind === "index";
  if (heatOk) {
    const t = thresholds(metric);
    map.setPaintProperty("heat", "heatmap-weight", ["interpolate", ["linear"], ["coalesce", val, 0], 0, 0, t[3], 1]);
    const ramp = ["interpolate", ["linear"], ["heatmap-density"], 0, "rgba(0,0,0,0)"];
    INDEX_COLORS.forEach((c, i) => ramp.push(0.15 + i * 0.17, c));
    map.setPaintProperty("heat", "heatmap-color", ramp);
  }
  // plama ma sens tylko dla pyłów — przy innej wielkości przełączamy na PM2.5 i mówimy dlaczego
  if (!heatOk && state.view === "heat") {
    state.metric = "pm25";
    state.heatNote = `Plama działa tylko dla pyłów — przełączono z „${m.label}” na PM2.5.`;
    return render();
  }

  const v = state.view;
  const vis = (id, on) => map.setLayoutProperty(id, "visibility", on ? "visible" : "none");
  vis("clusters", v === "points");
  vis("cluster-count", v === "points");
  vis("heat", v === "heat");
  vis("hex-fill", v === "hex");
  vis("hex-line", v === "hex");
  // w widoku sześciokątów i plamy kropki pojawiają się dopiero przy przybliżeniu
  if (v !== "points") {
    map.setPaintProperty("points", "circle-radius", ["interpolate", ["linear"], ["zoom"], 6, 0, 9, 3, 12, 7, 16, 10]);
  } else {
    map.setPaintProperty("points", "circle-radius", ["interpolate", ["linear"], ["zoom"], 6, 5, 12, 8, 16, 11]);
  }

  const giosOn = state.showGios && (metric === "pm25" || metric === "pm10");
  vis("gios", giosOn);
  map.setPaintProperty("gios", "circle-color",
    ["case", ["==", ["get", metric], null], SUSPECT_COLOR, colorExpr(metric, ["get", metric])]);

  if (v === "hex") refreshHex(true);
  document.querySelectorAll("#views button").forEach(b => b.classList.toggle("active", b.dataset.view === state.view));
  document.querySelectorAll("#metrics button").forEach(b => b.classList.toggle("active", b.dataset.metric === metric));
  $("#view-hint").textContent = {
    points: "Przy oddaleniu czujniki łączą się w grupy — kolor grupy to najgorszy czujnik (pyły) albo średnia.",
    hex: "Mediana z czujników w każdym sześciokącie. Odporna na pojedyncze zepsute czujniki.",
    heat: "Plama rośnie tam, gdzie jest dużo czujników z wysokim odczytem.",
  }[state.view] + (state.view === "heat" && state.heatNote ? " " + state.heatNote : "");
  state.heatNote = null;
  renderLegend();
  if (state.origin) renderNearest();
}

async function refreshHex(force = false) {
  const base = state.cfg.hex_resolution;
  const res = Math.max(3, Math.min(8, base + Math.floor((map.getZoom() - 6.5) / 1.5)));
  const key = `${res}|${state.metric}|${state.showSuspect}`;
  if (!force && key === state.hexRes) return;
  state.hexRes = key;
  const r = await fetch(`/api/hex?res=${res}&metric=${state.metric}&suspect=${state.showSuspect}`);
  map.getSource("hex").setData(await r.json());
  map.setPaintProperty("hex-fill", "fill-color", colorExpr(state.metric, ["get", "value"]));
}

function renderLegend() {
  const metric = state.metric, m = METRICS[metric];
  let html = "";
  if (m.kind === "index") {
    const t = thresholds(metric);
    INDEX_LABELS.forEach((label, i) => {
      const range = i === 0 ? `≤ ${t[0]}` : i === t.length ? `> ${t[t.length - 1]}` : `${t[i - 1]}–${t[i]}`;
      html += `<div class="row"><span class="sw" style="background:${INDEX_COLORS[i]}"></span>${label} <small>(${range} ${m.unit})</small></div>`;
    });
    if (metric === "pm1") html += `<div class="note">PM1 nie ma oficjalnego indeksu — progi umowne.</div>`;
  } else {
    const [lo, hi] = state.domains[metric];
    html += `<div class="ramp" style="background:linear-gradient(90deg,${m.ramp.join(",")})"></div>
      <div class="ramp-labels"><span>${fmt(lo, 0)} ${m.unit}</span><span>${fmt(hi, 0)} ${m.unit}</span></div>`;
  }
  html += `<div class="row"><span class="sw" style="background:${SUSPECT_COLOR}"></span>podejrzany / brak danych</div>`;
  if (state.showGios && (metric === "pm25" || metric === "pm10"))
    html += `<div class="row"><span class="sw" style="background:#fff;border:2.5px solid #1d2330;border-radius:50%"></span>stacja GIOŚ</div>`;
  if (m.note) html += `<div class="note">${m.note}</div>`;
  $("#legend").innerHTML = html;
}

// ------------------------------------------------------------------ dymki

function featureByName(name) {
  return state.data.features.find(f => f.properties.name === name);
}

async function openSensor(name, fly = false) {
  const f = featureByName(name);
  if (!f) return;
  const p = f.properties;
  const flags = typeof p.flags === "string" ? JSON.parse(p.flags) : p.flags;
  if (fly) map.flyTo({ center: f.geometry.coordinates, zoom: Math.max(map.getZoom(), 13) });
  const cls = indexClass("pm25", p.pm25);
  const badge = cls === null ? "" :
    `<span class="badge" style="background:${p.suspect ? SUSPECT_COLOR : INDEX_COLORS[cls]}">PM2.5: ${INDEX_LABELS[cls]}</span>`;
  const pct = (v, k) => (v == null ? "" : ` <small>${Math.round(v / WHO[k] * 100)}% normy WHO</small>`);
  const rows = [
    ["PM1", fmt(p.pm1) + " µg/m³"],
    ["PM2.5", fmt(p.pm25) + " µg/m³" + pct(p.pm25, "pm25")],
    p.pm4 != null ? ["PM4", fmt(p.pm4) + " µg/m³"] : null,
    ["PM10", fmt(p.pm10) + " µg/m³" + pct(p.pm10, "pm10")],
    ["Ciśnienie (n.p.m.)", fmt(p.pressure_sl, 0) + " hPa"],
    p.gen === "new" ? ["Ciśnienie (na miejscu)", fmt(p.pressure, 0) + " hPa"] : null,
    ["Wilgotność", fmt(p.humidity, 0) + " %"],
    ["Temperatura <small>(obudowa)</small>", fmt(p.temperature) + " °C"],
  ].filter(Boolean);
  const warn = flags.length ? `<div class="warn">⚠ ${flags.map(x => esc(state.cfg.flag_labels[x] || x)).join("; ")}</div>` : "";
  const status = p.status !== "ok" ? `<div class="warn">Ostatnie zapytanie: brak danych z czujnika — pokazany poprzedni odczyt.</div>` : "";
  const html = `<div class="pop">
    <h3>${esc(p.name)}</h3>
    <div class="addr">${esc(p.address)}${p.description ? " · " + esc(p.description) : ""}</div>
    ${badge}
    <table>${rows.map(([a, b]) => `<tr><td>${a}</td><td>${b}</td></tr>`).join("")}</table>
    ${warn}${status}
    <div class="chart" data-name="${esc(p.name)}"></div>
    <div class="meta">Odczyt ${ago(p.ts)} · bez zmian od ${ago(p.changed_at)}${p.elevation != null ? ` · ${Math.round(p.elevation)} m n.p.m.` : ""}
    ${safeUrl(p.page_url) ? ` · <a href="${esc(p.page_url)}" target="_blank" rel="noopener noreferrer">strona paczkomatu</a>` : ""}</div>
  </div>`;
  new maplibregl.Popup({ maxWidth: "320px" }).setLngLat(f.geometry.coordinates).setHTML(html).addTo(map);
  drawChart(p.name);
}

async function drawChart(name) {
  const metric = state.metric, m = METRICS[metric];
  const r = await fetch(`/api/history/${encodeURIComponent(name)}?hours=24`);
  const rows = (await r.json()).filter(x => x[metric] != null);
  const box = [...document.querySelectorAll(".pop .chart")].find(el => el.dataset.name === name);
  if (!box) return;
  if (rows.length < 2) {
    box.innerHTML = `<div class="meta">Wykres 24 h pojawi się po kilku odczytach.</div>`;
    return;
  }
  const W = 280, H = 56, pad = 3;
  const xs = rows.map(x => x.ts), ys = rows.map(x => x[metric]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  const y0 = Math.min(...ys), y1 = Math.max(...ys);
  const sx = t => pad + (W - 2 * pad) * (t - x0) / (x1 - x0 || 1);
  const sy = v => H - pad - (H - 2 * pad) * (v - y0) / (y1 - y0 || 1);
  const pts = rows.map(x => `${sx(x.ts).toFixed(1)},${sy(x[metric]).toFixed(1)}`).join(" ");
  const stroke = m.kind === "index" ? colorFor(metric, Math.max(...ys)) : "#1f6feb";
  box.innerHTML = `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none">
      <polyline points="${pts}" fill="none" stroke="${stroke}" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
    <div class="chart-label"><span>${m.label}, ostatnie 24 h</span><span>${fmt(y0, m.digits ?? 1)}–${fmt(y1, m.digits ?? 1)} ${m.unit}</span></div>`;
}

function openGios(f) {
  const p = f.properties;
  new maplibregl.Popup().setLngLat(f.geometry.coordinates).setHTML(`<div class="pop">
    <h3>Stacja GIOŚ: ${esc(p.name)}</h3><div class="addr">${esc(p.city)}</div>
    <table><tr><td>PM2.5</td><td>${fmt(p.pm25)} µg/m³</td></tr><tr><td>PM10</td><td>${fmt(p.pm10)} µg/m³</td></tr></table>
    <div class="meta">Pomiar oficjalny, średnia godzinowa · ${esc(p.ts || "–")}</div></div>`).addTo(map);
}

// ------------------------------------------------------------------ wyszukiwarka

async function search(q) {
  const box = $("#search-results");
  box.textContent = "Szukam…";
  try {
    const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
    if (!r.ok) throw new Error((await r.json()).detail || r.statusText);
    const res = await r.json();
    if (!res.length) { box.textContent = "Nic nie znaleziono."; return; }
    box.innerHTML = "";
    if (res.length === 1) { goTo(res[0]); box.textContent = ""; return; }
    res.forEach(x => {
      const b = document.createElement("button");
      b.textContent = x.label;
      b.onclick = () => { goTo(x); box.textContent = ""; };
      box.appendChild(b);
    });
  } catch (e) {
    box.textContent = `Błąd wyszukiwania: ${e.message}`;
  }
}

function goTo(x) {
  state.origin = [x.lon, x.lat];
  if (state.searchMarker) state.searchMarker.remove();
  state.searchMarker = new maplibregl.Marker({ color: "#1d2330" }).setLngLat(state.origin).addTo(map);
  map.flyTo({ center: state.origin, zoom: 12 });
  renderNearest();
}

function renderNearest() {
  const metric = state.metric, m = METRICS[metric];
  const list = visibleFeatures().filter(f => f.properties[metric] != null)
    .map(f => ({ f, d: distanceKm(state.origin, f.geometry.coordinates) }))
    .sort((a, b) => a.d - b.d).slice(0, state.cfg.nearest_count);
  $("#nearest-box").hidden = false;
  $("#nearest").innerHTML = list.map(({ f, d }) => {
    const p = f.properties;
    const color = p.suspect ? SUSPECT_COLOR : colorFor(metric, p[metric]);
    return `<li data-name="${esc(p.name)}"><span class="dot" style="background:${color}"></span>
      <span class="what"><b>${esc(p.name)}</b><span>${esc(p.address)}</span></span>
      <span class="val">${fmt(p[metric], m.digits ?? 1)} ${m.unit}<small>${d < 1 ? Math.round(d * 1000) + " m" : fmt(d, 1) + " km"}</small></span></li>`;
  }).join("");
  document.querySelectorAll("#nearest li").forEach(li => { li.onclick = () => openSensor(li.dataset.name, true); });
}

// ------------------------------------------------------------------ statystyki

const num = n => (n ?? 0).toLocaleString("pl-PL");

function renderSummary(st) {
  const cls = st.pm25.median == null ? null : indexClass("pm25", st.pm25.median);
  $("#summary").innerHTML = `
    <div><b>${num(st.sensors_reporting - st.sensors_suspect)}</b><span>czujników działa</span></div>
    <div><b style="color:${cls == null ? "inherit" : INDEX_COLORS[cls]}">${fmt(st.pm25.median)}</b><span>mediana PM2.5 w PL</span></div>
    <div><b>${fmt(st.pressure_sl_median, 0)}</b><span>hPa, mediana</span></div>`;
}

function trendSvg(trend) {
  if (trend.length < 2) return `<p class="muted">Trend pojawi się po kilku dniach zbierania danych (zbieramy od ${esc(trend[0]?.day || "dziś")}).</p>`;
  const W = 600, H = 90, pad = 6, ys = trend.map(d => d.pm25_median);
  const y0 = Math.min(...ys, 0), y1 = Math.max(...ys);
  const sx = i => pad + (W - 2 * pad) * i / (trend.length - 1);
  const sy = v => H - pad - (H - 2 * pad) * (v - y0) / (y1 - y0 || 1);
  const pts = trend.map((d, i) => `${sx(i).toFixed(1)},${sy(d.pm25_median).toFixed(1)}`).join(" ");
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none"><polyline points="${pts}" fill="none" stroke="#1f6feb" stroke-width="2" vector-effect="non-scaling-stroke"/></svg>
    <div class="ramp-labels"><span>${esc(trend[0].day)}</span><span>${fmt(y0)}–${fmt(y1)} µg/m³</span><span>${esc(trend[trend.length - 1].day)}</span></div>`;
}

function sensorRows(list) {
  return list.map(p => `<tr class="click" data-name="${esc(p.name)}"><td>${esc(p.name)}<br><span class="muted">${esc(p.address)}</span></td>
    <td class="num"><b style="color:${colorFor("pm25", p.pm25)}">${fmt(p.pm25)}</b></td><td class="num">${fmt(p.pm10)}</td></tr>`).join("");
}

async function openStats() {
  $("#stats-modal").hidden = false;
  const st = await fetch("/api/stats").then(r => r.json());
  const total = st.pm25_classes.reduce((a, b) => a + b, 0) || 1;
  const bar = st.pm25_classes.map((n, i) => n ? `<div style="width:${n / total * 100}%;background:${INDEX_COLORS[i]}" title="${INDEX_LABELS[i]}: ${n}"></div>` : "").join("");
  const legend = st.pm25_classes.map((n, i) => `<span class="row" style="display:inline-flex;margin-right:10px"><span class="sw" style="background:${INDEX_COLORS[i]}"></span>${INDEX_LABELS[i]} ${Math.round(n / total * 100)}%</span>`).join("");
  const h = st.history;
  $("#stats-body").innerHTML = `
    <div class="kpis">
      <div><b>${num(st.lockers_total)}</b><span>paczkomatów w Polsce</span></div>
      <div><b>${num(st.sensors_listed)}</b><span>z czujnikiem powietrza</span></div>
      <div><b>${num(st.sensors_reporting)}</b><span>przysyła dane</span></div>
      <div><b>${num(st.sensors_suspect)}</b><span>podejrzanych (pominięte)</span></div>
      <div><b>${fmt(st.pm25.median)}</b><span>mediana PM2.5 [µg/m³]</span></div>
      <div><b>${fmt(st.pm25.p90)}</b><span>90% czujników poniżej</span></div>
      <div><b>${fmt(st.humidity_median, 0)}%</b><span>mediana wilgotności</span></div>
      <div><b>${num(st.gios_stations)}</b><span>stacji GIOŚ (odniesienie)</span></div>
    </div>
    <h2>Jakość powietrza teraz (PM2.5)</h2>
    <div class="bar">${bar}</div><div style="font-size:12px">${legend}</div>
    <h2>Mediana PM2.5 w Polsce, ostatnie dni</h2><div class="trend">${trendSvg(st.trend)}</div>
    <div class="cols">
      <div><h2>Najwyższe PM2.5 teraz</h2><table class="stat-table"><tr><th>Paczkomat</th><th>PM2.5</th><th>PM10</th></tr>${sensorRows(st.worst)}</table></div>
      <div><h2>Najczystsze powietrze teraz</h2><table class="stat-table"><tr><th>Paczkomat</th><th>PM2.5</th><th>PM10</th></tr>${sensorRows(st.best)}</table></div>
    </div>
    <h2>Województwa (mediana PM2.5)</h2>
    <table class="stat-table"><tr><th>Województwo</th><th class="num">Czujników</th><th class="num">PM2.5</th></tr>
      ${st.provinces.map(p => `<tr><td>${esc(p.province)}</td><td class="num">${num(p.sensors)}</td><td class="num"><b style="color:${colorFor("pm25", p.pm25_median)}">${fmt(p.pm25_median)}</b></td></tr>`).join("")}</table>
    <p class="muted">Ostatni odczyt ${ago(st.last_collect)}, odświeżanie co ${st.collect_interval_min} min. W bazie ${num(h.readings)} odczytów
      (surowe trzymamy ${h.raw_days} dni), dzienne średnie od ${esc(h.daily_since || "dziś")} — bezterminowo.
      Statystyki liczone bez czujników oznaczonych jako podejrzane. Dane nieoficjalne (InPost), stacje GIOŚ jako odniesienie.</p>`;
  document.querySelectorAll("#stats-body tr.click").forEach(tr => {
    tr.onclick = () => { $("#stats-modal").hidden = true; openSensor(tr.dataset.name, true); };
  });
}

// ------------------------------------------------------------------ start

async function load() {
  const [cfg, data, gios, status] = await Promise.all([
    fetch("/api/config").then(r => r.json()),
    fetch("/api/sensors").then(r => r.json()),
    fetch("/api/gios").then(r => r.json()),
    fetch("/api/status").then(r => r.json()),
  ]);
  const first = !state.cfg;
  state.cfg = cfg;
  state.data = data;
  if (first) {
    state.metric = cfg.default_metric;
    state.view = cfg.default_view;
    state.showSuspect = !cfg.hide_flagged;
    state.showGios = cfg.gios_layer;
    $("#show-suspect").checked = state.showSuspect;
    $("#show-gios").checked = state.showGios;
    $("#show-gios").parentElement.hidden = !cfg.gios_layer;
    document.title = cfg.site_title;
    $("#title").textContent = cfg.site_title;
  }
  map.getSource("gios").setData(gios);
  fetch("/api/stats").then(r => r.json()).then(renderSummary).catch(() => {});
  computeDomains();
  state.hexRes = null;
  render();
  $("#status").textContent = status.last_collect
    ? `Dane ${ago(status.last_collect)} · ${status.sensors} czujników (${status.suspect} podejrzanych)`
    : `${status.sensors} czujników · pierwsze zbieranie w toku`;
}

function setupUi() {
  $("#metrics").innerHTML = Object.entries(METRICS).map(([k, m]) => `<button data-metric="${k}">${m.label}</button>`).join("");
  document.querySelectorAll("#metrics button").forEach(b => { b.onclick = () => { state.metric = b.dataset.metric; render(); }; });
  document.querySelectorAll("#views button").forEach(b => { b.onclick = () => { state.view = b.dataset.view; render(); }; });
  $("#show-suspect").onchange = e => { state.showSuspect = e.target.checked; render(); };
  $("#show-gios").onchange = e => { state.showGios = e.target.checked; render(); };
  $("#search").onsubmit = e => { e.preventDefault(); const q = $("#q").value.trim(); if (q) search(q); };
  $("#panel-toggle").onclick = () => $("#panel").classList.toggle("collapsed");
  $("#stats-open").onclick = openStats;
  $("#stats-close").onclick = () => { $("#stats-modal").hidden = true; };
  $("#stats-modal").onclick = e => { if (e.target.id === "stats-modal") e.target.hidden = true; };
  document.addEventListener("keydown", e => { if (e.key === "Escape") $("#stats-modal").hidden = true; });
}

setupUi();
map.on("load", async () => {
  setupLayers();
  await load();
  setInterval(load, 5 * 60 * 1000);
});
