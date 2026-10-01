"""Zadania kolektora i harmonogram.

Jedno zadanie naraz (wspólna blokada) — żeby panel admina nie odpalił drugiego
przebiegu w trakcie pierwszego i nie podwoił ruchu do InPostu.
"""
import collections
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import h3

from . import db, sources
from .quality import compute_flags, sea_level_pressure

DATA_DIR = os.path.dirname(db.DB_PATH)
SITEMAP_CACHE = os.path.join(DATA_DIR, "sitemap-urls.txt")

log_lines = collections.deque(maxlen=500)
_logger = logging.getLogger("airmap")


def log(msg):
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    log_lines.append(line)
    _logger.info(msg)


class State:
    job = None          # nazwa trwającego zadania
    progress = (0, 0)   # (zrobione, wszystkie)
    started = None
    cancel = False


state = State()
_job_lock = threading.Lock()

# Gotowe odpowiedzi dla strony, przeliczane po każdym przebiegu.
cache = {"sensors": None, "hex": {}, "gios": None, "built": 0}


# ---------------------------------------------------------------- zadania

def job_lockers():
    """Lista paczkomatów z ShipX -> tabela lockers."""
    items = sources.shipx_all_lockers(log)
    now = int(time.time())
    rows = []
    for i in items:
        a = i.get("address_details") or {}
        loc = i.get("location") or {}
        rows.append((i["name"], loc.get("latitude"), loc.get("longitude"), a.get("city"), a.get("street"),
                     a.get("building_number"), a.get("post_code"), a.get("province"),
                     i.get("location_description"), i.get("air_index_level"), now, now))
    db.write_many("""
        INSERT INTO lockers(name, lat, lon, city, street, building, post_code, province, description,
                            shipx_level, first_seen, last_seen)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(name) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, city=excluded.city,
            street=excluded.street, building=excluded.building, post_code=excluded.post_code,
            province=excluded.province, description=excluded.description,
            shipx_level=excluded.shipx_level, last_seen=excluded.last_seen""", rows)
    sensors = sum(1 for i in items if i.get("air_index_level"))
    log(f"lista paczkomatów: {len(items)}, z czujnikiem {sensors}")
    return len(items), 0


def _sitemap_index(max_age_days):
    fresh = os.path.exists(SITEMAP_CACHE) and time.time() - os.path.getmtime(SITEMAP_CACHE) < max_age_days * 86400
    if not fresh:
        urls = sources.sitemap_urls(log)
        if urls:
            with open(SITEMAP_CACHE, "w") as f:
                f.write("\n".join(urls))
    with open(SITEMAP_CACHE) as f:
        return sources.page_index(f.read().split())


def job_resolve():
    """ID punktów dla czujników, które go jeszcze nie mają."""
    s = db.get_settings()
    retry_before = int(time.time()) - s["id_retry_days"] * 86400
    todo = [r["name"] for r in db.q(
        "SELECT name FROM lockers WHERE shipx_level IS NOT NULL AND point_id IS NULL "
        "AND (id_checked_at IS NULL OR id_checked_at < ?)", (retry_before,))]
    if not todo:
        log("ID: nic do zrobienia")
        return 0, 0
    idx = _sitemap_index(s["lockers_refresh_days"])
    log(f"ID: do ustalenia {len(todo)}")
    state.progress = (0, len(todo))
    ok = fail = 0

    def one(name):
        url = idx.get(name.lower())
        if not url:
            return name, None, None, "no_page"
        pid, status = sources.resolve_point_id(url)
        return name, url, pid, status

    with ThreadPoolExecutor(s["workers"]) as ex:
        for n, (name, url, pid, status) in enumerate(ex.map(one, todo), 1):
            if state.cancel:
                break
            db.write("UPDATE lockers SET point_id=?, page_url=?, id_status=?, id_checked_at=? WHERE name=?",
                     (pid, url, status, int(time.time()), name))
            ok += pid is not None
            fail += pid is None
            state.progress = (n, len(todo))
            if n % 200 == 0:
                log(f"ID: {n}/{len(todo)} (ok {ok}, bez ID {fail})")
    log(f"ID: koniec — ok {ok}, bez ID {fail}")
    return ok, fail


