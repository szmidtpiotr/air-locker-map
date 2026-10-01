#!/usr/bin/env python3
"""Najbliższe paczkomaty InPost z czujnikiem jakości powietrza.

Użycie:
    python3 paczkomat_powietrze.py            # zapyta o nazwę
    python3 paczkomat_powietrze.py WAW84A     # od razu
    python3 paczkomat_powietrze.py WAW84A -n 8

Tylko biblioteka standardowa Pythona 3.8+.

Jak to działa:
  1. ShipX API (publiczne) -> współrzędne podanego paczkomatu.
  2. ShipX API, relative_point + limit=500 -> najbliższe paczkomaty; czujnik ma ten,
     który ma ustawione pole air_index_level.
  3. Sitemapa inpost.pl -> strona paczkomatu -> data-shipx-url z wewnętrznym ID punktu.
  4. POST na inpost.pl/shipx-point-data/<ID>/... -> odczyty czujnika.
     Musi być POST z nagłówkiem X-Requested-With: GET jest cache'owany przez Drupala
     i potrafi zwracać przekierowanie zamiast danych.
"""
import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor

SHIPX = "https://api-shipx-pl.easypack24.net/v1/points"
INPOST = "https://inpost.pl"
UA = "Mozilla/5.0 (paczkomat-powietrze)"
CACHE_DIR = os.path.join(os.environ.get("XDG_CACHE_HOME", os.path.expanduser("~/.cache")), "paczkomat-powietrze")
SITEMAP_CACHE = os.path.join(CACHE_DIR, "sitemap-urls.txt")
IDS_CACHE = os.path.join(CACHE_DIR, "ids.json")
SITEMAP_MAX_AGE = 7 * 24 * 3600

LEVELS = {
    "VERY_GOOD": "bardzo dobra",
    "GOOD": "dobra",
    "SATISFACTORY": "umiarkowanie dobra",
    "MODERATE": "umiarkowana",
    "BAD": "zła",
    "VERY_BAD": "bardzo zła",
}


