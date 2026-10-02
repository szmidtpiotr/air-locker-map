"""Serwer WWW: strona z mapą, publiczne API i panel administracyjny."""
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import alerts, api_v1, apikeys, collector, config, db, sources, surface
from .quality import FLAG_LABELS

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

STATIC = os.path.join(os.path.dirname(__file__), "static")
SECRET = os.environ.get("AIRMAP_SECRET", "").encode()
ADMIN_HASH = os.environ.get("AIRMAP_ADMIN_HASH", "")  # scrypt$<salt hex>$<hash hex>
COOKIE = "airmap_admin"
SESSION_HOURS = 12

app = FastAPI(title="air-locker-map", docs_url=None, redoc_url=None, openapi_url=None)

# MapLibre potrzebuje workerów z blob: i kafelków/fontów z OpenFreeMap; style inline są w dymkach.
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
       "img-src 'self' data: blob: https://tiles.openfreemap.org; "
       "connect-src 'self' https://tiles.openfreemap.org; worker-src 'self' blob:; child-src blob:; manifest-src 'self'; "
       "font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


@app.middleware("http")
async def security_headers(request: Request, call_next):
    response = await call_next(request)
    h = response.headers
    h["Content-Security-Policy"] = CSP
    h["X-Content-Type-Options"] = "nosniff"
    h["Referrer-Policy"] = "strict-origin-when-cross-origin"
    h["X-Frame-Options"] = "DENY"
    h["Permissions-Policy"] = "geolocation=(self), camera=(), microphone=()"
    if request.url.path.startswith("/api/admin"):
        h["Cache-Control"] = "no-store"
    elif request.url.path.startswith("/static/"):
        h["Cache-Control"] = "public, max-age=31536000, immutable" if request.url.query.startswith("v=") else "no-cache"
    return response


def client_ip(request: Request):
    # za NPM uvicorn (--proxy-headers, zaufany tylko .4) podstawia prawdziwy adres klienta
    return request.client.host if request.client else "?"


@app.on_event("startup")
def startup():
    if not SECRET or not ADMIN_HASH:
        logging.warning("Brak AIRMAP_SECRET / AIRMAP_ADMIN_HASH — panel admina zablokowany")
    db.init()
    # przebiegi przerwane restartem usługi nie mają końca — zamykamy je, żeby nie wisiały jako „trwa”
    db.write("UPDATE runs SET finished=started, note='przerwane (restart usługi)' WHERE finished IS NULL")
    collector.build_cache()  # od razu po starcie, inaczej API przez kilka sekund zwraca 404
    alerts.start()
    collector.AFTER_COLLECT.append(alerts.evaluate_all)
    collector.start_scheduler()


# ---------------------------------------------------------------- logowanie

def hash_password(pw, salt=None):
    salt = salt or os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=2 ** 14, r=8, p=1)
    return f"scrypt${salt.hex()}${h.hex()}"


def check_password(pw):
    try:
        _, salt, _ = ADMIN_HASH.split("$")
    except ValueError:
        return False
    return hmac.compare_digest(hash_password(pw, bytes.fromhex(salt)), ADMIN_HASH)


def make_token():
    exp = str(int(time.time()) + SESSION_HOURS * 3600)
    sig = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f"{exp}:{sig}".encode()).decode()


def valid_token(token):
    try:
        exp, sig = base64.urlsafe_b64decode(token.encode()).decode().split(":")
    except Exception:  # noqa: BLE001
        return False
    good = hmac.new(SECRET, exp.encode(), hashlib.sha256).hexdigest()
    return bool(SECRET) and hmac.compare_digest(sig, good) and int(exp) > time.time()


def admin(request: Request):
    if not valid_token(request.cookies.get(COOKIE, "")):
        raise HTTPException(401, "Zaloguj się")


_login_fail = {}


@app.post("/api/admin/login")
async def login(request: Request, response: Response):
    ip = client_ip(request)
    fails = [t for t in _login_fail.get(ip, []) if t > time.time() - 600]
    if len(fails) >= 5:
        raise HTTPException(429, "Za dużo prób — odczekaj 10 minut")
    try:
        body = await request.json()
        password = str(body.get("password", ""))[:200]
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Zły format") from None
    if not SECRET or not check_password(password):
        _login_fail[ip] = fails + [time.time()]
        raise HTTPException(403, "Złe hasło")
    _login_fail.pop(ip, None)
    response.set_cookie(COOKIE, make_token(), max_age=SESSION_HOURS * 3600, httponly=True, samesite="strict",
                        secure=request.url.scheme == "https")
    return {"ok": True}