def job_elevation():
    todo = db.q("SELECT name, lat, lon FROM lockers WHERE shipx_level IS NOT NULL AND elevation IS NULL "
                "AND lat IS NOT NULL")
    ok = 0
    state.progress = (0, len(todo))
    for i in range(0, len(todo), 100):
        if state.cancel:
            break
        chunk = todo[i:i + 100]
        # Open-Meteo liczy limit od współrzędnych, nie zapytań — przy 429 czekamy minutę i ponawiamy
        for attempt in range(4):
            try:
                elev = sources.elevations([(r["lat"], r["lon"]) for r in chunk])
                break
            except Exception as e:  # noqa: BLE001
                log(f"wysokości: {e}, ponawiam za minutę")
                time.sleep(65)
        else:
            continue
        db.write_many("UPDATE lockers SET elevation=? WHERE name=?",
                      [(e, r["name"]) for e, r in zip(elev, chunk)])
        ok += len(chunk)
        state.progress = (ok, len(todo))
        time.sleep(3)
    log(f"wysokości: uzupełniono {ok}")
    if ok:
        recompute_pressure()
    return ok, 0


def recompute_pressure():
    """Po uzupełnieniu wysokości przelicza ciśnienie n.p.m. w ostatnich odczytach."""
    rows = db.q("SELECT l.name, l.pressure, l.gen, k.elevation FROM latest l JOIN lockers k USING(name) "
                "WHERE l.pressure IS NOT NULL")
    db.write_many("UPDATE latest SET pressure_sl=? WHERE name=?",
                  [(sea_level_pressure(r["pressure"], r["elevation"], r["gen"]), r["name"]) for r in rows])
    build_cache()


def job_collect():
    """Odczyty wszystkich czujników z ustalonym ID."""
    s = db.get_settings()
    lockers = db.q("SELECT name, point_id, elevation FROM lockers "
                   "WHERE shipx_level IS NOT NULL AND point_id IS NOT NULL")
    prev = {r["name"]: r for r in db.q("SELECT name, signature, changed_at FROM latest")}
    state.progress = (0, len(lockers))
    log(f"odczyty: start, {len(lockers)} czujników")
    ok = nodata = fail = 0
    now = int(time.time())
    hist, latest = [], []

    def one(r):
        try:
            return r, sources.read_sensors(r["point_id"], r["name"]), None
        except Exception as e:  # noqa: BLE001
            return r, None, str(e)

    with ThreadPoolExecutor(s["workers"]) as ex:
        for n, (r, v, err) in enumerate(ex.map(one, lockers), 1):
            if state.cancel:
                break
            state.progress = (n, len(lockers))
            name = r["name"]
            if err or v is None:
                status = "error" if err else "no_data"
                fail += bool(err)
                nodata += not err
                latest.append((name, now, status, *([None] * 9), None, None, None, None))
                continue
            ok += 1
            gen = "new" if "pm4" in v else "old"
            psl = sea_level_pressure(v.get("pressure"), r["elevation"], gen)
            sig = json.dumps([v.get(k) for k in ("pm1", "pm25", "pm4", "pm10", "pressure", "humidity", "temperature")])
            p = prev.get(name)
            changed_at = p["changed_at"] if p and p["signature"] == sig and p["changed_at"] else now
            vals = (v.get("pm1"), v.get("pm25"), v.get("pm4"), v.get("pm10"), v.get("pressure"), psl,
                    v.get("humidity"), v.get("temperature"), v.get("level"))
            hist.append((name, now, *vals))
            latest.append((name, now, "ok", *vals, gen, sig, changed_at))
            if n % 500 == 0:
                log(f"odczyty: {n}/{len(lockers)}")

    db.write_many("INSERT INTO readings(name, ts, pm1, pm25, pm4, pm10, pressure, pressure_sl, humidity, "
                  "temperature, level) VALUES(?,?,?,?,?,?,?,?,?,?,?)", hist)
    # przy błędzie/braku danych zostawiamy ostatni dobry odczyt, zmieniamy tylko status
    good = [row for row in latest if row[2] == "ok"]
    bad = [(row[2], row[1], row[0]) for row in latest if row[2] != "ok"]
    db.write_many("""
        INSERT INTO latest(name, ts, status, pm1, pm25, pm4, pm10, pressure, pressure_sl, humidity, temperature,
                           level, gen, signature, changed_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(name) DO UPDATE SET ts=excluded.ts, status=excluded.status, pm1=excluded.pm1,
            pm25=excluded.pm25, pm4=excluded.pm4, pm10=excluded.pm10, pressure=excluded.pressure,
            pressure_sl=excluded.pressure_sl, humidity=excluded.humidity, temperature=excluded.temperature,
            level=excluded.level, gen=excluded.gen, signature=excluded.signature, changed_at=excluded.changed_at""",
                  good)
    db.write_many("INSERT INTO latest(name, ts, status) VALUES(?, ?, ?) ON CONFLICT(name) DO UPDATE SET status=?",
                  [(n, t, st, st) for st, t, n in bad])
    cutoff = now - s["history_days"] * 86400
    db.write("DELETE FROM readings WHERE ts < ?", (cutoff,))
    log(f"odczyty: koniec — ok {ok}, brak danych {nodata}, błędy {fail}")
    aggregate_daily(now)
    job_flags()
    return ok, nodata + fail


