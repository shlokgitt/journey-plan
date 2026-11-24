import duckdb, os

base = os.path.dirname(os.path.abspath(__file__))
seg = os.path.join(base, "segments_2026_10_15.parquet")

con = duckdb.connect()
df = con.execute(f"""
  SELECT CAST(floor((t_from + 19800) / 3600) % 24 AS INTEGER) AS hour_ist,
         count(*) AS n,
         round(3.6 * sum(seg_m) / sum(seg_s), 1) AS kmh_weighted
  FROM '{seg}'
  WHERE seg_s >= 5 AND 3.6 * seg_m / seg_s <= 80
  GROUP BY 1 ORDER BY 1
""").df()
print(df.to_string(index=False))