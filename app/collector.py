"""Zadania kolektora i harmonogram.

Jedno zadanie naraz (wspólna blokada) — żeby panel admina nie odpalił drugiego
przebiegu w trakcie pierwszego i nie podwoił ruchu do InPostu.
"""
import collections
import json
import logging
import math
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import h3

from . import db, sources
from .quality import WARNING_FLAGS, compute_flags, distance_km, sea_level_pressure

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
    pressure_trend(now)
    national_profile()
    job_flags()
    return ok, nodata + fail


def pressure_trend(now):
    """Zmiana ciśnienia n.p.m. względem przebiegu sprzed ok. 3 h (okno 2,5–3,5 h). Rośnie = wyż nadchodzi."""
    prev = db.q1("SELECT ts FROM readings WHERE ts BETWEEN ? AND ? GROUP BY ts ORDER BY ABS(ts - ?) LIMIT 1",
                 (now - 3.5 * 3600, now - 2.5 * 3600, now - 3 * 3600))
    if not prev:
        db.write("UPDATE latest SET pressure_trend=NULL")
        return
    db.write("""
        UPDATE latest SET pressure_trend = (
            SELECT ROUND(latest.pressure_sl - r.pressure_sl, 1) FROM readings r
            WHERE r.name = latest.name AND r.ts = ? AND r.pressure_sl IS NOT NULL)""", (prev["ts"],))


def job_wind():
    """Wiatr z Open-Meteo na siatce co 0,75° (ok. 130 punktów = tyle „wywołań” w limicie darmowym)."""
    pts = [(round(la, 2), round(lo, 2)) for la in _frange(49.0, 54.9, 0.75) for lo in _frange(14.2, 24.2, 0.75)]
    rows = []
    for i in range(0, len(pts), 100):
        chunk = pts[i:i + 100]
        for (la, lo), w in zip(chunk, sources.wind_grid(chunk)):
            rows.append((la, lo, w["speed"], w["gust"], w["direction"], w["ts"]))
    db.write("DELETE FROM wind")
    db.write_many("INSERT INTO wind(lat, lon, speed, gust, direction, ts) VALUES(?,?,?,?,?,?)", rows)
    log(f"wiatr: {len(rows)} punktów siatki")
    build_cache()
    return len(rows), 0


