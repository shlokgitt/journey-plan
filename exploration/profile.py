import duckdb, os

db = os.path.expanduser(r"~\bus-data\bus_movements_2026_10_15.db")
con = duckdb.connect()
con.execute("INSTALL sqlite; LOAD sqlite;")
con.execute(f"ATTACH '{db}' AS d (TYPE sqlite)")

print("COUNTS")
print(con.execute("""
  SELECT count(*) AS rows,
         count(DISTINCT vehicle_id) AS buses,
         count(DISTINCT route_id) AS routes,
         count(DISTINCT trip_id) AS trips
  FROM d.vehicle_feed
""").fetchall())

print("SAMPLE ROWS")
for r in con.execute("SELECT * FROM d.vehicle_feed LIMIT 5").fetchall():
    print(r)

print("PING INTERVAL SECONDS (p50, p90, p99)")
print(con.execute("""
  WITH x AS (
    SELECT CAST(timestamp AS BIGINT) AS ts,
           lag(CAST(timestamp AS BIGINT)) OVER (
             PARTITION BY vehicle_id ORDER BY CAST(timestamp AS BIGINT)) AS prev
    FROM d.vehicle_feed)
  SELECT quantile_cont(ts - prev, [0.5, 0.9, 0.99]) FROM x WHERE prev IS NOT NULL
""").fetchall())
