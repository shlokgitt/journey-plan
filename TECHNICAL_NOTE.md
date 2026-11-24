# Technical note

## Data and snapping

GPS pings are matched to route shapes and projected onto cumulative shape distance. The shape with the smallest median ping distance is selected. Stops are projected onto the same shape and ping times are linearly interpolated between adjacent pings.

The GPS `speed` field is not used. Segment distance is shape distance; segment time is the difference between projected stop passage times.

## Model

The model aggregates `sum(segment metres) / sum(segment seconds)` by stop pair, day type, and IST hour. It stores count, median/p25/p75 segment time, and weighted speed. Segment observations shorter than 5 seconds, longer than 80 km/h, or part of a ping gap over 60 seconds are not included.

Sparse fallbacks are, in order: exact stop-pair and hour; neighbouring hours; route-wide hour average; stop-distance/free-flow estimate. The free-flow estimate uses the route's 85th-percentile hourly weighted speed. This fallback list is implemented in `app.py`.

## Known limitations

The static GTFS is from 2024; GPS is from October 2026. Some routes and stops may not match. Dwell is included in stop-to-stop segment time. The representative stop sequence comes from the longest trip on each shape; short-turn trips may omit stops. About 10% of GPS trips are currently skipped. The model is trained from 5 October 2026 days.
The selected sample dates have no gazetted Delhi public holiday; the official 2026 gazette lists Dussehra on 20 October.

## Validation

The accuracy results below are leave-one-day-out held-out results. Each October day is excluded from model training, then tested only with that held-out day.

### Held-out segment validation

The combined 25,000-segment sample reported MAE 49.26 seconds, MAPE 36.81%, and RMSE 147.28 seconds:

| Held-out day | MAE (seconds) | MAPE | RMSE (seconds) |
|---|---:|---:|---:|
| 2026-10-07 | 45.71 | 37.65% | 122.64 |
| 2026-10-14 | 46.75 | 38.00% | 139.02 |
| 2026-10-15 | 46.12 | 36.16% | 125.61 |
| 2026-10-17 | 61.62 | 36.61% | 198.86 |
| 2026-10-18 | 46.10 | 35.62% | 136.98 |

Machine-readable results are in `validation_loo_segments.csv`.

### Held-out journey validation

The script samples up to 300 qualifying observed trips from each held-out day. The observed journey time is the sum of held-out GPS segment times for each sampled trip.

| Split | Observations | MAE (seconds) | Median absolute % error |
|---|---:|---:|---:|
| Overall | 1499 | 610.40 | 15.32% |
| Peak | 732 | 661.51 | 15.76% |
| Off-peak | 767 | 561.62 | 14.72% |
| Weekday | 900 | 444.65 | 11.44% |
| Saturday | 299 | 1030.03 | 28.12% |
| Sunday | 300 | 689.42 | 19.61% |

Machine-readable results are in `validation_journey.csv`.

### Fallback use

The held-out segment sample used exact-hour observations for 54.67%, nearby-hour observations for 4.88%, route-wide averages for 0.20%, and stop-distance/free-flow estimates for 40.26%. Held-out journey segments used exact hour 51.23%, nearby hours 6.09%, route-wide 0.29%, and free-flow 42.39%. Counts and percentages are in `validation_fallbacks.csv`.
