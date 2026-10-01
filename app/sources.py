"""Zewnętrzne źródła: ShipX, inpost.pl, Open-Meteo (wysokość), GIOŚ, Nominatim.

Wszystkie zapytania do inpost.pl przechodzą przez wspólny limiter tempa.
"""
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

UA = "air-locker-map/0.1 (+https://github.com/szmidtpiotr/air-locker-map)"
SHIPX = "https://api-shipx-pl.easypack24.net/v1/points"
INPOST = "https://inpost.pl"


class RateLimiter:
    def __init__(self, rate):
        self.rate = rate
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        with self._lock:
            now = time.monotonic()
            if self._next < now:
                self._next = now
            delay = self._next - now
            self._next += 1.0 / max(self.rate, 0.01)
        if delay > 0:
            time.sleep(delay)


inpost_limiter = RateLimiter(2.5)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # 3xx wraca jako HTTPError zamiast pobierać stronę docelową


_no_redirect = urllib.request.build_opener(_NoRedirect)


def http(url, method="GET", headers=None, data=None, timeout=25, follow=True):
    req = urllib.request.Request(url, method=method, data=data, headers={"User-Agent": UA, **(headers or {})})
    opener = urllib.request.urlopen if follow else _no_redirect.open
    with opener(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


# --- ShipX ---

def shipx_all_lockers(log=print):
    """Wszystkie paczkomaty w Polsce (ok. 33 tys.), 500 na stronę."""
    fields = "name,air_index_level,location,address_details,location_description,status"
    items, page = [], 1
    while True:
        url = f"{SHIPX}?type=parcel_locker&per_page=500&page={page}&fields={fields}"
        for attempt in range(4):
            try:
                d = json.loads(http(url))
                break
            except Exception as e:  # noqa: BLE001
                log(f"ShipX strona {page}: {e}, ponawiam")
                time.sleep(3 * (attempt + 1))
        else:
            raise RuntimeError(f"ShipX: nie udało się pobrać strony {page}")
        items += d["items"]
        if page >= d["total_pages"]:
            return items
        page += 1
        time.sleep(0.3)


# --- inpost.pl ---

def sitemap_urls(log=print):
    urls = []
    for i in range(1, 100):
        inpost_limiter.wait()
        try:
            xml = http(f"{INPOST}/sitemap/points/{i}.xml")
        except urllib.error.HTTPError:
            break
        found = re.findall(r"<loc>([^<]+)</loc>", xml)
        if not found:
            break
        urls += found
    log(f"sitemapa: {len(urls)} adresów")
    return urls


def page_index(urls):
    idx = {}
    for u in urls:
        for token in u.rsplit("/", 1)[-1].split("-"):
            if token:
                idx.setdefault(token, u)
    return idx


def resolve_point_id(url):
    """(point_id, status). Status: ok | no_url | error."""
    # Strona punktu bez ID przekierowuje (302) na wyszukiwarkę — nie idziemy za tym, to ciężka strona.
    for attempt in range(3):
        inpost_limiter.wait()
        try:
            html = http(url, follow=False)
            m = re.search(r'data-shipx-url="/shipx-point-data/(\d+)/', html)
            if m:
                return int(m.group(1)), "ok"
            return None, "no_url"
        except urllib.error.HTTPError as e:
            if e.code in (301, 302, 303, 307, 308, 404, 410):
                return None, "no_url"
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1.5 * (attempt + 1))
    return None, "error"


def read_sensors(point_id, name):
    """Słownik odczytów, None gdy brak danych. Tylko POST — GET jest cache'owany przez Drupala."""
    inpost_limiter.wait()
    try:
        raw = http(f"{INPOST}/shipx-point-data/{point_id}/{name}/air_index_level", method="POST",
                   headers={"X-Requested-With": "XMLHttpRequest"})
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    data = json.loads(raw)
    sensors = data.get("air_sensors")
    if not sensors:
        return None
    out = {"level": data.get("air_index_level")}
    keymap = {"PM1": "pm1", "PM25": "pm25", "PM4": "pm4", "PM10": "pm10",
              "PRESSURE": "pressure", "HUMIDITY": "humidity", "TEMPERATURE": "temperature"}
    for item in sensors:
        parts = item.split(":")
        k = keymap.get(parts[0])
        if k and len(parts) > 1:
            try:
                out[k] = round(float(parts[1]), 2)
            except ValueError:
                pass
    return out


# --- Open-Meteo: wysokość terenu ---

def elevations(coords):
    """coords: lista (lat, lon), max 100. Zwraca listę wysokości [m n.p.m.]."""
    lat = ",".join(f"{a:.5f}" for a, _ in coords)
    lon = ",".join(f"{o:.5f}" for _, o in coords)
    d = json.loads(http(f"https://api.open-meteo.com/v1/elevation?latitude={lat}&longitude={lon}"))
    return d["elevation"]


# --- GIOŚ ---

GIOS = "https://api.gios.gov.pl/pjp-api/v1/rest"


def _gios_list(d):
    for k, v in d.items():
        if isinstance(v, list):
            return v
    return []


def gios_stations():
    d = json.loads(http(f"{GIOS}/station/findAll?size=500"))
    out = []
    for s in _gios_list(d):
        out.append(dict(station_id=s["Identyfikator stacji"], name=s["Nazwa stacji"], city=s.get("Nazwa miasta"),
                        lat=float(s["WGS84 φ N"]), lon=float(s["WGS84 λ E"])))
    return out


def gios_sensors(station_id):
    d = json.loads(http(f"{GIOS}/station/sensors/{station_id}?size=50"))
    out = {}
    for s in _gios_list(d):
        code = s.get("Wskaźnik - kod")
        if code in ("PM2.5", "PM10"):
            out[code] = s["Identyfikator stanowiska"]
    return out


def gios_latest(sensor_id):
    """(wartość, czas) ostatniego niepustego pomiaru."""
    d = json.loads(http(f"{GIOS}/data/getData/{sensor_id}?size=24"))
    for row in _gios_list(d):
        v = row.get("Wartość")
        if v is not None:
            return float(v), row.get("Data")
    return None, None


# --- Nominatim ---

def geocode(query):
    url = ("https://nominatim.openstreetmap.org/search?format=jsonv2&limit=5&countrycodes=pl&q="
           + urllib.parse.quote(query))
    res = json.loads(http(url))
    return [dict(lat=float(r["lat"]), lon=float(r["lon"]), label=r["display_name"]) for r in res]
