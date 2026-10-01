"use strict";

const $ = s => document.querySelector(s);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const when = ts => (ts ? new Date(ts * 1000).toLocaleString("pl-PL", { dateStyle: "short", timeStyle: "short" }) : "–");
const dur = s => (s == null ? "–" : s < 90 ? `${Math.round(s)} s` : `${Math.round(s / 60)} min`);
const num = n => (n ?? 0).toLocaleString("pl-PL");

let schema = [], values = {}, timer = null, sensorKind = "flagged";

function toast(msg, error = false) {
  const t = $("#toast");
  t.textContent = msg;
  t.className = "show" + (error ? " error" : "");
  clearTimeout(t._h);
  t._h = setTimeout(() => { t.className = ""; }, 3500);
}

async function api(path, opts = {}) {
  const r = await fetch(path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (r.status === 401) { showLogin(); throw new Error("Zaloguj się"); }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || r.statusText);
  return body;
}

// ------------------------------------------------------------------ logowanie

function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  clearInterval(timer);
  $("#pw").focus();
}

$("#login-form").onsubmit = async e => {
  e.preventDefault();
  $("#login-err").textContent = "";
  try {
    await api("/api/admin/login", { method: "POST", body: JSON.stringify({ password: $("#pw").value }) });
    $("#pw").value = "";
    start();
  } catch (err) {
    $("#login-err").textContent = err.message;
  }
};

$("#logout").onclick = async () => { await api("/api/admin/logout", { method: "POST" }); showLogin(); };

// ------------------------------------------------------------------ zakładki

document.querySelectorAll("#tabs button").forEach(b => {
  b.onclick = () => {
    document.querySelectorAll("#tabs button").forEach(x => x.classList.toggle("active", x === b));
    document.querySelectorAll("main > section").forEach(s => { s.hidden = s.id !== `tab-${b.dataset.tab}`; });
    ({ status: loadStatus, settings: loadSettings, sensors: loadSensors, health: loadHealth, alerts: loadAlerts, keys: loadKeys, log: loadLog })[b.dataset.tab]();
  };
});

// ------------------------------------------------------------------ status

async function loadStatus() {
  const s = await api("/api/admin/status");
  const c = s.counts;
  const j = s.job;
  if (j.key) {
    const pct = j.total ? Math.round(j.done / j.total * 100) : 0;
    const eta = j.done && j.total ? (Date.now() / 1000 - j.started) / j.done * (j.total - j.done) : null;
    $("#job").innerHTML = `<h2>Trwa: ${esc(j.label)}</h2>
      <div class="progress"><div style="width:${pct}%"></div></div>
      <div class="hint">${num(j.done)} / ${num(j.total)} (${pct}%) · od ${dur(Date.now() / 1000 - j.started)}${eta ? ` · zostało ok. ${dur(eta)}` : ""}</div>
      <button class="btn danger" id="cancel">Przerwij</button>`;
    $("#cancel").onclick = async () => { await api("/api/admin/cancel", { method: "POST" }); toast("Przerywam po bieżącej partii"); };
  } else {
    $("#job").innerHTML = `<h2>Kolektor: ${values.collector_enabled === false ? "wyłączony" : "czeka na następny przebieg"}</h2>`;
  }

  const stats = [
    [c.lockers, "paczkomatów (ShipX)"],
    [c.with_sensor, "z czujnikiem"],
    [c.with_id, "z ustalonym ID"],
    [c.reading_ok, "odczytów OK"],
    [c.reading_no_data, "bez danych"],
    [c.flagged, "podejrzanych"],
    [c.id_pending, "ID do ustalenia"],
    [(c.no_page || 0) + (c.no_url || 0), "bez strony / ID"],
    [c.hidden, "ukrytych ręcznie"],
    [s.readings.n, "odczytów w historii"],
    [`${(s.db_bytes / 1048576).toFixed(1)} MB`, "baza danych"],
  ];
  $("#counts").innerHTML = stats.map(([v, l]) => `<div class="stat"><b>${typeof v === "number" || v == null ? num(v) : esc(v)}</b><span>${l}</span></div>`).join("");
  const fl = Object.entries(s.flag_counts).map(([k, v]) => `${k}: ${v}`).join(", ");
  if (fl) $("#counts").insertAdjacentHTML("beforeend", `<div class="stat"><b style="font-size:14px">${esc(fl)}</b><span>flagi</span></div>`);

  $("#job-buttons").innerHTML = Object.entries(s.jobs).map(([k, label]) =>
    `<button class="btn secondary" data-job="${k}" ${j.key ? "disabled" : ""}>${esc(label)}</button>`).join("");
  document.querySelectorAll("#job-buttons button").forEach(b => {
    b.onclick = async () => {
      try { await api(`/api/admin/run/${b.dataset.job}`, { method: "POST" }); toast(`Uruchomione: ${b.textContent}`); loadStatus(); }
      catch (e) { toast(e.message, true); }
    };
  });
  const n = s.next;
  $("#next").innerHTML = `Następnie: odczyty ${when(n.collect)} · lista paczkomatów ${when(n.lockers)}${n.gios ? ` · GIOŚ ${when(n.gios)}` : ""}`;

  $("#runs tbody").innerHTML = s.runs.map(r => `<tr>
    <td>${esc(s.jobs[r.job] || r.job)}</td><td>${when(r.started)}</td>
    <td>${r.finished ? dur(r.finished - r.started) : "trwa…"}</td><td>${num(r.ok)}</td><td>${num(r.fail)}</td>
    <td>${esc(r.note || "")}</td></tr>`).join("");
}

