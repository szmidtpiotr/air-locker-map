# Czujniki powietrza w paczkomatach InPost poza Polską — rozpoznanie (2026-10-01)

Rozpoznanie tylko do odczytu: około 90 zapytań, po kolei, z przerwą 0,5–0,7 s,
User-Agent `air-locker-map research (+https://github.com/szmidtpiotr/air-locker-map)`.
Do `shipx-point-data` szły wyłącznie zapytania POST z nagłówkiem `X-Requested-With: XMLHttpRequest`.

## Wniosek w jednym zdaniu
**Poza Polską w paczkomatach InPost nie ma czujników powietrza, które dałoby się odczytać.** Pole
`air_index_level` istnieje w każdym kraju (ten sam schemat API), ale w żadnej sprawdzonej próbce
nie ma wartości. Endpoint odczytu dla punktów zagranicznych odpowiada `"Air sensors are not available."`.

## API punktów — co istnieje
| Host | Stan | Uwagi |
|---|---|---|
| `api-global-points.easypack24.net/v1/points` | **200, publiczny, bez autoryzacji** | 196 012 punktów, filtr `country=XX` działa, `type=parcel_locker` działa, pole `air_index_level` jest. Nie da się filtrować ani sortować po `air_index_level` (parametry są ignorowane). |
| `api-shipx-pl.easypack24.net` | 200 | Odsyła do `api-pl-points.easypack24.net` (34 680 punktów PL). |
| `api-pl-points`, `api-uk-points`, `api-it-points` `.easypack24.net` | 200 | Krajowe podzbiory tego samego API (GB 27 698, IT 11 679). |
| `api-uk-global-points.easypack24.net` | 200 | Tego hosta używa strona inpost.co.uk (Next.js, dane punktu wpisane w HTML, `air_index_level:null`). |
| `api-shipx-{fr,uk,es,pt,hu,be,cz}`, `api-{fr,es,hu}-points`, `api-*-global-points` (poza uk) | NXDOMAIN | — |
| `api-shipx-it.easypack24.net` | 520 (Cloudflare) | Nie działa. |
| `api-global-points-internal.easypack24.net` | **NXDOMAIN** (brak publicznego DNS) | Publicznym odpowiednikiem jest w praktyce `api-global-points` (ten sam schemat, te same dane PL). |

Kontrola: w `api-global-points` dla PL jedna strona (500 paczkomatów, page=10) miała 74 niepuste wartości
(26 VERY_GOOD, 43 GOOD, 4 MODERATE, 1 VERY_BAD), więc to globalne API **przenosi** dane o powietrzu —
za granicą po prostu ich nie ma.

