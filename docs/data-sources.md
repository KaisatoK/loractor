# Data sources and citations

## Log corpora

- **BGL** — Blue Gene/L supercomputer RAS logs, 4,747,963 lines over 214.7 days (2005-06-03 → 2006-01-04). Loaded into the native `bgl` table. Its long span makes it the corpus for long-range questions.
- **OpenStack** — three nova runs (two normal, one with failures), 207,820 lines (2017-05-14 → 2017-05-17). Loaded into the native `openstack` table; the runs are read in chronological order and `LineId` runs on across them, so record-id order is time order.
- **OpenTelemetry demo** — a day of telemetry from the OpenTelemetry demo app, 50 Parquet files of about a billion rows each, in public object storage. Never downloaded: queried in place (`otel_s3`), and searchable through an inverted index built in place (`otel_slice`).

Both BGL and OpenStack come from [Loghub](https://github.com/logpai/loghub). The repository bundles 2,000-line samples in `src/backend/data/`; the full corpora are separate loghub downloads, loaded by the loghub loader (see the [setup guide's corpus-loading section](../setup.md#4-loading-the-corpora)). The local corpora are read from native tables; the OTel corpus is read as remote Parquet, in place.

## Citations and licenses

- **Loghub**: Jieming Zhu, Shilin He, Pinjia He, Jinyang Liu, Michael R. Lyu. *Loghub: A Large Collection of System Log Datasets for AI-driven Log Analytics.* ISSRE, 2023. The bundled samples are redistributed under the Loghub research-use licence, which requires this citation — see [`src/backend/data/LICENSE-loghub`](../src/backend/data/LICENSE-loghub).
- **BGL**: A. Oliner and J. Stearley, *What Supercomputers Say: A Study of Five System Logs*, DSN 2007.
- **OpenTelemetry demo dataset**: generated from the OpenTelemetry demo app and published by ClickHouse under Apache-2.0 as public Parquet. One file, `part_000.parquet`, is already a billion rows; the corpus is queried where it is, never downloaded.
- **This repository's own code** is released under the [MIT License](../LICENSE). The log samples are separate: they are not covered by the MIT license.

## The team-ownership table

`src/backend/data/team_ownership.csv` is a **fictional** `dataset,component,team` mapping. `component` matches the corpus's component column exactly; components not mapped count as `unowned`, so totals never silently drop rows. `count_by_team_tool` joins this table by corpus and component. The CSV is loaded into SereneDB as `team_ownership` by `bash ./setup.sh`. Replace the CSV (keep the three columns) if the organisers' real table becomes available. The README's [Team ownership](../README.md#team-ownership-fictional) section lists the mapped components.

## Schema

Every corpus is declared once in the dataset registry (`src/backend/db/datasets.py`) under canonical field names, and every query is composed from those declarations — never from a table name supplied by the model:

`ts`, `severity`, `service`, `message`, `entity_id`, `template`, `record_id`, plus `request_id` where declared.

A field is declared only where the corpus really has it. `request_id` exists only on OpenStack, derived at query time from the `ADDR` column (no reload needed).

| Dataset | Kind | Rows | Key fields | Role |
| --- | --- | --- | --- | --- |
| `openstack` | native table | 207,820 | `ts`, `severity`, `service`, `message`, `entity_id` (Pid), `template`, `record_id` (LineId), `request_id` (derived) | default corpus; local, exact, fully indexed |
| `bgl` | native table | 4,747,963 | `ts`, `severity`, `service` (Component), `message`, `entity_id` (Node), `template`, `record_id` (LineId) | long-range questions; local, exact, fully indexed |
| `otel_s3` | remote Parquet | 1,000,000,000 (per file) | `ts`, `severity`, `service` (ServiceName), `message` (Body), `entity_id` (TraceId), `template` (derived) | the remote corpus, read in place; approximate aggregates |
| `otel_slice` | in-place index | — | `ts`, `severity`, `service`, `message`, `entity_id` | text search over the remote corpus; one window at a time |

`event_id` is not declared on any corpus. Severity vocabularies (in each corpus's own casing) and the derived OTel template expression are also declared in the registry.