@app.post("/api/admin/logout")
def logout(response: Response):
    response.delete_cookie(COOKIE)
    return {"ok": True}


# ---------------------------------------------------------------- API publiczne

def public_config():
    s = db.get_settings()
    keys = ("site_title", "default_metric", "default_view", "hex_resolution", "nearest_count", "pm25_thresholds",
            "pm10_thresholds", "pm1_thresholds", "hide_flagged", "gios_layer")
    return {k: s[k] for k in keys} | {"flag_labels": FLAG_LABELS}


@app.get("/api/config")
def api_config():
    return public_config()


@app.get("/api/sensors")
def api_sensors():
    data = collector.cache["sensors"] or {"type": "FeatureCollection", "features": []}
    return JSONResponse(data, headers={"Cache-Control": "max-age=60"})


_surface_lock = threading.Lock()
_STOP = re.compile(r"^-?\d+(\.\d+)?:#[0-9a-fA-F]{6}$")


def _check_ts(ts):
    if ts is not None and not any(f["ts"] == ts for f in collector.frames(26)):
        raise HTTPException(404, "nie ma takiej klatki")


@app.get("/api/hex")
def api_hex(res: int = 6, metric: str = "pm25", suspect: bool = False, ts: int | None = None):
    if metric not in collector.METRICS or not 3 <= res <= 8:
        raise HTTPException(400, "zły parametr")
    _check_ts(ts)
    return JSONResponse(collector.hex_layer(res, metric, suspect, ts), headers={"Cache-Control": "max-age=300"})


@app.get("/api/frames")
def api_frames():
    """Klatki suwaka czasu: pełne przebiegi z ostatnich 24 h."""
    return JSONResponse(collector.frames(24), headers={"Cache-Control": "max-age=120"})


@app.get("/api/frame")
def api_frame(ts: int, metric: str = "pm25"):
    if metric not in collector.HISTORY_METRICS:
        raise HTTPException(400, "ta wielkość nie ma historii")
    _check_ts(ts)
    return JSONResponse({"ts": ts, "values": collector.frame_values(ts, metric)},
                        headers={"Cache-Control": "max-age=3600"})


@app.get("/api/surface.png")
def api_surface(metric: str = "pm25", stops: str = "", suspect: bool = False, ts: int | None = None):
    """Plama (interpolacja) dla wielkości; przystanki kolorów z legendy przeglądarki: „12:#2e9e44,30:#9ccc3a…”."""
    if metric not in collector.METRICS:
        raise HTTPException(400, "zła wielkość")
    parts = stops.split(",")
    if not 2 <= len(parts) <= 10 or not all(_STOP.match(p) for p in parts):
        raise HTTPException(400, "złe przystanki kolorów")
    _check_ts(ts)
    stop_list = sorted((float(v), c) for v, c in (p.split(":") for p in parts))
    key = (metric, suspect, stops, ts, collector.cache["built"])
    cached = collector.cache.setdefault("surface", {})
    if key not in cached:
        with _surface_lock:  # jedno liczenie naraz — to najdroższy endpoint
            if key not in cached:
                pts = collector.metric_points(metric, ts, suspect)
                if len(pts) < 3:
                    raise HTTPException(404, "za mało danych")
                if len(cached) > 60:
                    cached.clear()
                cached[key] = surface.render(pts, stop_list)
    # klatki z przeszłości się nie zmieniają — przeglądarka może je trzymać długo
    age = 86400 if ts else 300
    return Response(cached[key], media_type="image/png", headers={"Cache-Control": f"max-age={age}"})


@app.get("/api/isobars")
def api_isobars(ts: int | None = None, step: float = 2.0):
    """Izobary (ciśnienie n.p.m.) co `step` hPa."""
    if not 0.5 <= step <= 5:
        raise HTTPException(400, "krok 0,5–5 hPa")
    _check_ts(ts)
    key = ("iso", ts, step, collector.cache["built"])
    cached = collector.cache.setdefault("surface", {})
    if key not in cached:
        with _surface_lock:
            if key not in cached:
                pts = collector.metric_points("pressure_sl", ts)
                cached[key] = (surface.isolines(pts, step) if len(pts) >= 3
                               else {"type": "FeatureCollection", "features": []})
    return JSONResponse(cached[key], headers={"Cache-Control": "max-age=86400" if ts else "max-age=300"})