def _frange(a, b, step):
    x = a
    while x <= b + 1e-9:
        yield x
        x += step


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

    city = {r["name"]: r["city"] for r in db.q("SELECT name, city FROM lockers WHERE shipx_level IS NOT NULL")}
    by_city = collections.defaultdict(list)
    for p in clean:
        if p["pm25"] is not None and city.get(p["name"]):
            by_city[city[p["name"]]].append(p["pm25"])
    cities = [{"city": c, "sensors": len(v), "pm25_median": _median(v)} for c, v in by_city.items() if len(v) >= 5]
    cities.sort(key=lambda x: -(x["pm25_median"] or 0))

    data = {
        "updated": int(time.time()),
        "cities_worst": cities[:10],
        "cities_best": cities[::-1][:10],
        "cities_ranked": len(cities),
        "profile": cache.get("profile"),
        "compare": (cache.get("compare") or {}).get("summary"),
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


def sensor_profile(name):
    """Profil dobowy czujnika: średnie PM2.5/PM10 dla każdej godziny doby, osobno dni robocze i weekend."""
    pm_max = db.setting("pm_max")
    rows = db.q("""SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS h,
                          strftime('%w', ts, 'unixepoch', 'localtime') IN ('0', '6') AS weekend,
                          AVG(pm25) AS pm25, AVG(pm10) AS pm10, COUNT(*) AS n
                   FROM readings WHERE name = ? AND pm25 <= ? GROUP BY h, weekend""", (name, pm_max))
    days = db.q1("SELECT COUNT(DISTINCT date(ts, 'unixepoch', 'localtime')) AS d FROM readings WHERE name = ?", (name,))
    out = {"days": days["d"], "workdays": [None] * 24, "weekend": [None] * 24, "all": [None] * 24}
    acc = collections.defaultdict(lambda: [0.0, 0])
    for r in rows:
        out["weekend" if r["weekend"] else "workdays"][r["h"]] = round(r["pm25"], 1)
        acc[r["h"]][0] += r["pm25"] * r["n"]
        acc[r["h"]][1] += r["n"]
    for h, (tot, n) in acc.items():
        out["all"][h] = round(tot / n, 1)
    return out


def national_profile():
    """Profil dobowy całej Polski (średnia z czujników bez nierealnych odczytów) — liczony co godzinę."""
    pm_max = db.setting("pm_max")
    rows = db.q("""SELECT CAST(strftime('%H', ts, 'unixepoch', 'localtime') AS INTEGER) AS h,
                          ROUND(AVG(pm25), 1) AS pm25, COUNT(DISTINCT ts) AS frames
                   FROM readings WHERE pm25 <= ? GROUP BY h ORDER BY h""", (pm_max,))
    prof = [None] * 24
    for r in rows:
        prof[r["h"]] = r["pm25"]
    days = db.q1("SELECT COUNT(DISTINCT date(ts, 'unixepoch', 'localtime')) AS d FROM readings")
    cache["profile"] = {"pm25": prof, "days": days["d"]}


def compare_gios(radius_km=3.0):
    """Paczkomaty kontra stacje GIOŚ: pary stacja ↔ czujniki do `radius_km`, dopasowanie godzinowe.

    GIOŚ podaje średnie godzinowe; odczyt paczkomatu przypisujemy do godziny, w której wypadł.
    Wynik: obciążenie (paczkomat − GIOŚ), stosunek, błąd bezwzględny i korelacja — osobno dla
    starszych i nowszych czujników."""
    pm_max = db.setting("pm_max")
    stations = db.q("SELECT station_id, name, city, lat, lon FROM gios")
    suspect = {f["properties"]["name"] for f in (cache["sensors"] or {}).get("features", []) if f["properties"]["suspect"]}
    sensors = [r for r in db.q("SELECT l.name, k.lat, k.lon, l.gen FROM latest l JOIN lockers k USING(name) "
                               "WHERE l.gen IS NOT NULL") if r["name"] not in suspect]
    gios_hours = collections.defaultdict(dict)
    for r in db.q("SELECT station_id, ts, pm25 FROM gios_readings WHERE pm25 IS NOT NULL"):
        gios_hours[r["station_id"]][r["ts"][:13]] = r["pm25"]     # klucz: "YYYY-MM-DD HH"
    pairs, per_station = collections.defaultdict(list), []
    for st in stations:
        near = [x for x in sensors if distance_km(st["lat"], st["lon"], x["lat"], x["lon"]) <= radius_km]
        hours = gios_hours.get(st["station_id"])
        if not near or not hours:
            continue
        st_pairs = []
        for x in near:
            for r in db.q("SELECT strftime('%Y-%m-%d %H', ts, 'unixepoch', 'localtime') AS hh, pm25 FROM readings "
                          "WHERE name = ? AND pm25 IS NOT NULL AND pm25 <= ?", (x["name"], pm_max)):
                g = hours.get(r["hh"])
                if g is not None:
                    pairs[x["gen"]].append((r["pm25"], g))
                    st_pairs.append((r["pm25"], g))
        if st_pairs:
            per_station.append({"station_id": st["station_id"], "name": st["name"], "city": st["city"],
                                "sensors": len(near), **_pair_stats(st_pairs)})
    summary = {gen: _pair_stats(p) for gen, p in pairs.items() if p}
    summary["all"] = _pair_stats([x for p in pairs.values() for x in p]) if pairs else None
    summary["stations"] = len(per_station)
    summary["radius_km"] = radius_km
    cache["compare"] = {"summary": summary, "stations": sorted(per_station, key=lambda s: -s["n"])}


def _pair_stats(pairs):
    a = [p for p, _ in pairs]
    g = [q for _, q in pairs]
    n = len(pairs)
    ma, mg = sum(a) / n, sum(g) / n
    cov = sum((x - ma) * (y - mg) for x, y in pairs)
    va, vg = sum((x - ma) ** 2 for x in a), sum((y - mg) ** 2 for y in g)
    r = cov / math.sqrt(va * vg) if va > 0 and vg > 0 else None
    return {"n": n, "locker_mean": round(ma, 1), "gios_mean": round(mg, 1), "bias": round(ma - mg, 1),
            "ratio": round(ma / mg, 2) if mg else None,
            "mae": round(sum(abs(x - y) for x, y in pairs) / n, 1), "r": round(r, 2) if r is not None else None}


def network_health():
    """Zdrowie sieci dla panelu admina: per województwo — czujniki, działające, flagi, braki danych i ID."""
    rows = db.q("""SELECT k.province AS province, COUNT(*) AS sensors,
                          SUM(k.point_id IS NULL) AS no_id,
                          SUM(l.status = 'ok') AS ok,
                          SUM(l.status IN ('no_data', 'error')) AS no_data,
                          SUM(l.flags LIKE '%stuck%') AS stuck, SUM(l.flags LIKE '%dead%') AS dead,
                          SUM(l.flags LIKE '%absurd%') AS absurd, SUM(l.flags LIKE '%outlier%') AS outlier,
                          SUM(l.flags LIKE '%stale%') AS stale, SUM(l.flags LIKE '%wet%') AS wet
                   FROM lockers k LEFT JOIN latest l USING(name)
                   WHERE k.shipx_level IS NOT NULL GROUP BY k.province""")
    out = []
    for r in rows:
        d = dict(r)
        broken = (d["stuck"] or 0) + (d["dead"] or 0) + (d["absurd"] or 0) + (d["outlier"] or 0) + (d["stale"] or 0)
        d["broken"] = broken
        d["broken_pct"] = round(100 * broken / d["sensors"], 1) if d["sensors"] else 0
        out.append(d)
    return sorted(out, key=lambda x: -x["broken_pct"])


ABROAD = ("FR", "IT", "ES", "PT", "GB", "BE", "NL", "LU")


def job_abroad():
    """Raz w miesiącu: czy za granicą pojawiły się paczkomaty z czujnikiem (docs/zagranica.md: na 10.2026 — zero)."""
    found = {}
    for cc in ABROAD:
        n = 0
        for page in (1, 2, 3):
            try:
                d = json.loads(sources.http(f"https://api-global-points.easypack24.net/v1/points?country={cc}"
                                            f"&type=parcel_locker&per_page=500&page={page}&fields=name,air_index_level"))
            except Exception as e:  # noqa: BLE001
                log(f"zagranica {cc}: {e}")
                break
            n += sum(1 for i in d.get("items", []) if i.get("air_index_level"))
            if page >= d.get("total_pages", 0):
                break
            time.sleep(0.6)
        found[cc] = n
    total = sum(found.values())
    log(f"zagranica: czujniki w próbce {found}" + (" — POJAWIŁY SIĘ!" if total else ""))
    db.write("INSERT INTO settings(key, value) VALUES('abroad_last', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
             (json.dumps({"ts": int(time.time()), "found": found}),))
    return total, 0


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
        if ts:
            db.write("INSERT OR REPLACE INTO gios_readings(station_id, ts, pm25, pm10) VALUES(?,?,?,?)",
                     (st["station_id"], ts, vals.get("PM2.5"), vals.get("PM10")))
        db.write("INSERT INTO gios(station_id, name, city, lat, lon, pm25, pm10, ts, updated) VALUES(?,?,?,?,?,?,?,?,?) "
                 "ON CONFLICT(station_id) DO UPDATE SET pm25=excluded.pm25, pm10=excluded.pm10, ts=excluded.ts, "
                 "updated=excluded.updated",
                 (st["station_id"], st["name"], st["city"], st["lat"], st["lon"], vals.get("PM2.5"), vals.get("PM10"),
                  ts, now))
        ok += 1
        time.sleep(0.2)
    db.write("DELETE FROM gios_readings WHERE ts < datetime('now', 'localtime', ?)", (f"-{db.setting('history_days')} days",))
    log(f"GIOŚ: stacji z PM {ok}")
    try:
        compare_gios()
    except Exception as e:  # noqa: BLE001
        log(f"porównanie z GIOŚ: {e}")
    build_cache()
    return ok, 0


JOBS = {
    "lockers": ("Lista paczkomatów (ShipX)", job_lockers),
    "resolve": ("Ustalanie ID punktów", job_resolve),
    "elevation": ("Wysokości terenu", job_elevation),
    "collect": ("Odczyty czujników", job_collect),
    "flags": ("Przeliczenie flag", job_flags),
    "gios": ("Stacje GIOŚ", job_gios),
    "wind": ("Wiatr (Open-Meteo)", job_wind),
    "abroad": ("Czujniki za granicą (próbka)", job_abroad),
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
               l.pressure_sl, l.pressure_trend, l.humidity, l.temperature, l.level, l.gen, l.changed_at, l.flags
        FROM latest l JOIN lockers k USING(name) WHERE l.pm25 IS NOT NULL OR l.pressure IS NOT NULL""")
    feats = []
    for r in rows:
        flags = [f for f in (r["flags"] or "").split(",") if f]
        if r["hidden"]:
            flags.append("hidden")
        props = {k: r[k] for k in ("name", "city", "description", "page_url", "point_id", "ts", "status", "pm1",
                                   "pm25", "pm4", "pm10", "pressure", "pressure_sl", "pressure_trend", "humidity", "temperature",
                                   "level", "gen", "changed_at", "elevation")}
        props["address"] = " ".join(x for x in (r["street"], r["building"]) if x) + f", {r['post_code']} {r['city']}"
        props["flags"] = flags
        props["suspect"] = any(f not in WARNING_FLAGS for f in flags)
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
                      "properties": props})
    cache["sensors"] = {"type": "FeatureCollection", "features": feats}
    cache["hex"] = {}
    cache["surface"] = {}
    g = db.q("SELECT * FROM gios WHERE pm25 IS NOT NULL OR pm10 IS NOT NULL")
    cache["gios"] = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
         "properties": {"station_id": r["station_id"], "name": r["name"], "city": r["city"], "pm25": r["pm25"],
                        "pm10": r["pm10"], "ts": r["ts"]}}
        for r in g]}
    cache["wind"] = {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [r["lon"], r["lat"]]},
         "properties": {"speed": r["speed"], "gust": r["gust"], "direction": r["direction"], "ts": r["ts"]}}
        for r in db.q("SELECT * FROM wind")]}
    cache["built"] = int(time.time())
    stats_cache["at"] = 0


METRICS = ("pm1", "pm25", "pm10", "pressure_sl", "pressure_trend", "humidity", "temperature")


HISTORY_METRICS = ("pm1", "pm25", "pm10", "pressure_sl", "humidity", "temperature")  # są w tabeli readings


def metric_points(metric, ts=None, include_suspect=False):
    """(lat, lon, wartość) dla wielkości — z bieżącego stanu albo z przebiegu o czasie `ts` (suwak czasu).
    Dla przeszłości podejrzane odfiltrowujemy wg dzisiejszych flag (historycznych nie trzymamy)."""
    feats = (cache["sensors"] or {}).get("features", [])
    if ts is None:
        return [(f["geometry"]["coordinates"][1], f["geometry"]["coordinates"][0], f["properties"][metric])
                for f in feats if f["properties"].get(metric) is not None
                and (include_suspect or not f["properties"]["suspect"])]
    if metric not in HISTORY_METRICS:
        return []
    skip = set() if include_suspect else {f["properties"]["name"] for f in feats if f["properties"]["suspect"]}
    rows = db.q(f"SELECT r.name, k.lat, k.lon, r.{metric} AS v FROM readings r JOIN lockers k USING(name) "
                f"WHERE r.ts = ? AND r.{metric} IS NOT NULL", (ts,))
    return [(r["lat"], r["lon"], r["v"]) for r in rows if r["name"] not in skip]


_frames_memo = {}


def frames(hours=24):
    """Pełne przebiegi z ostatnich godzin (pomijamy przerwane, z małą liczbą odczytów). Pamięć 60 s."""
    memo = _frames_memo.get(hours)
    if memo and time.time() - memo[0] < 60:
        return memo[1]
    rows = db.q("SELECT ts, COUNT(*) AS n FROM readings WHERE ts >= ? GROUP BY ts HAVING n > 500 ORDER BY ts",
                (int(time.time()) - hours * 3600 - 1800,))
    out = [{"ts": r["ts"], "sensors": r["n"]} for r in rows]
    _frames_memo[hours] = (time.time(), out)
    return out


def frame_values(ts, metric):
    key = ("frame", ts, metric)
    if key not in cache.setdefault("frames", {}):
        if len(cache["frames"]) > 200:
            cache["frames"].clear()
        rows = db.q(f"SELECT name, {metric} AS v FROM readings WHERE ts = ? AND {metric} IS NOT NULL", (ts,))
        cache["frames"][key] = {r["name"]: r["v"] for r in rows}
    return cache["frames"][key]


def hex_layer(res, metric, include_suspect, ts=None):
    key = (res, metric, include_suspect, ts)
    if key in cache["hex"]:
        return cache["hex"][key]
    cells = collections.defaultdict(list)
    for lat, lon, v in metric_points(metric, ts, include_suspect):
        cells[h3.latlng_to_cell(lat, lon, res)].append(v)
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
    if len(cache["hex"]) > 100:
        cache["hex"].clear()
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
        "wind": _last_finished("wind") + 3600,
        "abroad": _last_finished("abroad") + 30 * 86400,
    }


def scheduler_loop():
    time.sleep(5)
    for fn in (national_profile, compare_gios):
        try:
            fn()
        except Exception as e:  # noqa: BLE001
            log(f"start: {fn.__name__}: {e}")
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
                elif now >= nxt["wind"]:
                    run_job("wind")
                elif now >= nxt["abroad"]:
                    run_job("abroad")
        except Exception as e:  # noqa: BLE001
            log(f"harmonogram: {e}")
        time.sleep(20)


def start_scheduler():
    threading.Thread(target=scheduler_loop, daemon=True).start()