def aggregate_daily(now=None):
    """Przelicza dzienne agregaty za wczoraj i dziś (dzień lokalny). Do zbiorczych średnich
    nie wchodzą odczyty nierealne (powyżej pm_max) — zepsuty czujnik z 2000 µg/m³ zepsułby średnią."""
    now = int(now or time.time())
    pm_max = db.setting("pm_max")
    db.write("""
        INSERT OR REPLACE INTO daily(name, day, n, pm1_avg, pm25_avg, pm25_max, pm10_avg, pm10_max,
                                     pressure_sl_avg, humidity_avg)
        SELECT name, date(ts, 'unixepoch', 'localtime') AS day, COUNT(*),
               ROUND(AVG(pm1), 2), ROUND(AVG(pm25), 2), MAX(pm25), ROUND(AVG(pm10), 2), MAX(pm10),
               ROUND(AVG(pressure_sl), 1), ROUND(AVG(humidity), 1)
        FROM readings
        WHERE ts >= ? AND (pm25 IS NULL OR pm25 <= ?) AND (pm10 IS NULL OR pm10 <= ?)
        GROUP BY name, day""", (now - 2 * 86400, pm_max, pm_max))
    stats_cache["at"] = 0


stats_cache = {"at": 0, "data": None}


def _median(vals):
    vals = sorted(vals)
    if not vals:
        return None
    m = len(vals) // 2
    return round(vals[m] if len(vals) % 2 else (vals[m - 1] + vals[m]) / 2, 1)


def _quantile(vals, q):
    vals = sorted(vals)
    if not vals:
        return None
    return round(vals[min(len(vals) - 1, int(q * len(vals)))], 1)


def compute_stats():
    """Statystyki dla strony i API. Liczone z czujników bez flag (podejrzane nie psują median)."""
    if stats_cache["data"] and time.time() - stats_cache["at"] < 300:
        return stats_cache["data"]
    s = db.get_settings()
    feats = (cache["sensors"] or {}).get("features", [])
    clean = [f["properties"] for f in feats if not f["properties"]["suspect"]]
    pm25 = [p["pm25"] for p in clean if p["pm25"] is not None]
    t = s["pm25_thresholds"]
    classes = [0] * (len(t) + 1)
    for v in pm25:
        i = 0
        while i < len(t) and v > t[i]:
            i += 1
        classes[i] += 1

    prov = collections.defaultdict(list)
    for r in db.q("SELECT l.name, k.province FROM latest l JOIN lockers k USING(name)"):
        prov[r["name"]] = r["province"]
    by_prov = collections.defaultdict(list)
    for p in clean:
        if p["pm25"] is not None:
            by_prov[prov.get(p["name"]) or "?"].append(p["pm25"])
    provinces = sorted(({"province": k, "sensors": len(v), "pm25_median": _median(v)} for k, v in by_prov.items()),
                       key=lambda x: -(x["pm25_median"] or 0))

    def brief(p):
        return {k: p[k] for k in ("name", "address", "pm25", "pm10")}

    ranked = sorted((p for p in clean if p["pm25"] is not None), key=lambda p: -p["pm25"])
    counts = dict(db.q1("SELECT COUNT(*) AS lockers, SUM(shipx_level IS NOT NULL) AS sensors_shipx FROM lockers"))
    last = db.q1("SELECT finished FROM runs WHERE job='collect' AND finished IS NOT NULL AND note='' "
                 "ORDER BY id DESC LIMIT 1")
    hist = dict(db.q1("SELECT COUNT(*) AS readings, MIN(ts) AS since FROM readings"))
    days = db.q1("SELECT COUNT(DISTINCT day) AS days, MIN(day) AS first FROM daily")
    trend = []
    for r in db.q("SELECT day, pm25_avg FROM daily WHERE day >= date('now', 'localtime', '-13 days') "
                  "AND pm25_avg IS NOT NULL ORDER BY day"):
        if not trend or trend[-1]["day"] != r["day"]:
            trend.append({"day": r["day"], "vals": []})
        trend[-1]["vals"].append(r["pm25_avg"])
    trend = [{"day": d["day"], "pm25_median": _median(d["vals"]), "sensors": len(d["vals"])} for d in trend]

    data = {
        "updated": int(time.time()),
        "last_collect": last["finished"] if last else None,
        "collect_interval_min": s["collect_interval_min"],
        "lockers_total": counts["lockers"],
        "sensors_listed": counts["sensors_shipx"],
        "sensors_reporting": len(feats),
        "sensors_suspect": len(feats) - len(clean),
        "gios_stations": len((cache["gios"] or {}).get("features", [])),
        "pm25": {"median": _median(pm25), "p90": _quantile(pm25, 0.9),
                 "min": min(pm25) if pm25 else None, "max": max(pm25) if pm25 else None},
        "pressure_sl_median": _median([p["pressure_sl"] for p in clean if p["pressure_sl"] is not None]),
        "humidity_median": _median([p["humidity"] for p in clean if p["humidity"] is not None]),
        "pm25_classes": classes,
        "pm25_thresholds": t,
        "provinces": provinces,
        "worst": [brief(p) for p in ranked[:5]],
        "best": [brief(p) for p in ranked[::-1][:5]],
        "history": {"readings": hist["readings"], "since": hist["since"], "raw_days": s["history_days"],
                    "daily_days": days["days"], "daily_since": days["first"]},
        "trend": trend,
    }
    stats_cache.update(at=time.time(), data=data)
    return data


