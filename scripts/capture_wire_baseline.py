#!/usr/bin/env python3
"""Snapshot the JSON every wire model produces, for compatibility checking.

Run this on a build to record what the protocol looks like on the wire, then
run it again after changing the serialisation layer and diff the two. The
protocol is what every deployed client and the server agree on, so "the tests
pass" is not enough — the bytes have to be identical.

Usage:
  ./scripts/capture_wire_baseline.py                 # print to stdout
  ./scripts/capture_wire_baseline.py -o base.json    # write to a file

The fixtures live in tests/unit/test_wire_compat.py, which asserts the current
code still reproduces tests/fixtures/wire_baseline.json.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from hle_common.wire_samples import SAMPLES  # noqa: E402


def build() -> dict[str, str]:
    """Return {sample name: JSON string} for every representative model."""
    out: dict[str, str] = {}
    for name, instance in SAMPLES.items():
        out[name] = instance.model_dump_json()
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("-o", "--output", type=Path, help="write JSON here instead of stdout")
    args = ap.parse_args()

    data = build()
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text)
        print(f"Wrote {len(data)} samples to {args.output}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
