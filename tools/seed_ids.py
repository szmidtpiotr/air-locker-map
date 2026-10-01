#!/usr/bin/env python3
"""Jednorazowo: wczytuje ID punktów ustalone wcześniej (data/ids.json) do bazy kontenera,
żeby nie pobierać tych samych stron drugi raz. Uruchamiać NA kontenerze, po zadaniu „lockers”:

    sudo -u airmap AIRMAP_DB=/var/lib/air-locker-map/airmap.sqlite python3 seed_ids.py ids.json [sitemap-urls.txt]
"""
import json
import os
import sqlite3
import sys
import time

ids = json.load(open(sys.argv[1]))
idx = {}
if len(sys.argv) > 2:
    for u in open(sys.argv[2]).read().split():
        for tok in u.rsplit("/", 1)[-1].split("-"):
            if tok:
                idx.setdefault(tok, u)
c = sqlite3.connect(os.environ.get("AIRMAP_DB", "/var/lib/air-locker-map/airmap.sqlite"))
now = int(time.time())
n = 0
for name, pid in ids.items():
    cur = c.execute("UPDATE lockers SET point_id=?, page_url=?, id_status='ok', id_checked_at=? "
                    "WHERE name=? AND point_id IS NULL", (pid, idx.get(name.lower()), now, name))
    n += cur.rowcount
c.commit()
print(f"wczytano {n} z {len(ids)}")
