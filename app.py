import asyncio
import logging
import math
import os
import statistics
import time
import urllib.error
import urllib.request
from collections import defaultdict, deque
from datetime import datetime, timedelta
from functools import lru_cache
from heapq import heappop, heappush
from itertools import count
from urllib.parse import parse_qsl, quote_plus, urlencode, urlsplit, urlunsplit
from zoneinfo import ZoneInfo

import duckdb
import pandas as pd
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from google.protobuf.message import DecodeError
from google.transit import gtfs_realtime_pb2
from pydantic import BaseModel
from scipy.spatial import cKDTree


try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))
except ImportError:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
GTFS = os.path.join(BASE, "gtfs")
SPEED_TABLE = os.path.join(BASE, "speed_table.parquet")
ROUTE_SPEED_TABLE = os.path.join(BASE, "speed_route_table.parquet")
FREEFLOW_TABLE = os.path.join(BASE, "speed_freeflow_table.parquet")
IST = ZoneInfo("Asia/Kolkata")
WALK_SPEED_MPS = 4.5 * 1000 / 3600
BOARDING_WAIT_SECONDS = 5 * 60
OFFPEAK_HOUR = 14
FALLBACK_LEVELS = (
    "exact_hour",
    "nearby_hours",
    "weekend_proxy",
    "route_wide",
    "flat_route",
    "freeflow",
)
MAX_MODEL_SPEED_MPS = 80 * 1000 / 3600
LIVE_DISTANCE_STEP_M = 250
LIVE_DISTANCE_WEIGHTS = tuple(
    0.5 * math.exp(-(step * LIVE_DISTANCE_STEP_M) / 5000.0)
    for step in range(401)
)
LIVE_FEED_URL = os.getenv(
    "DELHI_OTD_RT_URL",
    "https://otd.delhi.gov.in/api/realtime/VehiclePositions.pb",
)
LIVE_FEED_API_KEY = os.getenv("DELHI_OTD_RT_API_KEY", "").strip()
try:
    LIVE_POLL_SECONDS = int(os.getenv("DELHI_OTD_RT_POLL_SECONDS", "30"))
except ValueError as exc:
    raise RuntimeError("DELHI_OTD_RT_POLL_SECONDS must be an integer") from exc
if not 10 <= LIVE_POLL_SECONDS <= 300:
    raise RuntimeError("DELHI_OTD_RT_POLL_SECONDS must be between 10 and 300")

app = FastAPI(title="Delhi Bus Journey Planner")
logger = logging.getLogger(__name__)


class MapRouteLeg(BaseModel):
    route_id: str
    from_stop_id: str
    to_stop_id: str


class MapRouteRequest(BaseModel):
    legs: list[MapRouteLeg]


stops = pd.read_csv(os.path.join(GTFS, "stops.txt"), dtype={"stop_id": str})
stops["stop_id"] = stops["stop_id"].astype(str)
stop_by_id = {
    str(row.stop_id): {
        "stop_id": str(row.stop_id),
        "stop_name": str(row.stop_name),
        "stop_lat": float(row.stop_lat),
        "stop_lon": float(row.stop_lon),
    }
    for row in stops.itertuples()
}
route_rows = pd.read_csv(os.path.join(GTFS, "routes.txt"), dtype={"route_id": str})
route_short_names = {
    str(row.route_id): str(row.route_short_name)
    for row in route_rows.itertuples()
}


