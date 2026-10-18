# OTel EXPLAIN ANALYZE latency report

Generated: `2026-10-18T16:09:37+00:00`

This report records SereneDB server-side `EXPLAIN ANALYZE` timings in milliseconds. It profiles the database functions behind the OTel tools, not the tool wrappers or client/network time.

- Window: `2025-09-23 00:00:04Z` to `2025-09-23 00:00:09.000000+00:00`
- Repetitions per query: `5`
- Search term for slice cases: `error`
- Existing `otel_slice` index covers the window: `True`

`avg ms`, `median ms`, `min ms`, and `max ms` are calculated from the successfully parsed runs. `total ms` is their cumulative server time. Skipped and refused cases do not execute an analyzed query.

| DB function | Dataset | Status | Avg ms | Median ms | Min ms | Max ms | Total ms | Runs | Note |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| count_by_field | otel_s3 | ok | 240.60 | 238.00 | 221.00 | 266.00 | 1203.00 | 5 |  |
| top_by_field | otel_s3 | ok | 206.00 | 197.00 | 186.00 | 242.00 | 1030.00 | 5 |  |
| compare_periods | otel_s3 | ok | 431.60 | 429.00 | 425.00 | 443.00 | 2158.00 | 5 |  |
| get_field_values | otel_s3 | ok | 214.00 | 209.00 | 198.00 | 238.00 | 1070.00 | 5 |  |
| get_trend | otel_s3 | ok | 1604.40 | 235.00 | 203.00 | 7120.00 | 8022.00 | 5 |  |
| search_logs | otel_slice | ok | 1027.20 | 746.00 | 655.00 | 2320.00 | 5136.00 | 5 |  |
| count_search_matches | otel_slice | ok | 359.80 | 367.00 | 323.00 | 401.00 | 1799.00 | 5 |  |
