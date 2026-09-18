#!/usr/bin/env python3
"""把 apps/*.yaml 的 deployment.hold 同步进已发布的 current.json（运维性暂停发布/解除）。

hold 独立于验证结果：发布/解除走本脚本，不等下一次验证。
用法：
    python scripts/verify/sync_holds.py                 # 同步全部声明了 hold 的应用
    python scripts/verify/sync_holds.py --app gitea     # 指定应用（yaml 无 hold 则解除暂停）
"""

from __future__ import annotations

import argparse

from corenova import holds
from corenova.backend import make_backend
from corenova.config import Config


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--app", action="append", help="指定应用，可重复；缺省处理全部声明了 hold 的应用")
    args = ap.parse_args()

    cfg = Config.load()
    result = holds.sync_holds(make_backend(cfg), cfg.root, args.app)
    print(
        f"set={result.set_apps} cleared={result.cleared} "
        f"unchanged={result.unchanged} absent={result.absent}"
    )
    for note in result.notes:
        print(f"  note: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
