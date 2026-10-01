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
  pressure_trend: { label: "Trend ciśnienia", unit: "hPa/3 h", kind: "ramp", digits: 1, fixed: [-4, 4], live: true,
                    ramp: ["#5e3c99", "#b2abd2", "#f7f7f7", "#fdb863", "#e66101"],
                    note: "Zmiana ciśnienia w ciągu ~3 h. Spada (fiolet) — nadchodzi niż albo front, często deszcz i wiatr; rośnie (pomarańcz) — wyż, poprawa pogody." },
  humidity: { label: "Wilgotność", unit: "%", kind: "ramp", digits: 0, fixed: [20, 100],
              ramp: ["#a6611a", "#dfc27d", "#f5f5f5", "#80cdc1", "#018571"],
              note: "Mierzona w obudowie paczkomatu." },
  temperature: { label: "Temperatura", unit: "°C", kind: "ramp", digits: 1,
                 ramp: ["#313695", "#74add1", "#ffffbf", "#f46d43", "#a50026"],
                 note: "Uwaga: mierzona w obudowie paczkomatu — w słońcu mocno zawyżona. To nie jest temperatura powietrza." },
};

// narożniki obrazu plamy — muszą się zgadzać z BBOX w app/surface.py
const SURFACE_CORNERS = [[13.9, 55.05], [24.4, 55.05], [24.4, 48.85], [13.9, 48.85]];
const TRANSPARENT_PNG = "/static/empty.png";  // pusty obraz startowy; data: blokuje CSP (connect-src)