def haversine_m(lat1, lon1, lat2, lon2):
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    dlat = lat2 - lat1
    dlon = lon2 - lon1
    a = math.sin(dlat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(dlon / 2) ** 2
    return 6371000 * 2 * math.atan2(math.sqrt(a), math.sqrt(max(0, 1 - a)))


def project_xy(lat, lon, lat_origin, lon_origin):
    return (
        (lon - lon_origin) * 111320 * math.cos(math.radians(lat_origin)),
        (lat - lat_origin) * 110540,
    )


stop_lat_origin = float(stops["stop_lat"].mean())
stop_lon_origin = float(stops["stop_lon"].mean())
stop_xy = {
    sid: project_xy(stop["stop_lat"], stop["stop_lon"], stop_lat_origin, stop_lon_origin)
    for sid, stop in stop_by_id.items()
}
stop_ids = list(stop_by_id)
stop_tree = cKDTree([stop_xy[sid] for sid in stop_ids])


def load_speed_models():
    if not all(os.path.exists(p) for p in (SPEED_TABLE, ROUTE_SPEED_TABLE, FREEFLOW_TABLE)):
        raise RuntimeError("Travel-time tables are missing; run python build_model.py first.")

    con = duckdb.connect()
    segment_rows = con.execute("SELECT * FROM read_parquet(?)", [SPEED_TABLE]).df()
    route_rows = con.execute("SELECT * FROM read_parquet(?)", [ROUTE_SPEED_TABLE]).df()
    free_rows = con.execute("SELECT * FROM read_parquet(?)", [FREEFLOW_TABLE]).df()
    con.close()

    segment_by_key = {}
    pair_hours = {}
    for row in segment_rows.itertuples(index=False):
        route_id = str(row.route_id)
        from_stop = str(row.from_stop)
        to_stop = str(row.to_stop)
        day = str(row.day_type)
        hour = int(row.hour_ist)
        aggregate = (float(row.sum_m), float(row.sum_s), int(row.n))
        segment_by_key[(route_id, from_stop, to_stop, day, hour)] = aggregate
        pair_hours.setdefault((route_id, from_stop, to_stop, day), {})[hour] = aggregate

    route_by_key = {}
    route_by_day = {}
    route_hours = {}
    route_flat_by_id = {}
    for row in route_rows.itertuples(index=False):
        key = (str(row.route_id), str(row.day_type), int(row.hour_ist))
        aggregate = (float(row.sum_m), float(row.sum_s), int(row.n))
        if key[1:] == ("all", -1):
            route_flat_by_id[key[0]] = aggregate
            continue
        route_by_key[key] = aggregate
        route_by_day.setdefault(key[:2], []).append(aggregate)
        route_hours.setdefault(key[:2], {})[key[2]] = aggregate

    freeflow_by_route = {
        str(row.route_id): float(row.freeflow_kmh)
        for row in free_rows.itertuples(index=False)
    }
    return (
        segment_by_key,
        pair_hours,
        route_by_key,
        route_by_day,
        route_hours,
        route_flat_by_id,
        freeflow_by_route,
    )


(
    segment_by_key,
    pair_hours,
    route_by_key,
    route_by_day,
    route_hours,
    route_flat_by_id,
    freeflow_by_route,
) = load_speed_models()


def load_graph():
    trips = pd.read_csv(
        os.path.join(GTFS, "trips.txt"),
        dtype={"trip_id": str, "shape_id": str, "route_id": str},
    )
    times = pd.read_csv(
        os.path.join(GTFS, "stop_times.txt.gz"),
        dtype={"trip_id": str, "stop_id": str},
    )

    trip_lengths = times.groupby("trip_id").size().rename("n").reset_index()
    representatives = (
        trip_lengths.merge(trips[["trip_id", "shape_id", "route_id"]], on="trip_id")
        .sort_values("n", ascending=False)
        .drop_duplicates("shape_id")
    )
    representative_times = times.merge(
        representatives[["trip_id", "shape_id", "route_id"]],
        on="trip_id",
    ).sort_values(["shape_id", "stop_sequence"])

    graph = {}
    added_ride_edges = set()
    for _, sequence in representative_times.groupby("shape_id", sort=False):
        route_id = str(sequence["route_id"].iloc[0])
        ids = sequence["stop_id"].astype(str).tolist()
        for from_stop, to_stop in zip(ids, ids[1:]):
            key = (from_stop, to_stop, route_id)
            if from_stop != to_stop and key not in added_ride_edges:
                a = stop_by_id[from_stop]
                b = stop_by_id[to_stop]
                metres = haversine_m(
                    a["stop_lat"], a["stop_lon"],
                    b["stop_lat"], b["stop_lon"],
                )
                graph.setdefault(from_stop, []).append(
                    (to_stop, "ride", route_id, metres)
                )
                added_ride_edges.add(key)

    for i, neighbors in enumerate(stop_tree.query_ball_point(
        [stop_xy[sid] for sid in stop_ids], 500
    )):
        from_stop = stop_ids[i]
        a = stop_by_id[from_stop]
        for j in neighbors:
            if i == j:
                continue
            to_stop = stop_ids[j]
            b = stop_by_id[to_stop]
            metres = haversine_m(a["stop_lat"], a["stop_lon"], b["stop_lat"], b["stop_lon"])
            if metres <= 500:
                graph.setdefault(from_stop, []).append(
                    (to_stop, "walk", None, metres)
                )
    return graph


graph = load_graph()
live_previous_positions = {}
live_speed_history = defaultdict(deque)
live_route_speeds = {}
live_vehicle_positions = {}
live_poll_task = None
live_feed_status = {
    "enabled": bool(LIVE_FEED_API_KEY),
    "state": "waiting_for_samples" if LIVE_FEED_API_KEY else "disabled",
    "last_poll_at": None,
    "last_success_at": None,
    "vehicles_seen": 0,
    "routes_with_speed_estimates": 0,
    "error": None,
}


def day_type(departure):
    if departure.weekday() == 5:
        return "sat"
    if departure.weekday() == 6:
        return "sun"
    return "weekday"


def parse_live_feed(payload):
    feed = gtfs_realtime_pb2.FeedMessage()
    feed.ParseFromString(payload)
    if not feed.IsInitialized():
        raise ValueError("GTFS-Realtime feed is missing required fields")
    return feed


def fetch_live_feed():
    if not LIVE_FEED_API_KEY:
        raise RuntimeError("DELHI_OTD_RT_API_KEY is not configured")
    parts = urlsplit(LIVE_FEED_URL)
    query = [(name, value) for name, value in parse_qsl(parts.query) if name != "key"]
    query.append(("key", LIVE_FEED_API_KEY))
    url = urlunsplit((
        parts.scheme,
        parts.netloc,
        parts.path,
        urlencode(query),
        parts.fragment,
    ))
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "DelhiBusJourneyPlanner/1.0"},
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = response.read()
    if not payload:
        raise ValueError("GTFS-Realtime feed returned an empty response")
    return parse_live_feed(payload)


