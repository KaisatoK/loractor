# OTel EXPLAIN ANALYZE latency report

Generated: `2026-10-18T14:17:27+00:00`

This report records SereneDB server-side `EXPLAIN ANALYZE` timings in milliseconds. It profiles the database functions behind the OTel tools, not the tool wrappers or client/network time.

- Window: `2025-09-23 00:00:00.000000+00:00` to `2025-09-23 00:00:01.000000+00:00`
- Repetitions per query: `5`
- Search term for slice cases: `error`
- Existing `otel_slice` index covers the window: `False`

`avg ms`, `median ms`, `min ms`, and `max ms` are calculated from the successfully parsed runs. `total ms` is their cumulative server time. Skipped and refused cases do not execute an analyzed query.

| DB function | Dataset | Status | Avg ms | Median ms | Min ms | Max ms | Total ms | Runs | Note |
| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | --- |
| count_by_field | otel_s3 | ok | 176.00 | 182.00 | 151.00 | 200.00 | 880.00 | 5 |  |
| top_by_field | otel_s3 | ok | 189.60 | 177.00 | 167.00 | 251.00 | 948.00 | 5 |  |
| compare_periods | otel_s3 | ok | 249.00 | 243.00 | 242.00 | 270.00 | 1245.00 | 5 |  |
| get_field_values | otel_s3 | ok | 171.40 | 177.00 | 152.00 | 184.00 | 857.00 | 5 |  |
| get_trend | otel_s3 | ok | 180.80 | 185.00 | 166.00 | 197.00 | 904.00 | 5 |  |
| trace_logs | otel_s3 | ok | 198.20 | 192.00 | 173.00 | 233.00 | 991.00 | 5 |  |
| search_logs | otel_slice | skipped | n/a | n/a | n/a | n/a | n/a | - | no index |
| count_search_matches | otel_slice | skipped | n/a | n/a | n/a | n/a | n/a | - | no index |