def job_flags():
    s = db.get_settings()
    rows = [dict(r) for r in db.q("SELECT l.name, k.lat, k.lon, l.status, l.pm1, l.pm25, l.pm10, l.humidity, "
                                  "l.changed_at FROM latest l JOIN lockers k USING(name)")]
    flags = compute_flags(rows, s)
    db.write("UPDATE latest SET flags=''")
    db.write_many("UPDATE latest SET flags=? WHERE name=?", [(f, n) for n, f in flags.items()])
    log(f"flagi: podejrzanych {len(flags)}")
    build_cache()
    return len(rows), len(flags)


def job_gios():
    """Ostatnie godzinowe PM2.5/PM10 ze stacji GIOŚ (ok. 290 stacji, ~600 zapytań)."""
    stations = sources.gios_stations()
    now = int(time.time())
    ok = 0
    state.progress = (0, len(stations))
    for n, st in enumerate(stations, 1):
        if state.cancel:
            break
        state.progress = (n, len(stations))
        try:
            sens = sources.gios_sensors(st["station_id"])
            vals = {}
            ts = None
            for code, sid in sens.items():
                v, t = sources.gios_latest(sid)
                vals[code] = v
                ts = ts or t
                time.sleep(0.2)
        except Exception as e:  # noqa: BLE001
            log(f"GIOŚ {st['name']}: {e}")
            continue
        if not vals:
            continue
        db.write("INSERT INTO gios(station_id, name, city, lat, lon, pm25, pm10, ts, updated) VALUES(?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(station_id) DO UPDATE SET pm25=excluded.pm25, pm10=excluded.pm10, ts=excluded.ts, "
                 "updated=excluded.updated",
                 (st["station_id"], st["name"], st["city"], st["lat"], st["lon"], vals.get("PM2.5"), vals.get("PM10"),
                  ts, now))
        ok += 1
        time.sleep(0.2)
    log(f"GIOŚ: stacji z PM {ok}")
    build_cache()
    return ok, 0


JOBS = {
    "lockers": ("Lista paczkomatów (ShipX)", job_lockers),
    "resolve": ("Ustalanie ID punktów", job_resolve),
    "elevation": ("Wysokości terenu", job_elevation),
    "collect": ("Odczyty czujników", job_collect),
    "flags": ("Przeliczenie flag", job_flags),
    "gios": ("Stacje GIOŚ", job_gios),
}


def run_job(key):
    """Uruchamia zadanie, jeśli żadne inne nie trwa. Zwraca False, gdy zajęte."""
    if not _job_lock.acquire(blocking=False):
        return False
    try:
        state.job, state.progress, state.started, state.cancel = key, (0, 0), time.time(), False
        sources.inpost_limiter.rate = db.setting("request_rate")
        run_id = db.write("INSERT INTO runs(job, started) VALUES(?, ?)", (key, int(time.time()))).lastrowid
        ok = fail = 0
        note = ""
        try:
            ok, fail = JOBS[key][1]()
            if state.cancel:
                note = "przerwane"
        except Exception as e:  # noqa: BLE001
            note = f"błąd: {e}"
            log(f"{key}: {note}")
        db.write("UPDATE runs SET finished=?, ok=?, fail=?, note=? WHERE id=?",
                 (int(time.time()), ok, fail, note, run_id))
        return True
    finally:
        state.job = None
        _job_lock.release()


def start_job(key):
    if state.job:
        return False
    threading.Thread(target=run_job, args=(key,), daemon=True).start()
    return True


