import duckdb, os

home = os.path.expanduser("~")
db = home + r"\bus-data\bus_movements_2026_10_15.db"
out = home + r"\bus-data\day_2026_10_15.parquet"

con = duckdb.connect()
con.execute("INSTALL sqlite; LOAD sqlite;")
con.execute(f"ATTACH '{db}' AS d (TYPE sqlite)")
con.execute(f"""
COPY (
  SELECT DISTINCT
    vehicle_id,
    CAST(lat AS DOUBLE) AS lat,
    CAST(lng AS DOUBLE) AS lng,
    speed,
    CAST(route_id AS INTEGER) AS route_id,
    trip_id,
    CAST(timestamp AS BIGINT) AS ts
  FROM d.vehicle_feed
  WHERE lat IS NOT NULL AND lng IS NOT NULL
    AND CAST(lat AS DOUBLE) BETWEEN 28.2 AND 29.2
    AND CAST(lng AS DOUBLE) BETWEEN 76.5 AND 77.8
) TO '{out}' (FORMAT PARQUET, COMPRESSION ZSTD)
""")

print(con.execute(f"SELECT count(*) FROM '{out}'").fetchall())
print(os.path.getsize(out) / 1e6, "MB")