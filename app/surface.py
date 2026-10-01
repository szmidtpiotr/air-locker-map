"""„Plama”: wartości rozlane między czujnikami (interpolacja IDW), jako obraz PNG nakładany na mapę.

Siatka liczona jest w odwzorowaniu Mercatora (tak jak kafelki mapy), więc obraz rozciągnięty między
narożnikami pokrywa się z mapą bez przesunięć. Piksele dalej niż `max_km` od najbliższego czujnika są
przezroczyste — tam nie mamy pomiarów i plama nie powinna niczego udawać.
"""
import io
import math

import numpy as np
from PIL import Image, ImageFilter
from scipy.spatial import cKDTree

# obszar Polski z zapasem: zachód, południe, wschód, północ
BBOX = (13.9, 48.85, 24.4, 55.05)
GRID_W = 420            # szerokość siatki obliczeniowej; obraz jest potem powiększany z wygładzeniem
OUT_W = 1260
POWER = 2.0             # wykładnik IDW
NEIGHBORS = 12          # tylko k najbliższych czujników — lokalny obraz, pojedynczy czujnik nie „zalewa” regionu
SMOOTH = 7              # mediana z tylu najbliższych (łącznie z samym czujnikiem) przed interpolacją


def _merc_y(lat):
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def _inv_merc_y(y):
    return math.degrees(2 * math.atan(math.exp(y)) - math.pi / 2)


def corners():
    """Narożniki obrazu dla źródła MapLibre (lewy-górny, prawy-górny, prawy-dolny, lewy-dolny)."""
    w, s, e, n = BBOX
    return [[w, n], [e, n], [e, s], [w, s]]


def _hex(c):
    return tuple(int(c[i:i + 2], 16) for i in (1, 3, 5))


def colorize(values, stops):
    """values: tablica; stops: lista (wartość, '#rrggbb') rosnąco. Interpolacja liniowa między przystankami."""
    xs = np.array([s[0] for s in stops], dtype=float)
    rgb = np.array([_hex(s[1]) for s in stops], dtype=float)
    out = np.zeros(values.shape + (3,), dtype=float)
    for ch in range(3):
        out[..., ch] = np.interp(values, xs, rgb[:, ch])
    return out


def render(points, stops, max_km=25.0, opacity=0.78):
    """points: lista (lat, lon, value). Zwraca PNG (bytes)."""
    w, s, e, n = BBOX
    y_n, y_s = _merc_y(n), _merc_y(s)
    grid_h = int(GRID_W * (y_n - y_s) / math.radians(e - w))
    lons = np.linspace(w, e, GRID_W)
    lats = np.array([_inv_merc_y(y) for y in np.linspace(y_n, y_s, grid_h)])
    glon, glat = np.meshgrid(lons, lats)
    gx, gy = glon.ravel(), glat.ravel()

    pts = np.array(points, dtype=float)
    kx = 111.32 * math.cos(math.radians(52.0))  # km na stopień długości (wystarczająco dla Polski)
    # współrzędne w km, drzewo k-d do szukania najbliższych czujników
    tree = cKDTree(np.column_stack([pts[:, 1] * kx, pts[:, 0] * 111.32]))
    # mediana z najbliższych sąsiadów zamiast surowej wartości: pojedynczy zepsuty czujnik
    # (np. ciśnienie 943 hPa) nie robi „tarczy” w plamie; szczegóły widać w widoku kropek
    m = min(SMOOTH, len(pts))
    _, nidx = tree.query(tree.data, k=m)
    values = np.median(pts[:, 2][nidx.reshape(len(pts), -1)], axis=1)
    k = min(NEIGHBORS, len(pts))
    dk, idx = tree.query(np.column_stack([gx * kx, gy * 111.32]), k=k)
    if k == 1:
        dk, idx = dk[:, None], idx[:, None]
    wgt = 1.0 / np.maximum(dk, 0.3) ** POWER
    result = (wgt * values[idx]).sum(axis=1) / wgt.sum(axis=1)
    result[dk[:, 0] > max_km] = np.nan

    grid = result.reshape(grid_h, GRID_W)
    alpha = np.where(np.isnan(grid), 0, int(255 * opacity)).astype(np.uint8)
    rgb = colorize(np.nan_to_num(grid, nan=stops[0][0]), stops).astype(np.uint8)
    img = Image.fromarray(np.dstack([rgb, alpha]), "RGBA")
    out_h = int(OUT_W * grid_h / GRID_W)
    img = img.resize((OUT_W, out_h), Image.BILINEAR).filter(ImageFilter.GaussianBlur(1.2))
    # paleta 256 kolorów z kanałem alfa — kilkakrotnie mniejszy plik, różnicy na mapie nie widać
    img = img.quantize(colors=256, method=Image.Quantize.FASTOCTREE)
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
