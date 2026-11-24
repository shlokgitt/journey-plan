import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import duckdb
import numpy as np
import pandas as pd


BASE = os.path.dirname(os.path.abspath(__file__))
IST = ZoneInfo("Asia/Kolkata")
DAYS = {
    "2026_10_07": "weekday",
    "2026_10_14": "weekday",
    "2026_10_15": "weekday",
    "2026_10_17": "sat",
    "2026_10_18": "sun",
}
FALLBACKS = (
    "exact_hour",
    "nearby_hours",
    "weekend_proxy",
    "route_wide",
    "flat_route",
    "freeflow",
)


def load_stops():
    frame = pd.read_csv(os.path.join(BASE, "gtfs", "stops.txt"), dtype={"stop_id": str})
    return {
        str(row.stop_id): (float(row.stop_lat), float(row.stop_lon))
        for row in frame.itertuples()
    }


STOP_COORDS = load_stops()


def haversine(a, b):
    import math

    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    c = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(c), math.sqrt(max(0, 1 - c)))


def load_fold(con, held_day):
    tag = f"_loo_{held_day}"
    exact_path = os.path.join(BASE, f"speed_table{tag}.parquet")
    route_path = os.path.join(BASE, f"speed_route_table{tag}.parquet")
    free_path = os.path.join(BASE, f"speed_freeflow_table{tag}.parquet")
    exact_frame = con.execute("SELECT * FROM read_parquet(?)", [exact_path]).df()
    route_frame = con.execute("SELECT * FROM read_parquet(?)", [route_path]).df()
    free_frame = con.execute("SELECT * FROM read_parquet(?)", [free_path]).df()

    exact = {}
    pair_hours = {}
    for row in exact_frame.itertuples(index=False):
        route = str(row.route_id)
        from_stop = str(row.from_stop)
        to_stop = str(row.to_stop)
        day = str(row.day_type)
        hour = int(row.hour_ist)
        aggregate = (float(row.sum_m), float(row.sum_s), int(row.n))
        exact[(route, from_stop, to_stop, day, hour)] = aggregate
        pair_hours.setdefault((route, from_stop, to_stop, day), {})[hour] = aggregate

    route_hour = {}
    route_day = {}
    flat_route = {}
    for row in route_frame.itertuples(index=False):
        route = str(row.route_id)
        day = str(row.day_type)
        hour = int(row.hour_ist)
        aggregate = (float(row.sum_m), float(row.sum_s), int(row.n))
        if day == "all" and hour == -1:
            flat_route[route] = aggregate
        else:
            route_hour[(route, day, hour)] = aggregate
            route_day.setdefault((route, day), []).append(aggregate)
    free = {
        str(row.route_id): float(row.freeflow_kmh)
        for row in free_frame.itertuples(index=False)
    }
    return exact, pair_hours, route_hour, route_day, flat_route, free


def seconds(aggregate):
    if not aggregate or aggregate[2] <= 0 or aggregate[1] <= 0:
        return None
    return aggregate[1] / aggregate[2]


def day_type(departure):
    if departure.weekday() == 5:
        return "sat"
    if departure.weekday() == 6:
        return "sun"
    return "weekday"


def pair_nearby(pair_hours, key, hour):
    by_hour = pair_hours.get(key)
    if not by_hour:
        return None
    nearest = sorted(
        by_hour,
        key=lambda h: min((h - hour) % 24, (hour - h) % 24),
    )[:2]
    return seconds((
        sum(by_hour[h][0] for h in nearest),
        sum(by_hour[h][1] for h in nearest),
        sum(by_hour[h][2] for h in nearest),
    ))


