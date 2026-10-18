# OTel EXPLAIN ANALYZE latency report

Generated: `2026-10-18T16:13:10+00:00`

This report records SereneDB server-side `EXPLAIN ANALYZE` timings in milliseconds. It profiles the database functions behind the OTel tools, not the tool wrappers or client/network time.

- Window: `2025-09-23 00:00:00.000000+00:00` to `2025-09-23 00:00:05.000000+00:00`
- Repetitions per query: `5`
- Search term for slice cases: `error`
- Existing `otel_slice` index covers the window: `False`

`avg ms`, `median ms`, `min ms`, and `max ms` are calculated from the successfully parsed runs. `total ms` is their cumulative server time. Skipped and refused cases do not execute an analyzed query.

| DB function | Dataset | Status | Avg ms | Median ms | Min ms | Max ms | Total ms | Runs | Note |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| count_by_field | otel_s3 | ok | 196.60 | 178.00 | 160.00 | 298.00 | 983.00 | 5 |  |
| top_by_field | otel_s3 | ok | 153.80 | 153.00 | 149.00 | 162.00 | 769.00 | 5 |  |
| compare_periods | otel_s3 | ok | 777.80 | 1020.00 | 253.00 | 1210.00 | 3889.00 | 5 |  |
| get_field_values | otel_s3 | ok | 170.60 | 158.00 | 129.00 | 263.00 | 853.00 | 5 |  |
| get_trend | otel_s3 | ok | 1372.80 | 183.00 | 175.00 | 6090.00 | 6864.00 | 5 |  |
| trace_logs | otel_s3 | ok | 34076.00 | 27650.00 | 19590.00 | 63910.00 | 170380.00 | 5 |  |
| search_logs | otel_slice | skipped | n/a | n/a | n/a | n/a | n/a | - | no index |
| count_search_matches | otel_slice | skipped | n/a | n/a | n/a | n/a | n/a | - | no index |
