import os
from datetime import datetime
from heapq import heappop, heappush

import duckdb
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse


BASE = os.path.dirname(os.path.abspath(__file__))
SPEED_TABLE = os.path.join(BASE, "speed_table.parquet")
ROUTE_SPEED_TABLE = os.path.join(BASE, "speed_route_table.parquet")
FREEFLOW_TABLE = os.path.join(BASE, "speed_freeflow_table.parquet")
GTFS = os.path.join(BASE, "gtfs")

app = FastAPI(title="Delhi Bus Journey Planner")
stops = pd.read_csv(os.path.join(GTFS, "stops.txt"), dtype={"stop_id": str})
stops["stop_id"] = stops["stop_id"].astype(str)


def haversine_m(lat1, lon1, lat2, lon2):
    import math
    lat1, lon1, lat2, lon2 = map(math.radians, [lat1, lon1, lat2, lon2])
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0, 1 - a)))


def resolve_stop(value):
    matches = stops[
        stops["stop_id"].str.lower().eq(str(value).lower())
        | stops["stop_name"].str.lower().str.contains(str(value).lower(), na=False)
    ]
    if matches.empty:
        raise HTTPException(404, f"Stop not found: {value}")
    return matches.iloc[0]


def load_graph():
    trips = pd.read_csv(os.path.join(GTFS, "trips.txt"), dtype={"shape_id": str, "trip_id": str})
    times = pd.read_csv(
        os.path.join(GTFS, "stop_times.txt.gz"),
        dtype={"trip_id": str, "stop_id": str},
    )
    rep = (
        times.merge(trips[["trip_id", "shape_id", "route_id"]], on="trip_id")
        .groupby("shape_id")
        .apply(lambda g: g.assign(n=g.groupby("trip_id").transform("size")).sort_values("stop_sequence").iloc[:1])
        if False else None
    )
    # Representative trip: longest stop sequence for each shape.
    longest = times.groupby("trip_id").size().rename("n").reset_index()
    rep_trips = longest.merge(trips[["trip_id", "shape_id", "route_id"]], on="trip_id")
    rep_trips = rep_trips.sort_values("n", ascending=False).drop_duplicates("shape_id")
    rep_times = times.merge(rep_trips[["trip_id", "shape_id", "route_id"]], on="trip_id")
    rep_times = rep_times.sort_values(["shape_id", "stop_sequence"])

    graph = {}
    route_edges = set()
    shape_ids = rep_times["shape_id"].dropna().unique()
    for shape_id in shape_ids:
        seq = rep_times[rep_times["shape_id"] == shape_id].sort_values("stop_sequence")
        route_id = seq["route_id"].iloc[0]
        ids = seq["stop_id"].astype(str).tolist()
        for a, b in zip(ids, ids[1:]):
            if a != b:
                graph.setdefault(a, []).append((b, "ride", 0, str(route_id), a, b))
                route_edges.add((a, b))

    # Add short walk links among all stops within 500 m.
    from scipy.spatial import cKDTree

    walk_kmh = 4.5
    coords = stops.set_index("stop_id")[["stop_lat", "stop_lon"]]
    lat0 = float(coords.stop_lat.mean())
    xy = [[
        (float(row.stop_lon) - float(stops["stop_lon"].mean())) * 111320 * 0.999,
        (float(row.stop_lat) - lat0) * 110540,
    ] for _, row in coords.iterrows()]
    stop_ids = [str(sid) for sid in coords.index]
    tree = cKDTree(xy)
    for i, neighbors in enumerate(tree.query_ball_point(xy, 500)):
        sid = stop_ids[i]
        x0, y0 = xy[i]
        for j in neighbors:
            if i == j:
                continue
            other = stop_ids[j]
            d = ((xy[j][0] - x0) ** 2 + (xy[j][1] - y0) ** 2) ** 0.5
            graph.setdefault(sid, []).append((other, "walk", d, "walk", sid, other))

    return graph


if os.path.exists(SPEED_TABLE):
    con = duckdb.connect()
    speed = con.execute("SELECT * FROM read_parquet(?)", [SPEED_TABLE]).df()
    route_speed = con.execute("SELECT * FROM read_parquet(?)", [ROUTE_SPEED_TABLE]).df()
    freeflow = con.execute("SELECT * FROM read_parquet(?)", [FREEFLOW_TABLE]).df()
    con.close()
else:
    speed = pd.DataFrame()
    route_speed = pd.DataFrame()
    freeflow = pd.DataFrame()

graph = load_graph()
planner_fallback_counts = {
    "exact_hour": 0,
    "nearby_hours": 0,
    "route_wide": 0,
    "freeflow": 0,
}


def day_type(date):
    return "weekday" if date.weekday() < 5 else ("sat" if date.weekday() == 5 else "sun")


