# Worked investigation

Topic: **BGL FATAL and ERROR events**. The same agent and base corpus answer three role-specific questions; each role gets different evidence and a different next step.

_Generated 2026-10-18 12:35 UTC by `bash ./worked_investigation.sh`; this file is regenerated from live runs, do not edit by hand._

## Data scope
- Datasets queried: bgl
- Time windows: 2005-06-03 22:42:50 – 2006-01-04 16:00:06; 2005-06-05 00:00:00 – 2005-06-06 00:00:00; 2005-06-05 07:00:00 – 2005-06-05 07:15:00; 2005-09-20 19:00:00 – 2005-09-20 19:20:00; 2005-09-20 17:00:00 – 2005-09-20 20:00:00; 2005-06-03 22:42:50 – 2005-09-19 00:00:00; 2005-09-19 00:00:00 – 2006-01-04 16:00:06; 2005-06-03 22:42:50 – 2005-09-19 00:00:00; 2005-09-19 00:00:00 – 2006-01-04 16:00:06
- Roles run: Engineer, Team lead, Manager

## Engineer
**Question:** What failed involving BGL FATAL and ERROR events, in which component, and what happened just before and after?

**Route:** `find_evidence` · **Effort:** High · **Datasets:** bgl
> ⚠ No full-text search result was captured for this role.

### Tool calls
| id | tool | kind | arguments | rows | truncated |
| --- | --- | --- | --- | --- | --- |
| R1 | list_datasets_tool | catalog | `{}` | 4 | no |
| R2 | get_log_by_line_id_tool | lookup | `{"line_id": 17742, "dataset": "bgl"}` | 1 | no |
| R3 | get_nearby_events_tool | context | `{"line_id": 17742, "dataset": "bgl", "before": 20, "after": 20}` | 41 | no |
| R4 | count_by_category_tool | aggregate | `{"dataset": "bgl", "start_time": "2005-06-03 22:42:50", "end_time": "2006-01-04 16:00:06", "type": "service", "limit": …` | 22 | no |
| R5 | count_by_category_tool | aggregate | `{"dataset": "bgl", "start_time": "2005-06-03 22:42:50", "end_time": "2006-01-04 16:00:06", "type": "template", "categor…` | 10 | yes |
| R6 | get_trend_tool | trend | `{"dataset": "bgl", "start_time": "2005-06-05 00:00:00", "end_time": "2005-06-06 00:00:00", "type": ["service", "KERNEL"…` | 24 | no |
| R7 | count_by_category_tool | aggregate | `{"category": "FATAL", "dataset": "bgl", "end_time": "2005-06-05 07:15:00", "limit": 100, "start_time": "2005-06-05 07:0…` | 76 | no |
| R8 | count_by_category_tool | aggregate | `{"dataset": "bgl", "end_time": "2005-09-20 19:20:00", "limit": 25, "start_time": "2005-09-20 19:00:00", "type": "servic…` | 3 | no |
| R9 | get_nearby_events_tool | context | `{"after": 10, "before": 25, "dataset": "bgl", "line_id": 3395426, "same_entity": true}` | 36 | no |
| R10 | get_trend_tool | trend | `{"bucket": "minute", "category": "ERROR", "dataset": "bgl", "end_time": "2005-09-20 20:00:00", "start_time": "2005-09-2…` | 180 | no |
| R11 | count_by_category_tool | aggregate | `{"category": "ERROR", "dataset": "bgl", "end_time": "2006-01-04 16:00:06", "limit": 10, "start_time": "2005-06-03 22:42…` | 10 | yes |
| R12 | timeline | derived | `{"dataset": "bgl", "anchor_record_id": 17742, "from": ["R3"]}` | 41 | no |

### Executed queries
**R1** list_datasets_tool

```sql
SELECT table_name FROM information_schema.tables WHERE table_name IN ('bgl', 'openstack', 'otel_ext', 'otel_ext_meta');
SELECT "Level" AS severity, count(*) AS count, min("Timestamp") AS first_ts, max("Timestamp") AS last_ts
FROM "bgl"
GROUP BY "Level"
ORDER BY count DESC;
SELECT "Level" AS severity, count(*) AS count, min("Timestamp") AS first_ts, max("Timestamp") AS last_ts
FROM "openstack"
GROUP BY "Level"
ORDER BY count DESC;
SELECT count(*) AS n FROM "otel_ext_idx";
SELECT window_start, window_end, severities, indexed_rows, built_at FROM "otel_ext_meta" ORDER BY built_at DESC LIMIT 1
```

**R2** get_log_by_line_id_tool

```sql
SELECT "LineId" AS record_id, "Timestamp" AS "ts", "Level" AS "severity", "Component" AS "service", "Node" AS "entity_id", "Content" AS "message", "EventTemplate" AS template
FROM "bgl"
WHERE "LineId" = 17742
```

**R3** get_nearby_events_tool

```sql
WITH anchor AS (
SELECT "LineId" AS rid FROM "bgl" WHERE "LineId" = 17742
)
SELECT * FROM (
SELECT s.*, -ROW_NUMBER() OVER (ORDER BY s.record_id DESC) AS "offset"
FROM (
SELECT "LineId" AS "record_id", "Timestamp" AS "ts", "Level" AS "severity", "Component" AS "service", "Node" AS "entity_id", "Content" AS "message", "EventTemplate" AS "template"
FROM "bgl" t, anchor a
WHERE "LineId" < a.rid
ORDER BY "LineId" DESC
LIMIT 20
) s
UNION ALL
SELECT "LineId" AS "record_id", "Timestamp" AS "ts", "Level" AS "severity", "Component" AS "service", "Node" AS "entity_id", "Content" AS "message", "EventTemplate" AS "template", 0 AS "offset"
FROM "bgl" t JOIN anchor a ON "LineId" = a.rid
UNION ALL
SELECT s.*, ROW_NUMBER() OVER (ORDER BY s.record_id ASC) AS "offset"
FROM (
SELECT "LineId" AS "record_id", "Timestamp" AS "ts", "Level" AS "severity", "Component" AS "service", "Node" AS "entity_id", "Content" AS "message", "EventTemplate" AS "template"
FROM "bgl" t, anchor a
WHERE "LineId" > a.rid
ORDER BY "LineId" ASC
LIMIT 20
) s
) nearby
ORDER BY record_id
```

**R4** count_by_category_tool

```sql
SELECT "Component" AS "service", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz
GROUP BY "Component", "Level"
ORDER BY count DESC
LIMIT 25
```

**R5** count_by_category_tool

```sql
SELECT "EventTemplate" AS "template", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "Level" = 'FATAL'
GROUP BY "EventTemplate", "Level"
ORDER BY count DESC
LIMIT 10
```

**R6** get_trend_tool

```sql
WITH bucket_counts AS MATERIALIZED (
SELECT CAST(date_trunc('hour', "Timestamp") AS timestamptz) AS bucket, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-05 00:00:00'::timestamptz AND "Timestamp" < '2005-06-06 00:00:00'::timestamptz AND "Component" = 'KERNEL' AND "Level" = 'FATAL'
GROUP BY CAST(date_trunc('hour', "Timestamp") AS timestamptz)
),
grid AS (
SELECT s.bucket
FROM generate_series(
date_trunc('hour', '2005-06-05 00:00:00'::timestamptz), '2005-06-06 00:00:00'::timestamptz,
CAST('1 hour' AS INTERVAL)
) AS s(bucket)
WHERE s.bucket < '2005-06-06 00:00:00'::timestamptz
),
filled AS (
SELECT g.bucket, COALESCE(c.count, 0) AS count
FROM grid g
LEFT JOIN bucket_counts c ON c.bucket = g.bucket
)
SELECT
bucket,
count,
LAG(count) OVER (ORDER BY bucket) AS prev_count,
CASE
WHEN LAG(count) OVER (ORDER BY bucket) IS NULL THEN 'initial'
WHEN count > LAG(count) OVER (ORDER BY bucket) THEN 'up'
WHEN count < LAG(count) OVER (ORDER BY bucket) THEN 'down'
ELSE 'flat'
END AS trend
FROM filled
ORDER BY bucket
LIMIT 240
```

**R7** count_by_category_tool

```sql
SELECT "Node" AS "entity_id", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-05 07:00:00'::timestamptz AND "Timestamp" < '2005-06-05 07:15:00'::timestamptz AND "Level" = 'FATAL'
GROUP BY "Node", "Level"
ORDER BY count DESC
LIMIT 100
```

**R8** count_by_category_tool

```sql
SELECT "Component" AS "service", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-09-20 19:00:00'::timestamptz AND "Timestamp" < '2005-09-20 19:20:00'::timestamptz
GROUP BY "Component", "Level"
ORDER BY count DESC
LIMIT 25
```

**R9** get_nearby_events_tool

```sql
WITH anchor AS (
SELECT "LineId" AS rid, "Node" AS ent FROM "bgl" WHERE "LineId" = 3395426
)
SELECT * FROM (
SELECT s.*, -ROW_NUMBER() OVER (ORDER BY s.record_id DESC) AS "offset"
FROM (
SELECT "LineId" AS "record_id", "Timestamp" AS "ts", "Level" AS "severity", "Component" AS "service", "Node" AS "entity_id", "Content" AS "message", "EventTemplate" AS "template"
FROM "bgl" t, anchor a
WHERE "LineId" < a.rid AND "Node" = a.ent
ORDER BY "LineId" DESC
LIMIT 25
) s
UNION ALL
SELECT "LineId" AS "record_id", "Timestamp" AS "ts", "Level" AS "severity", "Component" AS "service", "Node" AS "entity_id", "Content" AS "message", "EventTemplate" AS "template", 0 AS "offset"
FROM "bgl" t JOIN anchor a ON "LineId" = a.rid
UNION ALL
SELECT s.*, ROW_NUMBER() OVER (ORDER BY s.record_id ASC) AS "offset"
FROM (
SELECT "LineId" AS "record_id", "Timestamp" AS "ts", "Level" AS "severity", "Component" AS "service", "Node" AS "entity_id", "Content" AS "message", "EventTemplate" AS "template"
FROM "bgl" t, anchor a
WHERE "LineId" > a.rid AND "Node" = a.ent
ORDER BY "LineId" ASC
LIMIT 10
) s
) nearby
ORDER BY record_id
```

**R10** get_trend_tool

```sql
WITH bucket_counts AS MATERIALIZED (
SELECT CAST(date_trunc('minute', "Timestamp") AS timestamptz) AS bucket, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-09-20 17:00:00'::timestamptz AND "Timestamp" < '2005-09-20 20:00:00'::timestamptz AND "Component" = 'MMCS' AND "Level" = 'ERROR'
GROUP BY CAST(date_trunc('minute', "Timestamp") AS timestamptz)
),
grid AS (
SELECT s.bucket
FROM generate_series(
date_trunc('minute', '2005-09-20 17:00:00'::timestamptz), '2005-09-20 20:00:00'::timestamptz,
CAST('1 minute' AS INTERVAL)
) AS s(bucket)
WHERE s.bucket < '2005-09-20 20:00:00'::timestamptz
),
filled AS (
SELECT g.bucket, COALESCE(c.count, 0) AS count
FROM grid g
LEFT JOIN bucket_counts c ON c.bucket = g.bucket
)
SELECT
bucket,
count,
LAG(count) OVER (ORDER BY bucket) AS prev_count,
CASE
WHEN LAG(count) OVER (ORDER BY bucket) IS NULL THEN 'initial'
WHEN count > LAG(count) OVER (ORDER BY bucket) THEN 'up'
WHEN count < LAG(count) OVER (ORDER BY bucket) THEN 'down'
ELSE 'flat'
END AS trend
FROM filled
ORDER BY bucket
LIMIT 240
```

**R11** count_by_category_tool

```sql
SELECT "EventTemplate" AS "template", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "Level" = 'ERROR'
GROUP BY "EventTemplate", "Level"
ORDER BY count DESC
LIMIT 10
```

**R12** computed in code from R3: rows ordered by record_id; offset = lines from the anchor line, seconds = time from the anchor line (negative = before)

### Relevant results
**R1** (catalog, first 4 of 4 rows)