@app.get("/api/wind")
def api_wind():
    return JSONResponse(collector.cache.get("wind") or {"type": "FeatureCollection", "features": []},
                        headers={"Cache-Control": "max-age=300"})


@app.get("/api/gios")
def api_gios():
    return collector.cache["gios"] or {"type": "FeatureCollection", "features": []}


@app.get("/api/history/{name}")
def api_history(name: str, hours: int = 24):
    hours = max(1, min(hours, 24))  # strona potrzebuje tylko wykresu 24 h; dłuższa historia → API v1 z kluczem
    rows = db.q("SELECT ts, pm1, pm25, pm10, pressure_sl, humidity, temperature FROM readings "
                "WHERE name=? AND ts>=? ORDER BY ts", (name.upper(), int(time.time()) - hours * 3600))
    return [dict(r) for r in rows]


_search_hits = defaultdict(deque)
_geo_lock = threading.Lock()
SEARCH_PER_MIN = 20


@app.get("/api/search")
def api_search(q: str, request: Request):
    q = " ".join(q.split())[:120]
    if not q:
        return []
    now = time.time()
    hits = _search_hits[client_ip(request)]
    while hits and hits[0] < now - 60:
        hits.popleft()
    if len(hits) >= SEARCH_PER_MIN:
        raise HTTPException(429, "Za dużo wyszukiwań — odczekaj minutę")
    hits.append(now)
    code = q.upper().replace(" ", "")
    r = db.q1("SELECT name, lat, lon, street, building, city FROM lockers WHERE name=?", (code,))
    if r:
        return [{"lat": r["lat"], "lon": r["lon"], "label": f"{r['name']} — {r['street'] or ''} {r['building'] or ''}, "
                 f"{r['city']}", "locker": r["name"]}]
    key = q.lower()
    cached = db.q1("SELECT result FROM geocode WHERE q=?", (key,))
    if cached:
        return json.loads(cached["result"])
    # Nominatim: max 1 zapytanie/s na całą aplikację (zasady usługi) — globalna blokada
    with _geo_lock:
        wait = 1.1 - (time.time() - _geo["last"])
        if wait > 0:
            time.sleep(wait)
        _geo["last"] = time.time()
        try:
            res = sources.geocode(q)
        except Exception:  # noqa: BLE001
            logging.exception("geokoder")
            raise HTTPException(502, "Wyszukiwarka adresów chwilowo niedostępna") from None
    db.write("INSERT OR REPLACE INTO geocode(q, result, ts) VALUES(?, ?, ?)", (key, json.dumps(res), int(time.time())))
    db.write("DELETE FROM geocode WHERE ts < ?", (int(time.time()) - 30 * 86400,))
    return res


_geo = {"last": 0.0}


@app.get("/api/profile/{name}")
def api_profile(name: str):
    """Profil dobowy czujnika (średnie PM2.5 dla godzin doby, dni robocze / weekend)."""
    name = name.upper()
    if not db.q1("SELECT 1 FROM lockers WHERE name = ?", (name,)):
        raise HTTPException(404, "nie ma takiego paczkomatu")
    return JSONResponse(collector.sensor_profile(name), headers={"Cache-Control": "max-age=900"})


@app.get("/api/compare")
def api_compare():
    """Paczkomaty kontra stacje GIOŚ (liczone po każdym pobraniu stacji)."""
    return JSONResponse(collector.cache.get("compare") or {"summary": None, "stations": []},
                        headers={"Cache-Control": "max-age=600"})


_sub_hits = defaultdict(deque)


def _sub_limit(request: Request, per_hour=10):
    now, q = time.time(), _sub_hits[client_ip(request)]
    while q and q[0] < now - 3600:
        q.popleft()
    if len(q) >= per_hour:
        raise HTTPException(429, "Za dużo prób — spróbuj za godzinę")
    q.append(now)


@app.get("/api/alerts/config")
def api_alerts_config():
    return {"telegram_bot": alerts.tg_bot_name(), "thresholds": alerts.THRESHOLDS,
            "radius_km": alerts.RADIUS_KM, "push_key": alerts.vapid_public_key()}