def estimate_segment(row, models, departure):
    exact, pair_hours, route_hour, route_day, flat_route, free = models
    route = str(row["route_id"])
    a = str(row["from_stop"])
    b = str(row["to_stop"])
    day = day_type(departure)
    hour = departure.hour

    aggregate = exact.get((route, a, b, day, hour))
    if aggregate and aggregate[2] >= 3:
        value = seconds(aggregate)
        if value is not None:
            return value, "exact_hour"

    value = pair_nearby(pair_hours, (route, a, b, day), hour)
    if value is not None:
        return value, "nearby_hours"

    proxy_day = "sun" if day == "sat" else "sat" if day == "sun" else None
    if proxy_day:
        aggregate = exact.get((route, a, b, proxy_day, hour))
        if aggregate and aggregate[2] >= 3:
            value = seconds(aggregate)
            if value is not None:
                return value, "weekend_proxy"
        value = pair_nearby(pair_hours, (route, a, b, proxy_day), hour)
        if value is not None:
            return value, "weekend_proxy"

    aggregate = route_hour.get((route, day, hour))
    value = seconds(aggregate)
    if value is not None:
        return value, "route_wide"
    hourly = route_day.get((route, day), ())
    if hourly:
        value = seconds((
            sum(x[0] for x in hourly),
            sum(x[1] for x in hourly),
            sum(x[2] for x in hourly),
        ))
        if value is not None:
            return value, "route_wide"

    if proxy_day:
        aggregate = route_hour.get((route, proxy_day, hour))
        value = seconds(aggregate)
        if value is not None:
            return value, "weekend_proxy"
        hourly = route_day.get((route, proxy_day), ())
        if hourly:
            value = seconds((
                sum(x[0] for x in hourly),
                sum(x[1] for x in hourly),
                sum(x[2] for x in hourly),
            ))
            if value is not None:
                return value, "weekend_proxy"

    value = seconds(flat_route.get(route))
    if value is not None:
        return value, "flat_route"

    coords_a = STOP_COORDS.get(a)
    coords_b = STOP_COORDS.get(b)
    distance = 1.3 * haversine(coords_a, coords_b) if coords_a and coords_b else 650.0
    speed_kmh = max(free.get(route, 20.0), 1.0)
    return distance / (speed_kmh / 3.6), "freeflow"


def metrics(frame):
    error = frame["estimated_s"] - frame["observed_s"]
    absolute_percent_error = error.abs() / frame["observed_s"].clip(lower=1) * 100
    return {
        "observations": len(frame),
        "mae_seconds": error.abs().mean(),
        "mape_percent": absolute_percent_error.mean(),
        "rmse_seconds": np.sqrt((error ** 2).mean()),
        "median_percent_error": absolute_percent_error.median(),
        "mean_percent_error": (error / frame["observed_s"].clip(lower=1) * 100).mean(),
        "p90_absolute_percent_error": absolute_percent_error.quantile(0.9),
        "bias_seconds": error.mean(),
    }


def valid_segment_sql(path):
    return f"""
        SELECT *
        FROM read_parquet('{path.replace(chr(92), chr(47))}')
        WHERE seg_s BETWEEN 5 AND 900
          AND seg_m > 0
          AND 3.6 * seg_m / seg_s BETWEEN 2 AND 80
    """


def validate_journeys(con, models, held_day, day_type, rng):
    path = os.path.join(BASE, f"segments_{held_day}.parquet")
    eligible = con.execute(
        f"""
        SELECT trip_id, route_id, count(*) AS n
        FROM ({valid_segment_sql(path)}) AS valid_segments
        GROUP BY trip_id, route_id
        HAVING count(*) >= 5
        """
    ).df()
    if len(eligible) > 300:
        selected = eligible.iloc[rng.choice(len(eligible), 300, replace=False)]
    else:
        selected = eligible
    con.register("journey_trip_ids", selected[["trip_id"]])
    all_segments = con.execute(
        f"""
        SELECT segments.*
        FROM ({valid_segment_sql(path)}) AS segments
        JOIN journey_trip_ids USING (trip_id)
        ORDER BY trip_id, t_from
        """
    ).df()

    records = []
    fallback_counts = {level: 0 for level in FALLBACKS}
    for trip in selected.itertuples(index=False):
        trip_segments = all_segments[all_segments["trip_id"] == trip.trip_id]
        if len(trip_segments) < 5:
            continue
        window_size = int(rng.integers(5, min(25, len(trip_segments)) + 1))
        start = int(rng.integers(0, len(trip_segments) - window_size + 1))
        window = trip_segments.iloc[start:start + window_size]
        observed = float(window["seg_s"].sum())
        first_ts = float(window["t_from"].iloc[0])
        departure = datetime.fromtimestamp(first_ts, timezone.utc).astimezone(IST)
        current_time = departure
        estimated = 0.0
        for row in window.to_dict("records"):
            estimate, level = estimate_segment(row, models, current_time)
            estimated += estimate
            fallback_counts[level] += 1
            current_time += timedelta(seconds=estimate)
        records.append({
            "held_out_day": held_day,
            "trip_id": trip.trip_id,
            "route_id": trip.route_id,
            "observed_s": observed,
            "estimated_s": estimated,
            "hour_ist": departure.hour,
            "bucket": "peak" if departure.hour in (7, 8, 9, 10, 17, 18, 19, 20) else "off_peak",
            "day_type": day_type,
            "window_segments": window_size,
        })
    return pd.DataFrame(records), fallback_counts