## Kraje
| Kraj | API (globalne, `country=`) | Paczkomaty (`type=parcel_locker`) | `air_index_level` w schemacie? | Próbka → niepuste | Odczyt (`shipx-point-data`) | Uwagi |
|---|---|---|---|---|---|---|
| FR | tak | 13 210 (wszystkie punkty 27 376) | tak | 2 210 → **0** (strony 1, 7, 14, 20, 27) | `inpost.pl/shipx-point-data/88935/FR078864/…` → `{"message":"Air sensors are not available."}` | 13 558 punktów FR jest w polskiej mapie strony. Strona punktu na inpost.pl ma szablon czujników (`air--sensor--pm` itd.), ale bez danych. www.inpost.fr: timeout; mondialrelay.fr: 403. |
| IT | tak (też `api-it-points`) | 6 761 | tak | 2 261 → **0** (strony 1, 4, 7, 11, 14) | inpost.it to też Drupal, ale strona paczkomatu (np. `/locker-cinisello-balsamo-itcin44359-…`) nie ma `data-shipx-url` ani niczego o powietrzu; `POST inpost.it/shipx-point-data/…` → 404 | Punktów IT nie ma w polskiej mapie strony. |
| ES | tak | 5 392 | tak | 2 392 → **0** (strony 1, 3, 6, 8, 11) | `…/99273/ES065089/…` → „not available” | Na www.inpost.es nie znaleziono nic o powietrzu. |
| PT | tak | 663 | tak | 663 (całość) → **0** | `…/132111/PT004409/…` → „not available” | |
| GB | tak (też `api-uk-points`, `api-uk-global-points`) | 18 851 | tak | 2 351 → **0** (strony 1, 10, 19, 29, 38) | brak endpointu; inpost.co.uk (16 194 paczkomaty w sitemapie) wpisuje do HTML punkt z `air_index_level:null` | |
| BE | tak | 737 | tak | 737 (całość) → **0** | `…/134928/BE042142/…` → „not available” | |
| NL | tak | 482 | tak | 482 (całość) → **0** | `…/132108/NL023017/…` → „not available” | |
| LU | tak | 31 | tak | 31 (całość) → **0** | `…/107472/LU010781/…` → „not available” | |
| HU | tak | **0** (3 274 punkty, wszystkie `pok`/`pop`, partner_id 98) | tak | 500 → **0** | — | Same punkty partnerów, bez paczkomatów InPost. inpost.hu to zaparkowana domena. |
| CZ | tak | **0** (10 206 × `pok`/`pop`, partner_id 100) | tak | 500 → **0** | — | Tylko punkty partnerów. |
| SK | tak | **0** (3 863 × `pok`/`pop`, partner_id 100) | tak | 500 → **0** | — | Tylko punkty partnerów. |
| AT | tak | **0** (9 279 × `pok`/`pop`, partner_id 98) | tak | 500 → **0** | — | Tylko punkty partnerów. |

Razem przejrzano ok. 13 600 zagranicznych paczkomatów (pełne PT/BE/NL/LU, 5 stron rozsianych po
FR/IT/ES/GB): **0 z czujnikiem**. W Polsce przy tej samej metodzie wychodzi ok. 12–15% na stronę.

## Endpoint odczytu — jak się zachowuje
- Paczkomat PL z czujnikiem (kontrola): `POST https://inpost.pl/shipx-point-data/59852/JGO02BAPP/air_index_level` →
  `{"message":"Data source: Drupal point_entity_field_data table","air_index_level":"GOOD","air_sensors":["PM1:12.15:","PM25:18.13:72.51",…,"PRESSURE:988.29:"]}`
  (bywa też `Data source: api-global-points-internal…`, czyli źródło zapasowe).
- Paczkomat zagraniczny z polskiej mapy strony (FR/ES/PT/BE/NL/LU): `{"message":"Air sensors are not available."}`.
- Krajowe strony InPostu (it, co.uk, es, pt) nie mają ani endpointu, ani elementów o powietrzu.

## Rekomendacja
Rozszerzenie mapy na inne kraje **dziś nie ma sensu, bo nie ma danych**. Czujniki to wyłącznie
program polski. Tanie zabezpieczenie na przyszłość:
1. Raz na tydzień albo raz na miesiąc przejść `api-global-points.easypack24.net/v1/points?country=XX&type=parcel_locker&per_page=500&fields=name,air_index_level,location`
   dla FR/IT/ES/PT/GB/BE/NL/LU (ok. 95 stron) i sprawdzić, czy pojawiła się jakakolwiek niepusta wartość.
2. Jeśli się pojawi: dla krajów z polskiej mapy strony (FR/ES/PT/BE/NL/LU) odczyt pójdzie od razu
   obecnym kolektorem (ID z `data-shipx-url` na inpost.pl, ten sam POST). Dla IT i GB trzeba by
   najpierw znaleźć osobne źródło odczytów, bo ich strony takiego endpointu nie mają.
3. Kolektor może już teraz brać listę PL z `api-global-points` zamiast `api-shipx-pl`. Schemat
   jest ten sam, a dochodzi filtr `country`.