@app.post("/api/push/subscribe")
async def api_push_subscribe(request: Request):
    _sub_limit(request)
    try:
        b = await request.json()
        sub = b["subscription"]
        endpoint = str(sub["endpoint"])
        keys = {"p256dh": str(sub["keys"]["p256dh"])[:200], "auth": str(sub["keys"]["auth"])[:100]}
        lat, lon, thr = float(b["lat"]), float(b["lon"]), float(b["threshold"])
        label = " ".join(str(b.get("label", "")).split())[:60]
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "Zły format") from None
    if not endpoint.startswith("https://") or len(endpoint) > 600:
        raise HTTPException(400, "Zły adres powiadomień")
    if not (48 < lat < 56 and 13 < lon < 25) or thr not in alerts.THRESHOLDS:
        raise HTTPException(400, "Zła lokalizacja albo próg")
    if db.q1("SELECT COUNT(*) AS n FROM push_subs WHERE ip=?", (client_ip(request),))["n"] >= 10:
        raise HTTPException(429, "Za dużo alertów z tego adresu")
    sid, token = alerts.push_subscribe({"endpoint": endpoint, "keys": keys}, lat, lon, thr, label, client_ip(request))
    return {"id": sid, "token": token}


async def _own_sub(request: Request):
    try:
        b = await request.json()
        row = db.q1("SELECT * FROM push_subs WHERE id=?", (int(b["id"]),))
        ok = row and hmac.compare_digest(row["token"], str(b["token"]))
    except (KeyError, TypeError, ValueError):
        ok = False
    if not ok:
        raise HTTPException(404, "Nie ma takiego alertu")
    return row


@app.post("/api/push/unsubscribe")
async def api_push_unsubscribe(request: Request):
    row = await _own_sub(request)
    db.write("DELETE FROM push_subs WHERE id=?", (row["id"],))
    return {"ok": True}


@app.post("/api/push/test")
async def api_push_test(request: Request):
    _sub_limit(request, per_hour=20)
    row = await _own_sub(request)
    ok = alerts.push_send(row, "Test powiadomienia", f"Alert dla „{row['label'] or 'tego miejsca'}” działa. "
                          f"Próg PM2.5: {row['threshold']:.0f} µg/m³.")
    if not ok:
        raise HTTPException(502, "Nie udało się wysłać — przeglądarka mogła wycofać zgodę")
    return {"ok": True}


@app.get("/sw.js")
def service_worker():
    # z katalogu głównego, żeby zasięg workera obejmował całą stronę
    return FileResponse(os.path.join(STATIC, "sw.js"), media_type="text/javascript",
                        headers={"Cache-Control": "no-cache", "Service-Worker-Allowed": "/"})


@app.get("/manifest.webmanifest")
def manifest():
    return FileResponse(os.path.join(STATIC, "manifest.webmanifest"), media_type="application/manifest+json")


@app.get("/api/stats")
def api_stats():
    return JSONResponse(collector.compute_stats(), headers={"Cache-Control": "max-age=120"})


@app.get("/api/status")
def api_status():
    last = db.q1("SELECT finished FROM runs WHERE job='collect' AND finished IS NOT NULL ORDER BY id DESC LIMIT 1")
    feats = (collector.cache["sensors"] or {}).get("features", [])
    return {"last_collect": last["finished"] if last else None, "sensors": len(feats),
            "suspect": sum(1 for f in feats if f["properties"]["suspect"]), "job": collector.state.job}


# ---------------------------------------------------------------- API admina

@app.get("/api/admin/settings", dependencies=[Depends(admin)])
def admin_settings():
    return {"schema": config.SETTINGS, "values": db.get_settings()}


@app.put("/api/admin/settings", dependencies=[Depends(admin)])
async def admin_save(request: Request):
    body = await request.json()
    try:
        saved = db.save_settings(body)
    except ValueError as e:
        raise HTTPException(400, str(e)) from None
    if "request_rate" in saved:
        sources.inpost_limiter.rate = saved["request_rate"]
    if any(k in saved for k in ("pm_max", "pm10_min", "humidity_max", "stale_hours", "outlier_radius_km", "outlier_min_neighbors",
                                "outlier_factor", "outlier_abs")):
        collector.start_job("flags")
    collector.log(f"admin: zmienione parametry {', '.join(saved)}")
    return {"ok": True, "saved": saved}


