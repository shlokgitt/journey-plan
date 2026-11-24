import duckdb, pandas as pd, numpy as np, os

base = os.path.dirname(os.path.abspath(__file__))
g = os.path.join(base, "gtfs")
pq = os.path.join(base, "day_2026_10_15.parquet")

trips = pd.read_csv(os.path.join(g, "trips.txt"))
print("trips.txt columns:", list(trips.columns))
route_shapes = trips[["route_id", "shape_id"]].drop_duplicates()
shapes = pd.read_csv(os.path.join(g, "shapes.txt")).sort_values(
    ["shape_id", "shape_pt_sequence"])

con = duckdb.connect()
busy = con.execute(
    f"SELECT route_id, count(*) c FROM '{pq}' GROUP BY 1 ORDER BY c DESC LIMIT 30"
).df()
R = next(r for r in busy["route_id"] if r in set(route_shapes["route_id"]))
cands = route_shapes[route_shapes["route_id"] == R]["shape_id"].tolist()
trip = con.execute(
    f"SELECT trip_id, count(*) c FROM '{pq}' WHERE route_id={R} "
    "GROUP BY 1 ORDER BY c DESC LIMIT 1").df().iloc[0]["trip_id"]
pts = con.execute(
    f"SELECT lat, lng, speed, ts FROM '{pq}' WHERE trip_id='{trip}' "
    "ORDER BY ts").df().drop_duplicates("ts")
print(f"\nroute {R}, trip {trip}, pings {len(pts)}, candidate shapes {len(cands)}")

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

P = xy(pts["lat"].values, pts["lng"].values)
best = None
for sid in cands:
    s = shapes[shapes["shape_id"] == sid]
    dist, along = snap(P, xy(s["shape_pt_lat"].values, s["shape_pt_lon"].values))
    med = np.median(dist)
    fwd = np.mean(np.diff(along) > -20)
    print(f"{sid}: median off-route {med:.0f} m, forward share {fwd:.2f}")
    if best is None or med < best[0]:
        best = (med, sid, dist, along)

med, sid, dist, along = best
print(f"\nbest shape: {sid}, median {med:.0f} m, "
      f"share within 50 m: {np.mean(dist < 50):.2f}")
dt = np.diff(pts["ts"].values)
dd = np.diff(along)
ok = (dt > 0) & (dd > 0)
print("reported speed median (moving):", np.median(pts["speed"][pts["speed"] > 0]))
print("computed speed median km/h:", np.median(3.6 * dd[ok] / dt[ok]))
sp = pts["speed"].values
rep = (sp[:-1] + sp[1:]) / 2
comp = 3.6 * dd / np.maximum(dt, 1)
m = (dt <= 30) & (dd > 0) & (comp > 8)
print("intervals used:", m.sum())
print("median reported/computed ratio:", np.median(rep[m] / comp[m]))
print("correlation:", np.corrcoef(rep[m], comp[m])[0, 1])
# --- stop arrival times for this trip ---
gt = trips[trips["shape_id"] == sid]["trip_id"].iloc[0]
st = con.execute(f"""
  SELECT CAST(stop_id AS VARCHAR) AS stop_id, stop_sequence
  FROM read_csv_auto('{os.path.join(g, "stop_times.txt")}')
  WHERE trip_id = '{gt}' ORDER BY stop_sequence""").df()
stops = pd.read_csv(os.path.join(g, "stops.txt"))
stops["stop_id"] = stops["stop_id"].astype(str)
st = st.merge(stops[["stop_id", "stop_lat", "stop_lon"]], on="stop_id")
st = st.sort_values("stop_sequence").reset_index(drop=True)

s = shapes[shapes["shape_id"] == sid]
S = xy(s["shape_pt_lat"].values, s["shape_pt_lon"].values)
sd, sa = snap(xy(st["stop_lat"].values, st["stop_lon"].values), S)
st["off_m"] = sd
st["along_m"] = sa

keep = dist < 50
a = np.maximum.accumulate(along[keep])
t = pts["ts"].values[keep]
arr = []
for d in st["along_m"].values:
    k = np.searchsorted(a, d, side="left")
    if k == 0 or k >= len(a) or t[k] - t[k - 1] > 60:
        arr.append(np.nan)
        continue
    f = (d - a[k - 1]) / max(a[k] - a[k - 1], 1e-6)
    arr.append(t[k - 1] + f * (t[k] - t[k - 1]))
st["arrive_ts"] = arr
st["seg_m"] = st["along_m"].diff()
st["seg_s"] = st["arrive_ts"].diff()
st["kmh"] = 3.6 * st["seg_m"] / st["seg_s"]

print("\nGTFS trip used for stops:", gt)
print(st[["stop_sequence", "off_m", "along_m", "seg_m", "seg_s", "kmh"]]
      .head(20).round(1).to_string())
print("stops with an arrival time:", st["arrive_ts"].notna().sum(), "of", len(st))