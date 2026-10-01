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

from . import api_v1, apikeys, collector, config, db, sources, surface
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
       "connect-src 'self' https://tiles.openfreemap.org; worker-src blob:; child-src blob:; "
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


@app.get("/api/hex")
def api_hex(res: int = 6, metric: str = "pm25", suspect: bool = False):
    if metric not in collector.METRICS or not 3 <= res <= 8:
        raise HTTPException(400, "zły parametr")
    return JSONResponse(collector.hex_layer(res, metric, suspect), headers={"Cache-Control": "max-age=60"})


_surface_lock = threading.Lock()
_STOP = re.compile(r"^-?\d+(\.\d+)?:#[0-9a-fA-F]{6}$")


@app.get("/api/surface.png")
def api_surface(metric: str = "pm25", stops: str = "", suspect: bool = False):
    """Plama (interpolacja) dla wielkości; przystanki kolorów z legendy przeglądarki: „12:#2e9e44,30:#9ccc3a…”."""
    if metric not in collector.METRICS:
        raise HTTPException(400, "zła wielkość")
    parts = stops.split(",")
    if not 2 <= len(parts) <= 10 or not all(_STOP.match(p) for p in parts):
        raise HTTPException(400, "złe przystanki kolorów")
    stop_list = sorted((float(v), c) for v, c in (p.split(":") for p in parts))
    key = (metric, suspect, stops, collector.cache["built"])
    cached = collector.cache.setdefault("surface", {})
    if key not in cached:
        with _surface_lock:  # jedno liczenie naraz — to najdroższy endpoint
            if key not in cached:
                pts = [(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0], f["properties"][metric])
                       for f in (collector.cache["sensors"] or {}).get("features", [])
                       if f["properties"].get(metric) is not None and (suspect or not f["properties"]["suspect"])]
                if len(pts) < 3:
                    raise HTTPException(404, "za mało danych")
                if len(cached) > 40:
                    cached.clear()
                cached[key] = surface.render(pts, stop_list)
    return Response(cached[key], media_type="image/png", headers={"Cache-Control": "max-age=300"})


@app.get("/api/surface/corners")
def api_surface_corners():
    return surface.corners()


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

@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"))


@app.get("/api")
def api_docs():
    return FileResponse(os.path.join(STATIC, "api.html"))


@app.get("/admin")
def admin_page():
    return FileResponse(os.path.join(STATIC, "admin.html"))


app.include_router(api_v1.router)
app.mount("/static", StaticFiles(directory=STATIC), name="static")
