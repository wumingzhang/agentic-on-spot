#!/usr/bin/env python3
"""Build the Lambda deployment zip for the CloudFormation template.

    python3 scripts/package-lambda.py [--out build/spot-handoff-lambda.zip]

Zips lambda/ (the spot_handoff package with its embedded host script),
exactly what terraform/main.tf's archive_file zips, minus __pycache__ and
.pyc. The zip is reproducible: entries are sorted, timestamps fixed and
permissions normalised, so two builds of the same tree have the same
SHA-256 and the owner can compare the hash printed here with the function's
CodeSha256 after deployment (the base64 form below is what
`aws lambda get-function-configuration --query CodeSha256` returns).

Nothing is uploaded: the owner copies the zip to their own S3 bucket in the
stack's region (see cloudformation/README.md).
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import os
import sys
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE_DIR = REPO_ROOT / "lambda"
DEFAULT_OUT = REPO_ROOT / "build" / "spot-handoff-lambda.zip"
# Files the Lambda cannot run without; the build refuses a tree missing them.
REQUIRED = ("spot_handoff/handler.py", "spot_handoff/spot-handoff-notify.sh")
EXCLUDED_DIRS = {"__pycache__"}
EXCLUDED_SUFFIXES = (".pyc", ".pyo")
FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)  # zip's epoch; a constant keeps the archive reproducible


def package_files(source_dir: Path = SOURCE_DIR) -> list[tuple[str, Path]]:
    """(archive name, path) for every file shipped, sorted by archive name."""
    out = []
    for root, dirs, files in os.walk(source_dir):
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED_DIRS)
        for name in files:
            if name.endswith(EXCLUDED_SUFFIXES):
                continue
            path = Path(root) / name
            out.append((path.relative_to(source_dir).as_posix(), path))
    out.sort()
    names = {n for n, _ in out}
    missing = [r for r in REQUIRED if r not in names]
    if missing:
        raise FileNotFoundError(f"{source_dir} lacks {missing}")
    return out


def build_zip(source_dir: Path = SOURCE_DIR) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for arcname, path in package_files(source_dir):
            info = zipfile.ZipInfo(arcname, date_time=FIXED_DATE_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3  # unix, so the permission bits below are honoured
            info.external_attr = 0o644 << 16
            zf.writestr(info, path.read_bytes())
    return buf.getvalue()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help=f"where to write the zip (default {DEFAULT_OUT.relative_to(REPO_ROOT)})")
    parser.add_argument("--source", type=Path, default=SOURCE_DIR, help="directory to zip (default lambda/)")
    args = parser.parse_args(argv)

    data = build_zip(args.source)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    digest = hashlib.sha256(data).digest()
    print(f"wrote      {args.out}")
    print(f"bytes      {len(data)}")
    print(f"files      {len(package_files(args.source))}")
    print(f"sha256     {digest.hex()}")
    print(f"CodeSha256 {base64.b64encode(digest).decode()}   (Lambda reports this form)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
