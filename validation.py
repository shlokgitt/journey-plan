import os

import duckdb
import numpy as np
import pandas as pd


BASE = os.path.dirname(os.path.abspath(__file__))
DAYS = {
    "2026_10_07": "weekday",
    "2026_10_14": "weekday",
    "2026_10_15": "weekday",
    "2026_10_17": "sat",
    "2026_10_18": "sun",
}


def metrics(frame):
    err = frame["estimated_s"] - frame["observed_s"]
    return {
        "observations": len(frame),
        "mae_seconds": err.abs().mean(),
        "mape_percent": (err.abs() / frame["observed_s"].clip(lower=1)).mean() * 100,
        "rmse_seconds": np.sqrt((err ** 2).mean()),
    }


def load_fold(con, held_day):
    tag = f"_loo_{held_day}"
    exact = con.execute(
        f"SELECT * FROM read_parquet('{os.path.join(BASE, 'speed_table' + tag + '.parquet').replace(chr(92), chr(47))}')"
    ).df()
    route = con.execute(
        f"SELECT * FROM read_parquet('{os.path.join(BASE, 'speed_route_table' + tag + '.parquet').replace(chr(92), chr(47))}')"
    ).df()
    free = con.execute(
        f"SELECT * FROM read_parquet('{os.path.join(BASE, 'speed_freeflow_table' + tag + '.parquet').replace(chr(92), chr(47))}')"
    ).df()
    exact_map = {}
    pair_map = {}
    for row in exact.itertuples(index=False):
        key = (str(row.route_id), str(row.from_stop), str(row.to_stop),
               str(row.day_type), int(row.hour_ist))
        exact_map[key] = (float(row.sum_m), float(row.sum_s), int(row.n))
        pair_key = (str(row.route_id), str(row.from_stop), str(row.to_stop), str(row.day_type))
        pair_map.setdefault(pair_key, {})[int(row.hour_ist)] = (
            float(row.sum_m), float(row.sum_s), int(row.n)
        )
    route_map = {}
    for row in route.itertuples(index=False):
        route_map[(str(row.route_id), str(row.day_type), int(row.hour_ist))] = (
            float(row.sum_m), float(row.sum_s), int(row.n)
        )
    free_map = {str(row.route_id): float(row.freeflow_kmh) for row in free.itertuples(index=False)}
    return (exact_map, pair_map, route_map, free_map)


def distance_fallback(row, free):
    kmh = free.get(str(row["route_id"]), 20.0)
    stop_a = stops_xy.get(str(row["from_stop"]))
    stop_b = stops_xy.get(str(row["to_stop"]))
    if stop_a and stop_b:
        metres = haversine(stop_a, stop_b)
    else:
        metres = 500.0
    if kmh <= 0:
        kmh = 20.0
    return metres / (kmh / 3.6)


def haversine(a, b):
    import math
    lat1, lon1, lat2, lon2 = map(math.radians, [a[0], a[1], b[0], b[1]])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    c = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(c), math.sqrt(max(0, 1 - c)))


def estimate_segment(row, exact, pair_lookup, route, free, hour, day_type):
    route_id = str(row["route_id"])
    pair_key = (route_id, str(row["from_stop"]), str(row["to_stop"]), str(day_type))
    exact_pool = exact.get(pair_key + (int(hour),))
    if exact_pool is not None:
        sum_m, sum_s, n = exact_pool
        kmh = sum_m / sum_s * 3.6
        seg_m = sum_m / n
        return seg_m / (kmh / 3.6), "exact_hour"

    pair_hours = pair_lookup.get(pair_key, {})
    if pair_hours:
        nearest = sorted(pair_hours, key=lambda h: abs(h - int(hour)))[:2]
        pool = [pair_hours[h] for h in nearest]
        sum_m = sum(x[0] for x in pool)
        sum_s = sum(x[1] for x in pool)
        n = sum(x[2] for x in pool)
        kmh = sum_m / sum_s * 3.6
        seg_m = sum_m / n
        return seg_m / (kmh / 3.6), "nearby_hours"

    route_hour = route.get((route_id, str(day_type), int(hour)))
    if route_hour is not None:
        sum_m, sum_s, n = route_hour
        kmh = sum_m / sum_s * 3.6
        seg_m = sum_m / n
        return seg_m / (kmh / 3.6), "route_wide"

    route_all = [
        value for key, value in route.items()
        if key[0] == route_id and key[1] == str(day_type)
    ]
    if route_all:
        sum_m = sum(x[0] for x in route_all)
        sum_s = sum(x[1] for x in route_all)
        n = sum(x[2] for x in route_all)
        kmh = sum_m / sum_s * 3.6
        seg_m = sum_m / n
        return seg_m / (kmh / 3.6), "route_wide"

    return distance_fallback(row, free), "freeflow"


