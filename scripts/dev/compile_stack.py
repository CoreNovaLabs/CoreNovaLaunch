"""Offline experimental stack compiler; never deploys or publishes."""

import argparse
import json
from pathlib import Path

import yaml

from corenova.stack import build_bundle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("spec", type=Path)
    parser.add_argument("--out", type=Path, required=True, help="New output directory (must not exist)")
    args = parser.parse_args()
    try:
        compose, lock = build_bundle(yaml.safe_load(args.spec.read_text()))
        args.out.mkdir(mode=0o700, parents=False, exist_ok=False)
        (args.out / "compose.yaml").write_text(compose, encoding="utf-8")
        (args.out / "stack-lock.json").write_text(json.dumps(lock, indent=2) + "\n", encoding="utf-8")
    except (ValueError, OSError, yaml.YAMLError) as exc:
        parser.exit(1, f"Compilation failed: {exc}\n")
    print("UNVERIFIED bundle generated; no containers started and no credentials created.")


if __name__ == "__main__":
    main()