const state = {
  frames: [], frameIdx: null, frameValues: null, playing: null, showWind: false, showIsobars: false,
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

function frameTs() {
  return state.frameIdx === null ? null : state.frames[state.frameIdx]?.ts ?? null;
}

// W trybie historii podmieniamy wartość wybranej wielkości na tę z klatki; reszta (adres, flagi) z bieżących danych.
function visibleFeatures() {
  const feats = state.data.features.filter(f => state.showSuspect || !f.properties.suspect);
  if (!state.frameValues) return feats;
  const fv = state.frameValues, metric = state.metric;
  return feats.map(f => ({ ...f, properties: { ...f.properties, [metric]: fv[f.properties.name] ?? null } }));
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
  map.addSource("hex", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addSource("gios", { type: "geojson", data: { type: "FeatureCollection", features: [] } });

  // sześciokąty i plama pod nazwami miejscowości, kropki nad nimi
  const firstLabel = map.getStyle().layers.find(l => l.type === "symbol")?.id;
  map.addLayer({ id: "hex-fill", type: "fill", source: "hex", paint: { "fill-opacity": 0.6, "fill-color": "#ccc" } }, firstLabel);
  map.addLayer({ id: "hex-line", type: "line", source: "hex", paint: { "line-color": "#fff", "line-width": 0.6 } }, firstLabel);

  // plama: interpolacja liczona na serwerze (obraz PNG), pod nazwami miejscowości
  map.addSource("surface", { type: "image", url: TRANSPARENT_PNG, coordinates: SURFACE_CORNERS });
  map.addLayer({ id: "heat", type: "raster", source: "surface", paint: { "raster-opacity": 0.85, "raster-fade-duration": 0 } }, firstLabel);

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

  // izobary: linie + podpisy wzdłuż linii
  map.addSource("isobars", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "isobars", type: "line", source: "isobars",
    paint: { "line-color": "#1d2330", "line-width": ["case", ["==", ["%", ["get", "value"], 4], 0], 1.6, 0.8], "line-opacity": 0.7 } });
  map.addLayer({ id: "isobars-label", type: "symbol", source: "isobars",
    layout: { "symbol-placement": "line", "symbol-spacing": 280, "text-field": ["concat", ["to-string", ["get", "value"]], " hPa"],
              "text-size": 11, "text-font": ["Noto Sans Regular"] },
    paint: { "text-color": "#1d2330", "text-halo-color": "rgba(255,255,255,.9)", "text-halo-width": 1.5 } });

  // wiatr: strzałka narysowana na canvasie (SDF — kolor nadajemy stylem), obrót = kierunek, w który wieje
  map.addImage("wind-arrow", windArrowImage(), { sdf: true });
  map.addSource("wind", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
  map.addLayer({ id: "wind", type: "symbol", source: "wind",
    layout: { "icon-image": "wind-arrow", "icon-rotate": ["+", ["get", "direction"], 180], "icon-rotation-alignment": "map",
              "icon-allow-overlap": true, "icon-ignore-placement": true,
              "icon-size": ["interpolate", ["linear"], ["get", "speed"], 0, 0.35, 5, 0.6, 12, 0.95, 20, 1.2],
              "text-field": ["concat", ["to-string", ["round", ["get", "speed"]]], " m/s"], "text-size": 10,
              "text-font": ["Noto Sans Regular"], "text-offset": [0, 1.6], "text-optional": true },
    paint: { "icon-color": ["interpolate", ["linear"], ["get", "speed"], 0, "#64748b", 6, "#0f766e", 12, "#b45309", 18, "#b91c1c"],
             "text-color": "#334155", "text-halo-color": "rgba(255,255,255,.9)", "text-halo-width": 1,
             "text-opacity": ["step", ["zoom"], 0, 7, 1] } });

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

  const val = ["get", metric];
  map.setPaintProperty("points", "circle-color",
    ["case", ["any", ["get", "suspect"], ["==", val, null]], SUSPECT_COLOR, colorExpr(metric, val)]);
  map.setPaintProperty("points", "circle-opacity", ["case", ["==", val, null], 0.25, 1]);
  // grupy: dla pyłów najgorszy czujnik w grupie, dla reszty średnia
  const clusterVal = m.kind === "index" ? ["get", `${metric}_max`]
    : ["/", ["get", `${metric}_sum`], ["max", ["get", `${metric}_n`], 1]];
  map.setPaintProperty("clusters", "circle-color",
    ["case", ["==", ["get", `${metric}_n`], 0], SUSPECT_COLOR, colorExpr(metric, clusterVal)]);

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

  const giosOn = state.showGios && (metric === "pm25" || metric === "pm10") && state.frameIdx === null;
  vis("gios", giosOn);
  // stacje, które danej wielkości w ogóle nie mierzą, nie mają być szarymi kropkami „bez danych”
  map.setFilter("gios", ["!=", ["get", metric], null]);
  map.setPaintProperty("gios", "circle-color",
    ["case", ["==", ["get", metric], null], SUSPECT_COLOR, colorExpr(metric, ["get", metric])]);

  const pressureMetric = metric === "pressure_sl" || metric === "pressure_trend";
  vis("isobars", state.showIsobars);
  vis("isobars-label", state.showIsobars);
  vis("wind", state.showWind);
  if (state.showIsobars) refreshIsobars();
  if (state.showWind && !state.windLoaded) {
    state.windLoaded = true;
    fetch("/api/wind").then(r => r.json()).then(d => map.getSource("wind").setData(d));
  }
  if (v === "hex") refreshHex(true);
  if (v === "heat") refreshSurface();
  document.querySelectorAll("#views button").forEach(b => b.classList.toggle("active", b.dataset.view === state.view));
  document.querySelectorAll("#metrics button").forEach(b => b.classList.toggle("active", b.dataset.metric === metric));
  $("#view-hint").textContent = {
    points: "Przy oddaleniu czujniki łączą się w grupy — kolor grupy to najgorszy czujnik (pyły) albo średnia.",
    hex: "Mediana z czujników w każdym sześciokącie. Odporna na pojedyncze zepsute czujniki.",
    heat: "Wartości rozlane między czujnikami (średnia ważona odległością). Dalej niż ~25 km od czujnika plama znika — tam nie ma pomiarów.",
  }[state.view];
  renderLegend();
  if (state.origin) renderNearest();
}

async function refreshHex(force = false) {
  const base = state.cfg.hex_resolution;
  const res = Math.max(3, Math.min(8, base + Math.floor((map.getZoom() - 6.5) / 1.5)));
  const ts = frameTs();
  const key = `${res}|${state.metric}|${state.showSuspect}|${ts}`;
  if (!force && key === state.hexRes) return;
  state.hexRes = key;
  const r = await fetch(`/api/hex?res=${res}&metric=${state.metric}&suspect=${state.showSuspect}${ts ? `&ts=${ts}` : ""}`);
  map.getSource("hex").setData(await r.json());
  map.setPaintProperty("hex-fill", "fill-color", colorExpr(state.metric, ["get", "value"]));
}

// przystanki kolorów plamy — te same barwy co w legendzie
function surfaceStops(metric) {
  const m = METRICS[metric];
  if (m.kind === "index") {
    const t = thresholds(metric);
    const mids = INDEX_COLORS.map((c, i) => [i === 0 ? t[0] / 2 : i === t.length ? t[t.length - 1] * 1.2 : (t[i - 1] + t[i]) / 2, c]);
    return mids;
  }
  const [lo, hi] = state.domains[metric];
  return m.ramp.map((c, i) => [lo + (hi - lo) * i / (m.ramp.length - 1), c]);
}

function surfaceUrl(metric, ts) {
  const stops = surfaceStops(metric).map(([v, c]) => `${Math.round(v * 100) / 100}:${c}`).join(",");
  return `/api/surface.png?metric=${metric}&suspect=${state.showSuspect}&stops=${encodeURIComponent(stops)}` +
    (ts ? `&ts=${ts}` : `&t=${state.lastCollect || ""}`);
}

function refreshSurface() {
  const url = surfaceUrl(state.metric, frameTs());
  if (url === state.surfaceUrl) return;
  state.surfaceUrl = url;
  map.getSource("surface").updateImage({ url, coordinates: SURFACE_CORNERS });
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
  if (state.frameIdx !== null && m.live) html += `<div class="note"><b>Ta wielkość nie ma historii</b> — przesuń suwak na „Teraz”.</div>`;
  if (state.showWind) html += `<div class="note">Strzałki: kierunek, w który wieje wiatr; kolor i wielkość — prędkość (Open-Meteo, co godzinę).</div>`;
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
    <div class="profile" data-name="${esc(p.name)}"></div>
    <button class="alert-btn" data-alert="${esc(p.name)}">🔔 Alert smogowy w tej okolicy</button>
    <div class="meta">Odczyt ${ago(p.ts)} · bez zmian od ${ago(p.changed_at)}${p.elevation != null ? ` · ${Math.round(p.elevation)} m n.p.m.` : ""}
    ${safeUrl(p.page_url) ? ` · <a href="${esc(p.page_url)}" target="_blank" rel="noopener noreferrer">strona paczkomatu</a>` : ""}</div>
  </div>`;
  new maplibregl.Popup({ maxWidth: "320px" }).setLngLat(f.geometry.coordinates).setHTML(html).addTo(map);
  drawChart(p.name);
  drawProfile(p.name);
  const ab = [...document.querySelectorAll(".pop .alert-btn")].find(el => el.dataset.alert === p.name);
  if (ab) ab.onclick = () => openAlert(f.geometry.coordinates[1], f.geometry.coordinates[0], p.address);
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

async function drawProfile(name) {
  const box = [...document.querySelectorAll(".pop .profile")].find(el => el.dataset.name === name);
  if (!box) return;
  const r = await fetch(`/api/profile/${encodeURIComponent(name)}`).then(r => r.json()).catch(() => null);
  if (!r || r.days < 2) {
    box.innerHTML = `<div class="meta">Profil dobowy (o której godzinie jest najgorzej) pojawi się po 2 dobach zbierania.</div>`;
    return;
  }
  box.innerHTML = `<div class="chart-label"><span>PM2.5 o różnych porach dnia</span><span>${r.days} dni</span></div>${profileSvg(r.all, 54)}`;
}

function openGios(f) {
  const p = f.properties;
  new maplibregl.Popup().setLngLat(f.geometry.coordinates).setHTML(`<div class="pop">
    <h3>Stacja GIOŚ: ${esc(p.name)}</h3><div class="addr">${esc(p.city)}</div>
    <table><tr><td>PM2.5</td><td>${fmt(p.pm25)} µg/m³</td></tr><tr><td>PM10</td><td>${fmt(p.pm10)} µg/m³</td></tr></table>
    <div class="meta">Pomiar oficjalny, średnia godzinowa · ${esc(p.ts || "–")}</div>
    <div class="gios-compare" data-id="${Number(p.station_id)}"></div></div>`).addTo(map);
  fetch("/api/compare").then(r => r.json()).then(c => {
    const st = (c.stations || []).find(x => x.station_id === Number(p.station_id));
    const box = document.querySelector(`.gios-compare[data-id="${Number(p.station_id)}"]`);
    if (!box) return;
    box.innerHTML = st
      ? `<div class="warn" style="background:#f0f6ff;color:#1d2330">Paczkomaty do ${c.summary.radius_km} km (${st.sensors}): średnio
          <b>${fmt(st.locker_mean)}</b> wobec <b>${fmt(st.gios_mean)}</b> µg/m³ ze stacji
          (różnica ${st.bias > 0 ? "+" : ""}${fmt(st.bias)}, korelacja ${st.r ?? "–"}, ${num(st.n)} par godzinowych).</div>`
      : `<div class="meta">Brak paczkomatów z czujnikiem w pobliżu tej stacji albo jeszcze za mało wspólnych pomiarów.</div>`;
  });
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

function goTo(x, zoom = 12) {
  state.origin = [x.lon, x.lat];
  state.originLabel = x.label;
  if (state.searchMarker) state.searchMarker.remove();
  state.searchMarker = new maplibregl.Marker({ color: "#1d2330" }).setLngLat(state.origin).addTo(map);
  map.flyTo({ center: state.origin, zoom });
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

// ------------------------------------------------------------------ izobary, wiatr

function refreshIsobars() {
  const ts = frameTs();
  const key = `${ts}|${state.lastCollect}`;
  if (key === state.isoKey) return;
  state.isoKey = key;
  fetch(`/api/isobars?step=2${ts ? `&ts=${ts}` : `&t=${state.lastCollect || ""}`}`)
    .then(r => r.json()).then(d => map.getSource("isobars").setData(d));
}

function windArrowImage() {
  const size = 48, c = document.createElement("canvas");
  c.width = c.height = size;
  const g = c.getContext("2d");
  g.fillStyle = "#000";
  g.beginPath();                       // strzałka w górę (północ); obrót robi styl
  g.moveTo(24, 3); g.lineTo(38, 22); g.lineTo(28, 20); g.lineTo(28, 45);
  g.lineTo(20, 45); g.lineTo(20, 20); g.lineTo(10, 22); g.closePath();
  g.fill();
  return g.getImageData(0, 0, size, size);
}

// ------------------------------------------------------------------ suwak czasu

const fmtTime = ts => new Date(ts * 1000).toLocaleString("pl-PL", { weekday: "short", hour: "2-digit", minute: "2-digit" });

async function loadFrames() {
  state.frames = await fetch("/api/frames").then(r => r.json()).catch(() => []);
  const sl = $("#time-slider");
  sl.max = Math.max(state.frames.length - 1, 0);
  if (state.frameIdx === null) sl.value = sl.max;
  $("#time-box").hidden = state.frames.length < 2;
  $("#time-hint").textContent = state.frames.length < 24
    ? `Zebrane klatki: ${state.frames.length} (pełne 24 h będą po dobie zbierania).` : "";
}

async function setFrame(idx) {
  const last = state.frames.length - 1;
  if (idx === null || idx >= last) {      // ostatnia klatka = bieżące dane (z flagami, trendem, GIOŚ)
    state.frameIdx = null;
    state.frameValues = null;
    $("#time-slider").value = last;
    $("#time-label").textContent = "na żywo";
  } else {
    state.frameIdx = idx;
    const f = state.frames[idx];
    $("#time-label").textContent = fmtTime(f.ts);
    if (METRICS[state.metric].live) {      // trend ciśnienia nie ma historii
      state.frameValues = {};
    } else {
      const r = await fetch(`/api/frame?ts=${f.ts}&metric=${state.metric}`).then(r => r.json());
      if (state.frameIdx !== idx) return;  // użytkownik przesunął suwak dalej
      state.frameValues = r.values;
    }
  }
  $("#time-live").classList.toggle("active", state.frameIdx === null);
  state.hexRes = null;
  render();
}

function togglePlay() {
  if (state.playing) {
    clearInterval(state.playing);
    state.playing = null;
    $("#time-play").textContent = "▶";
    return;
  }
  if (state.frames.length < 2) return;
  let i = state.frameIdx === null ? 0 : state.frameIdx;
  $("#time-play").textContent = "❚❚";
  if (state.view === "heat") {             // plamy kolejnych klatek pobieramy z wyprzedzeniem
    state.frames.forEach(f => { new Image().src = surfaceUrl(state.metric, f.ts); });
  }
  const step = () => {
    setFrame(i);
    i += 1;
    if (i >= state.frames.length) togglePlay();
  };
  step();
  state.playing = setInterval(step, 1200);
}

// #lat=52.23&lon=20.96&z=14 — link z karty HA i do udostępniania konkretnego miejsca
function applyHash() {
  const p = new URLSearchParams(location.hash.slice(1));
  const lat = parseFloat(p.get("lat")), lon = parseFloat(p.get("lon"));
  if (!isFinite(lat) || !isFinite(lon) || lat < 48 || lat > 56 || lon < 13 || lon > 25) return;
  const z = Math.min(Math.max(parseFloat(p.get("z")) || 13, 5), 17);
  goTo({ lat, lon, label: "" }, z);
}

// ------------------------------------------------------------------ alerty smogowe

const ALERTS_KEY = "airmap-alerts";
const myAlerts = () => JSON.parse(localStorage.getItem(ALERTS_KEY) || "[]");
const saveAlerts = list => { localStorage.setItem(ALERTS_KEY, JSON.stringify(list)); renderMyAlerts(); };

async function alertsConfig() {
  if (!state.alertsCfg) state.alertsCfg = await fetch("/api/alerts/config").then(r => r.json());
  return state.alertsCfg;
}

const b64ToBytes = b64 => {
  const pad = "=".repeat((4 - b64.length % 4) % 4);
  const raw = atob((b64 + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
};

async function openAlert(lat, lon, label) {
  const cfg = await alertsConfig();
  const pushOk = "serviceWorker" in navigator && "PushManager" in window && window.isSecureContext;
  const opts = Object.entries(cfg.thresholds).map(([v, desc]) =>
    `<label class="opt"><input type="radio" name="thr" value="${v}" ${v === "35" ? "checked" : ""}> PM2.5 &gt; <b>${v}</b> µg/m³ <span class="muted">(${esc(desc)})</span></label>`).join("");
  $("#alert-body").innerHTML = `
    <p>Powiadomię, gdy w okolicy <b>${esc(label || "tego miejsca")}</b> przekroczony zostanie próg — wartość to mediana
      z 3 najbliższych działających czujników do ${cfg.radius_km} km. Gdy powietrze się poprawi, przyjdzie odwołanie.</p>
    ${opts}
    <div class="ways">
      <button class="way primary" id="alert-push" ${pushOk ? "" : "disabled"}>🔔 Powiadomienia w tej przeglądarce</button>
      ${pushOk ? "" : `<p class="muted">${window.isSecureContext ? "Ta przeglądarka nie obsługuje powiadomień." :
        "Powiadomienia działają tylko przez https — otwórz stronę pod publicznym adresem."} Na iPhonie: najpierw „Do ekranu początkowego”.</p>`}
      ${cfg.telegram_bot ? `<a class="way" id="alert-tg" target="_blank" rel="noopener">✈️ Na Telegramie (@${esc(cfg.telegram_bot)})</a>` : ""}
      <a class="way" href="https://github.com/szmidtpiotr/ha-air-locker-map" target="_blank" rel="noopener">🏠 W Home Assistant (integracja)</a>
    </div>
    <div class="msg" id="alert-msg"></div>`;
  const thr = () => Number(document.querySelector('input[name="thr"]:checked').value);
  const tgLink = () => `https://t.me/${cfg.telegram_bot}?start=${Math.round(lat * 1e5)}_${Math.round(lon * 1e5)}_${thr()}`;
  if (cfg.telegram_bot) {
    const a = $("#alert-tg");
    a.href = tgLink();
    document.querySelectorAll('input[name="thr"]').forEach(r => { r.onchange = () => { a.href = tgLink(); }; });
  }
  if (pushOk) $("#alert-push").onclick = () => subscribePush(lat, lon, label, thr(), cfg.push_key);
  $("#alert-modal").hidden = false;
}

async function subscribePush(lat, lon, label, threshold, key) {
  const msg = $("#alert-msg");
  try {
    msg.textContent = "Proszę o zgodę na powiadomienia…";
    if (await Notification.requestPermission() !== "granted") throw new Error("Bez zgody na powiadomienia alert nie zadziała.");
    const reg = await navigator.serviceWorker.register("/sw.js");
    await navigator.serviceWorker.ready;
    const sub = await reg.pushManager.getSubscription() ||
      await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: b64ToBytes(key) });
    const r = await fetch("/api/push/subscribe", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ subscription: sub.toJSON(), lat, lon, threshold, label }) });
    const body = await r.json();
    if (!r.ok) throw new Error(body.detail || r.statusText);
    // jedna przeglądarka = jedna subskrypcja; nowy alert zastępuje poprzedni
    saveAlerts([{ id: body.id, token: body.token, label: label || "wybrane miejsce", threshold, lat, lon }]);
    msg.innerHTML = `Gotowe ✓ Alert włączony. <button class="linkbtn" id="alert-test">Wyślij próbne powiadomienie</button>`;
    $("#alert-test").onclick = () => testAlert(body.id, body.token);
  } catch (e) {
    msg.textContent = `Nie udało się: ${e.message}`;
  }
}