def update_live_speeds(feed):
    global live_vehicle_positions
    now = int(time.time())
    feed_timestamp = (
        int(feed.header.timestamp) if feed.header.HasField("timestamp") else now
    )
    vehicles_seen = 0
    current_positions = {}
    for entity in feed.entity:
        if not entity.HasField("vehicle"):
            continue
        vehicle = entity.vehicle
        if not vehicle.HasField("position"):
            continue
        route_id = (
            str(vehicle.trip.route_id).strip()
            if vehicle.HasField("trip")
            else ""
        )
        vehicle_id = (
            str(vehicle.vehicle.id).strip()
            if vehicle.HasField("vehicle") and vehicle.vehicle.id
            else str(vehicle.trip.trip_id).strip()
            if vehicle.HasField("trip")
            else ""
        )
        if not vehicle_id:
            continue
        position = vehicle.position
        if not (-90 <= position.latitude <= 90 and -180 <= position.longitude <= 180):
            continue
        timestamp = (
            int(vehicle.timestamp) if vehicle.HasField("timestamp") else feed_timestamp
        )
        if timestamp <= 0 or timestamp < now - 180 or timestamp > now + 30:
            continue
        vehicles_seen += 1
        current_positions[vehicle_id] = {
            "vehicle_id": vehicle_id,
            "route_id": route_id or None,
            "latitude": position.latitude,
            "longitude": position.longitude,
            "timestamp": timestamp,
        }
        if not route_id:
            live_previous_positions.pop(vehicle_id, None)
            continue
        previous = live_previous_positions.get(vehicle_id)
        if previous and previous[3] == route_id:
            elapsed = timestamp - previous[2]
            if 10 <= elapsed <= 120:
                distance = haversine_m(
                    previous[0], previous[1], position.latitude, position.longitude
                )
                speed_kmh = distance / elapsed * 3.6
                if distance >= 20 and 0.5 <= speed_kmh <= 80:
                    live_speed_history[route_id].append((timestamp, speed_kmh))
        live_previous_positions[vehicle_id] = (
            position.latitude,
            position.longitude,
            timestamp,
            route_id,
        )

    live_vehicle_positions = current_positions
    cutoff = now - 180
    refreshed_speeds = {}
    for route_id, samples in list(live_speed_history.items()):
        while samples and samples[0][0] < cutoff:
            samples.popleft()
        if not samples:
            del live_speed_history[route_id]
            continue
        if len(samples) >= 3:
            refreshed_speeds[route_id] = (
                statistics.median(speed for _, speed in samples),
                len(samples),
                samples[-1][0],
            )
    live_route_speeds.clear()
    live_route_speeds.update(refreshed_speeds)
    live_feed_status.update({
        "enabled": True,
        "state": "active" if live_route_speeds else "waiting_for_samples",
        "last_poll_at": datetime.now(IST).isoformat(),
        "last_success_at": datetime.now(IST).isoformat(),
        "vehicles_seen": vehicles_seen,
        "routes_with_speed_estimates": len(live_route_speeds),
        "error": None,
    })


async def live_feed_poll_loop():
    while True:
        live_feed_status["last_poll_at"] = datetime.now(IST).isoformat()
        try:
            feed = await asyncio.to_thread(fetch_live_feed)
            update_live_speeds(feed)
        except (
            DecodeError,
            OSError,
            TimeoutError,
            urllib.error.URLError,
            ValueError,
        ) as exc:
            live_feed_status["state"] = "error"
            error = str(exc)
            for secret in (LIVE_FEED_API_KEY, quote_plus(LIVE_FEED_API_KEY)):
                if secret:
                    error = error.replace(secret, "[redacted]")
            live_feed_status["error"] = error
            logger.error("Delhi OTD live-feed poll failed: %s", error)
        await asyncio.sleep(LIVE_POLL_SECONDS)


@app.on_event("startup")
async def start_live_feed():
    global live_poll_task
    if not LIVE_FEED_API_KEY:
        logger.info(
            "Live bus feed disabled; set DELHI_OTD_RT_API_KEY to enable it."
        )
        return
    live_poll_task = asyncio.create_task(live_feed_poll_loop())


@app.on_event("shutdown")
async def stop_live_feed():
    if live_poll_task is None:
        return
    live_poll_task.cancel()
    try:
        await live_poll_task
    except asyncio.CancelledError:
        pass


def historical_route_speed_kmh(route_id, day, hour):
    days = [day]
    if day == "sat":
        days.append("sun")
    elif day == "sun":
        days.append("sat")
    for route_day in days:
        aggregate = route_by_key.get((route_id, route_day, hour))
        if aggregate is not None and aggregate[1] > 0:
            return aggregate[0] / aggregate[1] * 3.6
        route_aggregates = route_by_day.get((route_id, route_day), ())
        if route_aggregates:
            sum_m = sum(row[0] for row in route_aggregates)
            sum_s = sum(row[1] for row in route_aggregates)
            if sum_s > 0:
                return sum_m / sum_s * 3.6
    flat_route = route_flat_by_id.get(route_id)
    if flat_route and flat_route[1] > 0:
        return flat_route[0] / flat_route[1] * 3.6
    return None


def build_live_blend_context(departure):
    if not live_route_speeds:
        return None
    if abs((datetime.now(IST) - departure).total_seconds()) > 900:
        return None

    now = time.time()
    context = {}
    for route_id, (live_speed, sample_count, updated_at) in dict(live_route_speeds).items():
        if now - updated_at > 180:
            continue
        for day in ("weekday", "sat", "sun"):
            for hour in range(24):
                historical_speed = historical_route_speed_kmh(route_id, day, hour)
                if not historical_speed or historical_speed <= 0:
                    continue
                ratio = live_speed / historical_speed
                context[(route_id, day, hour)] = (
                    live_speed,
                    historical_speed,
                    ratio,
                    max(0.6, min(1.4, ratio)),
                    sample_count,
                )
    return context or None


def seconds_from_aggregate(aggregate):
    sum_m, sum_s, n = aggregate
    if n <= 0 or sum_m <= 0 or sum_s <= 0:
        return None
    return sum_s / n


def nearest_pair_seconds(key, hour):
    hours = pair_hours.get(key)
    if not hours:
        return None
    chosen_hours = sorted(hours, key=lambda value: min(
        (value - hour) % 24, (hour - value) % 24
    ))[:2]
    sum_m = sum(hours[h][0] for h in chosen_hours)
    sum_s = sum(hours[h][1] for h in chosen_hours)
    n = sum(hours[h][2] for h in chosen_hours)
    return seconds_from_aggregate((sum_m, sum_s, n))


def estimate_ride_seconds(
    from_stop,
    to_stop,
    route_id,
    departure,
    distance_ahead_m=0.0,
    live_context=None,
    include_live_details=False,
):
    seconds, fallback = _estimate_ride_seconds(
        str(route_id),
        str(from_stop),
        str(to_stop),
        day_type(departure),
        departure.hour,
    )
    route_id = str(route_id)
    if live_context is None:
        return seconds, fallback, None
    live = live_context.get((route_id, day_type(departure), departure.hour))
    if live is None:
        return seconds, fallback, None
    live_speed, historical_speed, ratio, bounded_ratio, sample_count = live
    distance_step = min(
        int(max(0.0, distance_ahead_m) / LIVE_DISTANCE_STEP_M),
        len(LIVE_DISTANCE_WEIGHTS) - 1,
    )
    weight = LIVE_DISTANCE_WEIGHTS[distance_step]
    speed_scale = 1.0 + weight * (bounded_ratio - 1.0)
    adjusted_seconds = seconds / speed_scale
    if not include_live_details:
        return adjusted_seconds, fallback, None
    return adjusted_seconds, fallback, {
        "live_speed_kmh": round(live_speed, 1),
        "historical_speed_kmh": round(historical_speed, 1),
        "speed_ratio": round(ratio, 3),
        "applied_speed_ratio": round(bounded_ratio, 3),
        "blend_weight": round(weight, 3),
        "sample_count": sample_count,
    }


