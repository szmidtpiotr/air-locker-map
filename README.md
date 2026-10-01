# paczkomaty-powietrze

Najbliższe paczkomaty InPost z czujnikiem jakości powietrza — z aktualnymi odczytami
(PM1, PM2.5, PM10, ciśnienie, wilgotność, temperatura).

```
$ python3 paczkomat_powietrze.py
Podaj nazwę paczkomatu (np. WAW84A): JGO03L

JGO03L: Piłsudskiego 47, 58-500 Jelenia Góra — bez czujnika

Najbliższe paczkomaty z czujnikiem (6):

   odl.  paczkomat       ID  jakość                PM1 PM2.5  PM10     hPa wilg. temp.  adres
---------------------------------------------------------------------------------------------
  572 m  JGO02BAPP    59852  bardzo dobra          5.4   7.8  11.8   987.7   40%  24.0  Jana Sobieskiego 37, 58-500 Jelenia Góra
  759 m  JGO03BAPP    59853  bardzo dobra          3.5   7.0  10.0   987.4   40%  25.0  Grunwaldzka 4, 58-506 Jelenia Góra
  ...
```

## Użycie

Wystarczy Python 3.8+ (bez dodatkowych pakietów).

```bash
python3 paczkomat_powietrze.py            # zapyta o nazwę
python3 paczkomat_powietrze.py WAW84A     # od razu
python3 paczkomat_powietrze.py WAW84A -n 8
```

### Bez klonowania repo (prosto z GitHuba)

`python3 https://...` nie zadziała — Python traktuje adres jak nazwę pliku na dysku.
Działają za to te sposoby:

```bash
# uv (najprościej, jeśli masz uv)
uv run https://raw.githubusercontent.com/szmidtpiotr/air-locker-map/develop/paczkomat_powietrze.py WAW84A

# sam Python — każdy system, także Windows (tam: python zamiast python3)
python3 -c "import urllib.request;exec(urllib.request.urlopen('https://raw.githubusercontent.com/szmidtpiotr/air-locker-map/develop/paczkomat_powietrze.py').read())" WAW84A

# bash / zsh
python3 <(curl -fsSL https://raw.githubusercontent.com/szmidtpiotr/air-locker-map/develop/paczkomat_powietrze.py)
```

Bez nazwy na końcu skrypt zapyta o nią. **Nie używaj `curl ... | python3 -` bez nazwy**:
skrypt idzie wtedy przez standardowe wejście, więc pytanie o nazwę od razu dostaje
koniec danych i program się wywala.

Uruchamiasz kod, który akurat leży na GitHubie. Żeby przypiąć konkretną wersję,
zamień w adresie `develop` na hash commita (np. `.../air-locker-map/59841e7/paczkomat_powietrze.py`).

Pierwsze uruchomienie pobiera listę stron paczkomatów z inpost.pl (ok. 20 s) i trzyma ją
tydzień w `~/.cache/paczkomat-powietrze/`. Kolejne trwają kilka sekund.

## Strona z mapą

Na żywo: **https://air-locker-map.studio-colorbox.com/** — wszystkie czujniki w Polsce na mapie
(kropki z grupowaniem, sześciokąty H3, plama), wyszukiwarka adresu i kodu paczkomatu, wykres 24 h,
oznaczanie podejrzanych czujników (zawieszone, zalane, nierealne, odstające od sąsiadów),
oficjalne stacje GIOŚ jako odniesienie. Panel `/admin` steruje kolektorem i parametrami.

Kod w `app/` (FastAPI + SQLite, kolektor w tym samym procesie), wdrożenie `deploy/deploy.sh`
na kontener Debiana (`/opt/air-locker-map`, usługa `air-locker-map.service`, port 8080).
Pierwsze wdrożenie generuje hasło panelu i wypisuje je raz na ekran.

### API i Home Assistant