def http(url, method="GET", headers=None, timeout=20):
    req = urllib.request.Request(url, method=method, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def shipx(params):
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return json.loads(http(f"{SHIPX}?{query}"))


def sitemap_urls():
    """Adresy stron wszystkich punktów; cache na tydzień."""
    fresh = os.path.exists(SITEMAP_CACHE) and time.time() - os.path.getmtime(SITEMAP_CACHE) < SITEMAP_MAX_AGE
    if not fresh:
        print("Pobieram listę stron paczkomatów z inpost.pl (raz na tydzień, ok. 20 s)...", file=sys.stderr)
        urls = []
        for i in range(1, 100):
            try:
                xml = http(f"{INPOST}/sitemap/points/{i}.xml")
            except urllib.error.HTTPError:
                break
            found = re.findall(r"<loc>([^<]+)</loc>", xml)
            if not found:
                break
            urls += found
        os.makedirs(CACHE_DIR, exist_ok=True)
        with open(SITEMAP_CACHE, "w") as f:
            f.write("\n".join(urls))
    with open(SITEMAP_CACHE) as f:
        return f.read().split()


def page_index(urls):
    """Kod paczkomatu (małymi literami) -> adres strony. Kod jest jednym z członów sluga."""
    idx = {}
    for u in urls:
        for token in u.rsplit("/", 1)[-1].split("-"):
            if token:
                idx.setdefault(token, u)
    return idx


def load_ids():
    try:
        with open(IDS_CACHE) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_ids(ids):
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(IDS_CACHE, "w") as f:
        json.dump(ids, f)


def resolve_id(name, idx):
    url = idx.get(name.lower())
    if not url:
        return None
    for attempt in range(3):
        try:
            m = re.search(r'data-shipx-url="/shipx-point-data/(\d+)/', http(url))
            if m:
                return int(m.group(1))
        except (urllib.error.URLError, TimeoutError):
            pass
        time.sleep(1 + attempt)
    return None


def read_sensors(point_id, name):
    """(poziom, {czujnik: wartość}) albo None, gdy inpost.pl nie ma odczytów dla punktu."""
    try:
        raw = http(f"{INPOST}/shipx-point-data/{point_id}/{name}/air_index_level", method="POST",
                   headers={"X-Requested-With": "XMLHttpRequest"})
    except urllib.error.HTTPError as e:
        if e.code == 404:  # {"message":"Air sensors are not available."}
            return None
        raise
    data = json.loads(raw)
    if not data.get("air_sensors"):
        return None
    values = {}
    for item in data.get("air_sensors") or []:
        key, value, _percent = (item.split(":") + ["", ""])[:3]
        try:
            values[key] = float(value)
        except ValueError:
            pass
    return data.get("air_index_level"), values


def distance(p):
    return f"{p['distance'] / 1000:.1f} km" if p["distance"] >= 1000 else f"{p['distance']} m"


def fmt(v, digits=1):
    return "-" if v is None else f"{v:.{digits}f}"


def main():
    ap = argparse.ArgumentParser(description="Najbliższe paczkomaty InPost z czujnikiem powietrza.")
    ap.add_argument("name", nargs="?", help="kod paczkomatu, np. WAW84A")
    ap.add_argument("-n", type=int, default=6, help="ile paczkomatów pokazać (domyślnie 6)")
    args = ap.parse_args()

    name = (args.name or input("Podaj nazwę paczkomatu (np. WAW84A): ")).strip().upper()
    if not name:
        sys.exit("Nie podano nazwy.")

    origin = json.loads(http(f"{SHIPX}/{name}"))
    if "location" not in origin:  # ShipX zwraca błąd w treści z kodem HTTP 200
        sys.exit(f"Nie znaleziono paczkomatu {name}: {origin.get('error', origin)}")
    loc = origin["location"]
    print(f"\n{name}: {origin['address']['line1']}, {origin['address']['line2']}"
          f" — {'ma czujnik' if origin.get('air_index_level') else 'bez czujnika'}")

    near = shipx({
        "relative_point": f"{loc['latitude']},{loc['longitude']}",
        "limit": 500,
        "type": "parcel_locker",
        "fields": "name,air_index_level,distance,address,location_description",
    })["items"]
    candidates = [p for p in near if p.get("air_index_level")]
    if not candidates:
        sys.exit("Wśród 500 najbliższych paczkomatów nie ma żadnego z czujnikiem.")

    # ShipX oznacza też punkty, dla których inpost.pl nie ma strony albo odczytów —
    # takie pomijamy i bierzemy kolejne, aż zbierze się args.n działających.
    ids = load_ids()
    idx = None
    results, skipped = [], []
    pos = 0
    while len(results) < args.n and pos < len(candidates):
        batch = candidates[pos: pos + args.n - len(results)]
        pos += len(batch)
        missing = [p["name"] for p in batch if p["name"] not in ids]
        if missing:
            idx = idx or page_index(sitemap_urls())
            with ThreadPoolExecutor(3) as ex:
                for n, pid in zip(missing, ex.map(lambda n: resolve_id(n, idx), missing)):
                    if pid:
                        ids[n] = pid
            save_ids(ids)

        def fetch(p):
            pid = ids.get(p["name"])
            if not pid:
                return p, None, None, "brak strony z ID na inpost.pl"
            try:
                reading = read_sensors(pid, p["name"])
            except Exception as e:
                return p, pid, None, str(e)
            return p, pid, reading, None if reading else "inpost.pl nie ma odczytów"

        with ThreadPoolExecutor(3) as ex:
            for p, pid, reading, err in ex.map(fetch, batch):
                (skipped if err else results).append((p, pid, reading, err))

    print(f"\nNajbliższe paczkomaty z czujnikiem ({len(results)}):\n")
    head = f"{'odl.':>7}  {'paczkomat':<11} {'ID':>6}  {'jakość':<19} {'PM1':>5} {'PM2.5':>5} {'PM10':>5} {'hPa':>7} {'wilg.':>5} {'temp.':>5}  adres"
    print(head)
    print("-" * len(head))
    for p, pid, reading, _ in results:
        dist = distance(p)
        addr = f"{p['address']['line1']}, {p['address']['line2']}"
        level, v = reading
        print(f"{dist:>7}  {p['name']:<11} {pid:>6}  {LEVELS.get(level, level or '-'):<19} "
              f"{fmt(v.get('PM1')):>5} {fmt(v.get('PM25')):>5} {fmt(v.get('PM10')):>5} "
              f"{fmt(v.get('PRESSURE')):>7} {fmt(v.get('HUMIDITY'), 0):>4}% {fmt(v.get('TEMPERATURE')):>5}  {addr}")

    if skipped:
        print("\nPominięte (ShipX zgłasza czujnik, ale danych nie da się pobrać):")
        for p, pid, _, err in skipped:
            print(f"  {distance(p):>7}  {p['name']:<11} {err}")

    print("\nPM w µg/m³. Temperatura i wilgotność mierzone w obudowie paczkomatu — bywają zawyżone/zaniżone.")
    print("Ciśnienie: nowsze czujniki podają rzeczywiste, starsze (wartości ~1020+) przeliczone do poziomu morza.")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(130)