def estimate_search_ride_seconds(
    from_stop,
    to_stop,
    route_id,
    day,
    hour,
    distance_ahead_m,
    live_context,
):
    seconds, fallback = _estimate_ride_seconds(
        route_id, from_stop, to_stop, day, hour
    )
    if live_context is None:
        return seconds, fallback
    live = live_context.get((route_id, day, hour))
    if live is None:
        return seconds, fallback
    bounded_ratio = live[3]
    distance_step = min(
        int(max(0.0, distance_ahead_m) / LIVE_DISTANCE_STEP_M),
        len(LIVE_DISTANCE_WEIGHTS) - 1,
    )
    weight = LIVE_DISTANCE_WEIGHTS[distance_step]
    speed_scale = 1.0 + weight * (bounded_ratio - 1.0)
    return seconds / speed_scale, fallback


@lru_cache(maxsize=500000)
def _estimate_ride_seconds(route_id, from_stop, to_stop, day, hour):
    key = (route_id, from_stop, to_stop, day, hour)
    aggregate = segment_by_key.get(key)
    if aggregate is not None:
        seconds = seconds_from_aggregate(aggregate)
        if seconds is not None:
            return seconds, "exact_hour"

    pair_key = (route_id, from_stop, to_stop, day)
    seconds = nearest_pair_seconds(pair_key, hour)
    if seconds is not None:
        return seconds, "nearby_hours"

    proxy_day = "sun" if day == "sat" else "sat" if day == "sun" else None
    if proxy_day:
        proxy_pair_key = (route_id, from_stop, to_stop, proxy_day)
        proxy_aggregate = segment_by_key.get(
            (route_id, from_stop, to_stop, proxy_day, hour)
        )
        if proxy_aggregate is not None and proxy_aggregate[2] >= 3:
            seconds = seconds_from_aggregate(proxy_aggregate)
            if seconds is not None:
                return seconds, "weekend_proxy"
        seconds = nearest_pair_seconds(proxy_pair_key, hour)
        if seconds is not None:
            return seconds, "weekend_proxy"

    route_aggregate = route_by_key.get((route_id, day, hour))
    if route_aggregate is not None:
        seconds = seconds_from_aggregate(route_aggregate)
        if seconds is not None:
            return seconds, "route_wide"

    def nearby_route_seconds(route_day):
        hours = route_hours.get(route_day, {})
        if not hours:
            return None
        closest = sorted(
            hours,
            key=lambda h: min((h - hour) % 24, (hour - h) % 24),
        )[:2]
        aggregate = (
            sum(hours[h][0] for h in closest),
            sum(hours[h][1] for h in closest),
            sum(hours[h][2] for h in closest),
        )
        return seconds_from_aggregate(aggregate)

    if route_aggregate is None:
        seconds = nearby_route_seconds((route_id, day))
        if seconds is not None:
            return seconds, "route_wide"

    all_route_aggregates = route_by_day.get((route_id, day), ())
    if all_route_aggregates:
        sum_m = sum(x[0] for x in all_route_aggregates)
        sum_s = sum(x[1] for x in all_route_aggregates)
        n = sum(x[2] for x in all_route_aggregates)
        seconds = seconds_from_aggregate((sum_m, sum_s, n))
        if seconds is not None:
            return seconds, "route_wide"

    if proxy_day:
        proxy_route_aggregate = route_by_key.get((route_id, proxy_day, hour))
        if proxy_route_aggregate is not None:
            seconds = seconds_from_aggregate(proxy_route_aggregate)
            if seconds is not None:
                return seconds, "weekend_proxy"
        seconds = nearby_route_seconds((route_id, proxy_day))
        if seconds is not None:
            return seconds, "weekend_proxy"
        proxy_route_rows = route_by_day.get((route_id, proxy_day), ())
        if proxy_route_rows:
            sum_m = sum(x[0] for x in proxy_route_rows)
            sum_s = sum(x[1] for x in proxy_route_rows)
            n = sum(x[2] for x in proxy_route_rows)
            seconds = seconds_from_aggregate((sum_m, sum_s, n))
            if seconds is not None:
                return seconds, "weekend_proxy"

    flat_route = route_flat_by_id.get(route_id)
    if flat_route is not None:
        seconds = seconds_from_aggregate(flat_route)
        if seconds is not None:
            return seconds, "flat_route"

    a = stop_by_id.get(from_stop)
    b = stop_by_id.get(to_stop)
    if a and b:
        metres = 1.3 * haversine_m(
            a["stop_lat"], a["stop_lon"], b["stop_lat"], b["stop_lon"]
        )
    else:
        metres = 500.0
    speed_kmh = freeflow_by_route.get(route_id, 20.0)
    speed_kmh = max(speed_kmh, 1.0)
    return metres / (speed_kmh / 3.6), "freeflow"


def point_candidates(lat, lon):
    xy = project_xy(lat, lon, stop_lat_origin, stop_lon_origin)
    indexes = stop_tree.query_ball_point(xy, 500)
    candidates = []
    for index in indexes:
        sid = stop_ids[index]
        stop = stop_by_id[sid]
        metres = haversine_m(lat, lon, stop["stop_lat"], stop["stop_lon"])
        if metres <= 500:
            candidates.append((sid, metres))
    candidates.sort(key=lambda item: item[1])
    return candidates


