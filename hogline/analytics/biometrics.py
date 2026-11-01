"""Local biometric store: checksummed NumPy archives kept apart from evidence and exports."""

import hashlib
import io
import json
import os
from pathlib import Path

import numpy as np

ROOT = "biometrics"
MAX_ARCHIVE_BYTES = 256 * 1024**2


def json_array(value):
    return np.frombuffer(json.dumps(value, sort_keys=True, separators=(",", ":")).encode(), dtype=np.uint8)


def array_json(array):
    return json.loads(np.asarray(array, dtype=np.uint8).tobytes().decode())


def _resolve(data_dir, relative):
    base = (Path(data_dir) / ROOT).resolve()
    path = (Path(data_dir) / relative).resolve()
    if not path.is_relative_to(base):
        raise ValueError("Biometric archive path escapes the biometric store")
    return path


def write_archive(data_dir, relative, arrays, reserve=None):
    """Encode first, reserve the growth with the caller's budget, then write atomically."""
    buffer = io.BytesIO()
    np.savez_compressed(buffer, **arrays)
    content = buffer.getvalue()
    if len(content) > MAX_ARCHIVE_BYTES:
        raise ValueError("Biometric archive exceeds its bounded size")
    if reserve:
        reserve(len(content))
    path = _resolve(data_dir, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(f".{os.getpid()}.tmp")
    temp.write_bytes(content)
    temp.replace(path)
    return {"path": str(Path(relative)), "sha256": hashlib.sha256(content).hexdigest(), "bytes": len(content)}


def read_archive(data_dir, ref):
    path = _resolve(data_dir, ref["path"])
    if not path.is_file() or path.stat().st_size != ref["bytes"] or ref["bytes"] > MAX_ARCHIVE_BYTES:
        raise ValueError("Biometric archive is missing or was deleted")
    content = path.read_bytes()
    if hashlib.sha256(content).hexdigest() != ref["sha256"]:
        raise ValueError("Biometric archive checksum mismatch")
    with np.load(io.BytesIO(content), allow_pickle=False) as archive:
        return {name: archive[name] for name in archive.files}


def delete_archive(data_dir, ref):
    path = _resolve(data_dir, ref["path"])
    if path.is_file():
        path.unlink()
        return True
    return False