- Dokumentacja API: **https://air-locker-map.studio-colorbox.com/api** — `/api/v1/...` (czujniki, najbliższe do punktu
  albo do dowolnego paczkomatu, historia, dzienne średnie, statystyki).
- **API wymaga klucza** (nagłówek `X-API-Key`). Klucze wydaje i unieważnia administrator w panelu `/admin` → Klucze API;
  każdy ma własny limit zapytań, unieważnienie jednego nie rusza pozostałych. Sama mapa jest publiczna.
- Integracja dla Home Assistanta (HACS): **[szmidtpiotr/ha-air-locker-map](https://github.com/szmidtpiotr/ha-air-locker-map)**.

### Bezpieczeństwo

- Dane z ShipX, GIOŚ i Nominatim traktowane jako obce: escapowane przed wstawieniem do HTML-a, linki tylko do `inpost.pl`.
- Nagłówki: CSP (`script-src 'self'`), `X-Frame-Options: DENY`, `nosniff`, `Referrer-Policy`; bez nagłówka `server`.
- Panel admina: hasło (scrypt), sesja w podpisanym ciasteczku `HttpOnly` + `SameSite=Strict` (+ `Secure` za HTTPS),
  5 nieudanych prób na 10 minut na IP. W wersji na żywo przed panelem stoi dodatkowo Authelia (2FA z internetu).
- Klucze API przechowywane jako SHA-256, pełny klucz widoczny tylko przy tworzeniu.
- Wyszukiwarka adresów: 20 zapytań/min na IP, do Nominatim najwyżej 1 zapytanie/s (zasady usługi).
- Uvicorn ufa `X-Forwarded-For` tylko od adresu proxy (`--forwarded-allow-ips`), więc limity liczą prawdziwe IP.

## Skąd są dane

1. **ShipX API** (`api-shipx-pl.easypack24.net/v1/points`, publiczne, bez klucza) — współrzędne
   paczkomatu i lista najbliższych (`relative_point` + `limit=500`). Pole `air_index_level`
   jest ustawione tylko w paczkomatach z czujnikiem — w Polsce ok. 4 tys. z 32,5 tys.
2. **Sitemapa inpost.pl** (`/sitemap/points/N.xml`) — adres strony paczkomatu. Kod paczkomatu
   jest członem adresu (`...-jgo02bapp-...`).
3. **Strona paczkomatu** — w HTML-u jest `data-shipx-url="/shipx-point-data/<ID>/..."`
   z wewnętrznym ID punktu. Innej drogi do ID nie ma.
4. **Odczyty** — `POST https://inpost.pl/shipx-point-data/<ID>/<KOD>/air_index_level`
   z nagłówkiem `X-Requested-With: XMLHttpRequest`.

```bash
curl -s -X POST -H 'X-Requested-With: XMLHttpRequest' \
  https://inpost.pl/shipx-point-data/59852/JGO02BAPP/air_index_level
```

### Pułapki

- **Tylko POST.** GET jest cache'owany przez Drupala: jedno zapytanie bez nagłówka zapisuje
  w cache przekierowanie i potem dostaje je każdy pytający o ten adres.
- **Liczy się tylko ID.** Kod paczkomatu i ostatni człon adresu są ignorowane — błędne ID
  zwraca dane innego paczkomatu.
- ShipX oznacza czujnikiem także punkty, dla których inpost.pl nie ma strony albo odczytów.
  Skrypt je pomija i dobiera kolejne.
- `air_sensors` ma format `NAZWA:wartość:%normy`. Procent normy liczony jest różnie w starszych
  i nowszych czujnikach, więc do porównań lepiej brać surowe µg/m³.
- **Temperatura i wilgotność** mierzone są w obudowie — w słońcu potrafią pokazać 30 °C.
- **Ciśnienie:** nowsze czujniki (z PM4) podają rzeczywiste, starsze przeliczone do poziomu morza.

To nieoficjalne źródło — InPost może je w każdej chwili zmienić. Pytaj z umiarem.