def main():
    con = duckdb.connect()
    rng = np.random.default_rng(202610)
    direct_folds = []
    journey_folds = []
    direct_fallbacks = {level: 0 for level in FALLBACKS}
    journey_fallbacks = {level: 0 for level in FALLBACKS}

    for held_day, held_type in DAYS.items():
        path = os.path.join(BASE, f"segments_{held_day}.parquet")
        models = load_fold(con, held_day)
        held_segments = con.execute(valid_segment_sql(path)).df()
        if len(held_segments) > 5000:
            held_segments = held_segments.iloc[rng.choice(len(held_segments), 5000, replace=False)]

        direct_rows = []
        for row in held_segments.to_dict("records"):
            departure = datetime.fromtimestamp(
                float(row["t_from"]), timezone.utc
            ).astimezone(IST)
            estimate, level = estimate_segment(row, models, departure)
            direct_rows.append({
                "held_out_day": held_day,
                "observed_s": float(row["seg_s"]),
                "estimated_s": estimate,
                "fallback": level,
            })
            direct_fallbacks[level] += 1
        direct_fold = pd.DataFrame(direct_rows)
        direct_folds.append(direct_fold)

        journeys, fallback_counts = validate_journeys(
            con, models, held_day, held_type, rng
        )
        journey_folds.append(journeys)
        for level, value in fallback_counts.items():
            journey_fallbacks[level] += value
        print(held_day, "direct held-out:", metrics(direct_fold))
        print(held_day, "journey windows:", len(journeys))

    direct = pd.concat(direct_folds, ignore_index=True)
    journeys = pd.concat(journey_folds, ignore_index=True)
    direct_reports = []
    for held_day, frame in direct.groupby("held_out_day"):
        direct_reports.append({"held_out_day": held_day, **metrics(frame)})
    direct_reports.append({"held_out_day": "overall", **metrics(direct)})
    pd.DataFrame(direct_reports).to_csv(
        os.path.join(BASE, "validation_loo_segments.csv"), index=False
    )

    journey_reports = [{"split": "overall", **metrics(journeys)}]
    for split_name, column in (("hour", "bucket"), ("day", "day_type")):
        for value, frame in journeys.groupby(column):
            journey_reports.append({"split": f"{split_name}_{value}", **metrics(frame)})
    pd.DataFrame(journey_reports).to_csv(
        os.path.join(BASE, "validation_journey.csv"), index=False
    )

    fallback_rows = []
    for scope, values in (
        ("held_out_segments", direct_fallbacks),
        ("held_out_journey_windows", journey_fallbacks),
    ):
        total = sum(values.values())
        fallback_rows.extend({
            "scope": scope,
            "fallback": level,
            "count": value,
            "percent": value / max(total, 1) * 100,
        } for level, value in values.items())
    pd.DataFrame(fallback_rows).to_csv(
        os.path.join(BASE, "validation_fallbacks.csv"), index=False
    )

    print("\nHeld-out segment results")
    print(pd.DataFrame(direct_reports).to_string(index=False))
    print("\nHeld-out journey-window results")
    print(pd.DataFrame(journey_reports).to_string(index=False))
    print("\nFallback use")
    print(pd.DataFrame(fallback_rows).to_string(index=False))


if __name__ == "__main__":
    main()
