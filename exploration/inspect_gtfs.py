import duckdb, pandas as pd, os

base = os.path.dirname(os.path.abspath(__file__))
g = os.path.join(base, "gtfs")

for f in ["stops", "routes", "trips", "shapes"]:
    df = pd.read_csv(os.path.join(g, f + ".txt"), nrows=3)
    print(f"\n== {f}.txt ==")
    print(df.to_string())

routes = pd.read_csv(os.path.join(g, "routes.txt"))
trips = pd.read_csv(os.path.join(g, "trips.txt"))

con = duckdb.connect()
pq = os.path.join(base, "day_2026_10_15.parquet")
gps_routes = con.execute(
    f"SELECT DISTINCT route_id FROM '{pq}' WHERE route_id IS NOT NULL"
).df()["route_id"]
gps_trips = con.execute(
    f"SELECT DISTINCT trip_id FROM '{pq}' LIMIT 2000"
).df()["trip_id"]

print("\nGPS routes:", len(gps_routes))
print("GPS route_ids found in routes.txt:", gps_routes.isin(routes["route_id"]).mean())
print("GPS trip_ids found in trips.txt:", gps_trips.isin(trips["trip_id"]).mean())
print("sample GPS trip_ids:", gps_trips.head(5).tolist())
print("sample GTFS trip_ids:", trips["trip_id"].head(5).tolist())