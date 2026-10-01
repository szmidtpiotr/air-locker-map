"""Obliczenia na odczytach: ciśnienie do poziomu morza, klasy indeksu, flagi podejrzanych czujników."""
import math
import statistics
import time
from collections import defaultdict


def sea_level_pressure(p, elevation, gen):
    """Nowsze czujniki (z PM4) podają ciśnienie rzeczywiste — przeliczamy wzorem barometrycznym
    dla atmosfery standardowej (15 °C; temperatura z obudowy się nie nadaje). Starsze podają już
    ciśnienie zredukowane do poziomu morza."""
    if p is None:
        return None
    if gen != "new":
        return round(p, 1)
    if elevation is None:  # bez wysokości nie da się przeliczyć — lepiej brak niż zła wartość
        return None
    return round(p * (1 - 0.0065 * elevation / 288.15) ** -5.255, 1)


def distance_km(a_lat, a_lon, b_lat, b_lon):
    dlat = math.radians(b_lat - a_lat)
    dlon = math.radians(b_lon - a_lon)
    h = math.sin(dlat / 2) ** 2 + math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) * math.sin(dlon / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def compute_flags(rows, s, now=None):
    """rows: lista słowników z name, lat, lon, pm1, pm25, pm10, humidity, changed_at, status.
    Zwraca {name: 'flaga1,flaga2'}."""
    now = now or time.time()
    flags = defaultdict(list)
    ok = [r for r in rows if r["status"] == "ok"]
    for r in ok:
        pms = [r["pm1"], r["pm25"], r["pm10"]]
        if all(v is not None for v in pms) and len(set(pms)) == 1:
            flags[r["name"]].append("stuck")
        if any(v is not None and v > s["pm_max"] for v in pms):
            flags[r["name"]].append("absurd")
        # na zewnątrz PM10 praktycznie nigdy nie spada do zera — taki odczyt to martwy czujnik
        if r["pm10"] is not None and r["pm10"] < s["pm10_min"]:
            flags[r["name"]].append("dead")
        if r["humidity"] is not None and r["humidity"] >= s["humidity_max"]:
            flags[r["name"]].append("wet")
        if r["changed_at"] and now - r["changed_at"] > s["stale_hours"] * 3600:
            flags[r["name"]].append("stale")

    # siatka o boku = promień (w stopniach szerokości); sąsiadów szukamy w sąsiednich komórkach
    radius = s["outlier_radius_km"]
    cell = max(radius / 111.0, 0.05)
    grid = defaultdict(list)
    clean = [r for r in ok if r["pm25"] is not None and not flags.get(r["name"])]
    for r in clean:
        grid[(int(r["lat"] // cell), int(r["lon"] // cell))].append(r)
    for r in clean:
        gy, gx = int(r["lat"] // cell), int(r["lon"] // cell)
        reach = int(math.ceil(radius / (111.0 * cell * max(math.cos(math.radians(r["lat"])), 0.3)))) + 1
        neigh = []
        for dy in (-1, 0, 1):
            for dx in range(-reach, reach + 1):
                for o in grid.get((gy + dy, gx + dx), ()):
                    if o is not r and distance_km(r["lat"], r["lon"], o["lat"], o["lon"]) <= radius:
                        neigh.append(o["pm25"])
        if len(neigh) >= s["outlier_min_neighbors"]:
            med = statistics.median(neigh)
            if r["pm25"] > med * s["outlier_factor"] and r["pm25"] - med > s["outlier_abs"]:
                flags[r["name"]].append("outlier")
    return {k: ",".join(v) for k, v in flags.items()}


FLAG_LABELS = {
    "stuck": "zawieszony czujnik (PM1 = PM2.5 = PM10)",
    "absurd": "nierealnie wysoki odczyt pyłu",
    "dead": "czujnik pyłu pokazuje zero (prawdopodobnie martwy)",
    "wet": "zalany czujnik (wilgotność ~100%)",
    "stale": "odczyt nie zmienia się od wielu godzin",
    "outlier": "mocno odstaje od sąsiednich czujników",
    "hidden": "ukryty ręcznie przez administratora",
}