| dataset | kind | exact_counts | available | rows | rows_exact | first_timestamp | last_timestamp | span_seconds | max_window_seconds | whole_span_fits_one_window | window_start | window_end | levels | timestamp_source | note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bgl | native_table | True | True | 4747963 | True | 2005-06-03 22:42:50+00:00 | 2006-01-04 16:00:05+00:00 | 18551835.0 | 31536000 | True | 2005-06-03 22:42:50 | 2006-01-04 16:00:06 | [{'severity': 'INFO', 'count': 3735823}, {'severity': 'FATAL', 'count': 855501}, {'severity': 'ERROR', 'count': 112355}… | rows | Exact: counted over every row. Each level's count is the number of log entries at that severity in the whole dataset. |
| openstack | native_table | True | True | 207820 | True | 2017-05-14 19:39:01.445000+00:00 | 2017-05-17 12:02:35.320000+00:00 | 231813.875 | 7776000 | True | 2017-05-14 19:39:01 | 2017-05-17 12:02:36 | [{'severity': 'INFO', 'count': 204506}, {'severity': 'WARNING', 'count': 3118}, {'severity': 'ERROR', 'count': 194}, {'… | rows | Exact: counted over every row. Each level's count is the number of log entries at that severity in the whole dataset. |
| otel_s3 | remote_parquet | False | True | 1000000000 | True | 2025-09-23 00:00:00+00:00 | 2025-09-23 00:29:24.039037+00:00 | 1764.039 | 300 | False | 2025-09-23 00:00:00+00 | 2025-09-23 00:29:25+00 | [{'severity': 'info', 'count': None}, {'severity': 'error', 'count': None}, {'severity': 'information', 'count': None},… | parquet_footers | Rows and span from the footers of 1 file(s) (1,624 row groups); no row was read. Levels are the declared vocabulary, NO… |
| otel_slice | external_index | True | True | 360519 | True | 2025-09-23 00:00:04+00:00 | 2025-09-23 00:00:08+00:00 | 4.0 | 86400 | True | 2025-09-23 00:00:04+00 | 2025-09-23 00:00:08+00 | [{'severity': 'error', 'count': None}, {'severity': 'fatal', 'count': None}, {'severity': 'warn', 'count': None}] | index_build_record | The timestamps are the window the index was BUILT over (end exclusive), not observed first/last rows; levels are the se… |

**R2** (lookup, first 1 of 1 rows)

| record_id | ts | severity | service | entity_id | message | template |
| --- | --- | --- | --- | --- | --- | --- |
| 17742 | 2005-06-05 07:08:13+00:00 | FATAL | KERNEL | R20-M0-N2-C:J10-U11 | data TLB error interrupt | data TLB error interrupt |

**R3** (context, first 5 of 41 rows)

| record_id | ts | severity | service | entity_id | message | template | offset |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 17722 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | R20-M1-N1-C:J02-U11 | generating core.939 | generating core.<*> | -20 |
| 17723 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | R20-M0-N2-C:J09-U11 | generating core.3646 | generating core.<*> | -19 |
| 17724 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | R20-M0-N2-C:J15-U11 | generating core.3772 | generating core.<*> | -18 |
| 17725 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | R20-M0-N2-C:J11-U11 | generating core.3773 | generating core.<*> | -17 |
| 17726 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | R20-M0-N2-C:J13-U11 | generating core.3645 | generating core.<*> | -16 |

**R4** (aggregate, first 5 of 22 rows)

| service | severity | count |
| --- | --- | --- |
| KERNEL | INFO | 3698211 |
| KERNEL | FATAL | 626756 |
| APP | FATAL | 228536 |
| MMCS | ERROR | 88529 |
| DISCOVERY | INFO | 35942 |

**R5** (aggregate, first 5 of 10 rows)

| template | severity | count |
| --- | --- | --- |
| data TLB error interrupt | FATAL | 152734 |
| data storage interrupt | FATAL | 63491 |
| instruction address: <*> | FATAL | 47563 |
| ciod: Error reading message prefix on CioStream socket to <*>.<*>.<*>.<*>:<*>, Link has been severed | FATAL | 26546 |
| ciod: Error loading /bgl/apps/scaletest/performance/MINIBEN/mb_243_0810/allreduce.rts: invalid or missing program image… | FATAL | 20480 |

**R6** (trend, first 5 of 24 rows)

| bucket | count | prev_count | trend |
| --- | --- | --- | --- |
| 2005-06-05 00:00:00+00:00 | 0 |  | initial |
| 2005-06-05 01:00:00+00:00 | 0 | 0 | flat |
| 2005-06-05 02:00:00+00:00 | 0 | 0 | flat |
| 2005-06-05 03:00:00+00:00 | 0 | 0 | flat |
| 2005-06-05 04:00:00+00:00 | 0 | 0 | flat |

**R7** (aggregate, first 5 of 76 rows)

| entity_id | severity | count |
| --- | --- | --- |
| R20-M0-N9-C:J14-U01 | FATAL | 1 |
| R20-M0-NA-C:J02-U11 | FATAL | 1 |
| R21-M1-N1-C:J02-U01 | FATAL | 1 |
| R21-M0-N2-C:J09-U01 | FATAL | 1 |
| R21-M0-ND-C:J14-U11 | FATAL | 1 |

**R8** (aggregate, first 3 of 3 rows)

| service | severity | count |
| --- | --- | --- |
| MMCS | ERROR | 66563 |
| DISCOVERY | INFO | 741 |
| DISCOVERY | SEVERE | 47 |

**R9** (context, first 5 of 36 rows)

| record_id | ts | severity | service | entity_id | message | template | offset |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 3394892 | 2005-09-20 18:19:45+00:00 | ERROR | MMCS | NULL | idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoT… | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | -25 |
| 3394893 | 2005-09-20 18:19:45+00:00 | ERROR | MMCS | NULL | idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoT… | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | -24 |
| 3394894 | 2005-09-20 18:19:46+00:00 | ERROR | MMCS | NULL | idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoT… | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | -23 |
| 3394895 | 2005-09-20 18:19:46+00:00 | ERROR | MMCS | NULL | Ido packet timeout | Ido packet timeout | -22 |
| 3394896 | 2005-09-20 18:21:02+00:00 | ERROR | MMCS | NULL | idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoT… | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | -21 |

**R10** (trend, first 5 of 180 rows)

| bucket | count | prev_count | trend |
| --- | --- | --- | --- |
| 2005-09-20 17:00:00+00:00 | 0 |  | initial |
| 2005-09-20 17:01:00+00:00 | 0 | 0 | flat |
| 2005-09-20 17:02:00+00:00 | 0 | 0 | flat |
| 2005-09-20 17:03:00+00:00 | 0 | 0 | flat |
| 2005-09-20 17:04:00+00:00 | 0 | 0 | flat |

**R11** (aggregate, first 5 of 10 rows)

| template | severity | count |
| --- | --- | --- |
| idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | ERROR | 84168 |
| Node card status: no ALERTs are active. Clock Mode is Low. Clock Select is Midplane. Phy JTAG Reset is asserted. ASIC J… | ERROR | 16896 |
| Ido packet timeout | ERROR | 4276 |
| Found invalid node ecid in processor card slot J08, ecid <*> | ERROR | 342 |
| Found invalid node ecid in processor card slot J04, ecid <*> | ERROR | 342 |

**R12** (derived, first 5 of 41 rows)

| offset | seconds | record_id | ts | severity | service | message | from |
| --- | --- | --- | --- | --- | --- | --- | --- |
| -20 | -1.0 | 17722 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | generating core.939 | R3 |
| -19 | -1.0 | 17723 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | generating core.3646 | R3 |
| -18 | -1.0 | 17724 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | generating core.3772 | R3 |
| -17 | -1.0 | 17725 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | generating core.3773 | R3 |
| -16 | -1.0 | 17726 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | generating core.3645 | R3 |

### Evidence report
**Summary:** BGL corpus (4,747,963 rows, 2005-06-03 22:42:50 → 2006-01-04 16:00:06 UTC) carries 855,501 FATAL and 112,355 ERROR log entries. Responsibility splits by severity: FATAL entries come from KERNEL (626,756) and APP (228,536), plus small LINKCARD (206) and MMCS (3) counts, while ERROR entries come from MMCS (88,529), DISCOVERY (23,825) and SERV_NET (1). Two concrete episodes were examined. (A) On 2005-09-20 an MMCS ERROR storm ran 19:05–19:14 (66,563 entries, peaking at 16,409 in the 19:06 minute) and dropped to exactly zero at 19:15; the anchor line 3395426 at 19:05:58 is 'idoproxydb hit ASSERT condition ... idotransportmgr.cpp Source line=1043 Function=int IdoTransportMgr::SendPacket(IdoUdpMgr*, BglCtlPavTrace*)'. In 19:00–19:20 the only rows present are MMCS ERROR 66,563 plus DISCOVERY INFO 741 and DISCOVERY SEVERE 47 — no FATAL, SEVERE or FAILURE from any other component. Context around the anchor shows an identical assertion precursor cluster at 18:19:45–18:21:06 with 'Ido packet timeout' entries at 18:19:46 and 18:21:06 (lines 3394892–3394916), then a 44 min 52 s gap with no MMCS ERROR before the anchor. (B) On 2005-06-05 at 07:08:13 the KERNEL FATAL 'data TLB error interrupt' appears at line 17742 (node R20-M0-N2-C:J10-U11), at +3 (line 17745, R20-M0-N2-C:J14-U01) and +16 (line 17758, R20-M0-ND-C:J13-U11), interleaved with INFO 'generating core.<n>' entries from neighbouring node cards; within 07:00–07:15 there were 76 FATAL rows across 76 distinct nodes (one each, racks R20, R21, R24, R25). Hourly KERNEL FATAL that day was 0 for 00:00–06:00, 206 at 07:00, 95 at 08:00, 342 at 11:00, 152 at 12:00, 304 at 13:00, 567 at 14:00 and 2,188 at 15:00. What triggered either the MMCS assertion or the TLB exceptions remains unknown; the anomaly Label field (e.g. KERNDTLB) is not exposed by any query tool.
- **E1** list_datasets_tool — available; results R1
- **E2** get_log_by_line_id_tool — available; results R2
- **E3** get_nearby_events_tool — available; results R3
- **E4** count_by_category_tool — available; results R4
- **E5** count_by_category_tool — available; results R5
- **E6** get_trend_tool — available; results R6
- **E7** count_by_category_tool — available; results R7
- **E8** count_by_category_tool — available; results R8
- **E9** get_nearby_events_tool — available; results R9
- **E10** get_trend_tool — available; results R10
- **E11** count_by_category_tool — available; results R11
- Hypothesis (supported): The 2005-09-20 ERROR episode is an MMCS control-system iDo transport failure inside IdoTransportMgr::SendPacket, not a compute-node failure.; next check: Inspect unfiltered MMCS INFO/SEVERE lines in 2005-09-20 19:00:00–19:05:59 (record ids ~3395000–3395425) for an idoproxydb/MMCS restart, iDo card or fabric-manager re-initialisation, or link-down/COMMUNICATION_ERROR entry.
- Hypothesis (unsupported): DISCOVERY node-card VPD/ecid mismatches logged shortly before the episode caused the MMCS ERROR storm.; contradicted by E8, E9, E10; next check: Compare the exact timestamps of DISCOVERY VPD/ecid mismatch lines with the MMCS ERROR minute buckets 18:15–19:15 to see whether any mismatch precedes an assertion by less than one minute.
- Hypothesis (supported): The 2005-06-05 07:08 FATAL burst is cluster-wide (job/application-scale) activity rather than a single-node hardware fault.; next check: Retrieve record ids ~17700–17721 plus ciod/loader activity in 2005-06-05 07:07:00–07:08:12 and look for a job-start boundary, LOAD_MESSAGE or 'kernel terminated' context immediately before the first 'data TLB error interrupt'.

### Answer for the engineer
The BGL data contain two distinct failure signatures: a KERNEL fatal burst on 2005-06-05 and an MMCS error-assertion episode on 2005-09-20. The failing services, exact messages and line ids are recorded in the findings; neither retrieved context establishes what triggered the failure.

The ordered timeline below is anchored on record 17742. Offsets and seconds are computed from that FATAL line and are not re-sorted or re-timed here.

_R12: timeline_

| offset | seconds | record_id | ts | severity | service | message | from |
| --- | --- | --- | --- | --- | --- | --- | --- |
| -20 | -1.0 | 17722 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | generating core.939 | R3 |
| -19 | -1.0 | 17723 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | generating core.3646 | R3 |
| -18 | -1.0 | 17724 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | generating core.3772 | R3 |
| -17 | -1.0 | 17725 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | generating core.3773 | R3 |
| -16 | -1.0 | 17726 | 2005-06-05 07:08:12+00:00 | INFO | KERNEL | generating core.3645 | R3 |

The FATAL line sits inside a continuous stream of routine core-generation INFO entries from node cards in the same rack group. Two further FATAL lines occur in the same second, and the INFO stream resumes immediately after; no job-start, loader or kernel-termination boundary appears in these 41 rows.

The second table shows the retrieved neighbours around the MMCS anchor at record 3395426 (25 lines before and 10 after, same entity). It covers an earlier identical assertion cluster and the immediate post-anchor burst.

_R9: get_nearby_events_tool_

| record_id | ts | severity | service | entity_id | message | template | offset |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 3394892 | 2005-09-20 18:19:45+00:00 | ERROR | MMCS | NULL | idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoT… | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | -25 |
| 3394893 | 2005-09-20 18:19:45+00:00 | ERROR | MMCS | NULL | idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoT… | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | -24 |
| 3394894 | 2005-09-20 18:19:46+00:00 | ERROR | MMCS | NULL | idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoT… | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | -23 |
| 3394895 | 2005-09-20 18:19:46+00:00 | ERROR | MMCS | NULL | Ido packet timeout | Ido packet timeout | -22 |
| 3394896 | 2005-09-20 18:21:02+00:00 | ERROR | MMCS | NULL | idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoT… | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | -21 |

The earlier cluster ends with an 'Ido packet timeout' entry and is separated from the 19:05:58 anchor by a long quiet gap; the lines after the anchor are further identical assertions in the same second. The retrieved rows do not show a restart, link-down or fabric-manager transition at the anchor.

The cause of either episode is not established by the retrieved rows; the supported interpretations and remaining unknowns are listed once in the findings.

**Measured** (shown by the cited query results)
- **F1** Over 2005-06-03 22:42:50 to 2006-01-04 16:00:06 UTC the BGL corpus has 4,747,963 rows, including 855,501 FATAL and 112,355 ERROR entries; FATAL entries are KERNEL 626,756, APP 228,536, LINKCARD 206 and MMCS 3, while ERROR entries are MMCS 88,529, DISCOVERY 23,825 and SERV_NET 1. — R1 (value list), R4 (count)
- **F2** The KERNEL anchor is record 17742 at 2005-06-05 07:08:13, entity R20-M0-N2-C:J10-U11, severity FATAL, message 'data TLB error interrupt'; the same template recurs at record 17745 on R20-M0-N2-C:J14-U01 and record 17758 on R20-M0-ND-C:J13-U11. — R2 (record), R3 (nearby events) (row-level evidence, not a frequency)
- **F3** In 2005-06-05 07:00–07:15 there are 76 FATAL entries, each with a different entity_id and count 1; the returned entity_ids include racks R20, R21, R24 and R25. — R7 (count)
- **F4** KERNEL FATAL hourly counts on 2005-06-05 are 0 from 00:00 through 06:00, 206 at 07:00, 95 at 08:00, 342 at 11:00, 152 at 12:00, 304 at 13:00, 567 at 14:00, 2,188 at 15:00 and 0 from 16:00 through 23:00. — R6 (trend)
- **F5** In 2005-09-20 19:00–19:20 the service/severity counts are MMCS ERROR 66,563, DISCOVERY INFO 741 and DISCOVERY SEVERE 47; no other service/severity pair is present. — R8 (count)
- **F6** The MMCS anchor is record 3395426 at 2005-09-20 19:05:58, severity ERROR, message 'idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoTransportMgr::SendPacket(IdoUdpMgr*, BglCtlPavTrace*)'. — R9 (nearby events) (row-level evidence, not a frequency)
- **F7** Immediately before the MMCS anchor, records 3394892–3394916 (2005-09-20 18:19:45–18:21:06) contain the same assertion and two 'Ido packet timeout' lines; immediately after the anchor, records 3395427–3395436 contain ten more identical assertions in the same second. — R9 (nearby events) (row-level evidence, not a frequency)
- **F8** Corpus-wide the top ERROR template is the same idoproxydb SendPacket assertion with 84,168 entries; the next two are a node-card status template with 16,896 entries and 'Ido packet timeout' with 4,276 entries. — R11 (count)
- **F9** The top FATAL template is 'data TLB error interrupt' with 152,734 entries; other top templates include 'data storage interrupt' 63,491, 'instruction address: <*>' 47,563, ciod link-severed 26,546, ciod invalid or missing program image 20,480, and 'rts: kernel terminated for reason <*>' 18,587. — R5 (count)

**Hypotheses** (not confirmed by the logs)
- **F10** The 2005-09-20 ERROR episode is an MMCS control-system iDo transport failure in IdoTransportMgr::SendPacket, not a compute-node failure; the rows support the component and assertion text but not the trigger. — R8 (count), R9 (nearby events), R11 (count)
- **F11** The 2005-06-05 07:08 FATAL burst is a multi-node, multi-rack pattern rather than a single-node hardware fault; whether a running job or a hardware condition common to racks R20, R21, R24 and R25 caused it is unknown. — R3 (nearby events), R6 (trend), R7 (count)

**Limitations** (what the data does not establish)
- **F12** The trigger for the MMCS iDo assertion is not identified; the retrieved 19:00–19:20 aggregate has no message-level MMCS INFO rows, so an MMCS restart or link transition cannot be confirmed or excluded from that result. — R8 (count)
- **F13** The trigger for the KERNEL TLB exceptions is not identified; the retrieved rows show no job-start, LOAD_MESSAGE, loader or 'kernel terminated' boundary immediately before the first FATAL line. — R3 (nearby events)
- **F14** The FATAL and ERROR template lists are top-10 results and are truncated, so additional templates exist beyond those shown. — R5 (count), R11 (count)
- **F15** The anomaly Label field (for example KERNDTLB) is not available from any query tool, so the TLB entries cannot be classified by that label.

**Recommended next steps**
1. **Inspect the unfiltered MMCS INFO/SEVERE and DISCOVERY event stream in 2005-09-20 19:00:00–19:05:59, around record ids 3395000–3395425, for an MMCS or idoproxydb restart, iDo card or fabric-manager re-initialisation, or link-down/COMMUNICATION_ERROR entry.** — These are the rows immediately before the first MMCS assertion; a restart or link-down there would confirm an external iDo transport trigger, while its absence would refute that and point to an internal assertion path. (owner: BG/L Control System; based on F6, F7, F10, F12)
2. **Retrieve records around 17700–17721 and ciod/loader activity in 2005-06-05 07:07:00–07:08:12, looking for a job-start boundary, LOAD_MESSAGE, or kernel-terminated context before the first 'data TLB error interrupt'.** — A job-start or load boundary would support a job/application-scale trigger; absent that, per-node hardware counters or ciod errors would support a common hardware fault, which is currently unresolved. (owner: BG/L Kernel; based on F2, F3, F4, F11, F13)

Query cost: 11 queries in 0.49s, slowest R1 at 0.08s (16% of the total), ~4,955,783 rows scanned, ~10,052,298 rows/s.

### Recommended action
1. **Inspect the unfiltered MMCS INFO/SEVERE and DISCOVERY event stream in 2005-09-20 19:00:00–19:05:59, around record ids 3395000–3395425, for an MMCS or idoproxydb restart, iDo card or fabric-manager re-initialisation, or link-down/COMMUNICATION_ERROR entry.** — These are the rows immediately before the first MMCS assertion; a restart or link-down there would confirm an external iDo transport trigger, while its absence would refute that and point to an internal assertion path. (owner: BG/L Control System; based on F6, F7, F10, F12)
2. **Retrieve records around 17700–17721 and ciod/loader activity in 2005-06-05 07:07:00–07:08:12, looking for a job-start boundary, LOAD_MESSAGE, or kernel-terminated context before the first 'data TLB error interrupt'.** — A job-start or load boundary would support a job/application-scale trigger; absent that, per-node hardware counters or ciod errors would support a common hardware fault, which is currently unresolved. (owner: BG/L Kernel; based on F2, F3, F4, F11, F13)

## Team lead
**Question:** Which problems involving BGL FATAL and ERROR events recur, how often, and which components or teams are involved?

**Route:** `find_evidence` · **Effort:** High · **Datasets:** bgl

### Tool calls
| id | tool | kind | arguments | rows | truncated |
| --- | --- | --- | --- | --- | --- |
| R1 | list_datasets_tool | catalog | `{}` | 4 | no |
| R2 | count_by_category_tool | aggregate | `{"dataset": "bgl", "start_time": "2005-06-03 22:42:50", "end_time": "2006-01-04 16:00:06", "type": "template", "categor…` | 25 | yes |
| R3 | count_by_category_tool | aggregate | `{"dataset": "bgl", "start_time": "2005-06-03 22:42:50", "end_time": "2006-01-04 16:00:06", "type": "template", "categor…` | 25 | yes |
| R4 | count_by_category_tool | aggregate | `{"category": "FATAL", "dataset": "bgl", "end_time": "2006-01-04 16:00:06", "limit": 15, "start_time": "2005-06-03 22:42…` | 4 | no |
| R5 | count_by_category_tool | aggregate | `{"category": "ERROR", "dataset": "bgl", "end_time": "2006-01-04 16:00:06", "limit": 15, "start_time": "2005-06-03 22:42…` | 3 | no |
| R6 | count_by_team_tool | aggregate | `{"datasets": ["bgl"], "end_time": "2006-01-04 16:00:06", "start_time": "2005-06-03 22:42:50"}` | 15 | no |
| R7 | get_trend_tool | trend | `{"dataset": "bgl", "start_time": "2005-06-03 22:42:50", "end_time": "2006-01-04 16:00:06", "type": ["template", "data T…` | 32 | no |
| R8 | get_trend_tool | trend | `{"dataset": "bgl", "start_time": "2005-06-03 22:42:50", "end_time": "2006-01-04 16:00:06", "type": ["template", "idopro…` | 32 | no |
| R9 | search_logs_tool | search | `{"dataset": "bgl", "query_text": "data TLB error interrupt", "category": "FATAL", "start_time": "2005-06-03 22:42:50", …` | 2 | yes |
| R10 | search_logs_tool | search | `{"dataset": "bgl", "query_text": "idoproxydb hit ASSERT condition assertion expression idotransportmgr SendPacket", "ca…` | 1 | yes |
| R11 | get_trend_tool | trend | `{"dataset": "bgl", "end_time": "2006-01-04 16:00:06", "start_time": "2005-06-03 22:42:50", "type": ["template", "ciod: …` | 32 | no |
| R12 | priority_score | derived | `{"dimension": "template", "from": ["R2", "R3", "R7", "R8", "R11"]}` | 10 | yes |

### Executed queries
**R1** list_datasets_tool

```sql
SELECT table_name FROM information_schema.tables WHERE table_name IN ('bgl', 'openstack', 'otel_ext', 'otel_ext_meta');
SELECT "Level" AS severity, count(*) AS count, min("Timestamp") AS first_ts, max("Timestamp") AS last_ts
FROM "bgl"
GROUP BY "Level"
ORDER BY count DESC;
SELECT "Level" AS severity, count(*) AS count, min("Timestamp") AS first_ts, max("Timestamp") AS last_ts
FROM "openstack"
GROUP BY "Level"
ORDER BY count DESC;
SELECT count(*) AS n FROM "otel_ext_idx";
SELECT window_start, window_end, severities, indexed_rows, built_at FROM "otel_ext_meta" ORDER BY built_at DESC LIMIT 1
```

**R2** count_by_category_tool

```sql
SELECT "EventTemplate" AS "template", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "Level" = 'FATAL'
GROUP BY "EventTemplate", "Level"
ORDER BY count DESC
LIMIT 25
```

**R3** count_by_category_tool

```sql
SELECT "EventTemplate" AS "template", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "Level" = 'ERROR'
GROUP BY "EventTemplate", "Level"
ORDER BY count DESC
LIMIT 25
```

**R4** count_by_category_tool

```sql
SELECT "Component" AS "service", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "Level" = 'FATAL'
GROUP BY "Component", "Level"
ORDER BY count DESC
LIMIT 15
```

**R5** count_by_category_tool

```sql
SELECT "Component" AS "service", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "Level" = 'ERROR'
GROUP BY "Component", "Level"
ORDER BY count DESC
LIMIT 15
```

**R6** count_by_team_tool

```sql
SELECT table_name FROM information_schema.tables WHERE table_name IN ('bgl', 'team_ownership');
SELECT dataset, team, severity, count FROM (SELECT 'bgl' AS dataset, COALESCE(o.team, 'unowned') AS team, t."Level" AS severity, count(*) AS count FROM "bgl" AS t LEFT JOIN "team_ownership" AS o ON o.dataset = 'BGL' AND o.component = t."Component" WHERE "t"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "t"."Timestamp" < '2006-01-04 16:00:06'::timestamptz GROUP BY COALESCE(o.team, 'unowned'), t."Level") AS counted ORDER BY count DESC, dataset, team, severity
```

**R7** get_trend_tool

```sql
WITH bucket_counts AS MATERIALIZED (
SELECT CAST(date_trunc('week', "Timestamp") AS timestamptz) AS bucket, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "EventTemplate" = 'data TLB error interrupt'
GROUP BY CAST(date_trunc('week', "Timestamp") AS timestamptz)
),
grid AS (
SELECT s.bucket
FROM generate_series(
date_trunc('week', '2005-06-03 22:42:50'::timestamptz), '2006-01-04 16:00:06'::timestamptz,
CAST('1 week' AS INTERVAL)
) AS s(bucket)
WHERE s.bucket < '2006-01-04 16:00:06'::timestamptz
),
filled AS (
SELECT g.bucket, COALESCE(c.count, 0) AS count
FROM grid g
LEFT JOIN bucket_counts c ON c.bucket = g.bucket
)
SELECT
bucket,
count,
LAG(count) OVER (ORDER BY bucket) AS prev_count,
CASE
WHEN LAG(count) OVER (ORDER BY bucket) IS NULL THEN 'initial'
WHEN count > LAG(count) OVER (ORDER BY bucket) THEN 'up'
WHEN count < LAG(count) OVER (ORDER BY bucket) THEN 'down'
ELSE 'flat'
END AS trend
FROM filled
ORDER BY bucket
LIMIT 240
```

**R8** get_trend_tool

```sql
WITH bucket_counts AS MATERIALIZED (
SELECT CAST(date_trunc('week', "Timestamp") AS timestamptz) AS bucket, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "EventTemplate" = 'idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int IdoTransportMgr::SendPacket(IdoUdpMgr*, BglCtlPavTrace*)'
GROUP BY CAST(date_trunc('week', "Timestamp") AS timestamptz)
),
grid AS (
SELECT s.bucket
FROM generate_series(
date_trunc('week', '2005-06-03 22:42:50'::timestamptz), '2006-01-04 16:00:06'::timestamptz,
CAST('1 week' AS INTERVAL)
) AS s(bucket)
WHERE s.bucket < '2006-01-04 16:00:06'::timestamptz
),
filled AS (
SELECT g.bucket, COALESCE(c.count, 0) AS count
FROM grid g
LEFT JOIN bucket_counts c ON c.bucket = g.bucket
)
SELECT
bucket,
count,
LAG(count) OVER (ORDER BY bucket) AS prev_count,
CASE
WHEN LAG(count) OVER (ORDER BY bucket) IS NULL THEN 'initial'
WHEN count > LAG(count) OVER (ORDER BY bucket) THEN 'up'
WHEN count < LAG(count) OVER (ORDER BY bucket) THEN 'down'
ELSE 'flat'
END AS trend
FROM filled
ORDER BY bucket
LIMIT 240
```

**R9** search_logs_tool

```sql
WITH fused AS (
SELECT rid, ROW_NUMBER() OVER (ORDER BY s DESC) AS rank
FROM (
SELECT "i"."LineId" AS rid, BM25("i".tableoid) AS s
FROM "bgl_idx" "i" JOIN "bgl" "bt" ON "bt"."LineId" = "i"."LineId"
WHERE "i"."Content" @@ ts_any([ts_levenshtein('data', 1), ts_levenshtein('TLB', 1), ts_levenshtein('error', 1), ts_levenshtein('interrupt', 1)]) AND "i"."Level" @@ 'FATAL' AND "bt"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "bt"."Timestamp" < '2006-01-04 16:00:06'::timestamptz
ORDER BY s DESC
LIMIT 100
) lex
)
SELECT t."LineId" AS "record_id", t."Timestamp" AS "ts", t."Level" AS "severity", t."Component" AS "service", t."Node" AS "entity_id", t."Content" AS "message",
SUM(1.0 / (60 + f.rank)) AS relevance
FROM fused f
JOIN "bgl" t ON t."LineId" = f.rid
WHERE "t"."Level" = 'FATAL' AND "t"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "t"."Timestamp" < '2006-01-04 16:00:06'::timestamptz
GROUP BY t."LineId", t."Timestamp", t."Level", t."Component", t."Node", t."Content"
ORDER BY relevance DESC
LIMIT 2;
SELECT count(*) AS n
FROM "bgl_idx" "i" JOIN "bgl" "bt" ON "bt"."LineId" = "i"."LineId"
WHERE "i"."Content" @@ ts_any([ts_levenshtein('data', 1), ts_levenshtein('TLB', 1), ts_levenshtein('error', 1), ts_levenshtein('interrupt', 1)]) AND "i"."Level" @@ 'FATAL' AND "bt"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "bt"."Timestamp" < '2006-01-04 16:00:06'::timestamptz
```

**R10** search_logs_tool

```sql
WITH fused AS (
SELECT rid, ROW_NUMBER() OVER (ORDER BY s DESC) AS rank
FROM (
SELECT "i"."LineId" AS rid, BM25("i".tableoid) AS s
FROM "bgl_idx" "i" JOIN "bgl" "bt" ON "bt"."LineId" = "i"."LineId"
WHERE "i"."Content" @@ ts_any([ts_levenshtein('idoproxydb', 1), ts_levenshtein('hit', 1), ts_levenshtein('ASSERT', 1), ts_levenshtein('condition', 1), ts_levenshtein('assertion', 1), ts_levenshtein('expression', 1), ts_levenshtein('idotransportmgr', 1), ts_levenshtein('SendPacket', 1)]) AND "i"."Level" @@ 'ERROR' AND "bt"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "bt"."Timestamp" < '2006-01-04 16:00:06'::timestamptz
ORDER BY s DESC
LIMIT 100
) lex
)
SELECT t."LineId" AS "record_id", t."Timestamp" AS "ts", t."Level" AS "severity", t."Component" AS "service", t."Node" AS "entity_id", t."Content" AS "message",
SUM(1.0 / (60 + f.rank)) AS relevance
FROM fused f
JOIN "bgl" t ON t."LineId" = f.rid
WHERE "t"."Level" = 'ERROR' AND "t"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "t"."Timestamp" < '2006-01-04 16:00:06'::timestamptz
GROUP BY t."LineId", t."Timestamp", t."Level", t."Component", t."Node", t."Content"
ORDER BY relevance DESC
LIMIT 1;
SELECT count(*) AS n
FROM "bgl_idx" "i" JOIN "bgl" "bt" ON "bt"."LineId" = "i"."LineId"
WHERE "i"."Content" @@ ts_any([ts_levenshtein('idoproxydb', 1), ts_levenshtein('hit', 1), ts_levenshtein('ASSERT', 1), ts_levenshtein('condition', 1), ts_levenshtein('assertion', 1), ts_levenshtein('expression', 1), ts_levenshtein('idotransportmgr', 1), ts_levenshtein('SendPacket', 1)]) AND "i"."Level" @@ 'ERROR' AND "bt"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "bt"."Timestamp" < '2006-01-04 16:00:06'::timestamptz
```

**R11** get_trend_tool

```sql
WITH bucket_counts AS MATERIALIZED (
SELECT CAST(date_trunc('week', "Timestamp") AS timestamptz) AS bucket, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "EventTemplate" = 'ciod: Error reading message prefix on CioStream socket to <*>.<*>.<*>.<*>:<*>, Link has been severed'
GROUP BY CAST(date_trunc('week', "Timestamp") AS timestamptz)
),
grid AS (
SELECT s.bucket
FROM generate_series(
date_trunc('week', '2005-06-03 22:42:50'::timestamptz), '2006-01-04 16:00:06'::timestamptz,
CAST('1 week' AS INTERVAL)
) AS s(bucket)
WHERE s.bucket < '2006-01-04 16:00:06'::timestamptz
),
filled AS (
SELECT g.bucket, COALESCE(c.count, 0) AS count
FROM grid g
LEFT JOIN bucket_counts c ON c.bucket = g.bucket
)
SELECT
bucket,
count,
LAG(count) OVER (ORDER BY bucket) AS prev_count,
CASE
WHEN LAG(count) OVER (ORDER BY bucket) IS NULL THEN 'initial'
WHEN count > LAG(count) OVER (ORDER BY bucket) THEN 'up'
WHEN count < LAG(count) OVER (ORDER BY bucket) THEN 'down'
ELSE 'flat'
END AS trend
FROM filled
ORDER BY bucket
LIMIT 240
```

**R12** computed in code from R2, R3, R7, R8, R11: score = 0.4*volume_share + 0.3*error_share + 0.2*trend_up + 0.1*breadth (unmeasured terms dropped, weights renormalized)

### Relevant results
**R1** (catalog, first 4 of 4 rows)

| dataset | kind | exact_counts | available | rows | rows_exact | first_timestamp | last_timestamp | span_seconds | max_window_seconds | whole_span_fits_one_window | window_start | window_end | levels | timestamp_source | note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bgl | native_table | True | True | 4747963 | True | 2005-06-03 22:42:50+00:00 | 2006-01-04 16:00:05+00:00 | 18551835.0 | 31536000 | True | 2005-06-03 22:42:50 | 2006-01-04 16:00:06 | [{'severity': 'INFO', 'count': 3735823}, {'severity': 'FATAL', 'count': 855501}, {'severity': 'ERROR', 'count': 112355}… | rows | Exact: counted over every row. Each level's count is the number of log entries at that severity in the whole dataset. |
| openstack | native_table | True | True | 207820 | True | 2017-05-14 19:39:01.445000+00:00 | 2017-05-17 12:02:35.320000+00:00 | 231813.875 | 7776000 | True | 2017-05-14 19:39:01 | 2017-05-17 12:02:36 | [{'severity': 'INFO', 'count': 204506}, {'severity': 'WARNING', 'count': 3118}, {'severity': 'ERROR', 'count': 194}, {'… | rows | Exact: counted over every row. Each level's count is the number of log entries at that severity in the whole dataset. |
| otel_s3 | remote_parquet | False | True | 1000000000 | True | 2025-09-23 00:00:00+00:00 | 2025-09-23 00:29:24.039037+00:00 | 1764.039 | 300 | False | 2025-09-23 00:00:00+00 | 2025-09-23 00:29:25+00 | [{'severity': 'info', 'count': None}, {'severity': 'error', 'count': None}, {'severity': 'information', 'count': None},… | parquet_footers | Rows and span from the footers of 1 file(s) (1,624 row groups); no row was read. Levels are the declared vocabulary, NO… |
| otel_slice | external_index | True | True | 360519 | True | 2025-09-23 00:00:04+00:00 | 2025-09-23 00:00:08+00:00 | 4.0 | 86400 | True | 2025-09-23 00:00:04+00 | 2025-09-23 00:00:08+00 | [{'severity': 'error', 'count': None}, {'severity': 'fatal', 'count': None}, {'severity': 'warn', 'count': None}] | index_build_record | The timestamps are the window the index was BUILT over (end exclusive), not observed first/last rows; levels are the se… |

**R2** (aggregate, first 5 of 25 rows)

| template | severity | count |
| --- | --- | --- |
| data TLB error interrupt | FATAL | 152734 |
| data storage interrupt | FATAL | 63491 |
| instruction address: <*> | FATAL | 47563 |
| ciod: Error reading message prefix on CioStream socket to <*>.<*>.<*>.<*>:<*>, Link has been severed | FATAL | 26546 |
| ciod: Error loading /bgl/apps/scaletest/performance/MINIBEN/mb_243_0810/allreduce.rts: invalid or missing program image… | FATAL | 20480 |

**R3** (aggregate, first 5 of 25 rows)

| template | severity | count |
| --- | --- | --- |
| idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | ERROR | 84168 |
| Node card status: no ALERTs are active. Clock Mode is Low. Clock Select is Midplane. Phy JTAG Reset is asserted. ASIC J… | ERROR | 16896 |
| Ido packet timeout | ERROR | 4276 |
| Found invalid node ecid in processor card slot J04, ecid <*> | ERROR | 342 |
| Found invalid node ecid in processor card slot J06, ecid <*> | ERROR | 342 |

**R4** (aggregate, first 4 of 4 rows)

| service | severity | count |
| --- | --- | --- |
| KERNEL | FATAL | 626756 |
| APP | FATAL | 228536 |
| LINKCARD | FATAL | 206 |
| MMCS | FATAL | 3 |

**R5** (aggregate, first 3 of 3 rows)

| service | severity | count |
| --- | --- | --- |
| MMCS | ERROR | 88529 |
| DISCOVERY | ERROR | 23825 |
| SERV_NET | ERROR | 1 |

**R6** (aggregate, first 5 of 15 rows)

| dataset | team | severity | count |
| --- | --- | --- | --- |
| bgl | BG/L Kernel | INFO | 3698211 |
| bgl | BG/L Kernel | FATAL | 855292 |
| bgl | BG/L Control System | ERROR | 88529 |
| bgl | Hardware Ops | INFO | 36906 |
| bgl | Hardware Ops | ERROR | 23825 |

**R7** (trend, first 5 of 32 rows)

| bucket | count | prev_count | trend |
| --- | --- | --- | --- |
| 2005-05-30 00:00:00+00:00 | 477 |  | initial |
| 2005-06-06 00:00:00+00:00 | 152182 | 477 | up |
| 2005-06-13 00:00:00+00:00 | 0 | 152182 | down |
| 2005-06-20 00:00:00+00:00 | 0 | 0 | flat |
| 2005-06-27 00:00:00+00:00 | 0 | 0 | flat |

**R8** (trend, first 5 of 32 rows)

| bucket | count | prev_count | trend |
| --- | --- | --- | --- |
| 2005-05-30 00:00:00+00:00 | 0 |  | initial |
| 2005-06-06 00:00:00+00:00 | 0 | 0 | flat |
| 2005-06-13 00:00:00+00:00 | 0 | 0 | flat |
| 2005-06-20 00:00:00+00:00 | 0 | 0 | flat |
| 2005-06-27 00:00:00+00:00 | 0 | 0 | flat |

**R9** (search, first 2 of 2 rows)

| record_id | ts | severity | service | entity_id | message | relevance |
| --- | --- | --- | --- | --- | --- | --- |
| 222723 | 2005-06-12 00:17:45+00:00 | FATAL | KERNEL | R30-M0-N9-C:J16-U01 | data TLB error interrupt | 0.01639344262295082 |
| 222803 | 2005-06-12 00:17:58+00:00 | FATAL | KERNEL | R30-M0-N9-C:J16-U01 | data TLB error interrupt | 0.016129032258064516 |

**R10** (search, first 1 of 1 rows)

| record_id | ts | severity | service | entity_id | message | relevance |
| --- | --- | --- | --- | --- | --- | --- |
| 2935903 | 2005-08-03 22:48:44+00:00 | ERROR | MMCS | NULL | idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoT… | 0.01639344262295082 |

**R11** (trend, first 5 of 32 rows)

| bucket | count | prev_count | trend |
| --- | --- | --- | --- |
| 2005-05-30 00:00:00+00:00 | 0 |  | initial |
| 2005-06-06 00:00:00+00:00 | 0 | 0 | flat |
| 2005-06-13 00:00:00+00:00 | 0 | 0 | flat |
| 2005-06-20 00:00:00+00:00 | 0 | 0 | flat |
| 2005-06-27 00:00:00+00:00 | 0 | 0 | flat |

**R12** (derived, first 5 of 10 rows)

| rank | dataset | template | non_info_rows | error_rows | volume_share_pct | error_share_pct | trend_up | breadth | score |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | bgl | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | 84168 | 84168 | 13.5 | 100.0 | 1.0 | n/a | 0.615 |
| 2 | bgl | ciod: Error reading message prefix on CioStream socket to <*>.<*>.<*>.<*>:<*>, Link has been severed | 26546 | 26546 | 4.3 | 100.0 | 1.0 | n/a | 0.574 |
| 3 | bgl | data TLB error interrupt | 152734 | 152734 | 24.5 | 100.0 | 0.0 | n/a | 0.442 |
| 4 | bgl | data storage interrupt | 63491 | 63491 | 10.2 | 100.0 | n/a | n/a | 0.379 |
| 5 | bgl | instruction address: <*> | 47563 | 47563 | 7.6 | 100.0 | n/a | n/a | 0.367 |

### Evidence report
**Summary:** Scope was the full BGL window 2005-06-03 22:42:50 → 2006-01-04 16:00:06 UTC (4,747,963 log entries), with severities measured over every row: INFO 3,735,823, FATAL 855,501, ERROR 112,355, WARNING 23,357, SEVERE 19,213, FAILURE 1,714. All figures are counts of log entries, not incidents. Recurring FATAL patterns: 'data TLB error interrupt' 152,734 entries, 'data storage interrupt' 63,491, 'instruction address: <*>' 47,563, 'ciod: Error reading message prefix on CioStream socket ... Link has been severed' 26,546, ciod Exec format error on allreduce.rts 20,480, 'rts: kernel terminated for reason <*>' 18,587, plus many register/state-dump templates at ~6,500 each. Recurring ERROR patterns: 'idoproxydb hit ASSERT condition ... IdoTransportMgr::SendPacket' 84,168, node-card PGOOD/MPGOOD status 16,896, 'Ido packet timeout' 4,276, 'Found invalid node ecid in processor card slot J02-J17' 342 each (5,472 across the 16 listed), 'Bad cable going into LinkCard' 206. Trend shapes differ sharply: the TLB interrupt FATALs are a single June burst (152,182 in the week of 2005-06-06) then near-zero; the idoproxydb ASSERT ERRORs occur in only two weeks (19,320 week of 2005-08-01; 64,848 week of 2005-09-19) with zero in the other 30 weeks; the ciod 'Link has been severed' FATALs are sustained across 8 consecutive non-zero weeks from 2005-11-14 to 2006-01-02 and are still active at the end of the period. Team ownership: BG/L Kernel (APP, KERNEL) accounts for 855,292 of the 855,501 FATAL entries (KERNEL 626,756, APP 228,536 by component); BG/L Control System (MMCS 3 FATAL, 88,529 ERROR, 49 FAILURE) and Hardware Ops (206 FATAL, 23,825 ERROR, 23,356 WARNING, 19,213 SEVERE, 1,665 FAILURE; components DISCOVERY 23,825 ERROR, LINKCARD 206 FATAL) account for essentially all ERROR entries; Network Ops (SERV_NET) has 1 ERROR and 1 WARNING. Nothing in the tool log failed or returned no rows.
- **E1** list_datasets_tool — available; results R1
- **E2** count_by_category_tool — available; results R2
- **E3** count_by_category_tool — available; results R3
- **E4** count_by_category_tool — available; results R4
- **E5** count_by_category_tool — available; results R5
- **E6** count_by_team_tool — available; results R6
- **E7** get_trend_tool — available; results R7
- **E8** get_trend_tool — available; results R8
- **E9** search_logs_tool — available; results R9
- **E10** search_logs_tool — available; results R10
- **E11** get_trend_tool — available; results R11
- Hypothesis (supported): The ciod 'Link has been severed' FATALs are an ongoing, still-active failure mode owned by the BG/L Control System rather than a one-off event.; next check: Group the ciod 'Link has been severed' entries by node and by day in the 2005-11-14 → 2006-01-04 window to see whether the ongoing volume is a few persistently affected nodes or fleet-wide.
- Hypothesis (inconclusive): A large share of the FATAL volume reflects a small number of failure episodes emitting many register/state-dump log entries, rather than a correspondingly large number of independent failures.; next check: Group the June 2005 FATAL entries by node and timestamp cluster to count distinct crash episodes instead of entries.
- Hypothesis (supported): The idoproxydb ASSERT ERRORs are episodic control-system events confined to two windows (early August and mid/late September 2005) rather than a steady background rate.; next check: Group the ASSERT entries by day within the two burst weeks to identify how many discrete onset times and nodes are involved.

### Answer for the team lead
**Period and scope.** The analysis covers the full BGL catalog window 2005-06-03 22:42:50 to 2006-01-04 16:00:06 UTC and all 4,747,963 BGL log entries; counts are exact and are log entries, not incidents. Severity totals over every row are FATAL 855,501 and ERROR 112,355, with INFO 3,735,823, WARNING 23,357, SEVERE 19,213 and FAILURE 1,714 also present. The template rankings below cover the FATAL and ERROR levels requested; WARNING and the other non-informational levels are reported only as totals.

**Highest-count FATAL templates.** The FATAL ranking is dominated by kernel and ciod message families; the list is truncated at 25 templates, so a lower-count tail is not enumerated.

_R2: count_by_category_tool_

| template | severity | count |
| --- | --- | --- |
| data TLB error interrupt | FATAL | 152734 |
| data storage interrupt | FATAL | 63491 |
| instruction address: <*> | FATAL | 47563 |
| ciod: Error reading message prefix on CioStream socket to <*>.<*>.<*>.<*>:<*>, Link has been severed | FATAL | 26546 |
| ciod: Error loading /bgl/apps/scaletest/performance/MINIBEN/mb_243_0810/allreduce.rts: invalid or missing program image… | FATAL | 20480 |

**Highest-count ERROR templates.** The ERROR ranking is led by a single control-system assertion template, with hardware-status and Ido timeout templates behind it; this list is also truncated at 25 templates.

_R3: count_by_category_tool_

| template | severity | count |
| --- | --- | --- |
| idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | ERROR | 84168 |
| Node card status: no ALERTs are active. Clock Mode is Low. Clock Select is Midplane. Phy JTAG Reset is asserted. ASIC J… | ERROR | 16896 |
| Ido packet timeout | ERROR | 4276 |
| Found invalid node ecid in processor card slot J04, ecid <*> | ERROR | 342 |
| Found invalid node ecid in processor card slot J06, ecid <*> | ERROR | 342 |

**Trend and recurrence.** The top templates do not share one recurrence pattern. The data TLB error interrupt FATALs are a single June burst: 477 entries in the 2005-05-30 bucket and 152,182 in the week of 2005-06-06, followed by zero in most later weeks. The idoproxydb ASSERT ERRORs are episodic: zero in 30 of 32 weekly buckets, with 19,320 in the week of 2005-08-01 and 64,848 in the week of 2005-09-19. The ciod Link has been severed FATALs are the only top pattern that is sustained at the end of the window: non-zero in eight consecutive weeks from 2005-11-14 to 2006-01-02, at 1,211, 8,173, 6,008, 919, 6,849, 618, 2,638 and 130 entries; the template has 26,546 entries overall. No weekly trend was queried for data storage interrupt or instruction address, so their recurrence shape is unknown. Retrieved examples, not frequency evidence, include a KERNEL TLB entry on R30-M0-N9-C:J16-U01 at 2005-06-12 00:17:45 and an MMCS ASSERT entry at 2005-08-03 22:48:44 with ASSERT expression=0.

**Teams and components.** FATAL entries are almost entirely BG/L Kernel: 855,292 team total, split across KERNEL at 626,756 and APP at 228,536. Hardware Ops has 206 FATAL entries through LINKCARD, and BG/L Control System has 3 FATAL entries through MMCS. ERROR entries are split between BG/L Control System at 88,529 through MMCS and Hardware Ops at 23,825 through DISCOVERY, with Network Ops contributing 1 through SERV_NET. Hardware Ops also owns the WARNING, SEVERE and FAILURE totals shown in the team table. The ciod template is not tied to a listed component in the ownership mapping, so its exact owning team is not established from these counts.

_R6: count_by_team_tool_

| dataset | team | severity | count |
| --- | --- | --- | --- |
| bgl | BG/L Kernel | INFO | 3698211 |
| bgl | BG/L Kernel | FATAL | 855292 |
| bgl | BG/L Control System | ERROR | 88529 |
| bgl | Hardware Ops | INFO | 36906 |
| bgl | Hardware Ops | ERROR | 23825 |

**Priority ranking.** The computed ranking weighs volume share, error share, trend-up and breadth; breadth is unmeasured and was dropped, with the remaining weights renormalized. It ranks the idoproxydb ASSERT first with score 0.615, the ciod Link has been severed FATAL second with 0.574 and the June data TLB burst third with 0.442. The ranking is truncated at 10 rows.

_R12: priority_score_

| rank | dataset | template | non_info_rows | error_rows | volume_share_pct | error_share_pct | trend_up | breadth | score |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | bgl | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | 84168 | 84168 | 13.5 | 100.0 | 1.0 | n/a | 0.615 |
| 2 | bgl | ciod: Error reading message prefix on CioStream socket to <*>.<*>.<*>.<*>:<*>, Link has been severed | 26546 | 26546 | 4.3 | 100.0 | 1.0 | n/a | 0.574 |
| 3 | bgl | data TLB error interrupt | 152734 | 152734 | 24.5 | 100.0 | 0.0 | n/a | 0.442 |
| 4 | bgl | data storage interrupt | 63491 | 63491 | 10.2 | 100.0 | n/a | n/a | 0.379 |
| 5 | bgl | instruction address: <*> | 47563 | 47563 | 7.6 | 100.0 | n/a | n/a | 0.367 |

**Investigate first.** The computed ranking places the idoproxydb ASSERT first on measured count, with the ciod Link has been severed pattern second and the June TLB burst third. The idoproxydb pattern is episodic rather than continuous, while the ciod pattern is the only top-ranked template still active at the end of the window; the TLB burst is the largest single-window volume but is not active after June. Those distinctions set the order of the recommended checks below; they do not identify a root cause.

**Measured** (shown by the cited query results)
- **F1** Scope is the full BGL window 2005-06-03 22:42:50 to 2006-01-04 16:00:06 UTC with 4,747,963 log entries; severity counts are INFO 3,735,823, FATAL 855,501, ERROR 112,355, WARNING 23,357, SEVERE 19,213 and FAILURE 1,714. These are log-entry counts, not incidents. — R1 (value list) (row-level evidence, not a frequency)
- **F2** Top FATAL templates by entries: data TLB error interrupt 152,734; data storage interrupt 63,491; instruction address 47,563; ciod Link has been severed 26,546; ciod Exec format error on allreduce.rts 20,480; rts kernel terminated 18,587. — R2 (count)
- **F3** Top ERROR templates by entries: idoproxydb ASSERT in IdoTransportMgr::SendPacket 84,168; node-card status PGOOD/MPGOOD 16,896; Ido packet timeout 4,276; invalid node ecid in each of 16 listed processor-card slots 342; Bad cable going into LinkCard 206. — R3 (count)
- **F4** The data TLB error interrupt FATALs are a single June burst: 477 entries in the 2005-05-30 bucket and 152,182 in the week of 2005-06-06, then zero in most later weeks. — R7 (trend)
- **F5** The idoproxydb ASSERT ERRORs occur in only two weekly buckets, 19,320 in the week of 2005-08-01 and 64,848 in the week of 2005-09-19, with zero in the other 30 of 32 buckets. — R8 (trend)
- **F6** The ciod Link has been severed FATALs are non-zero in eight consecutive weeks from 2005-11-14 to 2006-01-02, at 1,211, 8,173, 6,008, 919, 6,849, 618, 2,638 and 130 entries, and total 26,546 over the window. — R2 (count), R11 (trend)
- **F7** The computed priority ranking places idoproxydb ASSERT first with score 0.615, ciod Link has been severed second with 0.574, and data TLB error interrupt third with 0.442. — R12 (computed)
- **F8** Team and component counts: BG/L Kernel has 855,292 FATAL entries through KERNEL 626,756 and APP 228,536; Hardware Ops has 206 FATAL through LINKCARD and 23,825 ERROR through DISCOVERY; BG/L Control System has 3 FATAL and 88,529 ERROR through MMCS; Network Ops has 1 ERROR through SERV_NET. — R4 (count), R5 (count), R6 (count)

**Hypotheses** (not confirmed by the logs)
- **F9** The large FATAL volume may reflect a small number of failure episodes emitting many register/state-dump entries, but the data do not establish distinct failure episodes. — R2 (count), R7 (trend)

**Limitations** (what the data does not establish)
- **F10** FATAL and ERROR template rankings are truncated at 25 rows, so lower-count templates were not enumerated; WARNING, SEVERE and FAILURE templates were not broken down. — R2 (count), R3 (count)
- **F11** Node breadth per pattern was not queried, so whether the recurring templates are concentrated on a few nodes or are fleet-wide remains unknown.
- **F12** The exact owning team for the ciod Link has been severed FATALs is not established; the collected team counts show only 3 FATAL entries for BG/L Control System and 855,292 for BG/L Kernel. — R6 (count)

**Recommended next steps**
1. **Within the two idoproxydb burst weeks, group the ASSERT ERROR entries by day and node to count distinct onset times and affected nodes.** — It is the highest-count ERROR template and ranks first, but it is zero in 30 of 32 weeks, so the two burst windows are the relevant recurrence periods. (owner: BG/L Control System; based on F3, F5, F7)
2. **Group the ciod Link has been severed FATAL entries by node and by day across 2005-11-14 to 2006-01-04 to determine whether the sustained volume is concentrated on a few nodes or is fleet-wide.** — This pattern is the only top-ranked template still active at the end of the period, has 26,546 entries, and ranks second; its exact owning team is not established. (based on F6, F7, F12)
3. **Group the June 2005 FATAL entries by node and timestamp cluster to count distinct crash episodes rather than log entries, and check whether the register/state-dump templates cluster with the TLB burst.** — The TLB template has the largest single-template count at 152,734, but its volume is one June week and may reflect many dump lines per failure episode, which the current counts cannot resolve. (owner: BG/L Kernel; based on F2, F4, F9)

Query cost: 11 queries in 0.86s, slowest R9 at 0.21s (24% of the total), ~4,955,783 rows scanned, ~5,762,538 rows/s.

### Recommended action
1. **Within the two idoproxydb burst weeks, group the ASSERT ERROR entries by day and node to count distinct onset times and affected nodes.** — It is the highest-count ERROR template and ranks first, but it is zero in 30 of 32 weeks, so the two burst windows are the relevant recurrence periods. (owner: BG/L Control System; based on F3, F5, F7)
2. **Group the ciod Link has been severed FATAL entries by node and by day across 2005-11-14 to 2006-01-04 to determine whether the sustained volume is concentrated on a few nodes or is fleet-wide.** — This pattern is the only top-ranked template still active at the end of the period, has 26,546 entries, and ranks second; its exact owning team is not established. (based on F6, F7, F12)
3. **Group the June 2005 FATAL entries by node and timestamp cluster to count distinct crash episodes rather than log entries, and check whether the register/state-dump templates cluster with the TLB burst.** — The TLB template has the largest single-template count at 152,734, but its volume is one June week and may reflect many dump lines per failure episode, which the current counts cannot resolve. (owner: BG/L Kernel; based on F2, F4, F9)

## Manager
**Question:** Which observed problems involving BGL FATAL and ERROR events deserve attention first, and what should happen next?

**Route:** `find_evidence` · **Effort:** High · **Datasets:** bgl

### Tool calls
| id | tool | kind | arguments | rows | truncated |
| --- | --- | --- | --- | --- | --- |
| R1 | list_datasets_tool | catalog | `{}` | 4 | no |
| R2 | count_by_team_tool | aggregate | `{"datasets": ["bgl"], "start_time": "2005-06-03 22:42:50", "end_time": "2006-01-04 16:00:06"}` | 15 | no |
| R3 | count_by_category_tool | aggregate | `{"dataset": "bgl", "start_time": "2005-06-03 22:42:50", "end_time": "2006-01-04 16:00:06", "type": "service", "limit": …` | 22 | no |
| R4 | count_by_category_tool | aggregate | `{"dataset": "bgl", "end_time": "2006-01-04 16:00:06", "start_time": "2005-06-03 22:42:50", "type": "template", "categor…` | 12 | yes |
| R5 | count_by_category_tool | aggregate | `{"dataset": "bgl", "end_time": "2006-01-04 16:00:06", "start_time": "2005-06-03 22:42:50", "type": "template", "categor…` | 12 | yes |
| R6 | compare_periods_tool | aggregate | `{"baseline_end": "2005-09-19 00:00:00", "baseline_start": "2005-06-03 22:42:50", "dataset": "bgl", "field": "template",…` | 100 | yes |
| R7 | count_by_category_tool | aggregate | `{"category": "FATAL", "dataset": "bgl", "end_time": "2005-09-19 00:00:00", "limit": 8, "start_time": "2005-06-03 22:42:…` | 8 | yes |
| R8 | count_by_category_tool | aggregate | `{"category": "FATAL", "dataset": "bgl", "end_time": "2006-01-04 16:00:06", "limit": 8, "start_time": "2005-09-19 00:00:…` | 8 | yes |
| R9 | count_by_category_tool | aggregate | `{"category": "ERROR", "dataset": "bgl", "end_time": "2005-09-19 00:00:00", "limit": 8, "start_time": "2005-06-03 22:42:…` | 8 | yes |
| R10 | count_by_category_tool | aggregate | `{"category": "ERROR", "dataset": "bgl", "end_time": "2006-01-04 16:00:06", "limit": 8, "start_time": "2005-09-19 00:00:…` | 8 | yes |
| R11 | search_logs_tool | search | `{"category": "ERROR", "dataset": "bgl", "end_time": "2006-01-04 16:00:06", "limit": 1, "query_text": "idoproxydb hit AS…` | 1 | yes |
| R12 | priority_score | derived | `{"dimension": "template", "from": ["R4", "R5", "R6", "R7", "R8", "R9", "R10"]}` | 10 | yes |

### Executed queries
**R1** list_datasets_tool

```sql
SELECT table_name FROM information_schema.tables WHERE table_name IN ('bgl', 'openstack', 'otel_ext', 'otel_ext_meta');
SELECT "Level" AS severity, count(*) AS count, min("Timestamp") AS first_ts, max("Timestamp") AS last_ts
FROM "bgl"
GROUP BY "Level"
ORDER BY count DESC;
SELECT "Level" AS severity, count(*) AS count, min("Timestamp") AS first_ts, max("Timestamp") AS last_ts
FROM "openstack"
GROUP BY "Level"
ORDER BY count DESC;
SELECT count(*) AS n FROM "otel_ext_idx";
SELECT window_start, window_end, severities, indexed_rows, built_at FROM "otel_ext_meta" ORDER BY built_at DESC LIMIT 1
```

**R2** count_by_team_tool

```sql
SELECT table_name FROM information_schema.tables WHERE table_name IN ('bgl', 'team_ownership');
SELECT dataset, team, severity, count FROM (SELECT 'bgl' AS dataset, COALESCE(o.team, 'unowned') AS team, t."Level" AS severity, count(*) AS count FROM "bgl" AS t LEFT JOIN "team_ownership" AS o ON o.dataset = 'BGL' AND o.component = t."Component" WHERE "t"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "t"."Timestamp" < '2006-01-04 16:00:06'::timestamptz GROUP BY COALESCE(o.team, 'unowned'), t."Level") AS counted ORDER BY count DESC, dataset, team, severity
```

**R3** count_by_category_tool

```sql
SELECT "Component" AS "service", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz
GROUP BY "Component", "Level"
ORDER BY count DESC
LIMIT 50
```

**R4** count_by_category_tool

```sql
SELECT "EventTemplate" AS "template", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "Level" = 'FATAL'
GROUP BY "EventTemplate", "Level"
ORDER BY count DESC
LIMIT 12
```

**R5** count_by_category_tool

```sql
SELECT "EventTemplate" AS "template", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "Level" = 'ERROR'
GROUP BY "EventTemplate", "Level"
ORDER BY count DESC
LIMIT 12
```

**R6** compare_periods_tool

```sql
WITH baseline AS (
SELECT "EventTemplate" AS grp, "Level" AS sev, count(*) AS n
FROM "bgl" WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2005-09-19 00:00:00'::timestamptz
GROUP BY "EventTemplate", "Level"
), recent AS (
SELECT "EventTemplate" AS grp, "Level" AS sev, count(*) AS n
FROM "bgl" WHERE "Timestamp" >= '2005-09-19 00:00:00'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz
GROUP BY "EventTemplate", "Level"
)
SELECT
COALESCE(r.grp, b.grp) AS "template",
COALESCE(r.sev, b.sev) AS severity,
COALESCE(b.n, 0) AS baseline_count,
COALESCE(r.n, 0) AS recent_count,
COALESCE(r.n, 0) - COALESCE(b.n, 0) AS volume_change,
ROUND(
(COALESCE(r.n, 0) - COALESCE(b.n, 0)) * 100.0
/ NULLIF(b.n, 0), 2
) AS percentage_change
FROM recent r
FULL OUTER JOIN baseline b ON r.grp = b.grp AND r.sev = b.sev
ORDER BY volume_change DESC
LIMIT 100
```

**R7** count_by_category_tool

```sql
SELECT "EventTemplate" AS "template", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2005-09-19 00:00:00'::timestamptz AND "Level" = 'FATAL'
GROUP BY "EventTemplate", "Level"
ORDER BY count DESC
LIMIT 8
```

**R8** count_by_category_tool

```sql
SELECT "EventTemplate" AS "template", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-09-19 00:00:00'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "Level" = 'FATAL'
GROUP BY "EventTemplate", "Level"
ORDER BY count DESC
LIMIT 8
```

**R9** count_by_category_tool

```sql
SELECT "EventTemplate" AS "template", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "Timestamp" < '2005-09-19 00:00:00'::timestamptz AND "Level" = 'ERROR'
GROUP BY "EventTemplate", "Level"
ORDER BY count DESC
LIMIT 8
```

**R10** count_by_category_tool

```sql
SELECT "EventTemplate" AS "template", "Level" AS severity, count(*) AS count
FROM "bgl"
WHERE "Timestamp" >= '2005-09-19 00:00:00'::timestamptz AND "Timestamp" < '2006-01-04 16:00:06'::timestamptz AND "Level" = 'ERROR'
GROUP BY "EventTemplate", "Level"
ORDER BY count DESC
LIMIT 8
```

**R11** search_logs_tool

```sql
WITH fused AS (
SELECT rid, ROW_NUMBER() OVER (ORDER BY s DESC) AS rank
FROM (
SELECT "i"."LineId" AS rid, BM25("i".tableoid) AS s
FROM "bgl_idx" "i" JOIN "bgl" "bt" ON "bt"."LineId" = "i"."LineId"
WHERE "i"."Content" @@ ts_any([ts_levenshtein('idoproxydb', 1), ts_levenshtein('hit', 1), ts_levenshtein('ASSERT', 1), ts_levenshtein('condition', 1), ts_levenshtein('idotransportmgr', 1)]) AND "i"."Level" @@ 'ERROR' AND "bt"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "bt"."Timestamp" < '2006-01-04 16:00:06'::timestamptz
ORDER BY s DESC
LIMIT 100
) lex
)
SELECT t."LineId" AS "record_id", t."Timestamp" AS "ts", t."Level" AS "severity", t."Component" AS "service", t."Node" AS "entity_id", t."Content" AS "message",
SUM(1.0 / (60 + f.rank)) AS relevance
FROM fused f
JOIN "bgl" t ON t."LineId" = f.rid
WHERE "t"."Level" = 'ERROR' AND "t"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "t"."Timestamp" < '2006-01-04 16:00:06'::timestamptz
GROUP BY t."LineId", t."Timestamp", t."Level", t."Component", t."Node", t."Content"
ORDER BY relevance DESC
LIMIT 1;
SELECT count(*) AS n
FROM "bgl_idx" "i" JOIN "bgl" "bt" ON "bt"."LineId" = "i"."LineId"
WHERE "i"."Content" @@ ts_any([ts_levenshtein('idoproxydb', 1), ts_levenshtein('hit', 1), ts_levenshtein('ASSERT', 1), ts_levenshtein('condition', 1), ts_levenshtein('idotransportmgr', 1)]) AND "i"."Level" @@ 'ERROR' AND "bt"."Timestamp" >= '2005-06-03 22:42:50'::timestamptz AND "bt"."Timestamp" < '2006-01-04 16:00:06'::timestamptz
```

**R12** computed in code from R4, R5, R6, R7, R8, R9, R10: score = 0.4*volume_share + 0.3*error_share + 0.2*trend_up + 0.1*breadth (unmeasured terms dropped, weights renormalized)

### Relevant results
**R1** (catalog, first 4 of 4 rows)

| dataset | kind | exact_counts | available | rows | rows_exact | first_timestamp | last_timestamp | span_seconds | max_window_seconds | whole_span_fits_one_window | window_start | window_end | levels | timestamp_source | note |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bgl | native_table | True | True | 4747963 | True | 2005-06-03 22:42:50+00:00 | 2006-01-04 16:00:05+00:00 | 18551835.0 | 31536000 | True | 2005-06-03 22:42:50 | 2006-01-04 16:00:06 | [{'severity': 'INFO', 'count': 3735823}, {'severity': 'FATAL', 'count': 855501}, {'severity': 'ERROR', 'count': 112355}… | rows | Exact: counted over every row. Each level's count is the number of log entries at that severity in the whole dataset. |
| openstack | native_table | True | True | 207820 | True | 2017-05-14 19:39:01.445000+00:00 | 2017-05-17 12:02:35.320000+00:00 | 231813.875 | 7776000 | True | 2017-05-14 19:39:01 | 2017-05-17 12:02:36 | [{'severity': 'INFO', 'count': 204506}, {'severity': 'WARNING', 'count': 3118}, {'severity': 'ERROR', 'count': 194}, {'… | rows | Exact: counted over every row. Each level's count is the number of log entries at that severity in the whole dataset. |
| otel_s3 | remote_parquet | False | True | 1000000000 | True | 2025-09-23 00:00:00+00:00 | 2025-09-23 00:29:24.039037+00:00 | 1764.039 | 300 | False | 2025-09-23 00:00:00+00 | 2025-09-23 00:29:25+00 | [{'severity': 'info', 'count': None}, {'severity': 'error', 'count': None}, {'severity': 'information', 'count': None},… | parquet_footers | Rows and span from the footers of 1 file(s) (1,624 row groups); no row was read. Levels are the declared vocabulary, NO… |
| otel_slice | external_index | True | True | 360519 | True | 2025-09-23 00:00:04+00:00 | 2025-09-23 00:00:08+00:00 | 4.0 | 86400 | True | 2025-09-23 00:00:04+00 | 2025-09-23 00:00:08+00 | [{'severity': 'error', 'count': None}, {'severity': 'fatal', 'count': None}, {'severity': 'warn', 'count': None}] | index_build_record | The timestamps are the window the index was BUILT over (end exclusive), not observed first/last rows; levels are the se… |

**R2** (aggregate, first 5 of 15 rows)

| dataset | team | severity | count |
| --- | --- | --- | --- |
| bgl | BG/L Kernel | INFO | 3698211 |
| bgl | BG/L Kernel | FATAL | 855292 |
| bgl | BG/L Control System | ERROR | 88529 |
| bgl | Hardware Ops | INFO | 36906 |
| bgl | Hardware Ops | ERROR | 23825 |

**R3** (aggregate, first 5 of 22 rows)

| service | severity | count |
| --- | --- | --- |
| KERNEL | INFO | 3698211 |
| KERNEL | FATAL | 626756 |
| APP | FATAL | 228536 |
| MMCS | ERROR | 88529 |
| DISCOVERY | INFO | 35942 |

**R4** (aggregate, first 5 of 12 rows)

| template | severity | count |
| --- | --- | --- |
| data TLB error interrupt | FATAL | 152734 |
| data storage interrupt | FATAL | 63491 |
| instruction address: <*> | FATAL | 47563 |
| ciod: Error reading message prefix on CioStream socket to <*>.<*>.<*>.<*>:<*>, Link has been severed | FATAL | 26546 |
| ciod: Error loading /bgl/apps/scaletest/performance/MINIBEN/mb_243_0810/allreduce.rts: invalid or missing program image… | FATAL | 20480 |

**R5** (aggregate, first 5 of 12 rows)

| template | severity | count |
| --- | --- | --- |
| idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | ERROR | 84168 |
| Node card status: no ALERTs are active. Clock Mode is Low. Clock Select is Midplane. Phy JTAG Reset is asserted. ASIC J… | ERROR | 16896 |
| Ido packet timeout | ERROR | 4276 |
| Found invalid node ecid in processor card slot J05, ecid <*> | ERROR | 342 |
| Found invalid node ecid in processor card slot J10, ecid <*> | ERROR | 342 |

**R6** (aggregate, first 5 of 100 rows)

| template | severity | baseline_count | recent_count | volume_change | percentage_change |
| --- | --- | --- | --- | --- | --- |
| iar <*> dear <*> | INFO | 105082 | 337884 | 232802 | 221.54 |
| <*> microseconds spent in the rbs signal handler during <*> calls. <*> microseconds was the maximum time for a single i… | INFO | 0 | 135096 | 135096 |  |
| <*> total interrupts. <*> critical input interrupts. <*> microseconds total spent on critical input interrupts, <*> mic… | INFO | 0 | 135096 | 135096 |  |
| idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | ERROR | 19320 | 64848 | 45528 | 235.65 |
| ciod: Received signal <*>, code=<*>, errno=<*>, address=<*> | INFO | 0 | 39457 | 39457 |  |

**R7** (aggregate, first 5 of 8 rows)

| template | severity | count |
| --- | --- | --- |
| data TLB error interrupt | FATAL | 152723 |
| data storage interrupt | FATAL | 63488 |
| instruction address: <*> | FATAL | 47385 |
| ciod: Error loading /bgl/apps/scaletest/performance/MINIBEN/mb_243_0810/allreduce.rts: invalid or missing program image… | FATAL | 20480 |
| <*>:<*> <*>:<*> <*>:<*> <*>:<*> | FATAL | 17933 |

**R8** (aggregate, first 5 of 8 rows)

| template | severity | count |
| --- | --- | --- |
| ciod: Error reading message prefix on CioStream socket to <*>.<*>.<*>.<*>:<*>, Link has been severed | FATAL | 26546 |
| ciod: LOGIN chdir(/bgl/apps/SWL/performance/MINIBEN///<*>.<*>.<*>-<*>.<*>.<*>) failed: No such file or directory | FATAL | 11088 |
| ciod: Error reading message prefix after LOAD_MESSAGE on CioStream socket to <*>.<*>.<*>.<*>:<*>: Link has been severed | FATAL | 8091 |
| rts: kernel terminated for reason <*> | FATAL | 6663 |
| Error receiving packet on tree network, expecting type <*> instead of type <*> (softheader=<*> <*> <*> <*>) PSR0=<*> PS… | FATAL | 6144 |

**R9** (aggregate, first 5 of 8 rows)

| template | severity | count |
| --- | --- | --- |
| idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | ERROR | 19320 |
| Node card status: no ALERTs are active. Clock Mode is Low. Clock Select is Midplane. Phy JTAG Reset is asserted. ASIC J… | ERROR | 14727 |
| Ido packet timeout | ERROR | 1012 |
| Found invalid node ecid in processor card slot J05, ecid <*> | ERROR | 342 |
| Found invalid node ecid in processor card slot J12, ecid <*> | ERROR | 342 |

**R10** (aggregate, first 5 of 8 rows)

| template | severity | count |
| --- | --- | --- |
| idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | ERROR | 64848 |
| Ido packet timeout | ERROR | 3264 |
| Node card status: no ALERTs are active. Clock Mode is Low. Clock Select is Midplane. Phy JTAG Reset is asserted. ASIC J… | ERROR | 2169 |
| Node card status: no ALERTs are active. Clock Mode is Low. Clock Select is Midplane. Phy JTAG Reset is asserted. ASIC J… | ERROR | 227 |
| idoproxydb hit ASSERT condition: ASSERT expression=pTargetMgr Source file=idoclientmgr.cpp Source line=<*> Function=int… | ERROR | 82 |

**R11** (search, first 1 of 1 rows)

| record_id | ts | severity | service | entity_id | message | relevance |
| --- | --- | --- | --- | --- | --- | --- |
| 2935920 | 2005-08-03 22:48:46+00:00 | ERROR | MMCS | NULL | idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoT… | 0.01639344262295082 |

**R12** (derived, first 5 of 10 rows)

| rank | dataset | template | non_info_rows | error_rows | volume_share_pct | error_share_pct | trend_up | breadth | score |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | bgl | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | 84168 | 84168 | 13.4 | 100.0 | 1.0 | n/a | 0.615 |
| 2 | bgl | ciod: Error reading message prefix on CioStream socket to <*>.<*>.<*>.<*>:<*>, Link has been severed | 26546 | 26546 | 4.2 | 100.0 | 1.0 | n/a | 0.574 |
| 3 | bgl | ciod: LOGIN chdir(/bgl/apps/SWL/performance/MINIBEN///<*>.<*>.<*>-<*>.<*>.<*>) failed: No such file or directory | 11088 | 11088 | 1.8 | 100.0 | 1.0 | n/a | 0.563 |
| 4 | bgl | Error receiving packet on tree network, expecting type <*> instead of type <*> (softheader=<*> <*> <*> <*>) PSR0=<*> PS… | 6144 | 6144 | 1.0 | 100.0 | 1.0 | n/a | 0.56 |
| 5 | bgl | ciod: Error loading /home/spelce1/HPCC_IBM/Urgent/VNM/64K/vnm.rts: invalid or missing program image, Permission denied | 5120 | 5120 | 0.8 | 100.0 | 1.0 | n/a | 0.559 |

### Evidence report
**Summary:** Prioritization covered the whole BGL dataset, 2005-06-03 22:42:50 → 2006-01-04 16:00:06 — all 4,747,963 log entries, exact counts. Severity baseline: INFO 3,735,823; FATAL 855,501 (18.0%); ERROR 112,355 (2.4%); WARNING 23,357; SEVERE 19,213; FAILURE 1,714. Trend was measured by splitting the window at 2005-09-19 00:00:00 into two roughly equal halves. Ranked by measured count in the recent half plus trend direction, with whole-window totals as tiebreaker: (1) MMCS/BG-L Control System `idoproxydb hit ASSERT condition … idotransportmgr.cpp … IdoTransportMgr::SendPacket` ERROR — 84,168 entries, 19,320 → 64,848 (+236%), 74.9% of all ERROR; (2) APP `ciod` / CioStream FATAL family — 228,536 APP FATAL in total, largest single template 'Error reading message prefix on CioStream socket … Link has been severed' 26,546, 0 → 26,546 in half 2, with related 'after LOAD_MESSAGE' 7,182 → 8,091 and 'Error receiving packet on tree network' 0 → 6,144; (3) KERNEL machine-check FATAL cluster ('data TLB error interrupt' 152,734, 'data storage interrupt' 63,491, 'machine check: i-fetch' 47,563) inside KERNEL's 626,756 FATAL — the largest historical block but effectively absent from half 2 (152,723 → 11), hence ranked third as now-quiet. Just below the cut: MMCS `Ido packet timeout` ERROR 4,276 (1,012 → 3,264, +222%); DISCOVERY 'PGOOD IS NOT ASSERTED' ERROR 16,896 (14,727 → 2,169, declining); 'Timeout Over Limit on link card' FAILURE 972, all in half 2. Measured shape: FATAL/ERROR map to only three owning teams (BG/L Kernel, BG/L Control System, Hardware Ops), and 73.3% of FATAL sits in one component (KERNEL) — severity concentration, not breadth. The problem mix shifted between halves (KERNEL machine-check output stopped while APP ciod FATAL and MMCS ASSERT ERROR rose). Cause, hardware fault, job loss, downtime and customer impact are not established by these logs; node-level attribution per pattern was not measured.
- **E1** list_datasets_tool — available; results R1
- **E2** count_by_team_tool — available; results R2
- **E3** count_by_category_tool — available; results R3
- **E4** count_by_category_tool — available; results R4
- **E5** count_by_category_tool — available; results R5
- **E6** compare_periods_tool — available; results R6
- **E7** count_by_category_tool — available; results R7
- **E8** count_by_category_tool — available; results R8
- **E9** count_by_category_tool — available; results R9
- **E10** count_by_category_tool — available; results R10
- **E11** search_logs_tool — available; results R11

### Answer for the manager
**Top priorities — BGL (2005-06-03 22:42:50 → 2006-01-04 16:00:06, 4,747,963 log entries).** Only one dataset is in scope, so there is a single ranking.

1. **MMCS `idoproxydb hit ASSERT condition` ERROR (BG/L Control System)** — 84,168 entries, the largest ERROR template in the dataset; 19,320 in the baseline half → 64,848 in the recent half.
2. **ciod `Error reading message prefix on CioStream socket … Link has been severed` FATAL, with its related link/packet variants (BG/L Kernel)** — 26,546 entries, none in the baseline half; the after-LOAD_MESSAGE variant moved 7,182 → 8,091 and `Error receiving packet on tree network` 0 → 6,144.
3. **ciod `LOGIN chdir(/bgl/apps/SWL/performance/MINIBEN/…) failed: No such file or directory` FATAL (BG/L Kernel)** — 11,088 entries, none in the baseline half.

The ranking followed here is the computed priority ranking, derived from the counts:

_R12: priority_score_

| rank | dataset | template | non_info_rows | error_rows | volume_share_pct | error_share_pct | trend_up | breadth | score |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1 | bgl | idoproxydb hit ASSERT condition: ASSERT expression=<*> Source file=idotransportmgr.cpp Source line=<*> Function=int Ido… | 84168 | 84168 | 13.4 | 100.0 | 1.0 | n/a | 0.615 |
| 2 | bgl | ciod: Error reading message prefix on CioStream socket to <*>.<*>.<*>.<*>:<*>, Link has been severed | 26546 | 26546 | 4.2 | 100.0 | 1.0 | n/a | 0.574 |
| 3 | bgl | ciod: LOGIN chdir(/bgl/apps/SWL/performance/MINIBEN///<*>.<*>.<*>-<*>.<*>.<*>) failed: No such file or directory | 11088 | 11088 | 1.8 | 100.0 | 1.0 | n/a | 0.563 |
| 4 | bgl | Error receiving packet on tree network, expecting type <*> instead of type <*> (softheader=<*> <*> <*> <*>) PSR0=<*> PS… | 6144 | 6144 | 1.0 | 100.0 | 1.0 | n/a | 0.56 |
| 5 | bgl | ciod: Error loading /home/spelce1/HPCC_IBM/Urgent/VNM/64K/vnm.rts: invalid or missing program image, Permission denied | 5120 | 5120 | 0.8 | 100.0 | 1.0 | n/a | 0.559 |

**Why this order.** The sequence was computed from the counts — non-informational volume, share of the template's own rows that are ERROR-level, and upward trend — not from retrieved examples, and the first three positions match the three priorities above. Severity concentration explains why these outrank their apparent breadth: FATAL/ERROR map to only three owning teams (BG/L Kernel, BG/L Control System, Hardware Ops), and 626,756 of the 855,501 FATAL entries sit in one component (KERNEL). Breadth was not measured for any template, so component and team attribution rests on service ownership only.

**Risks supported by the data.**

- The KERNEL machine-check cluster (`data TLB error interrupt` 152,734; `data storage interrupt` 63,491; `machine check: i-fetch` 14,355) is the largest historical FATAL block but is not among the eight most frequent FATAL templates in the recent half, so it is currently quiet rather than resolved (BG/L Kernel).
- `Temperature Over Limit on link card` FAILURE appears 972 times, all in the recent half, alongside 206 LINKCARD FATAL entries (Hardware Ops).
- `Ido packet timeout` ERROR ranks sixth in the computed ranking with 4,276 entries (1,012 → 3,264), on the same MMCS control path as the top priority.

**What is not known.** These logs do not establish cause, a hardware fault, job loss, downtime or customer impact, and no result attributes the templates to specific nodes or link cards; template lists were truncated at 8–12 rows per category, so lower-count templates were not enumerated.

**Measured** (shown by the cited query results)
- **F1** The BGL dataset covers 4,747,963 log entries from 2005-06-03 22:42:50 to 2006-01-04 16:00:06; FATAL accounts for 855,501 entries and ERROR for 112,355, against 3,735,823 INFO entries. — R1 (value list) (row-level evidence, not a frequency)
- **F2** The MMCS template `idoproxydb hit ASSERT condition: … Source file=idotransportmgr.cpp … IdoTransportMgr::SendPacket` has 84,168 ERROR entries, the largest ERROR template, and rose from 19,320 in the baseline half to 64,848 in the recent half (+235.65%). — R5 (count), R6 (count), R9 (count), R10 (count)
- **F3** The computed priority ranking places that ASSERT template first (score 0.615) on 13.4% volume share, 100% error share and an upward trend, with breadth unmeasured. — R12 (computed)
- **F4** The FATAL template `ciod: Error reading message prefix on CioStream socket to …: Link has been severed` has 26,546 entries, all in the recent half (baseline 0); the after-LOAD_MESSAGE variant went 7,182 → 8,091 and `Error receiving packet on tree network` went 0 → 6,144. — R4 (count), R6 (count), R8 (count)
- **F5** The FATAL template `ciod: LOGIN chdir(/bgl/apps/SWL/performance/MINIBEN///…-…-…) failed: No such file or directory` has 11,088 entries, all in the recent half (baseline 0). — R6 (count), R8 (count)
- **F6** Severity is concentrated: KERNEL carries 626,756 FATAL entries, APP 228,536 and LINKCARD 206, while ERROR sits mainly in MMCS (88,529) and DISCOVERY (23,825). — R3 (count)
- **F7** The KERNEL machine-check templates `data TLB error interrupt` (152,734) and `data storage interrupt` (63,491) dominate the whole window, but the recent-half top eight FATAL templates are led by ciod entries and these machine-check templates are not among them. — R4 (count), R7 (count), R8 (count)
- **F8** `Temperature Over Limit on link card` (FAILURE) has 972 entries, all in the recent half (baseline 0), and MONITOR carries 1,665 FAILURE entries overall. — R6 (count), R3 (count)

**Hypotheses** (not confirmed by the logs)
- **F9** The recent-half concentration of CioStream link-severed and tree-network packet FATAL entries may indicate a link or interconnect fault rather than a program-load problem; the logs do not confirm this cause. — R6 (count), R8 (count)
- **F10** The ciod LOGIN chdir and `invalid or missing program image` FATAL entries plausibly reflect job-launch, path or permission problems rather than hardware; the logs do not confirm this. — R6 (count)

**Limitations** (what the data does not establish)
- **F11** No result attributes any template to specific nodes, link cards or jobs, so affected-component breadth is unmeasured and the ranking carries no breadth term. — R12 (computed)
- **F12** Cause, hardware fault, job loss, downtime and customer impact are not established, and the template lists were truncated (8 rows per half-window category, 12 rows whole-window, 100 rows in the period comparison), so lower-count templates were not enumerated. — R4 (count), R5 (count), R6 (count), R7 (count), R8 (count)

**Recommended next steps**
1. **Triage the `idoproxydb hit ASSERT condition` in `idotransportmgr.cpp` / `IdoTransportMgr::SendPacket`: add node or entity attribution for this template and check whether it clusters with `Ido packet timeout` on the same control paths.** — Ranks first: largest ERROR volume in the dataset (84,168) and the steepest measured rise into the recent half. (owner: BG/L Control System; based on F2, F3, F6)
2. **Investigate the ciod CioStream link-severed and tree-network packet FATALs as one transport symptom: confirm from raw entries whether they correlate in time with the link-card temperature-over-limit FAILURE entries before assigning a network versus link-hardware fix.** — Ranks second: 26,546 new FATAL entries with none in the baseline half, plus new tree-network packet errors; the cause (link hardware versus interconnect software) is unconfirmed. (owner: BG/L Kernel; based on F4, F8, F9)
3. **Check the ciod launch path handling for `LOGIN chdir` and `invalid or missing program image` FATALs — verify the MINIBEN working directory and image permissions exist on the affected deployment path.** — Ranks third: 11,088 new FATAL entries, all in the recent half, with no baseline volume; if it is a launch-path problem it is fixable without hardware work, but that is unconfirmed. (owner: BG/L Kernel; based on F5, F10)

Query cost: 11 queries in 0.52s, slowest R6 at 0.09s (16% of the total), ~4,955,783 rows scanned, ~9,493,837 rows/s.

### Recommended action
1. **Triage the `idoproxydb hit ASSERT condition` in `idotransportmgr.cpp` / `IdoTransportMgr::SendPacket`: add node or entity attribution for this template and check whether it clusters with `Ido packet timeout` on the same control paths.** — Ranks first: largest ERROR volume in the dataset (84,168) and the steepest measured rise into the recent half. (owner: BG/L Control System; based on F2, F3, F6)
2. **Investigate the ciod CioStream link-severed and tree-network packet FATALs as one transport symptom: confirm from raw entries whether they correlate in time with the link-card temperature-over-limit FAILURE entries before assigning a network versus link-hardware fix.** — Ranks second: 26,546 new FATAL entries with none in the baseline half, plus new tree-network packet errors; the cause (link hardware versus interconnect software) is unconfirmed. (owner: BG/L Kernel; based on F4, F8, F9)
3. **Check the ciod launch path handling for `LOGIN chdir` and `invalid or missing program image` FATALs — verify the MINIBEN working directory and image permissions exist on the affected deployment path.** — Ranks third: 11,088 new FATAL entries, all in the recent half, with no baseline volume; if it is a launch-path problem it is fixable without hardware work, but that is unconfirmed. (owner: BG/L Kernel; based on F5, F10)

## What the data does and does not establish

**Established (measured, cited to query results):**
- (Engineer) **F1** Over 2005-06-03 22:42:50 to 2006-01-04 16:00:06 UTC the BGL corpus has 4,747,963 rows, including 855,501 FATAL and 112,355 ERROR entries; FATAL entries are KERNEL 626,756, APP 228,536, LINKCARD 206 and MMCS 3, while ERROR entries are MMCS 88,529, DISCOVERY 23,825 and SERV_NET 1. — R1 (value list), R4 (count)
- (Engineer) **F2** The KERNEL anchor is record 17742 at 2005-06-05 07:08:13, entity R20-M0-N2-C:J10-U11, severity FATAL, message 'data TLB error interrupt'; the same template recurs at record 17745 on R20-M0-N2-C:J14-U01 and record 17758 on R20-M0-ND-C:J13-U11. — R2 (record), R3 (nearby events) (row-level evidence, not a frequency)
- (Engineer) **F3** In 2005-06-05 07:00–07:15 there are 76 FATAL entries, each with a different entity_id and count 1; the returned entity_ids include racks R20, R21, R24 and R25. — R7 (count)
- (Engineer) **F4** KERNEL FATAL hourly counts on 2005-06-05 are 0 from 00:00 through 06:00, 206 at 07:00, 95 at 08:00, 342 at 11:00, 152 at 12:00, 304 at 13:00, 567 at 14:00, 2,188 at 15:00 and 0 from 16:00 through 23:00. — R6 (trend)
- (Engineer) **F5** In 2005-09-20 19:00–19:20 the service/severity counts are MMCS ERROR 66,563, DISCOVERY INFO 741 and DISCOVERY SEVERE 47; no other service/severity pair is present. — R8 (count)
- (Engineer) **F6** The MMCS anchor is record 3395426 at 2005-09-20 19:05:58, severity ERROR, message 'idoproxydb hit ASSERT condition: ASSERT expression=0 Source file=idotransportmgr.cpp Source line=1043 Function=int IdoTransportMgr::SendPacket(IdoUdpMgr*, BglCtlPavTrace*)'. — R9 (nearby events) (row-level evidence, not a frequency)
- (Engineer) **F7** Immediately before the MMCS anchor, records 3394892–3394916 (2005-09-20 18:19:45–18:21:06) contain the same assertion and two 'Ido packet timeout' lines; immediately after the anchor, records 3395427–3395436 contain ten more identical assertions in the same second. — R9 (nearby events) (row-level evidence, not a frequency)
- (Engineer) **F8** Corpus-wide the top ERROR template is the same idoproxydb SendPacket assertion with 84,168 entries; the next two are a node-card status template with 16,896 entries and 'Ido packet timeout' with 4,276 entries. — R11 (count)
- (Engineer) **F9** The top FATAL template is 'data TLB error interrupt' with 152,734 entries; other top templates include 'data storage interrupt' 63,491, 'instruction address: <*>' 47,563, ciod link-severed 26,546, ciod invalid or missing program image 20,480, and 'rts: kernel terminated for reason <*>' 18,587. — R5 (count)
- (Team lead) **F1** Scope is the full BGL window 2005-06-03 22:42:50 to 2006-01-04 16:00:06 UTC with 4,747,963 log entries; severity counts are INFO 3,735,823, FATAL 855,501, ERROR 112,355, WARNING 23,357, SEVERE 19,213 and FAILURE 1,714. These are log-entry counts, not incidents. — R1 (value list) (row-level evidence, not a frequency)
- (Team lead) **F2** Top FATAL templates by entries: data TLB error interrupt 152,734; data storage interrupt 63,491; instruction address 47,563; ciod Link has been severed 26,546; ciod Exec format error on allreduce.rts 20,480; rts kernel terminated 18,587. — R2 (count)
- (Team lead) **F3** Top ERROR templates by entries: idoproxydb ASSERT in IdoTransportMgr::SendPacket 84,168; node-card status PGOOD/MPGOOD 16,896; Ido packet timeout 4,276; invalid node ecid in each of 16 listed processor-card slots 342; Bad cable going into LinkCard 206. — R3 (count)
- (Team lead) **F4** The data TLB error interrupt FATALs are a single June burst: 477 entries in the 2005-05-30 bucket and 152,182 in the week of 2005-06-06, then zero in most later weeks. — R7 (trend)
- (Team lead) **F5** The idoproxydb ASSERT ERRORs occur in only two weekly buckets, 19,320 in the week of 2005-08-01 and 64,848 in the week of 2005-09-19, with zero in the other 30 of 32 buckets. — R8 (trend)
- (Team lead) **F6** The ciod Link has been severed FATALs are non-zero in eight consecutive weeks from 2005-11-14 to 2006-01-02, at 1,211, 8,173, 6,008, 919, 6,849, 618, 2,638 and 130 entries, and total 26,546 over the window. — R2 (count), R11 (trend)
- (Team lead) **F7** The computed priority ranking places idoproxydb ASSERT first with score 0.615, ciod Link has been severed second with 0.574, and data TLB error interrupt third with 0.442. — R12 (computed)
- (Team lead) **F8** Team and component counts: BG/L Kernel has 855,292 FATAL entries through KERNEL 626,756 and APP 228,536; Hardware Ops has 206 FATAL through LINKCARD and 23,825 ERROR through DISCOVERY; BG/L Control System has 3 FATAL and 88,529 ERROR through MMCS; Network Ops has 1 ERROR through SERV_NET. — R4 (count), R5 (count), R6 (count)
- (Manager) **F1** The BGL dataset covers 4,747,963 log entries from 2005-06-03 22:42:50 to 2006-01-04 16:00:06; FATAL accounts for 855,501 entries and ERROR for 112,355, against 3,735,823 INFO entries. — R1 (value list) (row-level evidence, not a frequency)
- (Manager) **F2** The MMCS template `idoproxydb hit ASSERT condition: … Source file=idotransportmgr.cpp … IdoTransportMgr::SendPacket` has 84,168 ERROR entries, the largest ERROR template, and rose from 19,320 in the baseline half to 64,848 in the recent half (+235.65%). — R5 (count), R6 (count), R9 (count), R10 (count)
- (Manager) **F3** The computed priority ranking places that ASSERT template first (score 0.615) on 13.4% volume share, 100% error share and an upward trend, with breadth unmeasured. — R12 (computed)
- (Manager) **F4** The FATAL template `ciod: Error reading message prefix on CioStream socket to …: Link has been severed` has 26,546 entries, all in the recent half (baseline 0); the after-LOAD_MESSAGE variant went 7,182 → 8,091 and `Error receiving packet on tree network` went 0 → 6,144. — R4 (count), R6 (count), R8 (count)
- (Manager) **F5** The FATAL template `ciod: LOGIN chdir(/bgl/apps/SWL/performance/MINIBEN///…-…-…) failed: No such file or directory` has 11,088 entries, all in the recent half (baseline 0). — R6 (count), R8 (count)
- (Manager) **F6** Severity is concentrated: KERNEL carries 626,756 FATAL entries, APP 228,536 and LINKCARD 206, while ERROR sits mainly in MMCS (88,529) and DISCOVERY (23,825). — R3 (count)
- (Manager) **F7** The KERNEL machine-check templates `data TLB error interrupt` (152,734) and `data storage interrupt` (63,491) dominate the whole window, but the recent-half top eight FATAL templates are led by ciod entries and these machine-check templates are not among them. — R4 (count), R7 (count), R8 (count)
- (Manager) **F8** `Temperature Over Limit on link card` (FAILURE) has 972 entries, all in the recent half (baseline 0), and MONITOR carries 1,665 FAILURE entries overall. — R6 (count), R3 (count)

**Not established (hypotheses and limitations):**
- (Engineer) **F10** The 2005-09-20 ERROR episode is an MMCS control-system iDo transport failure in IdoTransportMgr::SendPacket, not a compute-node failure; the rows support the component and assertion text but not the trigger. — R8 (count), R9 (nearby events), R11 (count)
- (Engineer) **F11** The 2005-06-05 07:08 FATAL burst is a multi-node, multi-rack pattern rather than a single-node hardware fault; whether a running job or a hardware condition common to racks R20, R21, R24 and R25 caused it is unknown. — R3 (nearby events), R6 (trend), R7 (count)
- (Engineer) **F12** The trigger for the MMCS iDo assertion is not identified; the retrieved 19:00–19:20 aggregate has no message-level MMCS INFO rows, so an MMCS restart or link transition cannot be confirmed or excluded from that result. — R8 (count)
- (Engineer) **F13** The trigger for the KERNEL TLB exceptions is not identified; the retrieved rows show no job-start, LOAD_MESSAGE, loader or 'kernel terminated' boundary immediately before the first FATAL line. — R3 (nearby events)
- (Engineer) **F14** The FATAL and ERROR template lists are top-10 results and are truncated, so additional templates exist beyond those shown. — R5 (count), R11 (count)
- (Engineer) **F15** The anomaly Label field (for example KERNDTLB) is not available from any query tool, so the TLB entries cannot be classified by that label.
- (Team lead) **F9** The large FATAL volume may reflect a small number of failure episodes emitting many register/state-dump entries, but the data do not establish distinct failure episodes. — R2 (count), R7 (trend)
- (Team lead) **F10** FATAL and ERROR template rankings are truncated at 25 rows, so lower-count templates were not enumerated; WARNING, SEVERE and FAILURE templates were not broken down. — R2 (count), R3 (count)
- (Team lead) **F11** Node breadth per pattern was not queried, so whether the recurring templates are concentrated on a few nodes or are fleet-wide remains unknown.
- (Team lead) **F12** The exact owning team for the ciod Link has been severed FATALs is not established; the collected team counts show only 3 FATAL entries for BG/L Control System and 855,292 for BG/L Kernel. — R6 (count)
- (Manager) **F9** The recent-half concentration of CioStream link-severed and tree-network packet FATAL entries may indicate a link or interconnect fault rather than a program-load problem; the logs do not confirm this cause. — R6 (count), R8 (count)
- (Manager) **F10** The ciod LOGIN chdir and `invalid or missing program image` FATAL entries plausibly reflect job-launch, path or permission problems rather than hardware; the logs do not confirm this. — R6 (count)
- (Manager) **F11** No result attributes any template to specific nodes, link cards or jobs, so affected-component breadth is unmeasured and the ranking carries no breadth term. — R12 (computed)
- (Manager) **F12** Cause, hardware fault, job loss, downtime and customer impact are not established, and the template lists were truncated (8 rows per half-window category, 12 rows whole-window, 100 rows in the period comparison), so lower-count templates were not enumerated. — R4 (count), R5 (count), R6 (count), R7 (count), R8 (count)

General limits that apply to every role: log entries are counted, not incidents; retrieved examples show what a message looks like but never how often it happens; the logs contain no cost, downtime or customer-impact data, so none is claimed; team ownership comes from a fictional mapping.