def load_stops():
    stops = pd.read_csv(os.path.join(BASE, "gtfs", "stops.txt"), dtype={"stop_id": str})
    return {
        str(row.stop_id): (float(row.stop_lat), float(row.stop_lon))
        for row in stops.itertuples()
    }


stops_xy = load_stops()


def validate_journey(con, exact, pair_lookup, route, free, held_day, day_type, rng):
    path = os.path.join(BASE, f"segments_{held_day}.parquet")
    trip_info = con.execute(
        f"""
        SELECT trip_id, route_id, count(*) AS n
        FROM read_parquet('{path.replace(chr(92), chr(47))}')
        GROUP BY trip_id, route_id
        HAVING n >= 3
        """
    ).df()
    if len(trip_info) > 300:
        trips = trip_info.iloc[rng.choice(len(trip_info), 300, replace=False)]
    else:
        trips = trip_info.head(300)
    trip_ids = trips["trip_id"].tolist()
    con.register("journey_trip_ids", pd.DataFrame({"trip_id": trip_ids}))
    seg_all = con.execute(
        f"""
        SELECT s.*
        FROM read_parquet('{path.replace(chr(92), chr(47))}') s
        JOIN journey_trip_ids q ON s.trip_id = q.trip_id
        WHERE s.seg_s >= 5 AND s.seg_m > 0
        ORDER BY s.trip_id, s.t_from
        """
    ).df()

    records = []
    fallback_counts = {"exact_hour": 0, "nearby_hours": 0, "route_wide": 0, "freeflow": 0}
    for trip in trips.itertuples():
        seg = seg_all[seg_all["trip_id"] == trip.trip_id]
        if len(seg) < 3:
            continue
        observed = seg["seg_s"].sum()
        hour = int((seg["t_from"].median() + 19800) // 3600 % 24)
        estimated = 0.0
        levels = []
        for row in seg.itertuples():
            estimate, level = estimate_segment(row._asdict(), exact, pair_lookup, route, free, hour, day_type)
            estimated += estimate
            fallback_counts[level] += 1
            levels.append(level)
        error = estimated - observed
        records.append({
            "held_out_day": held_day,
            "trip_id": trip.trip_id,
            "route_id": trip.route_id,
            "observed_s": observed,
            "estimated_s": estimated,
            "hour_ist": hour,
            "bucket": "peak" if hour in (7, 8, 9, 10, 17, 18, 19, 20) else "off_peak",
            "day_type": day_type,
        })
    return pd.DataFrame(records), fallback_counts


def main():
    con = duckdb.connect()
    direct_rows = []
    journey_rows = []
    journey_fallback = {"exact_hour": 0, "nearby_hours": 0, "route_wide": 0, "freeflow": 0}
    direct_fallback = {"exact_hour": 0, "nearby_hours": 0, "route_wide": 0, "freeflow": 0}
    rng = np.random.default_rng(202610)

    for held_day, day_type in DAYS.items():
        exact, pair_lookup, route, free = load_fold(con, held_day)
        path = os.path.join(BASE, f"segments_{held_day}.parquet")
        if not os.path.exists(path):
            continue

        seg = con.execute(
            f"""
            SELECT *, CAST(floor((t_from + 19800) / 3600) % 24 AS INTEGER) AS hour_ist
            FROM read_parquet('{path.replace(chr(92), chr(47))}')
            WHERE seg_s >= 5 AND seg_m > 0
              AND 3.6 * seg_m / seg_s <= 80
            """
        ).df()
        if len(seg) > 5000:
            seg = seg.iloc[rng.choice(len(seg), 5000, replace=False)]

        fold_direct = []
        for row in seg.itertuples():
            estimate, level = estimate_segment(row._asdict(), exact, pair_lookup, route, free, row.hour_ist, day_type)
            fold_direct.append({
                "held_out_day": held_day,
                "observed_s": row.seg_s,
                "estimated_s": estimate,
                "fallback": level,
            })
            direct_fallback[level] += 1
        fold_direct = pd.DataFrame(fold_direct)
        direct_rows.append(fold_direct)

        journey, fallback = validate_journey(con, exact, pair_lookup, route, free, held_day, day_type, rng)
        journey_rows.append(journey)
        for level, count in fallback.items():
            journey_fallback[level] += count
        print(held_day, "direct holdout:", metrics(fold_direct))
        print(held_day, "journey holdout:", len(journey), "trips")

    direct = pd.concat(direct_rows, ignore_index=True)
    journeys = pd.concat(journey_rows, ignore_index=True)

    direct_report = direct.groupby("held_out_day").apply(
        lambda g: pd.Series(metrics(g)), include_groups=False
    ).reset_index()
    direct_overall = pd.DataFrame([{ "held_out_day": "overall", **metrics(direct) }])
    direct_report = pd.concat([direct_report, direct_overall], ignore_index=True)
    direct_report.to_csv(os.path.join(BASE, "validation_loo_segments.csv"), index=False)

    journey_report = []
    journey_report.append({
        "split": "overall",
        **journey_metrics(journeys),
    })
    for value, group in journeys.groupby("bucket"):
        journey_report.append({"split": f"hour_{value}", **journey_metrics(group)})
    for value, group in journeys.groupby("day_type"):
        journey_report.append({"split": f"day_{value}", **journey_metrics(group)})
    pd.DataFrame(journey_report).to_csv(os.path.join(BASE, "validation_journey.csv"), index=False)

    pd.DataFrame([
        {"scope": "held_out_segments", "fallback": k, "count": v,
         "percent": v / max(sum(direct_fallback.values()), 1) * 100}
        for k, v in direct_fallback.items()
    ] + [
        {"scope": "held_out_journey", "fallback": k, "count": v,
         "percent": v / max(sum(journey_fallback.values()), 1) * 100}
        for k, v in journey_fallback.items()
    ]).to_csv(os.path.join(BASE, "validation_fallbacks.csv"), index=False)

    print("\nHeld-out segment validation:")
    print(direct_report.to_string(index=False))
    print("\nHeld-out journey validation:")
    print(pd.DataFrame(journey_report).to_string(index=False))
    print("\nFallback use:")
    print(pd.DataFrame([
        {"scope": "held_out_segments", "fallback": k, "count": v,
         "percent": v / max(sum(direct_fallback.values()), 1) * 100}
        for k, v in direct_fallback.items()
    ] + [
        {"scope": "held_out_journey", "fallback": k, "count": v,
         "percent": v / max(sum(journey_fallback.values()), 1) * 100}
        for k, v in journey_fallback.items()
    ]).to_string(index=False))


def journey_metrics(frame):
    if frame.empty:
        return {"observations": 0, "mae_seconds": np.nan,
                "median_percent_error": np.nan}
    err = frame["estimated_s"] - frame["observed_s"]
    pct = (err / frame["observed_s"].clip(lower=1)) * 100
    return {
        "observations": len(frame),
        "mae_seconds": err.abs().mean(),
        "median_percent_error": pct.abs().median(),
    }


if __name__ == "__main__":
    main()
