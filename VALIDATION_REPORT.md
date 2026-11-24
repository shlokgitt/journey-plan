# Validation Report

## Method

Five leave-one-day-out folds were evaluated. For each fold, all data from the
held-out day was excluded from training. The direct-segment report uses a
deterministic random sample of 5,000 valid segments per held-out day.

Journey validation samples up to 300 trips from each held-out day. Each
observation is a randomly selected contiguous window of 5–25 valid stretches
from one trip. Estimated times advance the clock from stretch to stretch.
Observed and predicted values are GPS-derived in-vehicle stretch times; the
journey-window validation does not score walking, boarding waits, or transfers.

Filters for this run exclude stretches under 5 seconds, over 900 seconds, below
2 km/h, or above 80 km/h. These thresholds were chosen while looking at the
validation folds, so the results are not an untouched final test set. Weekend
folds use the other weekend day type as an explicitly labelled proxy before
falling back to route and free-flow estimates.

## Held-out segment results

Each day contributes 5,000 observations (25,000 total).

| Held-out day | MAE (s) | MAPE | RMSE (s) | Bias (s) |
|---|---:|---:|---:|---:|
| 2026-10-07 | 39.07 | 37.06% | 73.28 | 1.72 |
| 2026-10-14 | 38.34 | 36.26% | 71.80 | 1.86 |
| 2026-10-15 | 39.37 | 36.14% | 74.32 | -1.74 |
| 2026-10-17 (Sat) | 43.96 | 40.35% | 82.82 | -13.27 |
| 2026-10-18 (Sun) | 40.23 | 49.55% | 70.78 | 13.72 |
| **Overall** | **40.19** | **39.87%** | **74.72** | **0.46** |

## Held-out journey-window results

There are 1,500 sampled windows. The headline weekday journey-window MAE is
224.10 seconds; the combined result includes weekend proxy folds and is not the
headline learned-model accuracy.

| Split | Windows | MAE (s) | Median absolute % error | Mean % error | P90 absolute % error | Bias (s) |
|---|---:|---:|---:|---:|---:|---:|
| Weekday | 900 | 224.10 | 12.60% | 2.99% | 35.81% | -6.74 |
| Saturday (proxy fold) | 300 | 248.82 | 14.26% | -8.31% | 37.72% | -159.85 |
| Sunday (proxy fold) | 300 | 263.09 | 17.35% | 14.90% | 50.35% | 139.96 |
| Peak | 800 | 230.31 | 12.52% | 1.38% | 37.83% | -28.70 |
| Off-peak | 700 | 244.31 | 14.56% | 5.08% | 39.90% | 15.61 |
| **Overall** | **1,500** | **236.84** | **13.69%** | **3.11%** | **38.75%** | **-8.02** |

Machine-readable fold results are in `validation_loo_segments.csv` and
`validation_journey.csv`.

## Before/after context

The previous held-out run, before the new filters, weekend proxy, and
5–25-stretch windows, reported segment MAE 49.26 seconds, MAPE 36.81%, and RMSE
147.28 seconds. Its journey sample used whole trips and reported MAE 610.40
seconds over 1,499 trips. These earlier results are not directly comparable to
the current run: the filters, fallback order, and journey observation window
changed. The filtering thresholds were selected while looking at folds, which
may improve apparent results.

### Planner query timing

Earlier builds measured 5.53 seconds for the Oct 13 08:30 query and 5.04
seconds for 23:00 before limiting results to two; after that change they took
4.63 and 4.76 seconds. An isolated runtime-copy test also varied from 4.28 to
5.73 seconds.

After narrowing the search corridor and reducing per-edge live-blend work, the
current code returned two options in 3.69 seconds at 08:30 and 3.40 seconds at
23:00, with the live feed disabled. A synthetic live snapshot produced two
AIIMS options in 4.04 seconds with 19 adjusted segments. These are local
measurements, not a general latency guarantee; the synthetic run is not an
authenticated live-feed benchmark. The earlier active-feed AIIMS query took
15.0 seconds before this optimization, so recheck with the registered feed
after restarting the updated server.

## Fallback usage

Fallback counts are measured only on the held-out validation observations.

| Scope | Exact hour | Nearby hours | Weekend proxy | Route-wide | Flat route | Free-flow |
|---|---:|---:|---:|---:|---:|---:|
| Held-out segments | 38.29% | 17.17% | 39.92% | 4.27% | 0.23% | 0.12% |
| Journey windows | 39.43% | 16.76% | 40.56% | 2.84% | 0.25% | 0.15% |

See `validation_fallbacks.csv` for counts.

## Current Google Maps cross-check (not held-out validation)

Five Google Maps transit directions were queried on Nov 24, 2026, around
19:37 IST. The planner was evaluated for the same origin-destination pairs and
departure times. The comparison uses the first Google Maps transit result and
the planner's reported time including its assumed boarding wait. Google Maps
transit may use metro or rail, while this planner models buses; these are not
mode-matched results. The query date is also after the October GPS observations,
so no corresponding GPS observation exists and these values are not an
accuracy score. The OTD key was not configured during these queries, so the
planner values below are historical-only.

| Origin -> destination | Query time (IST) | Google Maps transit | Planner incl. wait | Google - planner |
|---|---|---:|---:|---:|
| Kashmere Gate ISBT -> Nehru Place | 2026-11-24 19:37:01 | 44 min | 46.5 min | -2.5 min |
| Kashmere Gate ISBT -> Connaught Place | 2026-11-24 19:37:06 | 22 min | 28.1 min | -6.1 min |
| Kashmere Gate ISBT -> AIIMS | 2026-11-24 19:37:10 | 32 min | 45.2 min | -13.2 min |
| Kashmere Gate ISBT -> Anand Vihar ISBT | 2026-11-24 19:37:14 | 36 min | 38.5 min | -2.5 min |
| Kashmere Gate ISBT -> Central Secretariat | 2026-11-24 19:37:44 | 27 min | 32.5 min | -5.5 min |

Mean absolute difference is 6.0 minutes for these five current, mixed-mode
cross-checks; it must not be interpreted as held-out model accuracy. The
planner ride-plus-walk estimates, query timestamps, and Google Maps URLs are in
`validation_google_maps.csv`. Each Google Maps URL uses transit mode with
departure set to the current time at query.

## Fallback and data caveats

GPS-derived observed values share the model's snapping/interpolation pipeline.
Segments removed by gap and speed filters are not represented, so retained
observations may be optimistic. The static GTFS is from 2024, the GPS data is
from October 2026, dwell remains included in segment times, and approximately
10% of trips were skipped. There is only one sampled Saturday and Sunday.