# ---------------------------------------------------------------- dane dla strony

def build_cache():
    rows = db.q("""
        SELECT k.name, k.lat, k.lon, k.city, k.street, k.building, k.post_code, k.description, k.page_url,
               k.point_id, k.elevation, k.hidden, l.ts, l.status, l.pm1, l.pm25, l.pm4, l.pm10, l.pressure,
               l.pressure_sl, l.humidity, l.temperature, l.level, l.gen, l.changed_at, l.flags
        FROM latest l JOIN lockers k USING(name) WHERE l.pm25 IS NOT NULL OR l.pressure IS NOT NULL""")
    feats = []
    for r in rows:
        flags = [f for f in (r["flags"] or "").split(",") if f]
        if r["hidden"]:
            flags.append("hidden")
        props = {k: r[k] for k in ("name", "city", "description", "page_url", "point_id", "ts", "status", "pm1",
                                   "pm25", "pm4", "pm10", "pressure", "pressure_sl", "humidity", "temperature",
                                   "level", "gen", "changed_at", "elevation")}
        props["address"] = " ".join(x for x in (r["street"], r["building"]) if x) + f", {r['post_code']} {r['city']}"
        props["flags"] = flags
        props["suspect"] = bool(flags)
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                      "properties": props})
    cache["sensors"] = {"type": "FeatureCollection", "features": feats}
    cache["hex"] = {}
    g = db.q("SELECT * FROM gios WHERE pm25 IS NOT NULL OR pm10 IS NOT NULL")
    cache["gios"] = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
         "properties": {"name": r["name"], "city": r["city"], "pm25": r["pm25"], "pm10": r["pm10"], "ts": r["ts"]}}
        for r in g]}
    cache["built"] = int(time.time())
    stats_cache["at"] = 0


METRICS = ("pm1", "pm25", "pm10", "pressure_sl", "humidity", "temperature")


def hex_layer(res, metric, include_suspect):
    key = (res, metric, include_suspect)
    if key in cache["hex"]:
        return cache["hex"][key]
    cells = collections.defaultdict(list)
    for f in (cache["sensors"] or {}).get("features", []):
        p = f["properties"]
        if p.get(metric) is None or (p["suspect"] and not include_suspect):
            continue
        lon, lat = f["geometry"]["coordinates"]
        cells[h3.latlng_to_cell(lat, lon, res)].append(p[metric])
    feats = []
    for cell, vals in cells.items():
        vals.sort()
        mid = len(vals) // 2
        med = vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2
        ring = [[lon, lat] for lat, lon in h3.cell_to_boundary(cell)]
        ring.append(ring[0])
        feats.append({"type": "Feature", "geometry": {"type": "Polygon", "coordinates": [ring]},
                      "properties": {"value": round(med, 1), "count": len(vals)}})
    out = {"type": "FeatureCollection", "features": feats}
    cache["hex"][key] = out
    return out


# ---------------------------------------------------------------- harmonogram

def _last_finished(job):
    r = db.q1("SELECT MAX(started) AS t FROM runs WHERE job=? AND finished IS NOT NULL AND (note IS NULL OR note='')",
              (job,))
    return r["t"] or 0


def next_runs():
    s = db.get_settings()
    return {
        "lockers": _last_finished("lockers") + s["lockers_refresh_days"] * 86400,
        "collect": _last_finished("collect") + s["collect_interval_min"] * 60,
        "gios": _last_finished("gios") + 3600 if s["gios_layer"] else None,
    }


def scheduler_loop():
    time.sleep(5)
    build_cache()
    while True:
        try:
            if db.setting("collector_enabled") and not state.job:
                now = time.time()
                nxt = next_runs()
                if now >= nxt["lockers"]:
                    for k in ("lockers", "resolve", "elevation"):
                        run_job(k)
                elif now >= nxt["collect"]:
                    # nowe czujniki (np. po ręcznym odświeżeniu listy) dostają ID i wysokość przed odczytem
                    if db.q1("SELECT 1 FROM lockers WHERE shipx_level IS NOT NULL AND point_id IS NULL "
                             "AND id_checked_at IS NULL LIMIT 1"):
                        run_job("resolve")
                    if db.q1("SELECT 1 FROM lockers WHERE shipx_level IS NOT NULL AND elevation IS NULL LIMIT 1"):
                        run_job("elevation")
                    run_job("collect")
                elif nxt["gios"] and now >= nxt["gios"]:
                    run_job("gios")
        except Exception as e:  # noqa: BLE001
            log(f"harmonogram: {e}")
        time.sleep(20)


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()