// ------------------------------------------------------------------ parametry

async function loadSettings() {
  const r = await api("/api/admin/settings");
  schema = r.schema;
  values = r.values;
  const groups = {};
  schema.forEach(s => (groups[s.group] = groups[s.group] || []).push(s));
  let html = "";
  for (const [g, items] of Object.entries(groups)) {
    html += `<div class="card"><h2>${esc(g)}</h2><fieldset>`;
    for (const s of items) html += fieldHtml(s, values[s.key]);
    html += `</fieldset></div>`;
  }
  html += `<div class="savebar"><button class="btn" id="save" disabled>Zapisz zmiany</button>
    <button type="button" class="btn secondary" id="reset" disabled>Cofnij</button><span class="hint" id="dirty"></span></div>`;
  const form = $("#settings-form");
  form.innerHTML = html;
  form.oninput = markDirty;
  form.onchange = markDirty;
  form.onsubmit = save;
  $("#reset").onclick = loadSettings;
}

function fieldHtml(s, v) {
  const id = `f-${s.key}`;
  let input;
  if (s.type === "bool") input = `<input type="checkbox" id="${id}" ${v ? "checked" : ""}>`;
  else if (s.type === "choice") input = `<select id="${id}">${s.choices.map(c => `<option ${c === v ? "selected" : ""}>${esc(c)}</option>`).join("")}</select>`;
  else if (s.type === "list") input = `<input type="text" id="${id}" value="${esc(v.join(", "))}">`;
  else if (s.type === "str") input = `<input type="text" id="${id}" value="${esc(v)}">`;
  else input = `<input type="number" id="${id}" value="${esc(v)}" min="${s.min}" max="${s.max}" step="${s.type === "int" ? 1 : "any"}">`;
  const range = s.min != null ? ` (${s.min}–${s.max})` : "";
  return `<div class="field"><label for="${id}">${esc(s.label)}</label><div>${input}</div>
    <div class="help">${esc(s.help || "")}${range} · domyślnie: ${esc(Array.isArray(s.default) ? s.default.join(", ") : s.default)}</div></div>`;
}

function readField(s) {
  const el = $(`#f-${s.key}`);
  if (s.type === "bool") return el.checked;
  if (s.type === "int" || s.type === "float") return el.value === "" ? null : Number(el.value);
  if (s.type === "list") return el.value;
  return el.value;
}

function changed() {
  const out = {};
  for (const s of schema) {
    let v = readField(s);
    const cur = values[s.key];
    const same = s.type === "list" ? String(v).replace(/\s/g, "") === cur.join(",") : v === cur;
    $(`#f-${s.key}`).classList.toggle("changed", !same);
    if (!same) out[s.key] = v;
  }
  return out;
}

function markDirty() {
  const n = Object.keys(changed()).length;
  $("#save").disabled = !n;
  $("#reset").disabled = !n;
  $("#dirty").textContent = n ? `Zmienione: ${n}` : "";
}

async function save(e) {
  e.preventDefault();
  try {
    const r = await api("/api/admin/settings", { method: "PUT", body: JSON.stringify(changed()) });
    toast(`Zapisano: ${Object.keys(r.saved).join(", ")}`);
    loadSettings();
  } catch (err) {
    toast(err.message, true);
  }
}

// ------------------------------------------------------------------ czujniki

