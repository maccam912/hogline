"""Check staged/tracked Git blobs before publishing this public repository.

This is a focused backstop, not a substitute for reviewing the staged diff.
It reads Git's index, so unstaged copies cannot hide a staged secret/artifact.
"""

import argparse
import re
import subprocess
import sys
from pathlib import PurePosixPath

MAX_BYTES = 1024 * 1024
PRIVATE_ROOTS = {"data", "dist", "models", "weights", "artifacts", ".venv", ".aws", ".ssh"}
PRIVATE_SUFFIXES = {
    ".mp4",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".parquet",
    ".onnx",
    ".pt",
    ".pth",
    ".safetensors",
    ".pem",
    ".key",
    ".p12",
    ".pfx",
}
SECRET_PATTERNS = [
    re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(rb"gh[pousr]_[A-Za-z0-9]{30,}"),
    re.compile(rb"github_pat_[A-Za-z0-9_]{50,}"),
    re.compile(rb"AKIA[0-9A-Z]{16}"),
    re.compile(rb"sk-(?:proj-)?[A-Za-z0-9_-]{40,}"),
    re.compile(rb"https?://[^\s/:]+:[^\s/@]+@[^\s/]+"),
    re.compile(rb"(?i)(?:googlevideo\.com/[^\s\"']*|[?&](?:signature|sig|token)=)[^\s\"']{35,}"),
]


def violations(path: str, content: bytes):
    file = PurePosixPath(path)
    errors = []
    if file.parts[0] in PRIVATE_ROOTS or file.suffix.lower() in PRIVATE_SUFFIXES:
        errors.append("private data, media, credentials, or model artifact path")
    if file.name.startswith(".env") and file.name != ".env.example":
        errors.append("environment file")
    if len(content) > MAX_BYTES:
        errors.append("file exceeds the 1 MiB repository limit")
    try:
        content.decode("utf-8")
        if b"\0" in content:
            errors.append("binary artifact; keep generated media, archives, and catalogs outside Git")
    except UnicodeDecodeError:
        errors.append("binary artifact; keep generated media, archives, and catalogs outside Git")
    if any(pattern.search(content) for pattern in SECRET_PATTERNS):
        errors.append("possible credential, signed media URL, or private key")
    return errors


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--all", action="store_true", help="Check every tracked blob (for CI)")
    args = parser.parse_args()
    command = (
        ["git", "ls-files", "-z"]
        if args.all
        else ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR", "-z"]
    )
    paths = subprocess.check_output(command).decode().split("\0")
    failures = []
    for path in filter(None, paths):
        content = subprocess.check_output(["git", "show", f":{path}"])
        failures.extend(f"{path}: {error}" for error in violations(path, content))
    if failures:
        print("Repository publication check failed:", file=sys.stderr)
        print("\n".join(failures), file=sys.stderr)
        return 1
    print(f"Repository publication check passed ({len(list(filter(None, paths)))} files; max 1 MiB each).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
