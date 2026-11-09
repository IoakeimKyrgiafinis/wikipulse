"""The lake's storage: hourly Parquet files in S3 (or a local folder, for tests).

**Object storage.** S3 stores *objects* (a blob of bytes) under *keys* (a name like
`edits/year=2026/month=11/day=07/hour=13/part-0.parquet`) inside a *bucket*. There are
no real folders: a "folder" is just a common key *prefix*, and listing a prefix is how
you find the hours that exist. Any S3-compatible server (RustFS, MinIO, AWS S3, ...)
speaks the same HTTP API, so changing `S3_ENDPOINT` is all it takes to move the lake.

**Atomic replace.** An S3 upload becomes visible only when it is complete, and an upload
to an existing key replaces the old object in one step: readers see the old file or the
new one, never a half-written or a missing one. That is why every hour is a single object
that is always rewritten whole (old rows + new rows, duplicates removed): re-running an
hour, or a crash at any point, can never leave the same edit in the archive twice.
On a local filesystem the same guarantee comes from writing a temporary file and renaming
it over the old one.
"""

import logging
import uuid
from datetime import datetime
from urllib.parse import urlparse

import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.fs as pafs
import pyarrow.parquet as pq

from wikipulse.config import Settings
from wikipulse.lake.partitions import parse_partition, part_path
from wikipulse.lake.table import EDIT_SCHEMA, conform, merge_edits

logger = logging.getLogger(__name__)

# Partition columns as query engines see them (from the folder names).
PARTITION_SCHEMA = pa.schema(
    [
        pa.field("year", pa.int16()),
        pa.field("month", pa.int8()),
        pa.field("day", pa.int8()),
        pa.field("hour", pa.int8()),
    ]
)

S3_CONNECT_TIMEOUT_S = 5
S3_REQUEST_TIMEOUT_S = 60


def s3_filesystem(settings: Settings) -> pafs.S3FileSystem:
    """pyarrow's S3 client for S3_ENDPOINT, with the configured credentials."""
    endpoint = urlparse(settings.s3_endpoint)
    if endpoint.scheme not in ("http", "https") or not endpoint.netloc:
        raise ValueError(f"S3_ENDPOINT must look like http://host:port, not {endpoint!r}")
    return pafs.S3FileSystem(
        access_key=settings.s3_access_key.get_secret_value(),
        secret_key=settings.s3_secret_key.get_secret_value(),
        endpoint_override=endpoint.netloc,
        scheme=endpoint.scheme,
        region=settings.s3_region,
        connect_timeout=S3_CONNECT_TIMEOUT_S,
        request_timeout=S3_REQUEST_TIMEOUT_S,
        allow_bucket_creation=True,
    )


class LakeStore:
    """Reads and replaces the hourly edit files under `root` ('<bucket>/<prefix>')."""

    def __init__(self, filesystem: pafs.FileSystem, root: str, compression: str = "zstd") -> None:
        self.fs = filesystem
        self.root = root.strip("/")
        self.compression = compression
        self._local = filesystem.type_name == "local"

    @classmethod
    def from_settings(cls, settings: Settings, prefix: str | None = None) -> "LakeStore":
        """The S3 lake of the settings (creating the bucket if needed)."""
        filesystem = s3_filesystem(settings)
        bucket = settings.s3_bucket
        if filesystem.get_file_info(bucket).type == pafs.FileType.NotFound:
            filesystem.create_dir(bucket)
            logger.info("Created bucket %s at %s", bucket, settings.s3_endpoint)
        return cls(filesystem, f"{bucket}/{prefix if prefix is not None else settings.lake_prefix}")

    def path(self, hour: datetime) -> str:
        """Full path (bucket and key) of an hour's file."""
        return part_path(self.root, hour)

    # --- one hour ---

    def read_hour(self, hour: datetime) -> pa.Table | None:
        """The rows archived for `hour`, or None if the hour has no file yet."""
        path = self.path(hour)
        if self.fs.get_file_info(path).type == pafs.FileType.NotFound:
            return None
        return conform(pq.read_table(path, filesystem=self.fs))

    def write_hour(self, hour: datetime, table: pa.Table) -> None:
        """Replace the hour's file with `table`, atomically (see the module docstring)."""
        path = self.path(hour)
        table = conform(table)
        if not self._local:
            pq.write_table(table, path, filesystem=self.fs, compression=self.compression)
            return
        folder = path.rsplit("/", 1)[0]
        self.fs.create_dir(folder, recursive=True)
        temporary = f"{folder}/.tmp-{uuid.uuid4().hex}.parquet"  # ignored by readers ('.')
        pq.write_table(table, temporary, filesystem=self.fs, compression=self.compression)
        self.fs.move(temporary, path)

    def merge_hour(self, hour: datetime, new_rows: pa.Table) -> int:
        """Add `new_rows` to the hour (read, merge without duplicates, replace).

        Returns the number of rows the hour holds afterwards.
        """
        existing = self.read_hour(hour)
        tables = [new_rows] if existing is None else [existing, new_rows]
        merged = merge_edits(*tables)
        self.write_hour(hour, merged)
        return merged.num_rows

    # --- the whole archive ---

    def hours(self) -> list[datetime]:
        """Every hour that has a file, oldest first."""
        if self.fs.get_file_info(self.root).type == pafs.FileType.NotFound:
            return []
        selector = pafs.FileSelector(self.root, recursive=True)
        found = {
            parse_partition(info.path)
            for info in self.fs.get_file_info(selector)
            if info.type == pafs.FileType.File
            and info.path.endswith(".parquet")
            and not info.base_name.startswith(".")
        }
        return sorted(hour for hour in found if hour is not None)

    def file_size(self, hour: datetime) -> int | None:
        """Size of the hour's file in bytes, or None if it doesn't exist."""
        info = self.fs.get_file_info(self.path(hour))
        return None if info.type == pafs.FileType.NotFound else info.size

    def dataset(self) -> ds.Dataset:
        """The whole archive as one Arrow dataset, with year/month/day/hour columns."""
        partitioning = ds.partitioning(PARTITION_SCHEMA, flavor="hive")
        schema = pa.unify_schemas([EDIT_SCHEMA, PARTITION_SCHEMA])
        if not self.hours():
            return ds.dataset(pa.table({name: [] for name in schema.names}, schema=schema))
        return ds.dataset(
            self.root,
            filesystem=self.fs,
            format="parquet",
            partitioning=partitioning,
            schema=schema,
        )