document.querySelectorAll("#sensor-kinds button").forEach(b => {
  b.onclick = () => {
    sensorKind = b.dataset.kind;
    document.querySelectorAll("#sensor-kinds button").forEach(x => x.classList.toggle("active", x === b));
    loadSensors();
  };
});

async function loadSensors() {
  const rows = await api(`/api/admin/sensors?kind=${sensorKind}`);
  $("#sensor-hint").textContent = {
    flagged: "Czujniki oznaczone automatycznie jako podejrzane oraz ukryte ręcznie. Ukrycie usuwa czujnik z mapy na stałe (do przywrócenia).",
    no_data: "ShipX zgłasza czujnik, ale inpost.pl nie zwrócił odczytów. Mapa pokazuje wtedy ostatni dobry odczyt.",
    no_id: "Nie udało się ustalić ID punktu (brak strony w sitemapie albo strona przekierowuje). Ponowna próba po czasie z parametru.",
  }[sensorKind] + ` (${rows.length})`;
  $("#sensors thead").innerHTML = `<tr><th>Paczkomat</th><th>Adres</th><th>Status</th><th>Flagi</th><th>PM2.5</th><th>Wilg.</th><th>Bez zmian od</th><th></th></tr>`;
  $("#sensors tbody").innerHTML = rows.map(r => `<tr>
    <td>${r.page_url ? `<a href="${esc(r.page_url)}" target="_blank" rel="noopener">${esc(r.name)}</a>` : esc(r.name)}</td>
    <td>${esc([r.street, r.building].filter(Boolean).join(" "))}, ${esc(r.city)}</td>
    <td>${esc(r.status || r.id_status || "")}</td><td>${esc(r.flags || "")}</td>
    <td>${r.pm25 ?? "–"}</td><td>${r.humidity ?? "–"}</td><td>${when(r.changed_at)}</td>
    <td><button class="btn secondary" data-name="${esc(r.name)}" data-hidden="${r.hidden ? 0 : 1}">${r.hidden ? "Przywróć" : "Ukryj"}</button></td>
  </tr>`).join("");
  document.querySelectorAll("#sensors button[data-name]").forEach(b => {
    b.onclick = async () => {
      await api(`/api/admin/sensor/${b.dataset.name}`, { method: "POST", body: JSON.stringify({ hidden: b.dataset.hidden === "1" }) });
      toast(`${b.dataset.name}: ${b.dataset.hidden === "1" ? "ukryty" : "przywrócony"}`);
      loadSensors();
    };
  });
}

// ------------------------------------------------------------------ zdrowie sieci

async function loadHealth() {
  const h = await api("/api/admin/health");
  const tot = { sensors: 0, ok: 0, broken: 0, stuck: 0, dead: 0, absurd: 0, outlier: 0, stale: 0, wet: 0, no_data: 0, no_id: 0 };
  const row = (name, r) => `<tr><td>${esc(name)}</td><td>${num(r.sensors)}</td><td>${num(r.ok)}</td><td><b>${num(r.broken)}</b></td>
    <td>${r.sensors ? (100 * r.broken / r.sensors).toFixed(1) : "–"}</td><td>${num(r.stuck)}</td><td>${num(r.dead)}</td><td>${num(r.absurd)}</td>
    <td>${num(r.outlier)}</td><td>${num(r.stale)}</td><td>${num(r.wet)}</td><td>${num(r.no_data)}</td><td>${num(r.no_id)}</td></tr>`;
  let body = "";
  for (const r of h.provinces) {
    body += row(r.province || "?", r);
    for (const k of Object.keys(tot)) tot[k] += r[k] || 0;
  }
  $("#health tbody").innerHTML = body + row("RAZEM", tot).replace("<tr>", '<tr style="font-weight:600">');
  const a = h.abroad;
  $("#abroad").innerHTML = a
    ? `Ostatnie sprawdzenie: ${when(a.ts)} — ${Object.entries(a.found).map(([c, n]) => `${c}: <b>${n}</b>`).join(", ")}
       ${Object.values(a.found).some(n => n) ? " — <b style=\"color:var(--ok)\">pojawiły się czujniki!</b>" : ""}`
    : `Jeszcze nie sprawdzano — zadanie „Czujniki za granicą” uruchomi się samo albo z zakładki Status.`;
}

// ------------------------------------------------------------------ alerty

