#!/usr/bin/env python3
"""Pomiar, jak często InPost faktycznie zmienia odczyty: co 2 min pyta kilkanaście czujników,
zapisuje do data/cadence.csv tylko chwile, w których odczyt się zmienił."""
import json, sys, time, urllib.request

P = {"WAW43BAPP": 59720, "WAW604M": 40826, "WAW667M": 47799, "WAW596M": 40936, "WAW25APP": 50222,
     "WAW627M": 45810, "WAW644M": 46793, "KAP02M": 40945, "LSZ03M": 47842, "SCH03BAPP": 60550,
     "RDV01M": 40363, "HLQ01BAPP": 60842}
HOURS = float(sys.argv[1]) if len(sys.argv) > 1 else 3
last = {}
end = time.time() + HOURS * 3600
with open(__file__.rsplit("/", 2)[0] + "/data/cadence.csv", "a") as f:
    while time.time() < end:
        for name, pid in P.items():
            try:
                r = urllib.request.Request(f"https://inpost.pl/shipx-point-data/{pid}/{name}/air_index_level", method="POST",
                                           headers={"X-Requested-With": "XMLHttpRequest", "User-Agent": "Mozilla/5.0"})
                s = json.dumps(json.load(urllib.request.urlopen(r, timeout=15)).get("air_sensors"))
            except Exception as e:
                continue
            if last.get(name) != s:
                f.write(f"{time.strftime('%F %T')};{name};{s}\n"); f.flush()
                last[name] = s
            time.sleep(0.5)
        time.sleep(114)