@app.get("/api/admin/status", dependencies=[Depends(admin)])
def admin_status():
    counts = dict(db.q1("""SELECT COUNT(*) AS lockers,
        SUM(shipx_level IS NOT NULL) AS with_sensor,
        SUM(shipx_level IS NOT NULL AND point_id IS NOT NULL) AS with_id,
        SUM(shipx_level IS NOT NULL AND id_status='no_page') AS no_page,
        SUM(shipx_level IS NOT NULL AND id_status IN ('no_url','error')) AS no_url,
        SUM(shipx_level IS NOT NULL AND point_id IS NULL AND id_checked_at IS NULL) AS id_pending,
        SUM(shipx_level IS NOT NULL AND elevation IS NULL) AS no_elevation,
        SUM(hidden) AS hidden FROM lockers"""))
    counts |= dict(db.q1("SELECT (SELECT COUNT(*) FROM push_subs) AS push_subs, (SELECT COUNT(*) FROM tg_subs) AS tg_subs"))
    counts |= dict(db.q1("""SELECT SUM(status='ok') AS reading_ok, SUM(status='no_data') AS reading_no_data,
        SUM(status='error') AS reading_error, SUM(flags!='') AS flagged FROM latest"""))
    flag_counts = {}
    for r in db.q("SELECT flags FROM latest WHERE flags!=''"):
        for f in r["flags"].split(","):
            flag_counts[f] = flag_counts.get(f, 0) + 1
    runs = [dict(r) for r in db.q("SELECT * FROM runs ORDER BY id DESC LIMIT 30")]
    size = os.path.getsize(db.DB_PATH) if os.path.exists(db.DB_PATH) else 0
    readings = db.q1("SELECT COUNT(*) AS n, MIN(ts) AS since FROM readings")
    st = collector.state
    return {
        "counts": counts, "flag_counts": flag_counts, "runs": runs, "db_bytes": size,
        "readings": dict(readings), "next": collector.next_runs(),
        "job": {"key": st.job, "label": collector.JOBS[st.job][0] if st.job else None,
                "done": st.progress[0], "total": st.progress[1], "started": st.started},
        "jobs": {k: v[0] for k, v in collector.JOBS.items()},
    }


@app.post("/api/admin/run/{job}", dependencies=[Depends(admin)])
def admin_run(job: str):
    if job not in collector.JOBS:
        raise HTTPException(404, "nie ma takiego zadania")
    if not collector.start_job(job):
        raise HTTPException(409, "Trwa inne zadanie — poczekaj albo je przerwij")
    collector.log(f"admin: uruchomione zadanie {job}")
    return {"ok": True}


@app.post("/api/admin/cancel", dependencies=[Depends(admin)])
def admin_cancel():
    collector.state.cancel = True
    collector.log("admin: przerwanie zadania")
    return {"ok": True}


@app.get("/api/admin/log", dependencies=[Depends(admin)])
def admin_log():
    return list(collector.log_lines)[-300:]


@app.get("/api/admin/sensors", dependencies=[Depends(admin)])
def admin_sensors(kind: str = "flagged"):
    where = {
        "flagged": "l.flags != '' OR k.hidden = 1",
        "no_data": "l.status IN ('no_data', 'error')",
        "no_id": "k.shipx_level IS NOT NULL AND k.point_id IS NULL AND k.id_checked_at IS NOT NULL",
    }.get(kind)
    if not where:
        raise HTTPException(400, "zły rodzaj")
    rows = db.q(f"""SELECT k.name, k.city, k.street, k.building, k.hidden, k.id_status, k.page_url, l.status,
        l.flags, l.pm25, l.humidity, l.changed_at, l.ts FROM lockers k LEFT JOIN latest l USING(name)
        WHERE {where} ORDER BY k.name LIMIT 1000""")
    return [dict(r) for r in rows]


@app.get("/api/admin/health", dependencies=[Depends(admin)])
def admin_health():
    abroad = db.q1("SELECT value FROM settings WHERE key='abroad_last'")
    return {"provinces": collector.network_health(), "abroad": json.loads(abroad["value"]) if abroad else None}


