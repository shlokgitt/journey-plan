# Technical Note: Delhi Bus Travel-Time Model

## GPS-to-route linking

The October 2026 GPS archive contains bus pings with GPS trip IDs that do not
match the trip IDs in the 2024 static GTFS. Pings are therefore grouped by GPS
trip and matched against the GTFS shapes available for their route. The selected
shape is the one with the smallest median ping-to-shape distance. Trips without
a suitable shape or enough on-route pings are skipped.

The GPS `speed` field is not used because it did not agree reliably with
speed calculated from positions and timestamps. Each ping is projected onto
cumulative shape distance. GTFS stops are projected onto the same shape, and
the time of passage at each stop is interpolated between adjacent on-route
pings. Differences between consecutive stop passage times form the observed
stretch times.

## Per-stretch travel-time model

Travel time is aggregated by route, directed stop pair, day type, and IST hour.
The model stores sample counts and weighted distance/time totals. Exact-hour
and nearby-hour estimates require at least three observations. Training
observations shorter than 5 seconds, longer than 900 seconds, below 2 km/h, or
above 80 km/h are excluded. The free-flow speed is the 85th percentile of
route-hour weighted speeds, using only route-hours with at least 50 observations.

The planner fallback sequence is exact hour, nearby hours, the other weekend
day as a labelled proxy, route-hour average, a flat per-route average, then a
free-flow estimate. The free-flow fallback uses 1.3 times the straight-line
stop distance to approximate road distance.

## Journey planner

Stops are graph nodes. Directed consecutive-stop edges on representative GTFS
shapes are ride edges, and stops within 500 m are connected by walk edges.
Walking speed is assumed to be 4.5 km/h. A* search uses `(stop, route)` state
and limits exploration to a corridor at least 3 km wide (or 15% of direct
distance) around the origin-destination line; this speeds search but may miss
routes with larger detours. The planner adds a five-minute assumed wait at each
boarding and a five-minute transfer penalty only as a route-search weight when
the route changes. The transfer penalty is not included in reported travel
minutes.

Reported `minutes` is in-vehicle time plus walking time. The assumed boarding
wait is shown separately and also provided as
`including_wait_estimate_minutes`. Coordinate origins and destinations use
nearby stops within 500 m; access and egress walking are included. For a stop
name, all exact-name matches are considered first; otherwise literal,
case-insensitive substring matches are used.

Dwell time is included in stop-to-stop segment time. It is not separated from
moving time because the stop-passage interpolation does not reliably identify
individual dwell intervals, and the resulting stretch estimate is intended to
represent passenger in-vehicle time.

## Live data

The planner optionally polls Delhi OTD's GTFS-Realtime `VehiclePositions.pb`
feed every 30 seconds. Access requires a registered OTD API key, supplied as
`DELHI_OTD_RT_API_KEY` and sent as the `key` query parameter to
`https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb` by default. The URL
can be overridden with `DELHI_OTD_RT_URL`. The key is never returned by the
API. `/live-status` exposes polling state, and `/live-vehicles` returns fresh
vehicle coordinates restricted to the requested map bounds. The Leaflet page
refreshes visible vehicle markers every 30 seconds.

Vehicle speeds are computed from successive positions for the same vehicle and
route, not from the feed's optional speed field. Samples with gaps outside
10–120 seconds, displacement under 20 m, or computed speed outside 0.5–80 km/h
are rejected. A route needs at least three samples from the preceding three
minutes. Its median live speed is compared with that route's historical
day/hour speed. The ratio is bounded to 0.6–1.4, blended at 50% for the first
stretch, and decays exponentially with distance along the journey (5 km decay
scale). This is a coarse route-level blend, not a bus-to-stop arrival model.

Blending is limited to departures within 15 minutes of the current time. If
the feed is unconfigured, stale, or has insufficient samples for a route, the
historical estimate is unchanged. The quieter-hour comparison remains
historical-only because current live traffic does not describe that hour. The
feed and live blending were observed with the user's locally configured key;
the key is not stored in the repository. Without that runtime key, the planner
uses historical estimates and the live-vehicle endpoint returns no positions.

## Limitations

- Static GTFS is from 2024, while GPS observations are from October 2026.
- The model uses five days from one month. There is one Saturday and one Sunday.
- Held-out weekend folds use the other weekend day type as a labelled proxy;
  they do not represent a separately learned weekend model.
- About 10% of trips were skipped because no shape was within 50 m median
  distance or too few pings were on-route.
- A representative stop sequence is selected from the longest trip per shape;
  short-turn trips may serve fewer stops.
- Observed validation times use this same GPS processing pipeline. Dropped
  segments can make the retained observations optimistic.
- Journey-window validation measures in-vehicle stretch time only, not planner
  walking, waiting, or transfer time.
- Boarding wait is a fixed assumption, not derived from service headways.