def resolve_endpoint(value):
    value = value.strip()
    if not value:
        raise HTTPException(400, "from and to must not be empty")

    if "," in value:
        parts = value.split(",")
        if len(parts) == 2:
            try:
                lat, lon = (float(part.strip()) for part in parts)
            except ValueError:
                pass
            else:
                if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                    raise HTTPException(400, "Coordinates must be lat,lng in valid ranges")
                candidates = point_candidates(lat, lon)
                if not candidates:
                    raise HTTPException(404, "No GTFS stops within 500 m of the coordinates")
                return {
                    "label": value,
                    "candidates": candidates,
                    "is_point": True,
                    "target_points": [(lat, lon)],
                }

    if value in stop_by_id:
        return {
            "label": stop_by_id[value]["stop_name"],
            "candidates": [(value, 0.0)],
            "is_point": False,
            "target_points": [(
                stop_by_id[value]["stop_lat"],
                stop_by_id[value]["stop_lon"],
            )],
        }

    exact = stops[stops["stop_name"].str.casefold() == value.casefold()]
    if not exact.empty:
        matched = exact
    else:
        contains = stops["stop_name"].str.contains(value, case=False, regex=False, na=False)
        matched = stops[contains]
        if matched.empty:
            terms = value.casefold().split()
            token_matches = stops["stop_name"].fillna("").map(
                lambda name: all(term in name.casefold().split() for term in terms)
            )
            matched = stops[token_matches]
    if matched.empty:
        raise HTTPException(404, f"Stop not found: {value}")
    candidate_ids = [str(row.stop_id) for row in matched.itertuples()]
    return {
        "label": value,
        "candidates": [(sid, 0.0) for sid in candidate_ids],
        "is_point": False,
        "target_points": [
            (stop_by_id[sid]["stop_lat"], stop_by_id[sid]["stop_lon"])
            for sid in candidate_ids
        ],
    }


def walk_seconds(metres):
    return metres / WALK_SPEED_MPS


def find_paths(
    origin,
    destination,
    departure,
    transfer_penalty,
    limit=3,
    live_context=None,
):
    destination_distances = dict(destination["candidates"])
    departure_date = departure.date()
    departure_day = day_type(departure)
    best_cost = {}
    elapsed_at = {}
    distance_at = {}
    initial_walk_m = {}
    previous = {}
    queue = []
    sequence = count()

    target_xy = [
        project_xy(lat, lon, stop_lat_origin, stop_lon_origin)
        for lat, lon in destination["target_points"]
    ]
    heuristic = {
        stop_id: min(
            math.hypot(stop_xy[stop_id][0] - x, stop_xy[stop_id][1] - y)
            for x, y in target_xy
        ) * 0.97 / MAX_MODEL_SPEED_MPS
        for stop_id in stop_ids
    }
    origin_xy = [stop_xy[sid] for sid, _ in origin["candidates"]]
    corridor_segments = [
        (start, target)
        for start in origin_xy
        for target in target_xy
    ]
    direct_distance = min(
        math.hypot(start[0] - target[0], start[1] - target[1])
        for start, target in corridor_segments
    )
    corridor_width = max(1500.0, direct_distance * 0.075)
    in_corridor = {}
    for stop_id in stop_ids:
        x, y = stop_xy[stop_id]
        minimum = float("inf")
        for start, target in corridor_segments:
            dx = target[0] - start[0]
            dy = target[1] - start[1]
            length_squared = dx * dx + dy * dy
            if length_squared == 0:
                distance = math.hypot(x - start[0], y - start[1])
            else:
                fraction = max(
                    0.0,
                    min(1.0, ((x - start[0]) * dx + (y - start[1]) * dy) / length_squared),
                )
                distance = math.hypot(
                    x - (start[0] + fraction * dx),
                    y - (start[1] + fraction * dy),
                )
            minimum = min(minimum, distance)
        in_corridor[stop_id] = minimum <= corridor_width

    for stop_id, metres in origin["candidates"]:
        state = (stop_id, None)
        seconds = walk_seconds(metres)
        if in_corridor[stop_id] and seconds < best_cost.get(state, float("inf")):
            best_cost[state] = seconds
            elapsed_at[state] = seconds
            distance_at[state] = 0.0
            initial_walk_m[state] = metres
            heappush(
                queue,
                (seconds + heuristic[stop_id], next(sequence), seconds, state),
            )

    goals = []
    goal_states = set()
    while queue:
        priority, _, score, state = heappop(queue)
        if score != best_cost.get(state):
            continue
        if len(goals) >= limit and priority >= goals[-1][0]:
            break
        stop_id, current_route = state
        elapsed = elapsed_at[state]

        if stop_id in destination_distances:
            final_metres = destination_distances[stop_id]
            final_seconds = walk_seconds(final_metres)
            terminal_score = score + final_seconds
            if state not in goal_states:
                goals.append((terminal_score, state, final_metres))
                goals.sort(key=lambda result: result[0])
                goals = goals[:limit]
                goal_states.add(state)
            continue

        for next_stop, mode, route_id, metres in graph.get(stop_id, ()):
            if not in_corridor[next_stop]:
                continue
            edge_wait = 0.0
            edge_transfer_penalty = 0.0
            fallback = None
            if mode == "walk":
                edge_seconds = walk_seconds(metres)
                next_route = current_route
            else:
                next_route = route_id
                if next_route != current_route:
                    edge_wait = BOARDING_WAIT_SECONDS
                    if current_route is not None:
                        edge_transfer_penalty = transfer_penalty
                segment_departure = departure + timedelta(seconds=elapsed + edge_wait)
                segment_day = (
                    departure_day
                    if segment_departure.date() == departure_date
                    else day_type(segment_departure)
                )
                edge_seconds, fallback = estimate_search_ride_seconds(
                    stop_id,
                    next_stop,
                    route_id,
                    segment_day,
                    segment_departure.hour,
                    distance_at[state],
                    live_context,
                )

            next_state = (next_stop, next_route)
            new_elapsed = elapsed + edge_wait + edge_seconds
            new_score = score + edge_wait + edge_seconds + edge_transfer_penalty
            if new_score < best_cost.get(next_state, float("inf")):
                best_cost[next_state] = new_score
                elapsed_at[next_state] = new_elapsed
                distance_at[next_state] = distance_at[state] + metres
                previous[next_state] = {
                    "prev": state,
                    "from_stop": stop_id,
                    "to_stop": next_stop,
                    "mode": mode,
                    "route_id": route_id,
                    "metres": metres,
                    "fallback": fallback,
                }
                heappush(
                    queue,
                    (
                        new_score + heuristic[next_stop],
                        next(sequence),
                        new_score,
                        next_state,
                    ),
                )

    results = []
    for terminal_score, state, final_metres in goals:
        path = []
        cursor = state
        while cursor in previous:
            edge = previous[cursor]
            path.append(edge)
            cursor = edge["prev"]
        path.reverse()
        results.append({
            "path": path,
            "initial_walk_m": initial_walk_m.get(cursor, 0.0),
            "final_walk_m": final_metres,
            "search_cost_seconds": terminal_score,
        })
    return results


