"""Alerty smogowe: powiadomienia w przeglądarce (Web Push) i bot Telegram.

Wartość dla miejsca = mediana PM2.5 z maks. 3 najbliższych działających czujników do 5 km.
Alarm, gdy wartość przekroczy próg; odwołanie, gdy spadnie poniżej 80% progu (histereza, żeby
nie migało przy wartościach na granicy). Kolejny alarm najwcześniej po 3 h.
"""
import base64
import json
import logging
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import numpy as np
from scipy.spatial import cKDTree

from . import collector, db

log = logging.getLogger("airmap.alerts")

DATA_DIR = os.path.dirname(db.DB_PATH)
VAPID_FILE = os.path.join(DATA_DIR, "vapid_private.pem")
SITE = os.environ.get("AIRMAP_SITE", "https://air-locker-map.studio-colorbox.com")
TG_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()

RADIUS_KM = 5.0
NEAREST = 3
RESOLVE_RATIO = 0.8
MIN_GAP_S = 3 * 3600
THRESHOLDS = {13: "bardzo dobry → dobry", 35: "dobry → umiarkowany", 55: "umiarkowany → dostateczny",
              75: "dostateczny → zły"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS push_subs (
    id INTEGER PRIMARY KEY AUTOINCREMENT, endpoint TEXT UNIQUE, p256dh TEXT, auth TEXT,
    lat REAL, lon REAL, threshold REAL, label TEXT, token TEXT,
    state TEXT DEFAULT 'ok', last_alert INTEGER, created INTEGER, ip TEXT
);
CREATE TABLE IF NOT EXISTS tg_subs (
    chat_id INTEGER PRIMARY KEY, lat REAL, lon REAL, threshold REAL DEFAULT 35,
    state TEXT DEFAULT 'ok', last_alert INTEGER, created INTEGER
);
"""


def init():
    db.conn().executescript(SCHEMA)
    db.conn().commit()


# ---------------------------------------------------------------- wartość dla miejsca

def _index_label(v):
    t = db.setting("pm25_thresholds")
    labels = ["bardzo dobry", "dobry", "umiarkowany", "dostateczny", "zły", "bardzo zły"]
    i = 0
    while i < len(t) and v > t[i]:
        i += 1
    return labels[i]


class Locator:
    """Drzewo k-d z działających czujników — budowane raz na ocenę wszystkich subskrypcji."""

    def __init__(self):
        feats = [f for f in (collector.cache["sensors"] or {}).get("features", [])
                 if not f["properties"]["suspect"] and f["properties"].get("pm25") is not None]
        self.feats = feats
        self.kx = 111.32 * np.cos(np.radians(52.0))
        self.tree = cKDTree(np.array([[f["geometry"]["coordinates"][0] * self.kx,
                                       f["geometry"]["coordinates"][1] * 111.32] for f in feats])) if feats else None

    def value(self, lat, lon):
        """(mediana PM2.5, lista czujników) albo (None, []) gdy w promieniu nic nie ma."""
        if not self.tree:
            return None, []
        k = min(NEAREST, len(self.feats))
        d, idx = self.tree.query([lon * self.kx, lat * 111.32], k=k)
        d, idx = np.atleast_1d(d), np.atleast_1d(idx)
        near = [(self.feats[i]["properties"], dist) for dist, i in zip(d, idx) if dist <= RADIUS_KM]
        if not near:
            return None, []
        vals = sorted(p["pm25"] for p, _ in near)
        med = vals[len(vals) // 2] if len(vals) % 2 else (vals[len(vals) // 2 - 1] + vals[len(vals) // 2]) / 2
        return round(med, 1), [{"name": p["name"], "address": p["address"], "pm25": p["pm25"], "km": round(dist, 1)}
                               for p, dist in near]


def _transition(state, last_alert, value, threshold, now):
    """Nowy stan i rodzaj wiadomości (None / 'alert' / 'resolved')."""
    if value is None:
        return state, None
    if state != "high" and value > threshold and (not last_alert or now - last_alert >= MIN_GAP_S):
        return "high", "alert"
    if state == "high" and value < threshold * RESOLVE_RATIO:
        return "ok", "resolved"
    return state, None


def _message(kind, value, threshold, near, label):
    where = f" ({label})" if label else ""
    sensors = ", ".join(f"{n['name']} {n['pm25']:.0f}" for n in near)
    if kind == "alert":
        return (f"Smog w okolicy{where}",
                f"PM2.5 {value:.0f} µg/m³ — {_index_label(value)} (próg {threshold:.0f}). Czujniki: {sensors}.")
    return (f"Powietrze się poprawiło{where}", f"PM2.5 {value:.0f} µg/m³ — {_index_label(value)}.")


def evaluate_all():
    """Wołane po każdym przebiegu odczytów."""
    loc = Locator()
    now = int(time.time())
    sent = 0
    for s in db.q("SELECT * FROM push_subs"):
        value, near = loc.value(s["lat"], s["lon"])
        state, kind = _transition(s["state"], s["last_alert"], value, s["threshold"], now)
        if kind:
            title, body = _message(kind, value, s["threshold"], near, s["label"])
            if push_send(s, title, body):
                sent += 1
        if state != s["state"]:
            db.write("UPDATE push_subs SET state=?, last_alert=CASE WHEN ?='high' THEN ? ELSE last_alert END WHERE id=?",
                     (state, state, now, s["id"]))
    for s in db.q("SELECT * FROM tg_subs WHERE lat IS NOT NULL"):
        value, near = loc.value(s["lat"], s["lon"])
        state, kind = _transition(s["state"], s["last_alert"], value, s["threshold"], now)
        if kind:
            title, body = _message(kind, value, s["threshold"], near, None)
            emoji = "⚠️" if kind == "alert" else "✅"
            if tg_send(s["chat_id"], f"{emoji} <b>{title}</b>\n{body}\n\n<a href=\"{_map_link(s['lat'], s['lon'])}\">Mapa</a>"):
                sent += 1
        if state != s["state"]:
            db.write("UPDATE tg_subs SET state=?, last_alert=CASE WHEN ?='high' THEN ? ELSE last_alert END WHERE chat_id=?",
                     (state, state, now, s["chat_id"]))
    if sent:
        collector.log(f"alerty: wysłane {sent}")


def _map_link(lat, lon):
    return f"{SITE}/#lat={lat:.5f}&lon={lon:.5f}&z=13"


# ---------------------------------------------------------------- Web Push

def vapid_public_key():
    """Klucz publiczny VAPID (base64url, punkt nieskompresowany) — generowany przy pierwszym użyciu."""
    from py_vapid import Vapid
    from cryptography.hazmat.primitives import serialization

    if not os.path.exists(VAPID_FILE):
        v = Vapid()
        v.generate_keys()
        v.save_key(VAPID_FILE)
        os.chmod(VAPID_FILE, 0o600)
    v = Vapid.from_file(VAPID_FILE)
    raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def push_send(sub, title, body):
    from pywebpush import WebPushException, webpush

    try:
        webpush(subscription_info={"endpoint": sub["endpoint"], "keys": {"p256dh": sub["p256dh"], "auth": sub["auth"]}},
                data=json.dumps({"title": title, "body": body, "url": _map_link(sub["lat"], sub["lon"])}),
                vapid_private_key=VAPID_FILE, vapid_claims={"sub": SITE}, ttl=3 * 3600, timeout=10)
        return True
    except WebPushException as e:
        code = getattr(e.response, "status_code", None)
        if code in (404, 410):   # przeglądarka wycofała subskrypcję
            db.write("DELETE FROM push_subs WHERE id=?", (sub["id"],))
        else:
            log.warning("push %s: %s", sub["id"], e)
    except Exception as e:  # noqa: BLE001
        log.warning("push %s: %s", sub["id"], e)
    return False


def push_subscribe(sub, lat, lon, threshold, label, ip):
    token = secrets.token_urlsafe(16)
    db.write("""INSERT INTO push_subs(endpoint, p256dh, auth, lat, lon, threshold, label, token, created, ip)
                VALUES(?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(endpoint) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, threshold=excluded.threshold,
                    label=excluded.label, token=excluded.token, state='ok', last_alert=NULL""",
             (sub["endpoint"], sub["keys"]["p256dh"], sub["keys"]["auth"], lat, lon, threshold, label, token,
              int(time.time()), ip))
    row = db.q1("SELECT id FROM push_subs WHERE endpoint=?", (sub["endpoint"],))
    return row["id"], token


# ---------------------------------------------------------------- Telegram

def tg_api(method, **params):
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/{method}", data=data)
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())


def tg_send(chat_id, text):
    try:
        tg_api("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", disable_web_page_preview="true")
        return True
    except urllib.error.HTTPError as e:
        if e.code == 403:        # użytkownik zablokował bota
            db.write("DELETE FROM tg_subs WHERE chat_id=?", (chat_id,))
        log.warning("telegram %s: %s", chat_id, e)
    except Exception as e:  # noqa: BLE001
        log.warning("telegram %s: %s", chat_id, e)
    return False


HELP = ("Wyślij mi swoją <b>lokalizację</b> (spinacz → Lokalizacja), a powiadomię Cię, gdy w okolicy "
        "zrobi się smog — według czujników w paczkomatach.\n\n"
        "/prog 35 — próg PM2.5 w µg/m³ (13, 35, 55 albo 75; domyślnie 35)\n"
        "/stan — jakie jest teraz powietrze u Ciebie\n"
        "/stop — wyłącz powiadomienia\n\n"
        f"Mapa: {SITE}")


def _tg_handle(msg):
    chat = msg["chat"]["id"]
    text = (msg.get("text") or "").strip()
    now = int(time.time())
    if "location" in msg:
        lat, lon = msg["location"]["latitude"], msg["location"]["longitude"]
        _tg_set_location(chat, lat, lon)
        return
    if text.startswith("/start"):
        arg = text[6:].strip()
        if arg:                                     # link ze strony: /start 5223229_2095952_35
            try:
                a, b, t = arg.split("_")
                lat, lon, thr = int(a) / 1e5, int(b) / 1e5, float(t)
                if 48 < lat < 56 and 13 < lon < 25 and thr in THRESHOLDS:
                    db.write("INSERT INTO tg_subs(chat_id, threshold, created) VALUES(?,?,?) "
                             "ON CONFLICT(chat_id) DO UPDATE SET threshold=excluded.threshold", (chat, thr, now))
                    _tg_set_location(chat, lat, lon)
                    return
            except ValueError:
                pass
        tg_send(chat, "Cześć! " + HELP)
    elif text.startswith("/prog"):
        try:
            thr = float(text.split()[1].replace(",", "."))
            if thr not in THRESHOLDS:
                raise ValueError
        except (IndexError, ValueError):
            tg_send(chat, "Podaj próg: /prog 13, /prog 35, /prog 55 albo /prog 75 (PM2.5 w µg/m³).")
            return
        db.write("INSERT INTO tg_subs(chat_id, threshold, created) VALUES(?,?,?) "
                 "ON CONFLICT(chat_id) DO UPDATE SET threshold=excluded.threshold, state='ok'", (chat, thr, now))
        tg_send(chat, f"Próg ustawiony: PM2.5 &gt; {thr:.0f} µg/m³ ({THRESHOLDS[thr]}).")
    elif text.startswith("/stan"):
        s = db.q1("SELECT * FROM tg_subs WHERE chat_id=?", (chat,))
        if not s or s["lat"] is None:
            tg_send(chat, "Najpierw wyślij mi swoją lokalizację.")
            return
        value, near = Locator().value(s["lat"], s["lon"])
        if value is None:
            tg_send(chat, "W promieniu 5 km nie ma działającego czujnika.")
            return
        lines = "\n".join(f"• {n['name']} ({n['km']} km): {n['pm25']:.0f}" for n in near)
        tg_send(chat, f"PM2.5 u Ciebie: <b>{value:.0f} µg/m³</b> — {_index_label(value)}\n{lines}\n"
                      f"Próg alarmu: {s['threshold']:.0f}\n<a href=\"{_map_link(s['lat'], s['lon'])}\">Mapa</a>")
    elif text.startswith("/stop"):
        db.write("DELETE FROM tg_subs WHERE chat_id=?", (chat,))
        tg_send(chat, "Wyłączone. Żeby wrócić, wyślij lokalizację albo /start.")
    else:
        tg_send(chat, HELP)


def _tg_set_location(chat, lat, lon):
    db.write("INSERT INTO tg_subs(chat_id, lat, lon, created) VALUES(?,?,?,?) "
             "ON CONFLICT(chat_id) DO UPDATE SET lat=excluded.lat, lon=excluded.lon, state='ok'",
             (chat, lat, lon, int(time.time())))
    s = db.q1("SELECT threshold FROM tg_subs WHERE chat_id=?", (chat,))
    value, near = Locator().value(lat, lon)
    now_txt = (f"Teraz: PM2.5 <b>{value:.0f} µg/m³</b> — {_index_label(value)} (z {len(near)} czujników)."
               if value is not None else "Uwaga: w promieniu 5 km nie ma teraz działającego czujnika.")
    tg_send(chat, f"Zapisane 📍 Dam znać, gdy PM2.5 przekroczy {s['threshold']:.0f} µg/m³.\n{now_txt}\n"
                  "Zmiana progu: /prog 55, stan: /stan, wyłączenie: /stop.")


def _tg_loop():
    offset = 0
    while True:
        try:
            r = tg_api("getUpdates", offset=offset, timeout=50, allowed_updates='["message"]')
            for u in r.get("result", []):
                offset = u["update_id"] + 1
                if "message" in u:
                    try:
                        _tg_handle(u["message"])
                    except Exception as e:  # noqa: BLE001
                        log.warning("telegram wiadomość: %s", e)
        except Exception as e:  # noqa: BLE001
            log.warning("telegram getUpdates: %s", e)
            time.sleep(10)


_bot = {"name": None, "checked": 0}


def tg_bot_name():
    """Nazwa bota (do linku t.me/…), sprawdzana raz na godzinę."""
    if not TG_TOKEN:
        return None
    if time.time() - _bot["checked"] > 3600:
        _bot["checked"] = time.time()
        try:
            _bot["name"] = tg_api("getMe")["result"]["username"]
        except Exception:  # noqa: BLE001
            pass
    return _bot["name"]


def start():
    init()
    if TG_TOKEN:
        threading.Thread(target=_tg_loop, daemon=True).start()
        collector.log("alerty: bot Telegram uruchomiony")
