"""Dump Mix1 Decide JSON without HTTP, servers, or assistant replies."""

import argparse
import json
from pathlib import Path

from .upstage import dump_decide_payloads


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ids", help="Comma-separated scenario IDs (default: smoke_ids)")
    parser.add_argument("--out", type=Path, help="New JSON file outside runs/ (default: stdout)")
    args = parser.parse_args(argv)
    ids = None if args.ids is None else [value.strip() for value in args.ids.split(",")]
    if ids is not None and not all(ids):
        parser.error("--ids must contain nonempty scenario IDs")
    runs = Path(__file__).resolve().parent.parent / "runs"
    if args.out is not None and args.out.resolve().is_relative_to(runs):
        parser.error("--out must be outside runs/")
    try:
        payload = json.dumps(dump_decide_payloads(ids), ensure_ascii=False, indent=2) + "\n"
        if args.out is None:
            print(payload, end="")
        else:
            with args.out.open("x", encoding="utf-8") as output:
                output.write(payload)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
