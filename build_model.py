import os

import duckdb


BASE = os.path.dirname(os.path.abspath(__file__))
DATES = {
    "2026_10_07": "weekday",
    "2026_10_14": "weekday",
    "2026_10_15": "weekday",
    "2026_10_17": "sat",
    "2026_10_18": "sun",
}


def source_files():
    return [
        (day, day_type, os.path.join(BASE, f"segments_{day}.parquet"))
        for day, day_type in DATES.items()
        if os.path.exists(os.path.join(BASE, f"segments_{day}.parquet"))
    ]


def build_tables(con, output_tag, held_out=None):
    selected = [(d, t, p) for d, t, p in source_files() if d != held_out]
    if not selected:
        raise SystemExit("No training days found.")

    con.execute("DROP TABLE IF EXISTS all_segments")
    first = True
    for day, day_type, path in selected:
        con.execute(
            f"""
            CREATE {'OR REPLACE ' if first else ''}TEMP TABLE {'all_segments' if first else 'day_segments'}
            AS SELECT *, '{day_type}' AS day_type
            FROM read_parquet('{path.replace(chr(92), chr(47))}')
            """)
        if not first:
            con.execute("INSERT INTO all_segments SELECT * FROM day_segments")
            con.execute("DROP TABLE day_segments")
        first = False

    exact_path = os.path.join(BASE, f"speed_table{output_tag}.parquet")
    route_path = os.path.join(BASE, f"speed_route_table{output_tag}.parquet")
    free_path = os.path.join(BASE, f"speed_freeflow_table{output_tag}.parquet")

    con.execute(
        f"""
        COPY (
          SELECT route_id, from_stop, to_stop, day_type,
                 CAST(floor((t_from + 19800) / 3600) % 24 AS INTEGER) AS hour_ist,
                 count(*) AS n, sum(seg_m) AS sum_m, sum(seg_s) AS sum_s,
                 median(seg_s) AS median_s,
                 quantile_cont(seg_s, 0.25) AS p25_s,
                 quantile_cont(seg_s, 0.75) AS p75_s,
                 3.6 * sum(seg_m) / sum(seg_s) AS kmh_weighted
          FROM all_segments
          WHERE seg_s >= 5 AND seg_m > 0 AND seg_s <= 7200
            AND 3.6 * seg_m / seg_s <= 80
          GROUP BY route_id, from_stop, to_stop, day_type, 5
        ) TO '{exact_path.replace(chr(92), chr(47))}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)

    con.execute(
        f"""
        COPY (
          SELECT route_id, day_type,
                 CAST(floor((t_from + 19800) / 3600) % 24 AS INTEGER) AS hour_ist,
                 count(*) AS n, sum(seg_m) AS sum_m, sum(seg_s) AS sum_s,
                 3.6 * sum(seg_m) / sum(seg_s) AS kmh_weighted
          FROM all_segments
          WHERE seg_s >= 5 AND seg_m > 0 AND seg_s <= 7200
            AND 3.6 * seg_m / seg_s <= 80
          GROUP BY route_id, day_type, 3
        ) TO '{route_path.replace(chr(92), chr(47))}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)

    con.execute(
        f"""
        COPY (
          WITH hourly AS (
            SELECT route_id,
                   CAST(floor((t_from + 19800) / 3600) % 24 AS INTEGER) AS hour_ist,
                   3.6 * sum(seg_m) / sum(seg_s) AS kmh_weighted
            FROM all_segments
            WHERE seg_s >= 5 AND seg_m > 0 AND seg_s <= 7200
              AND 3.6 * seg_m / seg_s <= 80
            GROUP BY route_id, 2
          )
          SELECT route_id,
                 quantile_cont(kmh_weighted, 0.85) AS freeflow_kmh,
                 0.85 AS quantile
          FROM hourly
          GROUP BY route_id
        ) TO '{free_path.replace(chr(92), chr(47))}' (FORMAT PARQUET, COMPRESSION ZSTD)
        """)


def main():
    con = duckdb.connect()
    build_tables(con, "")

    for day, _, _ in source_files():
        build_tables(con, f"_loo_{day}", held_out=day)
        print(f"Built leave-one-out model without {day}")

    print("Built speed_table.parquet")
    print("Built speed_route_table.parquet")
    print("Built speed_freeflow_table.parquet")
    print("Training sources:", ", ".join(day for day, _, _ in source_files()))


if __name__ == "__main__":
    main()
