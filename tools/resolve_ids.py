#!/usr/bin/env python3
"""Nazwa paczkomatu -> ID punktu w Drupalu (inpost.pl), tylko dla paczkomatów z czujnikiem.

Czyta data/lockers.json (ShipX) i data/sitemap-urls.txt, zapisuje data/ids.json.
Wznawialny: pomija nazwy, które już mają ID.
"""
import json, os, re, sys, time, urllib.request
from concurrent.futures import ThreadPoolExecutor

ROOT = os.path.join(os.path.dirname(__file__), "..", "data")
OUT = os.path.join(ROOT, "ids.json")
UA = {"User-Agent": "Mozilla/5.0 (paczkomaty-powietrze; hobby)"}

lockers = [l for l in json.load(open(os.path.join(ROOT, "lockers.json"))) if l.get("air_index_level")]
idx = {}
for u in open(os.path.join(ROOT, "sitemap-urls.txt")).read().split():
    for tok in u.rsplit("/", 1)[-1].split("-"):
        idx.setdefault(tok, u)
ids = json.load(open(OUT)) if os.path.exists(OUT) else {}


def resolve(name):
    url = idx.get(name.lower())
    if not url:
        return name, None, "brak strony w sitemapie"
    for attempt in range(4):
        try:
            html = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=20).read().decode("utf8", "ignore")
            m = re.search(r'data-shipx-url="/shipx-point-data/(\d+)/', html)
            if m:
                return name, int(m.group(1)), None
        except Exception as e:
            err = str(e)
        time.sleep(2 * (attempt + 1))
    return name, None, "brak data-shipx-url"


todo = [l["name"] for l in lockers if l["name"] not in ids]
print(f"{len(lockers)} z czujnikiem, do zrobienia {len(todo)}", flush=True)
fail = {}
with ThreadPoolExecutor(3) as ex:
    for n, (name, pid, err) in enumerate(ex.map(resolve, todo), 1):
        if pid:
            ids[name] = pid
        else:
            fail[name] = err
        if n % 100 == 0 or n == len(todo):
            json.dump(ids, open(OUT, "w"))
            print(f"{n}/{len(todo)} ok={len(ids)} fail={len(fail)}", flush=True)
json.dump(fail, open(os.path.join(ROOT, "ids-fail.json"), "w"), ensure_ascii=False, indent=1)
