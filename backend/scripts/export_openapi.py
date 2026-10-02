"""Offline public schema export; does not start a server or connect to providers."""

import json
import os
import sys
from argparse import ArgumentParser
from pathlib import Path

# The app module also constructs its default app. This tooling process must never
# consume production configuration just to export its public request/response schema.
os.environ["SEOKPAN_ENVIRONMENT"] = "test"

from seokpan.app import create_app  # noqa: E402
from seokpan.settings import Settings  # noqa: E402


def main() -> None:
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    schema = create_app(settings=Settings(environment="test")).openapi()
    content = json.dumps(schema, sort_keys=True)
    if args.output is None:
        sys.stdout.write(content)
    else:
        args.output.write_text(content, encoding="utf-8")


if __name__ == "__main__":
    main()
