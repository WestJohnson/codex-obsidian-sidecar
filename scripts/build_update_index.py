from __future__ import annotations

import argparse
import json
from pathlib import Path

from obsidian_sidecar.release_index import DEFAULT_BASE_URL, build_index


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheels", type=Path, nargs="+")
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    index = build_index(args.wheels, args.base_url)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(index, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(index, indent=2))


if __name__ == "__main__":
    main()
