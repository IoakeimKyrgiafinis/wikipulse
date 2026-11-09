# Spec 06a: Data lake archive

Status: done
Stage: 6. Data lake and batch
Owner: agent
Depends on: 01

## Goal
Archive every kept edit as hourly Parquet files in S3-compatible storage (MinIO), building a permanent history for analysis and model training.

## What you'll learn
- **Object storage and the S3 interface:** buckets, keys, prefixes. The same code works on AWS by changing one address.
- **Columnar storage:** why Parquet is much smaller and faster to analyze than JSON. Compare the file sizes yourself.
- **Partitioning:** `year=/month=/day=/hour=` folders, and why query engines love them.
- **A second consumer group** on the same stream: independent consumers at their own pace.

## Outputs
- `docker-compose.yml`: MinIO added (or an S3-compatible alternative, if MinIO's free edition doesn't suit; explain the choice)
- `wikipulse/lake/archiver.py`: reads the stream with its own consumer group, buffers by hour of event time, writes `edits/year=YYYY/month=MM/day=DD/hour=HH/part-<n>.parquet`, then acknowledges
- `uv run python -m wikipulse.lake.archive`
- `uv run python -m wikipulse.lake.query "<SQL>"`: ad-hoc queries over the archive using DuckDB
- Config: `S3_ENDPOINT`, `S3_BUCKET`, credentials via `.env`

## Requirements
1. Messages are acknowledged only after their file is safely written (no data loss on crash).
2. Re-running for the same hour never creates duplicate rows (write by hour, replace atomically).
3. Late events go to the hour of their event time.

## Acceptance criteria
- Unit tests for buffering and partition paths; integration tests against MinIO.
- After a crash mid-hour, the archive has no gaps or duplicates (scripted check).
- Report the size of one hour of Parquet compared with the same events as JSON.

## Look at it yourself (optional, never blocking)
- Query a day of history with DuckDB: edits per hour, top pages, bot share.