async function testAlert(id, token) {
  const r = await fetch("/api/push/test", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, token }) });
  if (!r.ok) alert((await r.json()).detail || "Nie udało się wysłać");
}

async function removeAlert(id, token) {
  await fetch("/api/push/unsubscribe", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ id, token }) });
  saveAlerts(myAlerts().filter(a => a.id !== id));
}

function renderMyAlerts() {
  const list = myAlerts();
  $("#my-alerts-box").hidden = !list.length;
  $("#my-alerts").innerHTML = list.map(a => `<li><span>🔔 ${esc(a.label)} · PM2.5 &gt; ${a.threshold}</span>
    <button class="linkbtn" data-test="${a.id}">test</button><button class="linkbtn" data-del="${a.id}">usuń</button></li>`).join("");
  document.querySelectorAll("#my-alerts [data-test]").forEach(b => {
    const a = list.find(x => String(x.id) === b.dataset.test); b.onclick = () => testAlert(a.id, a.token);
  });
  document.querySelectorAll("#my-alerts [data-del]").forEach(b => {
    const a = list.find(x => String(x.id) === b.dataset.del); b.onclick = () => removeAlert(a.id, a.token);
  });
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

// 24 słupki (godziny doby), kolor wg indeksu PM2.5
function profileSvg(values, height = 70) {
  const vals = values.map(v => (v == null ? null : v));
  const max = Math.max(...vals.filter(v => v != null), 1);
  const W = 600, H = height, gap = 3, bw = (W - gap * 23) / 24;
  const bars = vals.map((v, h) => {
    if (v == null) return "";
    const bh = Math.max(2, (H - 14) * v / max);
    return `<rect x="${(h * (bw + gap)).toFixed(1)}" y="${(H - 14 - bh).toFixed(1)}" width="${bw.toFixed(1)}" height="${bh.toFixed(1)}" rx="2" fill="${colorFor("pm25", v)}"><title>${h}:00 — ${fmt(v)} µg/m³</title></rect>`;
  }).join("");
  const labels = [0, 6, 12, 18, 23].map(h => `<text x="${(h * (bw + gap) + bw / 2).toFixed(1)}" y="${H - 2}" font-size="10" text-anchor="middle" fill="#667085">${h}</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" style="width:100%;height:${H}px">${bars}${labels}</svg>`;
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

function cityRows(list) {
  return (list || []).map(c => `<tr><td>${esc(c.city)}</td><td class="num">${num(c.sensors)}</td>
    <td class="num"><b style="color:${colorFor("pm25", c.pm25_median)}">${fmt(c.pm25_median)}</b></td></tr>`).join("");
}

function compareHtml(c) {
  if (!c || !c.all) return `<p class="muted">Porównanie pojawi się, gdy zbierze się kilka godzin wspólnych pomiarów paczkomatów i stacji GIOŚ.</p>`;
  const row = (label, x) => x ? `<tr><td>${label}</td><td class="num">${num(x.n)}</td><td class="num">${fmt(x.locker_mean)}</td>
      <td class="num">${fmt(x.gios_mean)}</td><td class="num">${x.bias > 0 ? "+" : ""}${fmt(x.bias)}</td>
      <td class="num">${x.ratio ?? "–"}</td><td class="num">${x.r ?? "–"}</td></tr>` : "";
  return `<table class="stat-table"><tr><th>Czujniki</th><th class="num">Par</th><th class="num">Paczkomat</th><th class="num">GIOŚ</th>
      <th class="num">Różnica</th><th class="num">Stosunek</th><th class="num">Korelacja</th></tr>
    ${row("wszystkie", c.all)}${row("nowsze (z PM4)", c.new)}${row("starsze", c.old)}</table>
    <p class="muted">PM2.5 w µg/m³: godzinowe pary stacja GIOŚ ↔ paczkomaty do ${c.radius_km} km (${num(c.stations)} stacji).
      Różnica &gt; 0 — paczkomaty zawyżają; korelacja bliska 1 — dobrze śledzą zmiany, nawet jeśli mają stałe przesunięcie.</p>`;
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
    <div class="cols">
      <div><h2>Miasta — najwyższe PM2.5</h2><table class="stat-table"><tr><th>Miasto</th><th class="num">Czujn.</th><th class="num">PM2.5</th></tr>${cityRows(st.cities_worst)}</table></div>
      <div><h2>Miasta — najczystsze</h2><table class="stat-table"><tr><th>Miasto</th><th class="num">Czujn.</th><th class="num">PM2.5</th></tr>${cityRows(st.cities_best)}</table></div>
    </div>
    <p class="muted">Mediana z działających czujników; w rankingu ${num(st.cities_ranked)} miejscowości z co najmniej 5 czujnikami.</p>
    <h2>Profil dobowy PM2.5 w Polsce</h2>
    ${st.profile && st.profile.pm25.some(v => v != null)
      ? profileSvg(st.profile.pm25) + `<p class="muted">Średnia ze wszystkich odczytów o danej godzinie (dni zbierania: ${st.profile.days}). W sezonie grzewczym szczyt wypada zwykle wieczorem.</p>`
      : `<p class="muted">Profil pojawi się po pierwszych dobach zbierania.</p>`}
    <h2>Paczkomaty kontra stacje GIOŚ</h2>
    ${compareHtml(st.compare)}
    <h2>Województwa (mediana PM2.5)</h2>
    <table class="stat-table"><tr><th>Województwo</th><th class="num">Czujników</th><th class="num">PM2.5</th></tr>
      ${st.provinces.map(p => `<tr><td>${esc(p.province)}</td><td class="num">${num(p.sensors)}</td><td class="num"><b style="color:${colorFor("pm25", p.pm25_median)}">${fmt(p.pm25_median)}</b></td></tr>`).join("")}</table>
    <p class="muted">Ostatni odczyt ${ago(st.last_collect)}, odświeżanie co ${st.collect_interval_min} min. W bazie ${num(h.readings)} odczytów
      (surowe trzymamy ${h.raw_days} dni), dzienne średnie od ${esc(h.daily_since || "dziś")} — bezterminowo.
      Statystyki liczone bez czujników oznaczonych jako podejrzane. Dane nieoficjalne (InPost), stacje GIOŚ jako odniesienie.
      Kod projektu: <a href="https://github.com/szmidtpiotr/air-locker-map" target="_blank" rel="noopener noreferrer">github.com/szmidtpiotr/air-locker-map</a>
      · integracja Home Assistant: <a href="https://github.com/szmidtpiotr/ha-air-locker-map" target="_blank" rel="noopener noreferrer">ha-air-locker-map</a>.</p>`;
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
  if (status.last_collect !== state.lastCollect) state.windLoaded = false;  // nowy przebieg — odśwież wiatr
  state.lastCollect = status.last_collect;
  state.hexRes = null;
  render();
  $("#status").textContent = status.last_collect
    ? `Dane ${ago(status.last_collect)} · ${status.sensors} czujników (${status.suspect} podejrzanych)`
    : `${status.sensors} czujników · pierwsze zbieranie w toku`;
}

function setupUi() {
  $("#metrics").innerHTML = Object.entries(METRICS).map(([k, m]) => `<button data-metric="${k}">${m.label}</button>`).join("");
  document.querySelectorAll("#metrics button").forEach(b => {
    b.onclick = () => { state.metric = b.dataset.metric; state.frameIdx === null ? render() : setFrame(state.frameIdx); };
  });
  document.querySelectorAll("#views button").forEach(b => { b.onclick = () => { state.view = b.dataset.view; render(); }; });
  $("#show-suspect").onchange = e => { state.showSuspect = e.target.checked; render(); };
  $("#show-gios").onchange = e => { state.showGios = e.target.checked; render(); };
  $("#show-wind").onchange = e => { state.showWind = e.target.checked; render(); };
  $("#show-isobars").onchange = e => { state.showIsobars = e.target.checked; render(); };
  $("#time-slider").oninput = e => { if (state.playing) togglePlay(); setFrame(Number(e.target.value)); };
  $("#time-live").onclick = () => { if (state.playing) togglePlay(); setFrame(null); };
  $("#time-play").onclick = togglePlay;
  $("#alert-origin").onclick = () => state.origin && openAlert(state.origin[1], state.origin[0], (state.originLabel || "").split(",")[0]);
  $("#alert-close").onclick = () => { $("#alert-modal").hidden = true; };
  $("#alert-modal").onclick = e => { if (e.target.id === "alert-modal") e.target.hidden = true; };
  renderMyAlerts();
  $("#search").onsubmit = e => { e.preventDefault(); const q = $("#q").value.trim(); if (q) search(q); };
  $("#panel-toggle").onclick = () => $("#panel").classList.toggle("collapsed");
  $("#stats-open").onclick = openStats;
  $("#stats-close").onclick = () => { $("#stats-modal").hidden = true; };
  $("#stats-modal").onclick = e => { if (e.target.id === "stats-modal") e.target.hidden = true; };
  document.addEventListener("keydown", e => { if (e.key === "Escape") { $("#stats-modal").hidden = true; $("#alert-modal").hidden = true; } });
}

setupUi();
map.on("load", async () => {
  setupLayers();
  await load();
  await loadFrames();
  applyHash();
  window.addEventListener("hashchange", applyHash);
  setInterval(async () => { await load(); await loadFrames(); }, 5 * 60 * 1000);
});