def evaluate_path(path_result, origin, destination, departure, live_context=None):
    path = path_result["path"]
    initial_m = path_result["initial_walk_m"]
    final_m = path_result["final_walk_m"]
    elapsed = walk_seconds(initial_m)
    distance_ahead_m = initial_m
    walk_total = elapsed
    ride_total = 0.0
    walk_distance_total = initial_m
    ride_distance_total = 0.0
    wait_total = 0.0
    transfer_count = 0
    transfer_stops = []
    fallback_counts = {level: 0 for level in FALLBACK_LEVELS}
    live_blend_routes = set()
    live_blended_segments = 0
    legs = []
    destination_stop_ids = {sid for sid, _ in destination["candidates"]}
    last_ride_index = max(
        (index for index, edge in enumerate(path) if edge["mode"] == "ride"),
        default=-1,
    )

    if initial_m > 0:
        first_stop = stop_by_id[candidate_start_stop(path_result, origin)]
        legs.append({
            "type": "walk",
            "purpose": "to_first_stop",
            "from": origin["label"],
            "to": first_stop["stop_name"],
            "distance_m": round(initial_m, 1),
            "minutes": round(elapsed / 60, 3),
        })

    current_route = None
    i = 0
    while i < len(path):
        edge = path[i]
        if edge["mode"] == "walk":
            seconds = walk_seconds(edge["metres"])
            walk_total += seconds
            walk_distance_total += edge["metres"]
            elapsed += seconds
            distance_ahead_m += edge["metres"]
            is_egress = (
                i > last_ride_index and edge["to_stop"] in destination_stop_ids
            )
            legs.append({
                "type": "walk",
                "purpose": "from_last_stop" if is_egress else "transfer",
                "from": stop_by_id[edge["from_stop"]]["stop_name"],
                "to": (
                    destination["label"]
                    if is_egress
                    else stop_by_id[edge["to_stop"]]["stop_name"]
                ),
                "distance_m": round(edge["metres"], 1),
                "minutes": round(seconds / 60, 3),
            })
            i += 1
            continue

        route_id = str(edge["route_id"])
        board_stop = edge["from_stop"]
        boarding_wait = 0.0
        if route_id != current_route:
            boarding_wait = BOARDING_WAIT_SECONDS
            wait_total += boarding_wait
            elapsed += boarding_wait
            if current_route is not None:
                transfer_count += 1
                transfer_stops.append(stop_by_id[board_stop]["stop_name"])
            current_route = route_id

        ride_seconds = 0.0
        ride_distance = 0.0
        leg_fallbacks = {level: 0 for level in FALLBACK_LEVELS}
        leg_live_adjustments = []
        alight_stop = edge["to_stop"]
        while i < len(path) and path[i]["mode"] == "ride" and str(path[i]["route_id"]) == route_id:
            ride_edge = path[i]
            segment_departure = departure + timedelta(seconds=elapsed)
            seconds, fallback, live_adjustment = estimate_ride_seconds(
                ride_edge["from_stop"],
                ride_edge["to_stop"],
                route_id,
                segment_departure,
                distance_ahead_m=distance_ahead_m,
                live_context=live_context,
                include_live_details=True,
            )
            if live_adjustment is not None:
                live_blended_segments += 1
                live_blend_routes.add(route_id)
                leg_live_adjustments.append(live_adjustment)
            ride_seconds += seconds
            ride_distance += ride_edge["metres"]
            elapsed += seconds
            distance_ahead_m += ride_edge["metres"]
            fallback_counts[fallback] += 1
            leg_fallbacks[fallback] += 1
            alight_stop = ride_edge["to_stop"]
            i += 1

        ride_total += ride_seconds
        ride_distance_total += ride_distance
        legs.append({
            "type": "ride",
            "board_stop_id": board_stop,
            "board_stop": stop_by_id[board_stop]["stop_name"],
            "route_id": route_id,
            "route_short_name": route_short_names.get(route_id, route_id),
            "alight_stop_id": alight_stop,
            "alight_stop": stop_by_id[alight_stop]["stop_name"],
            "distance_m": round(ride_distance, 1),
            "ride_minutes": round(ride_seconds / 60, 3),
            "boarding_wait_minutes": round(boarding_wait / 60, 2),
            "fallback_usage": {k: v for k, v in leg_fallbacks.items() if v},
            "live_adjustment": leg_live_adjustments[0] if leg_live_adjustments else None,
        })

    if final_m > 0:
        last_stop_id = path[-1]["to_stop"] if path else candidate_start_stop(path_result, origin)
        final_seconds = walk_seconds(final_m)
        walk_total += final_seconds
        walk_distance_total += final_m
        elapsed += final_seconds
        legs.append({
            "type": "walk",
            "purpose": "from_last_stop",
            "from": stop_by_id[last_stop_id]["stop_name"],
            "to": destination["label"],
            "distance_m": round(final_m, 1),
            "minutes": round(final_seconds / 60, 3),
        })

    ride_minutes = round(sum(
        leg["ride_minutes"] for leg in legs if leg["type"] == "ride"
    ), 3)
    walk_minutes = round(sum(
        leg["minutes"] for leg in legs if leg["type"] == "walk"
    ), 3)
    wait_minutes = round(sum(
        leg["boarding_wait_minutes"] for leg in legs if leg["type"] == "ride"
    ), 2)
    ride_and_walk_minutes = round(ride_minutes + walk_minutes, 3)
    with_wait_minutes = round(ride_and_walk_minutes + wait_minutes, 3)
    return {
        "legs": legs,
        "minutes": ride_and_walk_minutes,
        "ride_minutes": ride_minutes,
        "walk_minutes": walk_minutes,
        "ride_distance_m": round(ride_distance_total, 1),
        "walk_distance_m": round(walk_distance_total, 1),
        "total_distance_m": round(ride_distance_total + walk_distance_total, 1),
        "wait_estimate_minutes": wait_minutes,
        "wait_estimate_label": "Estimated 5 minutes per boarding",
        "ride_and_walk_minutes": ride_and_walk_minutes,
        "total_minutes": ride_and_walk_minutes,
        "including_wait_estimate_minutes": with_wait_minutes,
        "transfer_count": transfer_count,
        "transfer_stops": transfer_stops,
        "fallback_usage": {k: v for k, v in fallback_counts.items() if v},
        "live_blend_usage": {
            "segments_adjusted": live_blended_segments,
            "routes": sorted(live_blend_routes),
        },
    }


