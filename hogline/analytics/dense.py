"""Columnar dense observations; SQL stores only the bounded window manifest."""

import hashlib
import io
import json
import os

import pyarrow as pa
import pyarrow.parquet as pq

SCHEMA_VERSION = "hogline-dense-observations-v3"
DETECTION = pa.struct(
    [
        ("color", pa.string()),
        ("center_px", pa.list_(pa.float64(), 2)),
        ("bbox_px", pa.list_(pa.int32(), 4)),
        ("area_px2", pa.float64()),
        ("shape_score", pa.float64()),
        ("boundary_clipped", pa.bool_()),
    ]
)
SCHEMA = pa.schema(
    [
        ("sample_index", pa.int32()),
        ("source_id", pa.string()),
        ("source_metadata_sha256", pa.string()),
        ("profile_sha256", pa.string()),
        ("model_version", pa.string()),
        ("pts", pa.int64()),
        ("time_base_num", pa.int32()),
        ("time_base_den", pa.int32()),
        ("source_seconds", pa.float64()),
        ("requested_interval_seconds", pa.float64()),
        ("decoded_rgb_sha256", pa.string()),
        ("youtube_url", pa.string()),
        ("view", pa.string()),
        ("crop_width", pa.int32()),
        ("crop_height", pa.int32()),
        ("motion_mean_abs", pa.float64()),
        (
            "foreground_regions",
            pa.list_(pa.struct([("bbox_px", pa.list_(pa.int32(), 4)), ("area_px2", pa.int32())])),
        ),
        ("detections", pa.list_(DETECTION)),
        (
            "rejected_candidates",
            pa.list_(pa.struct([*DETECTION, ("rejection_reasons", pa.list_(pa.string()))])),
        ),
    ]
)


def save_dense(rows, manifest, root, max_bytes, budget):
    schema = SCHEMA.with_metadata(
        {
            b"hogline.schema_version": SCHEMA_VERSION.encode(),
            b"hogline.input_manifest": json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode(),
        }
    )
    table = pa.Table.from_pylist(rows, schema=schema)
    buffer = io.BytesIO()
    pq.write_table(table, buffer, compression="zstd", row_group_size=120, write_page_checksum=True)
    content = buffer.getvalue()
    if len(content) > max_bytes:
        raise ValueError("Dense observation file exceeds the window byte budget")
    sha = hashlib.sha256(content).hexdigest()
    relative = f"dense/{sha[:2]}/{sha}.parquet"
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    valid = path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == sha
    budget.consume(len(content), 0 if valid else len(content) - (path.stat().st_size if path.exists() else 0))
    if not valid:
        temp = path.with_suffix(f".{os.getpid()}.tmp")
        temp.write_bytes(content)
        os.replace(temp, path)
    return {
        "path": relative,
        "sha256": sha,
        "bytes": len(content),
        "rows": len(rows),
        "schema": SCHEMA_VERSION,
    }
