# Delhi Bus Travel-Time Model and Journey Planner

This project estimates Delhi bus journey times from observed vehicle GPS
movement. It combines an October 2026 historical model, static Delhi bus GTFS,
and an optional Delhi Open Transit Data (OTD) live feed. It includes a FastAPI
JSON API and a browser journey planner with a Leaflet map.

The planner is bus-focused: journey options use bus routes and short walks
between stops. The API continues to return JSON for other clients; the web page
shows the same response as readable journey cards and provides an expandable
raw-JSON view.

## What is included

| File or directory | Purpose |
|---|---|
| `app.py` | FastAPI application, stop search, journey search, historical estimates, optional live-feed blending, and map support endpoints |
| `index.html` | Browser interface: stop search, journey options, distance and time summaries, and Leaflet map |
| `speed_table.parquet` | Historical speed/time aggregates by route, directed stop pair, day type, and IST hour |
| `speed_route_table.parquet` | Route-wide hourly and flat-route fallback aggregates |
| `speed_freeflow_table.parquet` | Per-route free-flow fallback speeds |
| `gtfs/` | Static GTFS stops, routes, trips, stop times, and shapes used by the planner |
| `process_day.py` | Processes one day of raw GPS data into stop-to-stop observations |
| `build_model.py` | Builds the three model tables and leave-one-day-out tables |
| `validation.py` | Runs held-out segment and journey-window validation |
| `TECHNICAL_NOTE.md` | Detailed modelling, planner, live-feed, and limitation notes |
| `VALIDATION_REPORT.md` | Held-out model results and Google Maps cross-check |

The prebuilt model tables and GTFS files are included, so the API can be run
without downloading the large raw GPS archive or rebuilding the model.

## Quick start on Windows

### Requirements

- Windows 10/11 and Python 3.12 (the project was run with Python 3.12).
- Internet access for installing Python packages, loading the Leaflet library,
  and displaying OpenStreetMap tiles.
- The OTD API key is optional. Without it, the historical model and journey
  planner still work.

### 1. Get the project

If you already have this project folder, open it in VS Code and open a
PowerShell terminal in that folder. To make a fresh copy:

```powershell
git clone https://github.com/shlokgitt/journey-plan.git
```

```powershell
cd journey-plan
```

Check that Python is available:

```powershell
python --version
```

### 2. Create an isolated Python environment

Run each command from the project folder:

```powershell
python -m venv .venv
```

Install the project's dependencies into that environment:

```powershell
.\.venv\Scripts\python.exe -m pip install --upgrade pip
```

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

The dependencies include FastAPI and Uvicorn for the API, DuckDB and PyArrow
for Parquet/model data, Pandas and NumPy for data processing, SciPy for spatial
search, and GTFS-Realtime protobuf bindings for the live feed.

### 3. Start the API and web page

```powershell
.\.venv\Scripts\python.exe -m uvicorn app:app --host 127.0.0.1 --port 8000
```

Leave that terminal open while using the app. Open:

- Journey planner: <http://127.0.0.1:8000/>
- Interactive API documentation: <http://127.0.0.1:8000/docs>

Stop the server with **Ctrl+C** in its terminal. If port 8000 is already in
use, stop the other server first, or start this one with `--port 8001` and use
port 8001 in the browser address. Restart Uvicorn after code changes so the
running process loads the new Python API.

The Leaflet map uses standard OpenStreetMap tiles and attribution; it does not
need a Google Maps or other map-provider API key. The browser does need internet
access to load the map library and map tiles.

## Using the journey planner

1. Type part of a stop name in **Starting stop or location** or **Destination**.
   Matching stop names appear in a dropdown while typing. The search is
   case-insensitive, matches text anywhere in the name, and shows all matches.
   A selected suggestion uses that stop's ID. You may also enter a `lat,lng`
   coordinate pair.
2. Set a departure date and time, or leave the field at its current IST value.
   The planner interprets a timezone-free time as India Standard Time.
3. Select **Find journeys**. The result panel displays up to two options,
   including bus legs, walks, estimated distances, transfers, and time
   components. Expand **View raw API JSON** to see the response returned by the
   API.
4. On the map, the selected option is shown with a pulsing green start marker,
   a pulsing red destination marker, and colored route lines. Blue dots are
   live vehicle positions when the OTD feed is enabled.

The displayed journey time is ride time plus walking time. The assumed wait
for boarding is shown separately; it is not included in that total. The
five-minute transfer penalty affects route search only and is not reported as
travel time. Waits are fixed assumptions, not predictions based on bus
headways.

## Data sources and files

### Historical GPS archive

The challenge's [Problem 1 page](https://delhi.transportstack.in/hackathon/)
links the October 2026 DTC GPS archive:

- [Download the October 2026 bus GPS archive](https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/delhi-dtc-bus-gtfs-rt-2026-10.7z)
- [Data README](https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/README.md)
- [SHA-256 checksums](https://pub-091bd9e794bb4877b10b29e6df80f065.r2.dev/2026-10/SHA256SUMS)

The archive is about 16.1 GB compressed and about 124 GB when fully unpacked.
It contains one SQLite database per day. **Do not extract the entire archive.**
`process_day.py` extracts and processes one requested day at a time.

For rebuilding, place the archive at:

```text
%USERPROFILE%\Downloads\delhi-dtc-bus-gtfs-rt-2026-10.7z
```

The processing script expects 7-Zip at
`C:\Program Files\7-Zip\7z.exe`. Install [7-Zip](https://www.7-zip.org/) at
that location, or update the `sevenz` path in `process_day.py` to match your
installation.

### Static GTFS

The static GTFS was published on 2024-10-11, earlier than the October 2026 GPS
observations. It supplies stop locations, route labels, scheduled stop
sequences, and route shapes:

- [Delhi bus GTFS data page](https://delhi.transportstack.in/data-services/datadetails/1)
- [Download `delhi_buses_static_gtfs_v1.zip`](https://dts-backend.transportstack.in/api/dataset/otd/download-file?agency=delhi-buses&category=static_gtfs&filename=delhi_buses_static_gtfs_v1.zip)

The required files are already in the repository's `gtfs/` directory. If
replacing them, extract the GTFS text files into `gtfs/`; the planner and
rebuild scripts use `stops.txt`, `routes.txt`, `trips.txt`, `shapes.txt`, and
`stop_times.txt.gz`.

## API reference

All API responses are JSON. The full interactive schema and try-it-out forms
are at `/docs`.

### `GET /journeys`

Search for bus journey options:

```text
/journeys?from=ISBT%20Kashmere%20Gate&to=Nehru%20Place&depart=2026-10-13T08%3A30
```

`from` and `to` accept a stop name, stop ID, or `latitude,longitude`.
Coordinates connect to nearby stops within 500 metres, with the access and
egress walks included. A stop-name search checks exact names first, then
case-insensitive substring matches. `depart` accepts `now` or an ISO date/time;
timezone-free values are interpreted as IST.

PowerShell example:

```powershell
Invoke-RestMethod "http://127.0.0.1:8000/journeys?from=ISBT%20Kashmere%20Gate&to=Nehru%20Place&depart=2026-10-13T08%3A30"
```

Coordinate example:

```text
http://127.0.0.1:8000/journeys?from=28.6694%2C77.2271&to=28.5486%2C77.2491&depart=2026-10-13T23%3A00
```

The response contains the requested origin, destination, departure and
timezone, `query_seconds`, live-feed status, and an `options` array. Each
option includes:

| Field | Meaning |
|---|---|
| `minutes`, `total_minutes` | Estimated in-vehicle time plus walking time; excludes the assumed wait |
| `ride_minutes`, `walk_minutes` | Time split by travel mode |
| `ride_distance_m`, `walk_distance_m`, `total_distance_m` | Estimated distance split by travel mode and combined |
| `wait_estimate_minutes` | Assumed five-minute wait for each boarding, separate from `minutes` |
| `including_wait_estimate_minutes` | Ride plus walk plus the assumed boarding wait |
| `transfer_count`, `transfer_stops` | Number and locations of route changes |
| `legs` | Ordered walk and ride steps, with stop names, route short name, distance, and minutes |
| `fallback_usage` | Model fallback counts for this returned itinerary only |
| `live_blend_usage` | Number of ride segments adjusted by recent live speeds and the routes used |
| `quieter_hour_comparison` | The same selected path evaluated at 14:00 IST using historical estimates |

The journey search advances the time of day along the route when estimating
each ride segment. The transfer penalty influences which paths are searched,
but is not added to reported journey minutes.

### Other endpoints

| Method and path | Description |
|---|---|
| `GET /` | Serves the Leaflet journey-planner page |
| `GET /docs` | Interactive FastAPI documentation |
| `GET /stops` | Returns GTFS stop IDs, names, and coordinates |
| `GET /stops?search=ISBT` | Returns all matching stop names, exact name first and otherwise case-insensitive substring matches |
| `GET /live-status` | Reports whether live polling is disabled, collecting samples, active, or in error; never returns the API key |
| `GET /live-vehicles?west=...&south=...&east=...&north=...` | Returns fresh vehicle positions within the requested map bounds; defaults to at most 1,500 results, with `limit` configurable up to 5,000 |
| `POST /map-route-shapes` | Map support endpoint that returns GTFS route-shape coordinates for the bus legs in its JSON request |

Live-vehicle bounds are longitude/latitude values: `west` and `east` are
longitudes; `south` and `north` are latitudes. Vehicle positions older than
three minutes are excluded from the map endpoint.

## Optional live bus data

The historical planner works without a key. To enable current OTD
vehicle-position polling, register with
[Open Transit Data Delhi](https://otd.delhi.gov.in/) and request access to the
vehicle-positions feed.

In the **same PowerShell terminal** that will run Uvicorn, enter the key:

```powershell
$env:DELHI_OTD_RT_API_KEY = Read-Host "Enter your registered OTD API key"
```

Then start Uvicorn in that terminal. Do not paste the key into source files,
README examples, screenshots, issue reports, or commits. The app passes it to
the default feed as the `key` query parameter:

```text
https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb
```

Optional environment settings:

| Variable | Purpose | Default |
|---|---|---|
| `DELHI_OTD_RT_API_KEY` | Enables authenticated OTD polling | unset; live feed disabled |
| `DELHI_OTD_RT_URL` | Selects a compatible GTFS-Realtime feed URL | Delhi OTD vehicle-positions URL above |
| `DELHI_OTD_RT_POLL_SECONDS` | Poll interval, from 10 to 300 seconds | `30` |

The server refreshes vehicle positions every poll and the map refreshes visible
markers every 30 seconds. Live speed estimates are calculated from successive
positions for the same vehicle and route; the optional feed speed field is not
used. A route needs at least three recent valid speed samples before it can
adjust journey estimates. Live blending only applies to departures near the
current time; otherwise the historical estimate is used. Current live traffic
does not change the 14:00 quieter-hour comparison.

## How the historical model is built

The GPS archive's trip IDs do not match the IDs in static GTFS. The processing
script links GPS trips to candidate GTFS shapes using the GPS route ID and
geometry, then selects the shape with the smallest median distance to the bus
pings. It projects pings and stops onto cumulative shape distance, interpolates
stop-passage times, and records the elapsed time between consecutive stops.
The source GPS `speed` value is not used; speed is derived from positions and
timestamps.

The learned tables aggregate observations by directed stop pair, route, day
type, and IST hour. Model speeds use summed distance divided by summed time,
not an average of per-trip speeds. Sparse data uses a documented fallback
chain: exact stop pair and hour, nearby hours, the other weekend day as a
labelled proxy, route-hour data, a flat route average, and finally a free-flow
estimate.

Stop-to-stop times include dwell at the first stop of a pair. This is retained
as passenger in-vehicle time because the passage-time interpolation cannot
reliably separate individual dwell intervals. Segments with unusable ping
gaps or implausible speeds are excluded.

## Rebuild the model and validation

Rebuilding is optional when using the model tables already in the repository.
It requires the raw GPS archive in the Downloads location above, the required
GTFS files in `gtfs/`, and 7-Zip at the path expected by `process_day.py`.
Allow each daily processing command to finish before starting the next one;
in the original environment a full day took around 14 minutes, but runtime
depends on the computer and disk.

Run each command from the project folder, one at a time:

```powershell
python process_day.py 2026_10_07
```

```powershell
python process_day.py 2026_10_14
```

```powershell
python process_day.py 2026_10_15
```

```powershell
python process_day.py 2026_10_17
```

```powershell
python process_day.py 2026_10_18
```

Then build the model tables and leave-one-day-out folds:

```powershell
python build_model.py
```

Run validation:

```powershell
python validation.py
```

`process_day.py` writes `day_*.parquet` and `segments_*.parquet` files. Its
optional second argument limits a test run to a sample of trips, for example
`python process_day.py 2026_10_15 2000`; a sample is not a full training day.
`build_model.py` writes the production speed tables and the `_loo_` fold tables.
`validation.py` writes `validation_loo_segments.csv`,
`validation_journey.csv`, and `validation_fallbacks.csv`.

Parquet output is written by DuckDB `COPY`. Raw SQLite databases, the `.7z`
archive, daily ping Parquet, segment Parquet, and leave-one-day-out model files
are intermediate data and should not be committed.

## Validation results

The current report is in [`VALIDATION_REPORT.md`](VALIDATION_REPORT.md). Its
headline weekday journey-window MAE is **224.10 seconds**; held-out segment MAE
is **40.19 seconds**. These are GPS-pipeline validation figures, not a promise
of real-world door-to-door accuracy. The report explains the filtering and
sampling methods.

Five Google Maps transit comparisons were made on November 24, 2026. Their
mean absolute difference is 6.0 minutes, but the comparison is not
mode-matched (Google Maps may use metro or rail) and is not held-out accuracy.
The validation report includes the individual pairs and timestamps.

## Limitations and interpretation

- The static GTFS is from 2024; GPS observations are from October 2026.
- The model uses five days only: three weekdays, one Saturday, and one Sunday.
  The weekend validation folds use the other weekend day as a proxy.
- About 10% of GPS trips were skipped for poor shape matches or too few
  on-route pings.
- A representative stop sequence is selected from the longest trip per GTFS
  shape; short-turn trips may serve fewer stops.
- Dwell is included in stop-to-stop time. GPS segments with long ping gaps are
  dropped, which may bias retained observed times toward faster travel.
- Journey-window validation uses the same GPS processing pipeline and measures
  in-vehicle time; it does not validate real walking, waiting, or transfers.
- Boarding wait is a fixed five-minute assumption, not derived from headways.
- Live speeds are route-level adjustments, not vehicle-to-stop arrival
  predictions. Without a configured key or enough recent samples, estimates
  remain historical.
- The route search uses a limited geographic corridor for performance and may
  miss a journey with a large detour. The free-flow fallback approximates road
  distance as 1.3 times straight-line stop distance.

See [`TECHNICAL_NOTE.md`](TECHNICAL_NOTE.md) for the full method and
[`VALIDATION_REPORT.md`](VALIDATION_REPORT.md) for metrics, fallback usage, and
the Google Maps cross-check.