def candidate_start_stop(path_result, origin):
    if path_result["path"]:
        return path_result["path"][0]["from_stop"]
    return origin["candidates"][0][0]


def path_signature(path_result):
    return tuple(
        (edge["mode"], edge["from_stop"], edge["to_stop"], edge["route_id"])
        for edge in path_result["path"]
    )


@app.get("/")
def index():
    return FileResponse(os.path.join(BASE, "index.html"))


def _stop_search_score(stop_name_lower: str, query: str) -> tuple:
    """Return a sort key so that better matches come first.

    Priority (lower is better):
      0  exact match  (full name equals query)
      1  starts-with  (name begins with the query)
      2  word-starts-with  (a word inside the name begins with the query)
      3  substring    (query appears anywhere in the name)

    Within the same priority, shorter names come first (more specific),
    and ties are broken alphabetically for a stable ordering.
    """
    if stop_name_lower == query:
        return (0, len(stop_name_lower), stop_name_lower)
    if stop_name_lower.startswith(query):
        return (1, len(stop_name_lower), stop_name_lower)
    words = stop_name_lower.replace("/", " ").replace("-", " ").split()
    if any(word.startswith(query) for word in words):
        return (2, len(stop_name_lower), stop_name_lower)
    return (3, len(stop_name_lower), stop_name_lower)


@app.get("/stops")
def stop_list(
    search: str | None = Query(None, max_length=100),
):
    available_stops = list(stop_by_id.values())
    if search is None:
        return {"stops": available_stops}

    query = search.strip().casefold()
    if not query:
        return {"stops": [], "count": 0, "total_matches": 0, "truncated": False}

    exact_matches = [
        stop for stop in available_stops
        if stop["stop_name"].casefold() == query
    ]
    if exact_matches:
        matches = exact_matches
    else:
        matches = [
            stop for stop in available_stops
            if query in stop["stop_name"].casefold()
        ]
        # Sort by relevance: exact > starts-with > word-starts-with > substring,
        # then shorter names first, then alphabetical.
        matches.sort(key=lambda stop: _stop_search_score(stop["stop_name"].casefold(), query))

    return {
        "stops": matches,
        "count": len(matches),
        "total_matches": len(matches),
        "truncated": False,
    }


@app.get("/live-status")
def get_live_status():
    status = dict(live_feed_status)
    status["poll_interval_seconds"] = LIVE_POLL_SECONDS
    return status


@app.get("/live-vehicles")
def get_live_vehicles(
    west: float = Query(..., ge=-180, le=180),
    south: float = Query(..., ge=-90, le=90),
    east: float = Query(..., ge=-180, le=180),
    north: float = Query(..., ge=-90, le=90),
    limit: int = Query(1500, ge=1, le=5000),
):
    if west > east or south > north:
        raise HTTPException(400, "west/south must not exceed east/north")

    now = int(time.time())
    positions = live_vehicle_positions
    visible = [
        vehicle
        for vehicle in positions.values()
        if now - 180 <= vehicle["timestamp"] <= now + 30
        and west <= vehicle["longitude"] <= east
        and south <= vehicle["latitude"] <= north
    ]
    visible.sort(key=lambda vehicle: vehicle["timestamp"], reverse=True)
    return {
        "as_of": datetime.now(IST).isoformat(),
        "count": min(len(visible), limit),
        "truncated": len(visible) > limit,
        "vehicles": visible[:limit],
    }


@lru_cache(maxsize=1)
def route_shape_ids():
    trips = pd.read_csv(
        os.path.join(GTFS, "trips.txt"),
        usecols=["route_id", "shape_id"],
        dtype={"route_id": str, "shape_id": str},
    ).dropna()
    result = {}
    for route_id, shape_id in trips.itertuples(index=False, name=None):
        result.setdefault(route_id, set()).add(shape_id)
    return {route_id: tuple(shape_ids) for route_id, shape_ids in result.items()}


