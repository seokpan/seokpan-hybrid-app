"""Export an offline schema with same-run/source evidence for the Node CI stage."""

import hashlib
import json
import os
import platform
import re
import subprocess
import sys
from argparse import ArgumentParser
from datetime import UTC, datetime
from pathlib import Path

SOURCE_PATHS = ("backend", "frontend", ".gitattributes", ".gitignore")


def git(root: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", *args], cwd=root, timeout=15)


def snapshot(root: Path) -> dict[str, str | bool]:
    root = root.resolve()
    if Path(git(root, "rev-parse", "--show-toplevel").decode().strip()).resolve() != root:
        raise ValueError("Use the App checkout root.")
    names = git(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", *SOURCE_PATHS
    )
    digest = hashlib.sha256()
    for name in sorted(set(names.split(b"\0")) - {b""}):
        relative = name.decode("utf-8")
        path = root / relative
        if path.resolve() != root.joinpath(relative).absolute() or path.is_symlink():
            raise ValueError("Source symlinks are not supported by CI export.")
        content_hash = hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else "MISSING"
        digest.update(name + b"\0" + content_hash.encode("ascii") + b"\n")
    return {
        "commit": git(root, "rev-parse", "HEAD").decode().strip(),
        "source_sha256": digest.hexdigest(),
        "dirty": bool(git(root, "status", "--porcelain=v1", "-z", "--", *SOURCE_PATHS)),
    }


def reserve(root: Path, run_id: str) -> Path:
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", run_id) is None:
        raise ValueError("Invalid CI run ID.")
    root = root.resolve()
    parent = root
    for part in ("test-results", run_id):
        parent = parent / part
        parent.mkdir(exist_ok=True)
        if parent.resolve() != parent:
            raise ValueError("Reports must stay inside this checkout.")
    output = parent / "openapi"
    output.mkdir()  # Never overwrite a failed or completed run.
    return output


def export(root: Path, run_id: str) -> Path:
    if platform.python_version() != "3.13.15":
        raise ValueError("CI export requires Python 3.13.15.")
    before = snapshot(root)
    output = reserve(root, run_id)
    # A failure leaves no manifest; the consumer must reject the partial export.
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("SEOKPAN_")
    }
    environment.update(PYTHONDONTWRITEBYTECODE="1", PYTHONIOENCODING="utf-8")
    raw = subprocess.check_output(
        [sys.executable, "scripts/export_openapi.py"],
        cwd=root / "backend",
        env=environment,
        timeout=30,
    )
    schema = json.loads(raw)
    if (
        not isinstance(schema, dict)
        or not isinstance(schema.get("openapi"), str)
        or not isinstance(schema.get("paths"), dict)
        or not schema["paths"]
    ):
        raise ValueError("Export did not produce an OpenAPI schema.")
    if snapshot(root) != before:
        raise ValueError("Sources changed during OpenAPI export; use a new run.")
    with (output / "openapi.json").open("xb") as stream:
        stream.write(raw)
    manifest = {
        "format_version": 1,
        "run_id": run_id,
        **before,
        "python": platform.python_version(),
        "schema_sha256": hashlib.sha256(raw).hexdigest(),
        "created_at": datetime.now(UTC).isoformat(),
    }
    with (output / "manifest.json").open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
    return output


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    operation = parser.add_mutually_exclusive_group(required=True)
    operation.add_argument("--run-id")
    operation.add_argument("--snapshot", action="store_true")
    args = parser.parse_args()
    if args.snapshot:
        print(json.dumps(snapshot(Path(__file__).resolve().parents[2])))
        return
    output = export(Path(__file__).resolve().parents[2], args.run_id)
    print(f"OpenAPI export complete: {output}")


if __name__ == "__main__":
    main()