def expected_segment_seconds(a, b, date, hour, route_hint=None):
    rows = speed.copy()
    if not rows.empty:
        rows = rows[
            (rows["from_stop"].astype(str) == str(a))
            & (rows["to_stop"].astype(str) == str(b))
            & (rows["day_type"] == day_type(date))
        ]
        if route_hint is not None:
            rows = rows[rows["route_id"].astype(str) == str(route_hint)]

        exact = rows[rows["hour_ist"] == hour]
        if not exact.empty:
            kmh = exact["sum_m"].sum() / exact["sum_s"].sum() * 3.6
            seg_m = exact["sum_m"].sum() / exact["n"].sum()
            return seg_m / (kmh / 3.6), "exact_hour"

        if not rows.empty:
            hours = (rows["hour_ist"] - hour).abs()
            pool = rows.loc[hours.nsmallest(min(2, len(hours))).index]
            kmh = pool["sum_m"].sum() / pool["sum_s"].sum() * 3.6
            seg_m = pool["sum_m"].sum() / pool["n"].sum()
            return seg_m / (kmh / 3.6), "nearby_hours"

    if route_hint is not None and not route_speed.empty:
        route_rows = route_speed[
            (route_speed["route_id"].astype(str) == str(route_hint))
            & (route_speed["day_type"] == day_type(date))
        ]
        route_hour = route_rows[route_rows["hour_ist"] == hour]
        if not route_hour.empty:
            kmh = route_hour["sum_m"].sum() / route_hour["sum_s"].sum() * 3.6
            seg_m = route_hour["sum_m"].sum() / route_hour["n"].sum()
            return seg_m / (kmh / 3.6), "route_wide"
        if not route_rows.empty:
            kmh = route_rows["sum_m"].sum() / route_rows["sum_s"].sum() * 3.6
            seg_m = route_rows["sum_m"].sum() / route_rows["n"].sum()
            return seg_m / (kmh / 3.6), "route_wide"

    stop_lookup = stops.set_index("stop_id")
    if str(a) in stop_lookup.index and str(b) in stop_lookup.index:
        sa = stop_lookup.loc[str(a)]
        sb = stop_lookup.loc[str(b)]
        metres = haversine_m(sa.stop_lat, sa.stop_lon, sb.stop_lat, sb.stop_lon)
    else:
        metres = 500.0
    kmh = 20.0
    if route_hint is not None and not freeflow.empty:
        ff = freeflow[freeflow["route_id"].astype(str) == str(route_hint)]
        if not ff.empty:
            kmh = float(ff.iloc[0]["freeflow_kmh"])
    return metres / (kmh / 3.6), "freeflow"


def shortest(start, goal, date, hour, transfer_penalty):
    state = (0, start, "none")
    pq = [state]
    dist = {(start, "none"): 0}
    previous = {}
    while pq:
        cost, node, last_mode = heappop(pq)
        if cost != dist.get((node, last_mode)):
            continue
        if node == goal:
            path = []
            cur = (node, last_mode)
            while cur in previous:
                edge = previous[cur]
                path.append(edge)
                cur = edge["prev"]
            path.reverse()
            return cost, path
        for nxt, mode, distance, route, a, b in graph.get(node, []):
            if mode == "ride":
                seconds, fallback = expected_segment_seconds(a, b, date, hour, route_hint=route)
                planner_fallback_counts[fallback] += 1
            else:
                seconds = distance / (4.5 * 1000 / 3600)
            transfer = transfer_penalty if last_mode == "ride" and mode == "walk" else 0
            if last_mode != "none" and last_mode != mode and mode == "ride":
                transfer = transfer_penalty
            new_cost = cost + seconds + transfer
            key = (nxt, mode)
            if new_cost < dist.get(key, float("inf")):
                dist[key] = new_cost
                previous[key] = {"prev": (node, last_mode), "mode": mode, "from": node, "to": nxt,
                                 "route": route, "seconds": seconds}
                heappush(pq, (new_cost, nxt, mode))
    return None


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "index.html"))


@app.get("/stops")
def stop_list():
    return {
        "stops": [
            {
                "stop_id": str(row["stop_id"]),
                "stop_name": row["stop_name"],
                "stop_lat": float(row["stop_lat"]),
                "stop_lon": float(row["stop_lon"]),
            }
            for _, row in stops.iterrows()
        ]
    }


@app.get("/journeys")
def journeys(
    from_: str = Query(..., alias="from"),
    to: str = Query(..., alias="to"),
    depart: str = "now",
):
    for level in planner_fallback_counts:
        planner_fallback_counts[level] = 0
    start = resolve_stop(from_)
    goal = resolve_stop(to)
    try:
        if depart == "now":
            dt = datetime.now()
        else:
            dt = datetime.fromisoformat(depart)
    except ValueError:
        raise HTTPException(400, "depart must be ISO time, for example 2026-10-14T18:30")

    hour = dt.hour
    results = []
    seen = set()
    for penalty in (120, 600, 1200):
        found = shortest(str(start["stop_id"]), str(goal["stop_id"]), dt, hour, penalty)
        if not found:
            continue
        cost, path = found
        rides = [e["route"] for e in path if e["mode"] == "ride"]
        signature = tuple(rides)
        if signature in seen:
            continue
        seen.add(signature)
        results.append({
            "minutes": round(cost / 60, 1),
            "transfer_count": max(0, len(set(rides)) - 1),
            "stops": [start["stop_id"]] + [e["to"] for e in path],
            "modes": [e["mode"] for e in path],
            "routes": rides,
        })

    quiet_hour = max(hour, 0)
    for h in (22, 6, 10, 14, 18):
        if h != hour:
            quiet_hour = h
            break
    quiet = shortest(str(start["stop_id"]), str(goal["stop_id"]), dt, quiet_hour, 600)
    quiet_result = None
    if quiet:
        cost, path = quiet
        quiet_result = {"hour_ist": quiet_hour, "minutes": round(cost / 60, 1)}

    if not results:
        raise HTTPException(404, "No journey found")
    return {"from": start["stop_name"], "to": goal["stop_name"], "depart": dt.isoformat(),
            "options": results[:3], "quieter_hour_example": quiet_result,
            "fallback_counts": dict(planner_fallback_counts)}