@app.post("/map-route-shapes")
def get_map_route_shapes(request: MapRouteRequest):
    legs = request.legs
    if len(legs) > 12:
        raise HTTPException(400, "A map request can include at most 12 bus legs")
    for leg in legs:
        if leg.from_stop_id not in stop_by_id or leg.to_stop_id not in stop_by_id:
            raise HTTPException(404, "A requested route stop was not found")

    shapes_by_route = route_shape_ids()
    wanted_shape_ids = {
        shape_id
        for leg in legs
        for shape_id in shapes_by_route.get(leg.route_id, ())
    }
    points_by_shape = defaultdict(list)
    if wanted_shape_ids:
        shape_path = os.path.join(GTFS, "shapes.txt")
        if not os.path.exists(shape_path):
            raise HTTPException(503, "GTFS shapes.txt is required to draw route geometry")
        for frame in pd.read_csv(
            shape_path,
            usecols=["shape_id", "shape_pt_lat", "shape_pt_lon", "shape_pt_sequence"],
            dtype={"shape_id": str},
            chunksize=100_000,
        ):
            selected = frame[frame["shape_id"].isin(wanted_shape_ids)]
            for row in selected.itertuples(index=False):
                points_by_shape[row.shape_id].append(
                    (int(row.shape_pt_sequence), float(row.shape_pt_lat), float(row.shape_pt_lon))
                )

    results = []
    for leg in legs:
        start = stop_by_id[leg.from_stop_id]
        end = stop_by_id[leg.to_stop_id]
        best = None
        best_score = float("inf")
        for shape_id in shapes_by_route.get(leg.route_id, ()):
            points = sorted(points_by_shape.get(shape_id, ()))
            if len(points) < 2:
                continue
            start_index, start_point = min(
                enumerate(points),
                key=lambda item: haversine_m(
                    start["stop_lat"], start["stop_lon"], item[1][1], item[1][2]
                ),
            )
            end_index, end_point = min(
                enumerate(points),
                key=lambda item: haversine_m(
                    end["stop_lat"], end["stop_lon"], item[1][1], item[1][2]
                ),
            )
            start_distance = haversine_m(
                start["stop_lat"], start["stop_lon"], start_point[1], start_point[2]
            )
            end_distance = haversine_m(
                end["stop_lat"], end["stop_lon"], end_point[1], end_point[2]
            )
            score = start_distance + end_distance
            if max(start_distance, end_distance) <= 1000 and score < best_score:
                best_score = score
                best = (shape_id, points, start_index, end_index)

        if best is None:
            coordinates = [
                [start["stop_lat"], start["stop_lon"]],
                [end["stop_lat"], end["stop_lon"]],
            ]
            selected_shape_id = None
            approximate = True
        else:
            selected_shape_id, points, start_index, end_index = best
            step = 1 if start_index <= end_index else -1
            shape_segment = points[start_index:end_index + step:step]
            coordinates = [[start["stop_lat"], start["stop_lon"]]]
            coordinates.extend([[point[1], point[2]] for point in shape_segment])
            coordinates.append([end["stop_lat"], end["stop_lon"]])
            approximate = False

        results.append({
            "route_id": leg.route_id,
            "from_stop_id": leg.from_stop_id,
            "to_stop_id": leg.to_stop_id,
            "shape_id": selected_shape_id,
            "approximate": approximate,
            "coordinates": coordinates,
        })
    return {"routes": results}


@app.get("/journeys")
def journeys(
    from_: str = Query(..., alias="from"),
    to: str = Query(..., alias="to"),
    depart: str = "now",
):
    started = time.perf_counter()
    origin = resolve_endpoint(from_)
    destination = resolve_endpoint(to)
    try:
        departure = datetime.now(IST) if depart == "now" else datetime.fromisoformat(depart)
    except ValueError as exc:
        raise HTTPException(400, "depart must be ISO time, e.g. 2026-11-24T08:30") from exc
    if departure.tzinfo is None:
        departure = departure.replace(tzinfo=IST)
    else:
        departure = departure.astimezone(IST)

    live_context = build_live_blend_context(departure)
    path_results = find_paths(
        origin,
        destination,
        departure,
        300,
        limit=2,
        live_context=live_context,
    )
    unique_paths = []
    seen = set()
    for path_result in path_results:
        signature = path_signature(path_result)
        if signature not in seen:
            seen.add(signature)
            unique_paths.append(path_result)
    path_results = unique_paths

    options = []
    for path_result in path_results:
        evaluated = evaluate_path(
            path_result, origin, destination, departure, live_context=live_context
        )

        quiet_departure = departure.replace(
            hour=OFFPEAK_HOUR, minute=0, second=0, microsecond=0
        )
        quiet_eval = evaluate_path(
            path_result, origin, destination, quiet_departure, live_context=None
        )
        evaluated["quieter_hour_comparison"] = {
            "label": f"{OFFPEAK_HOUR:02d}:00 IST (off-peak)",
            "depart": quiet_departure.isoformat(),
            "ride_minutes": quiet_eval["ride_minutes"],
            "walk_minutes": quiet_eval["walk_minutes"],
            "wait_estimate_minutes": quiet_eval["wait_estimate_minutes"],
            "minutes": quiet_eval["minutes"],
            "total_minutes": quiet_eval["total_minutes"],
            "including_wait_estimate_minutes": quiet_eval["including_wait_estimate_minutes"],
            "fallback_usage": quiet_eval["fallback_usage"],
            "live_blend_usage": quiet_eval["live_blend_usage"],
        }
        options.append(evaluated)
        if len(options) == 2:
            break

    if not options:
        raise HTTPException(404, "No journey found")

    return {
        "from": origin["label"],
        "to": destination["label"],
        "depart": departure.isoformat(),
        "timezone": "Asia/Kolkata",
        "total_time_definition": (
            "minutes/total_minutes = ride time + walking time. Boarding wait is "
            "reported separately; including_wait_estimate_minutes adds it. Transfer "
            "penalties affect route search only."
        ),
        "live_feed": dict(live_feed_status),
        "options": options,
        "query_seconds": round(time.perf_counter() - started, 3),
    }
