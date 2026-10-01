"""Publiczne API v1 — stabilny kontrakt dla Home Assistanta i innych klientów.

Endpointy strony (/api/sensors, /api/hex …) mogą się zmieniać razem z mapą; ten moduł nie.
Wszystko tylko do odczytu, bez ciasteczek, z CORS „*” i limitem zapytań na IP.
"""
import time
from collections import defaultdict, deque

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from . import collector, db
from .quality import FLAG_LABELS, distance_km

router = APIRouter(prefix="/api/v1")

RATE_PER_MIN = 120
_hits = defaultdict(deque)

FIELDS = ("pm1", "pm25", "pm4", "pm10", "pressure", "pressure_sl", "humidity", "temperature")


def _limit(request: Request):
    ip = request.client.host if request.client else "?"
    now = time.time()
    q = _hits[ip]
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= RATE_PER_MIN:
        raise HTTPException(429, f"Limit {RATE_PER_MIN} zapytań na minutę")
    q.append(now)


def _json(data, max_age=60):
    return JSONResponse(data, headers={"Cache-Control": f"public, max-age={max_age}",
                                       "Access-Control-Allow-Origin": "*"})


def _sensor(f, dist=None):
    p = f["properties"]
    lon, lat = f["geometry"]["coordinates"]
    out = {
        "name": p["name"], "address": p["address"], "description": p["description"],
        "lat": lat, "lon": lon, "elevation": p["elevation"],
        "updated": p["ts"], "changed": p["changed_at"], "status": p["status"],
        "level": p["level"], "sensor_generation": p["gen"],
        # nie "values": w szablonach HA (Jinja) value_json.x.values to metoda słownika
        "measurements": {k: p[k] for k in FIELDS},
        "units": {"pm": "µg/m³", "pressure": "hPa", "humidity": "%", "temperature": "°C"},
        "suspect": p["suspect"], "flags": p["flags"],
        "flag_labels": [FLAG_LABELS.get(x, x) for x in p["flags"]],
    }
    if dist is not None:
        out["distance_m"] = round(dist * 1000)
    return out


def _features():
    return (collector.cache["sensors"] or {}).get("features", [])


def _nearest(lat, lon, n, include_suspect):
    rows = [(distance_km(lat, lon, f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0]), f)
            for f in _features() if include_suspect or not f["properties"]["suspect"]]
    rows.sort(key=lambda x: x[0])
    return [_sensor(f, d) for d, f in rows[:n]]


@router.get("")
def index(request: Request):
    _limit(request)
    return _json({
        "name": "air-locker-map API", "version": 1, "docs": "/api",
        "source": "Nieoficjalne dane czujników z paczkomatów InPost; stacje GIOŚ jako odniesienie.",
        "endpoints": ["/api/v1/stats", "/api/v1/sensors", "/api/v1/sensors/{name}",
                      "/api/v1/sensors/{name}/history?hours=24", "/api/v1/sensors/{name}/daily?days=30",
                      "/api/v1/nearest?lat=..&lon=..&n=5", "/api/v1/lockers/{code}/nearest?n=5"],
    }, 3600)


@router.get("/stats")
def stats(request: Request):
    _limit(request)
    return _json(collector.compute_stats(), 300)


@router.get("/sensors")
def sensors(request: Request, province: str | None = None, include_suspect: bool = False):
    _limit(request)
    allowed = None
    if province:
        allowed = {r["name"] for r in db.q("SELECT name FROM lockers WHERE province = ?", (province.lower(),))}
    out = [_sensor(f) for f in _features()
           if (include_suspect or not f["properties"]["suspect"]) and (allowed is None or f["properties"]["name"] in allowed)]
    return _json({"count": len(out), "sensors": out})


@router.get("/sensors/{name}")
def sensor(name: str, request: Request):
    _limit(request)
    name = name.upper()
    for f in _features():
        if f["properties"]["name"] == name:
            return _json(_sensor(f))
    raise HTTPException(404, "Brak czujnika o tej nazwie (albo jeszcze nie ma odczytów)")


@router.get("/sensors/{name}/history")
def history(name: str, request: Request, hours: int = Query(24, ge=1, le=24 * 90)):
    _limit(request)
    rows = db.q(f"SELECT ts, {', '.join(FIELDS)} FROM readings WHERE name=? AND ts>=? ORDER BY ts",
                (name.upper(), int(time.time()) - hours * 3600))
    return _json({"name": name.upper(), "hours": hours, "readings": [dict(r) for r in rows]}, 300)


@router.get("/sensors/{name}/daily")
def daily(name: str, request: Request, days: int = Query(30, ge=1, le=3660)):
    _limit(request)
    rows = db.q("SELECT day, n, pm1_avg, pm25_avg, pm25_max, pm10_avg, pm10_max, pressure_sl_avg, humidity_avg "
                "FROM daily WHERE name=? AND day >= date('now', 'localtime', ?) ORDER BY day",
                (name.upper(), f"-{days} days"))
    return _json({"name": name.upper(), "days": [dict(r) for r in rows]}, 900)


@router.get("/nearest")
def nearest(request: Request, lat: float = Query(..., ge=48, le=56), lon: float = Query(..., ge=13, le=25),
            n: int = Query(5, ge=1, le=50), include_suspect: bool = False):
    _limit(request)
    return _json({"lat": lat, "lon": lon, "sensors": _nearest(lat, lon, n, include_suspect)})


@router.get("/lockers/{code}/nearest")
def locker_nearest(code: str, request: Request, n: int = Query(5, ge=1, le=50), include_suspect: bool = False):
    """Najbliższe czujniki do dowolnego paczkomatu (także bez czujnika) — jak skrypt z repo."""
    _limit(request)
    r = db.q1("SELECT name, lat, lon, street, building, city, shipx_level FROM lockers WHERE name=?", (code.upper(),))
    if not r:
        raise HTTPException(404, "Nie ma takiego paczkomatu")
    return _json({
        "locker": {"name": r["name"], "address": f"{r['street'] or ''} {r['building'] or ''}, {r['city']}".strip(),
                   "lat": r["lat"], "lon": r["lon"], "has_sensor": bool(r["shipx_level"])},
        "sensors": _nearest(r["lat"], r["lon"], n, include_suspect),
    })
