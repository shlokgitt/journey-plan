
import duckdb, pandas as pd, numpy as np, os, time

N = 2000  # trips to process; raise once it works
base = os.path.dirname(os.path.abspath(__file__))
g = os.path.join(base, "gtfs")
pq = os.path.join(base, "day_2026_10_15.parquet")
out = os.path.join(base, "segments_2026_10_15.parquet")
p_trips = os.path.join(g, "trips.txt")
p_stimes = os.path.join(g, "stop_times.txt")

lat0, lon0 = 28.6, 77.2
def xy(lat, lon):
    return np.column_stack([(lon - lon0) * 111320 * np.cos(np.radians(lat0)),
                            (lat - lat0) * 110540])

def snap(P, S):
    A, B = S[:-1], S[1:]
    AB = B - A
    L2 = np.maximum((AB ** 2).sum(1), 1e-9)
    seglen = np.sqrt(L2)
    cum = np.concatenate([[0], np.cumsum(seglen)])
    AP = P[:, None, :] - A[None]
    t = np.clip((AP * AB[None]).sum(2) / L2[None], 0, 1)
    proj = A[None] + t[..., None] * AB[None]
    d = np.sqrt(((P[:, None, :] - proj) ** 2).sum(2))
    j = d.argmin(1)
    i = np.arange(len(P))
    return d[i, j], cum[j] + t[i, j] * seglen[j]

con = duckdb.connect()
con.execute(f"CREATE VIEW tr AS SELECT route_id, trip_id, shape_id FROM read_csv_auto('{p_trips}')")
con.execute(f"CREATE VIEW st AS SELECT trip_id, CAST(stop_id AS VARCHAR) AS stop_id, stop_sequence FROM read_csv_auto('{p_stimes}')")

cand = con.execute("SELECT DISTINCT route_id, shape_id FROM tr").df() \
          .groupby("route_id")["shape_id"].unique().to_dict()

rep = con.execute("""
  WITH x AS (SELECT tr.shape_id, st.trip_id, count(*) AS n
             FROM st JOIN tr ON st.trip_id = tr.trip_id GROUP BY 1, 2),
       best AS (SELECT shape_id, arg_max(trip_id, n) AS trip_id FROM x GROUP BY 1)
  SELECT b.shape_id, st.stop_id, st.stop_sequence
  FROM st JOIN best b ON st.trip_id = b.trip_id""").df()
stops = pd.read_csv(os.path.join(g, "stops.txt"), usecols=["stop_id", "stop_lat", "stop_lon"])
stops["stop_id"] = stops["stop_id"].astype(str)
rep = rep.merge(stops, on="stop_id").sort_values(["shape_id", "stop_sequence"])
rep_g = dict(tuple(rep.groupby("shape_id")))

shapes = pd.read_csv(os.path.join(g, "shapes.txt")).sort_values(["shape_id", "shape_pt_sequence"])
shape_g = dict(tuple(shapes.groupby("shape_id")))

cache = {}
def shape_info(sid):
    if sid in cache:
        return cache[sid]
    s, r = shape_g.get(sid), rep_g.get(sid)
    if s is None or r is None:
        cache[sid] = None
        return None
    S = xy(s["shape_pt_lat"].values, s["shape_pt_lon"].values)
    sd, sa = snap(xy(r["stop_lat"].values, r["stop_lon"].values), S)
    sa = np.where(sd < 40, sa, np.nan)  # stops too far from the shape are ignored
    cache[sid] = (S, r["stop_id"].values, sa)
    return cache[sid]

samp = con.execute(f"""
  SELECT trip_id, route_id FROM '{pq}' GROUP BY 1, 2
  HAVING count(*) >= 100 ORDER BY hash(trip_id) LIMIT {N}""").df()
con.register("samp", samp)
pings = con.execute(f"""
  SELECT p.trip_id, p.lat, p.lng, p.ts FROM '{pq}' p
  JOIN samp s ON p.trip_id = s.trip_id ORDER BY p.trip_id, p.ts""").df()
route_of = dict(zip(samp["trip_id"], samp["route_id"]))

rows, skipped, t0 = [], 0, time.time()
for n, (tid, df) in enumerate(pings.groupby("trip_id")):
    df = df.drop_duplicates("ts")
    P = xy(df["lat"].values, df["lng"].values)
    ts = df["ts"].values
    sub = P[:: max(1, len(P) // 40)]
    best = None
    for sid in cand.get(int(route_of[tid]), []):
        info = shape_info(sid)
        if info is None:
            continue
        m = np.median(snap(sub, info[0])[0])
        if best is None or m < best[0]:
            best = (m, sid)
    if best is None or best[0] > 50:
        skipped += 1
        continue
    S, stop_ids, stop_along = shape_info(best[1])
    dist, along = snap(P, S)
    keep = dist < 50
    if keep.sum() < 20:
        skipped += 1
        continue
    a = np.maximum.accumulate(along[keep])
    t = ts[keep]
    arr = np.full(len(stop_along), np.nan)
    for i, d in enumerate(stop_along):
        if np.isnan(d):
            continue
        k = np.searchsorted(a, d, side="left")
        if k == 0 or k >= len(a) or t[k] - t[k - 1] > 60:
            continue
        f = (d - a[k - 1]) / max(a[k] - a[k - 1], 1e-6)
        arr[i] = t[k - 1] + f * (t[k] - t[k - 1])
    for i in range(1, len(stop_ids)):
        if np.isnan(arr[i]) or np.isnan(arr[i - 1]):
            continue
        seg_m = stop_along[i] - stop_along[i - 1]
        if seg_m <= 0:
            continue
        rows.append((tid, int(route_of[tid]), best[1], stop_ids[i - 1], stop_ids[i],
                     seg_m, arr[i] - arr[i - 1], arr[i - 1]))
    if n % 200 == 0:
        print(f"{n} trips, {len(rows)} segments, {time.time() - t0:.0f}s")

res = pd.DataFrame(rows, columns=["trip_id", "route_id", "shape_id", "from_stop",
                                  "to_stop", "seg_m", "seg_s", "t_from"])
con.register("res_df", res)
con.execute(f"COPY res_df TO '{out}' (FORMAT PARQUET)")
print("\ntrips in sample:", len(samp), " skipped:", skipped)
print("segments:", len(res))
ok = res[(res["seg_s"] > 0)]
print("km/h quantiles (5/25/50/75/95/99):")
print((3.6 * ok["seg_m"] / ok["seg_s"]).quantile([.05, .25, .5, .75, .95, .99]).round(1))