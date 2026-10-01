"""Parametry sterowane z panelu administracyjnego.

Każdy parametr ma opis, typ i zakres — z tej listy panel sam buduje formularz,
a backend sprawdza poprawność zapisu. Wartości trzymane są w tabeli `settings`.
"""

SETTINGS = [
    # --- zbieranie danych ---
    dict(key="collector_enabled", group="Zbieranie danych", type="bool", default=True,
         label="Kolektor włączony", help="Wyłączony = żadnych automatycznych zapytań do InPostu."),
    dict(key="collect_interval_min", group="Zbieranie danych", type="int", default=60, min=10, max=1440,
         label="Odczyty co (min)", help="Jak często pobierać odczyty wszystkich czujników. "
         "Pełny przebieg ~4000 czujników przy 2,5 zap./s trwa ok. 27 min."),
    dict(key="request_rate", group="Zbieranie danych", type="float", default=2.5, min=0.2, max=10,
         label="Tempo zapytań (na sekundę)", help="Łączny limit zapytań do inpost.pl. Trzymaj nisko."),
    dict(key="workers", group="Zbieranie danych", type="int", default=3, min=1, max=8,
         label="Równoległe połączenia", help="Więcej niż 3 potrafi skończyć się pustymi stronami (limit po stronie InPostu)."),
    dict(key="lockers_refresh_days", group="Zbieranie danych", type="int", default=7, min=1, max=60,
         label="Lista paczkomatów co (dni)", help="Odświeżanie listy paczkomatów z ShipX (66 zapytań) i ustalanie ID dla nowych czujników."),
    dict(key="id_retry_days", group="Zbieranie danych", type="int", default=7, min=1, max=90,
         label="Ponów nieudane ID po (dniach)", help="Punkty bez strony / bez data-shipx-url próbujemy ponownie po tylu dniach."),
    dict(key="history_days", group="Zbieranie danych", type="int", default=30, min=1, max=365,
         label="Historia (dni)", help="Starsze odczyty są kasowane."),

    # --- jakość danych ---
    dict(key="hide_flagged", group="Jakość danych", type="bool", default=True,
         label="Ukrywaj podejrzane", help="Podejrzane czujniki domyślnie szare/ukryte na mapie (użytkownik może je pokazać)."),
    dict(key="pm_max", group="Jakość danych", type="float", default=500, min=100, max=5000,
         label="PM powyżej (µg/m³) = nierealny", help="Odczyt któregokolwiek pyłu powyżej progu oznacza zepsuty czujnik."),
    dict(key="humidity_max", group="Jakość danych", type="float", default=99.0, min=80, max=100,
         label="Wilgotność ≥ (%) = zalany", help="Taki odczyt oznacza mokry czujnik; PM też bywa wtedy zawyżone."),
    dict(key="stale_hours", group="Jakość danych", type="int", default=6, min=1, max=168,
         label="Bez zmian przez (h) = zawieszony", help="Odczyt identyczny dłużej niż tyle godzin."),
    dict(key="outlier_radius_km", group="Jakość danych", type="float", default=10, min=1, max=50,
         label="Promień sąsiedztwa (km)", help="Do porównania z sąsiadami."),
    dict(key="outlier_min_neighbors", group="Jakość danych", type="int", default=3, min=1, max=20,
         label="Min. sąsiadów", help="Poniżej tej liczby sąsiadów czujnika nie oceniamy."),
    dict(key="outlier_factor", group="Jakość danych", type="float", default=3.0, min=1.5, max=20,
         label="Odstaje, gdy PM2.5 > mediana sąsiadów ×", help="Wraz z progiem bezwzględnym poniżej."),
    dict(key="outlier_abs", group="Jakość danych", type="float", default=25, min=0, max=500,
         label="…i różnica > (µg/m³)", help="Chroni przed fałszywymi alarmami przy bardzo czystym powietrzu."),

    # --- mapa ---
    dict(key="site_title", group="Mapa", type="str", default="Powietrze z paczkomatów",
         label="Tytuł strony", help=""),
    dict(key="default_metric", group="Mapa", type="choice", default="pm25",
         choices=["pm25", "pm10", "pm1", "pressure_sl", "humidity", "temperature"],
         label="Domyślna wielkość", help=""),
    dict(key="default_view", group="Mapa", type="choice", default="points",
         choices=["points", "hex", "heat"], label="Domyślny widok", help="Kropki / sześciokąty / plama ciepła."),
    dict(key="hex_resolution", group="Mapa", type="int", default=6, min=3, max=8,
         label="Rozmiar sześciokątów (H3)", help="5 ≈ 250 km², 6 ≈ 36 km², 7 ≈ 5 km²."),
    dict(key="nearest_count", group="Mapa", type="int", default=6, min=1, max=20,
         label="Najbliższe czujniki w wyszukiwarce", help=""),
    dict(key="pm25_thresholds", group="Mapa", type="list", default=[13, 35, 55, 75, 110],
         label="Progi PM2.5 (µg/m³)", help="Granice klas: bardzo dobry | dobry | umiarkowany | dostateczny | zły | bardzo zły (indeks GIOŚ)."),
    dict(key="pm10_thresholds", group="Mapa", type="list", default=[20, 50, 80, 110, 150],
         label="Progi PM10 (µg/m³)", help="Jak wyżej, indeks GIOŚ."),
    dict(key="pm1_thresholds", group="Mapa", type="list", default=[10, 25, 40, 55, 80],
         label="Progi PM1 (µg/m³)", help="Brak oficjalnego indeksu — wartości umowne."),
    dict(key="gios_layer", group="Mapa", type="bool", default=True,
         label="Warstwa stacji GIOŚ", help="Oficjalne stacje jako punkt odniesienia (pobierane co godzinę)."),
]

BY_KEY = {s["key"]: s for s in SETTINGS}


def validate(key, value):
    """Zwraca wartość w poprawnym typie albo rzuca ValueError z opisem po polsku."""
    s = BY_KEY.get(key)
    if not s:
        raise ValueError(f"nieznany parametr {key}")
    t = s["type"]
    try:
        if t == "bool":
            if isinstance(value, str):
                value = value.lower() in ("1", "true", "tak", "on")
            value = bool(value)
        elif t == "int":
            value = int(value)
        elif t == "float":
            value = float(value)
        elif t == "str":
            value = str(value).strip()[:120]
        elif t == "choice":
            if value not in s["choices"]:
                raise ValueError
        elif t == "list":
            if isinstance(value, str):
                value = [float(x) for x in value.replace(";", ",").split(",") if x.strip()]
            value = [float(x) for x in value]
            if len(value) != len(s["default"]) or value != sorted(value):
                raise ValueError(f"{s['label']}: podaj {len(s['default'])} rosnących liczb")
    except ValueError as e:
        raise ValueError(str(e) or f"{s['label']}: zła wartość") from None
    except TypeError:
        raise ValueError(f"{s['label']}: zła wartość") from None
    if t in ("int", "float"):
        if not s["min"] <= value <= s["max"]:
            raise ValueError(f"{s['label']}: dozwolone {s['min']}–{s['max']}")
    return value