@app.get("/api/admin/alerts", dependencies=[Depends(admin)])
def admin_alerts():
    return {
        "telegram": {"configured": bool(alerts.tg_token()), "from_panel": bool(db.setting_raw("telegram_token")),
                     "bot": alerts.tg_bot_name(), **alerts.tg_status,
                     "subs": db.q1("SELECT COUNT(*) AS n FROM tg_subs WHERE lat IS NOT NULL")["n"]},
        "push": {"subs": db.q1("SELECT COUNT(*) AS n FROM push_subs")["n"],
                 "high": db.q1("SELECT COUNT(*) AS n FROM push_subs WHERE state='high'")["n"]},
    }


@app.put("/api/admin/telegram", dependencies=[Depends(admin)])
async def admin_telegram(request: Request):
    body = await request.json()
    token = str(body.get("token", "")).strip()
    if not token:
        db.write("DELETE FROM settings WHERE key='telegram_token'")
        collector.log("admin: bot Telegram wyłączony")
        return {"ok": True, "bot": None}
    if not re.fullmatch(r"\d{5,15}:[A-Za-z0-9_-]{30,60}", token):
        raise HTTPException(400, "To nie wygląda na token z BotFathera (liczba:ciąg znaków)")
    try:
        name = alerts.tg_check_token(token)
    except Exception:  # noqa: BLE001
        raise HTTPException(400, "Telegram odrzucił ten token") from None
    db.write("INSERT INTO settings(key, value) VALUES('telegram_token', ?) "
             "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (token,))
    collector.log(f"admin: bot Telegram ustawiony (@{name})")
    return {"ok": True, "bot": name}


@app.get("/api/admin/keys", dependencies=[Depends(admin)])
def admin_keys():
    return apikeys.listing()


@app.post("/api/admin/keys", dependencies=[Depends(admin)])
async def admin_key_create(request: Request):
    body = await request.json()
    name = " ".join(str(body.get("name", "")).split())[:60]
    try:
        rate = int(body.get("rate_per_min", 60))
    except (TypeError, ValueError):
        rate = 0
    if not name:
        raise HTTPException(400, "Podaj nazwę klucza (np. „HA dom”)")
    if not 1 <= rate <= 600:
        raise HTTPException(400, "Limit: 1–600 zapytań na minutę")
    key = apikeys.create(name, rate)
    collector.log(f"admin: nowy klucz API „{name}” ({key[:10]}…, {rate}/min)")
    return {"key": key, "name": name, "rate_per_min": rate}


@app.post("/api/admin/keys/{key_id}/revoke", dependencies=[Depends(admin)])
def admin_key_revoke(key_id: int):
    apikeys.revoke(key_id)
    collector.log(f"admin: unieważniony klucz API #{key_id}")
    return {"ok": True}


@app.post("/api/admin/sensor/{name}", dependencies=[Depends(admin)])
async def admin_sensor(name: str, request: Request):
    body = await request.json()
    hidden = 1 if body.get("hidden") else 0
    db.write("UPDATE lockers SET hidden=? WHERE name=?", (hidden, name.upper()))
    collector.log(f"admin: {name} {'ukryty' if hidden else 'przywrócony'}")
    collector.build_cache()
    return {"ok": True}


# ---------------------------------------------------------------- strony

def _asset_version():
    """Znacznik wersji plików strony — zmienia się przy każdym wdrożeniu (najnowsza data modyfikacji)."""
    newest = max(os.path.getmtime(os.path.join(STATIC, f)) for f in os.listdir(STATIC)
                 if f.endswith((".js", ".css")))
    return str(int(newest))


def _html(name):
    """HTML z ?v=<wersja> przy skryptach i stylach. Bez tego przeglądarka trzymała stary app.js
    (serwer nie podawał Cache-Control, więc zgadywała czas ważności) i nie widać było poprawek."""
    v = _asset_version()
    with open(os.path.join(STATIC, name), encoding="utf-8") as f:
        html = f.read()
    html = re.sub(r'(/static/[\w.-]+\.(?:js|css))"', rf'\1?v={v}"', html)
    return Response(html, media_type="text/html; charset=utf-8", headers={"Cache-Control": "no-cache"})


@app.get("/")
def index():
    return _html("index.html")


@app.get("/api")
def api_docs():
    return _html("api.html")


@app.get("/admin")
def admin_page():
    return _html("admin.html")


app.include_router(api_v1.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
