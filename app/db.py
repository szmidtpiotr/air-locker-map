import json
import os
import sqlite3
import threading

from . import config

DB_PATH = os.environ.get("AIRMAP_DB", "/var/lib/air-locker-map/airmap.sqlite")

SCHEMA = """
CREATE TABLE IF NOT EXISTS lockers (
    name TEXT PRIMARY KEY,
    lat REAL, lon REAL,
    city TEXT, street TEXT, building TEXT, post_code TEXT, province TEXT,
    description TEXT,
    shipx_level TEXT,              -- air_index_level z ShipX; NULL = bez czujnika
    point_id INTEGER,              -- ID punktu w Drupalu inpost.pl
    page_url TEXT,
    id_status TEXT,                -- ok | no_page | no_url | error
    id_checked_at INTEGER,
    elevation REAL,
    hidden INTEGER DEFAULT 0,      -- ręcznie ukryty w panelu
    first_seen INTEGER, last_seen INTEGER
);
CREATE INDEX IF NOT EXISTS lockers_sensor ON lockers(shipx_level);

CREATE TABLE IF NOT EXISTS readings (
    name TEXT, ts INTEGER,
    pm1 REAL, pm25 REAL, pm4 REAL, pm10 REAL,
    pressure REAL, pressure_sl REAL, humidity REAL, temperature REAL,
    level TEXT
);
CREATE INDEX IF NOT EXISTS readings_name_ts ON readings(name, ts);
CREATE INDEX IF NOT EXISTS readings_ts ON readings(ts);

CREATE TABLE IF NOT EXISTS latest (
    name TEXT PRIMARY KEY, ts INTEGER, status TEXT,   -- ok | no_data | error
    pm1 REAL, pm25 REAL, pm4 REAL, pm10 REAL,
    pressure REAL, pressure_sl REAL, humidity REAL, temperature REAL,
    level TEXT, gen TEXT,                             -- gen: new (z PM4) | old
    signature TEXT, changed_at INTEGER,
    flags TEXT DEFAULT ''
);

-- dzienne agregaty na czujnik — trzymane bezterminowo (surowe odczyty kasuje history_days)
CREATE TABLE IF NOT EXISTS daily (
    name TEXT, day TEXT,           -- day: YYYY-MM-DD, czas lokalny
    n INTEGER,
    pm1_avg REAL, pm25_avg REAL, pm25_max REAL, pm10_avg REAL, pm10_max REAL,
    pressure_sl_avg REAL, humidity_avg REAL,
    PRIMARY KEY (name, day)
);
CREATE INDEX IF NOT EXISTS daily_day ON daily(day);

CREATE TABLE IF NOT EXISTS api_keys (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT, key_hash TEXT UNIQUE, prefix TEXT,     -- prefix: pierwsze znaki, żeby rozpoznać klucz w panelu
    rate_per_min INTEGER, created INTEGER, revoked INTEGER, last_used INTEGER, uses INTEGER DEFAULT 0
);

-- godzinowe odczyty stacji GIOŚ (do porównań z paczkomatami)
CREATE TABLE IF NOT EXISTS gios_readings (
    station_id INTEGER, ts TEXT, pm25 REAL, pm10 REAL,   -- ts: czas pomiaru GIOŚ, np. "2026-10-01 19:00:00"
    PRIMARY KEY (station_id, ts)
);

-- wiatr z Open-Meteo na siatce nad Polską (ostatnie pobranie)
CREATE TABLE IF NOT EXISTS wind (
    lat REAL, lon REAL, speed REAL, gust REAL, direction REAL, ts TEXT,
    PRIMARY KEY (lat, lon)
);

CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job TEXT, started INTEGER, finished INTEGER,
    ok INTEGER DEFAULT 0, fail INTEGER DEFAULT 0, note TEXT
);

CREATE TABLE IF NOT EXISTS geocode (q TEXT PRIMARY KEY, result TEXT, ts INTEGER);

CREATE TABLE IF NOT EXISTS gios (
    station_id INTEGER PRIMARY KEY, name TEXT, city TEXT, lat REAL, lon REAL,
    pm25 REAL, pm10 REAL, ts TEXT, updated INTEGER
);
"""

_local = threading.local()
_write_lock = threading.Lock()


def conn():
    c = getattr(_local, "conn", None)
    if c is None:
        os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
        c = sqlite3.connect(DB_PATH, timeout=30)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        _local.conn = c
    return c


# kolumny dodane po pierwszym wdrożeniu — CREATE TABLE IF NOT EXISTS ich nie dopisze
MIGRATIONS = [
    ("latest", "pressure_trend", "REAL"),   # zmiana ciśnienia n.p.m. w ciągu ~3 h [hPa]
]


def init():
    c = conn()
    c.executescript(SCHEMA)
    for table, column, ctype in MIGRATIONS:
        cols = {r[1] for r in c.execute(f"PRAGMA table_info({table})")}
        if column not in cols:
            c.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ctype}")
    c.commit()


def write(sql, params=()):
    with _write_lock:
        c = conn()
        cur = c.execute(sql, params)
        c.commit()
        return cur


def write_many(sql, rows):
    with _write_lock:
        c = conn()
        c.executemany(sql, rows)
        c.commit()


def q(sql, params=()):
    return conn().execute(sql, params).fetchall()


def q1(sql, params=()):
    return conn().execute(sql, params).fetchone()


# --- ustawienia ---

def get_settings():
    stored = {r["key"]: json.loads(r["value"]) for r in q("SELECT key, value FROM settings")}
    return {s["key"]: stored.get(s["key"], s["default"]) for s in config.SETTINGS}


def setting(key):
    r = q1("SELECT value FROM settings WHERE key=?", (key,))
    return json.loads(r["value"]) if r else config.BY_KEY[key]["default"]


def save_settings(values):
    clean = {k: config.validate(k, v) for k, v in values.items()}
    with _write_lock:
        c = conn()
        c.executemany("INSERT INTO settings(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      [(k, json.dumps(v)) for k, v in clean.items()])
        c.commit()
    return clean