async function loadAlerts() {
  const a = await api("/api/admin/alerts");
  const t = a.telegram;
  $("#tg-status").innerHTML = !t.configured ? "Bot <b>wyłączony</b> — brak tokenu."
    : `Bot <b>@${esc(t.bot || "?")}</b> — ${t.running ? "działa" : "uruchamia się…"}${t.error ? ` · <span class="err">błąd: ${esc(t.error)}</span>` : ""}
       · subskrybentów: <b>${num(t.subs)}</b>${t.last_update ? ` · ostatni kontakt z Telegramem ${when(t.last_update)}` : ""}
       ${t.bot ? ` · <a href="https://t.me/${esc(t.bot)}" target="_blank" rel="noopener">otwórz bota</a>` : ""}
       ${t.from_panel ? "" : " · (token z pliku env na serwerze)"}`;
  $("#tg-off").hidden = !t.from_panel;
  $("#push-status").innerHTML = `Subskrypcji: <b>${num(a.push.subs)}</b>, w tej chwili w stanie alarmu: <b>${num(a.push.high)}</b>.`;
}

$("#tg-form").onsubmit = async e => {
  e.preventDefault();
  try {
    const r = await api("/api/admin/telegram", { method: "PUT", body: JSON.stringify({ token: $("#tg-token").value }) });
    $("#tg-token").value = "";
    toast(`Bot @${r.bot} zapisany — rusza w ciągu kilkunastu sekund`);
    setTimeout(loadAlerts, 3000);
  } catch (err) {
    toast(err.message, true);
  }
};

$("#tg-off").onclick = async () => {
  if (!confirm("Wyłączyć bota? Subskrybenci przestaną dostawać alerty (ich zapisy zostają).")) return;
  await api("/api/admin/telegram", { method: "PUT", body: JSON.stringify({ token: "" }) });
  toast("Bot wyłączony");
  loadAlerts();
};

// ------------------------------------------------------------------ klucze API

async function loadKeys() {
  const rows = await api("/api/admin/keys");
  $("#keys tbody").innerHTML = rows.map(k => `<tr class="${k.revoked ? "revoked" : ""}">
    <td>${esc(k.name)}</td><td><code>${esc(k.prefix)}…</code></td><td>${num(k.rate_per_min)}/min</td>
    <td>${when(k.created)}</td><td>${when(k.last_used)}</td><td>${num(k.uses)}</td>
    <td>${k.revoked ? `unieważniony ${when(k.revoked)}` : `<button class="btn danger" data-id="${k.id}" data-name="${esc(k.name)}">Unieważnij</button>`}</td>
  </tr>`).join("") || `<tr><td colspan="7" class="hint">Brak kluczy.</td></tr>`;
  document.querySelectorAll("#keys button[data-id]").forEach(b => {
    b.onclick = async () => {
      if (!confirm(`Unieważnić klucz „${b.dataset.name}”? Kto go używa, straci dostęp od razu. Tego nie da się cofnąć.`)) return;
      await api(`/api/admin/keys/${b.dataset.id}/revoke`, { method: "POST" });
      toast(`Klucz „${b.dataset.name}” unieważniony`);
      loadKeys();
    };
  });
}

$("#key-form").onsubmit = async e => {
  e.preventDefault();
  try {
    const r = await api("/api/admin/keys", { method: "POST",
      body: JSON.stringify({ name: $("#key-name").value, rate_per_min: Number($("#key-rate").value) }) });
    const box = $("#key-new");
    box.hidden = false;
    box.innerHTML = `<b>Klucz „${esc(r.name)}” — skopiuj go teraz, później nie będzie widoczny:</b>
      <code>${esc(r.key)}</code><span class="hint">Użycie: nagłówek <code style="display:inline">X-API-Key: …</code> w każdym zapytaniu do /api/v1.</span>`;
    $("#key-name").value = "";
    loadKeys();
  } catch (err) {
    toast(err.message, true);
  }
};

// ------------------------------------------------------------------ dziennik

async function loadLog() {
  const lines = await api("/api/admin/log");
  const pre = $("#log");
  pre.textContent = lines.slice().reverse().join("\n") || "(pusto)";
}

// ------------------------------------------------------------------ start

async function start() {
  try {
    const r = await api("/api/admin/settings");
    values = r.values;
    schema = r.schema;
  } catch { return; }
  $("#login").hidden = true;
  $("#app").hidden = false;
  loadStatus();
  clearInterval(timer);
  timer = setInterval(() => {
    if (!$("#tab-status").hidden) loadStatus().catch(() => {});
    if (!$("#tab-log").hidden) loadLog().catch(() => {});
  }, 4000);
}

start();
