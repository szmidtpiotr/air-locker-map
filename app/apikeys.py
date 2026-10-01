"""Klucze do publicznego API v1.

W bazie trzymamy tylko skrót SHA-256 — pełny klucz widać raz, przy tworzeniu.
Każdy klucz ma własny limit zapytań na minutę i da się go unieważnić bez ruszania pozostałych.
Licznik użyć i „ostatnio użyty” zapisujemy co najwyżej raz na minutę na klucz, żeby nie pisać
do SQLite przy każdym zapytaniu.
"""
import hashlib
import secrets
import threading
import time
from collections import defaultdict, deque

from . import db

PREFIX = "alm_"

_lock = threading.Lock()
_by_hash = {}            # skrót -> wiersz (dict); przeładowywane po każdej zmianie
_loaded = False
_hits = defaultdict(deque)
_pending = defaultdict(int)
_last_flush = defaultdict(float)


def _digest(key):
    return hashlib.sha256(key.encode()).hexdigest()


def reload():
    global _loaded
    rows = db.q("SELECT id, name, key_hash, rate_per_min FROM api_keys WHERE revoked IS NULL")
    with _lock:
        _by_hash.clear()
        _by_hash.update({r["key_hash"]: dict(r) for r in rows})
        _loaded = True


def create(name, rate_per_min):
    key = PREFIX + secrets.token_urlsafe(24)
    db.write("INSERT INTO api_keys(name, key_hash, prefix, rate_per_min, created) VALUES(?,?,?,?,?)",
             (name, _digest(key), key[:10], rate_per_min, int(time.time())))
    reload()
    return key


def revoke(key_id):
    db.write("UPDATE api_keys SET revoked=? WHERE id=? AND revoked IS NULL", (int(time.time()), key_id))
    reload()


def listing():
    flush(force=True)
    return [dict(r) for r in db.q("SELECT id, name, prefix, rate_per_min, created, revoked, last_used, uses "
                                  "FROM api_keys ORDER BY revoked IS NOT NULL, id DESC")]


def check(key):
    """Zwraca (wiersz klucza, None) albo (None, (kod_http, komunikat))."""
    if not _loaded:
        reload()
    if not key:
        return None, (401, "Wymagany klucz API (nagłówek X-API-Key). Klucz wydaje administrator strony.")
    row = _by_hash.get(_digest(key.strip()))
    if not row:
        return None, (403, "Nieznany albo unieważniony klucz API")
    now = time.time()
    q = _hits[row["id"]]
    while q and q[0] < now - 60:
        q.popleft()
    if len(q) >= row["rate_per_min"]:
        return None, (429, f"Limit {row['rate_per_min']} zapytań na minutę dla tego klucza")
    q.append(now)
    _pending[row["id"]] += 1
    if now - _last_flush[row["id"]] > 60:
        flush(row["id"])
    return row, None


def flush(key_id=None, force=False):
    ids = list(_pending) if (force or key_id is None) else [key_id]
    now = time.time()
    for i in ids:
        n = _pending.pop(i, 0)
        if n:
            db.write("UPDATE api_keys SET uses = COALESCE(uses, 0) + ?, last_used=? WHERE id=?", (n, int(now), i))
        _last_flush[i] = now
