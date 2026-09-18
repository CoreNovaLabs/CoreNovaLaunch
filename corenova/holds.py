"""Deployment holds: 运维性部署暂停的发布与解除（deployment-contract.md §2.5）。

hold 独立于验证结果存在："已验证"≠"当前可部署"。验证流水线只在重新验证通过时
运行，而 hold 的生效不能等待下一次验证——否则会陷入"hold 拦住验证 → hold 字段
永远进不了发布数据"的死锁。因此 hold 有独立的运维发布路径：从 apps/*.yaml 的
deployment.hold（单一事实源）读取，条件写（If-Match）进已发布的 current.json，
不触碰 versions/、index.json 与任何验证字段。解除暂停 = 移除 yaml 里的 hold 后
重跑本模块的同步。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import appspec
from .util import log


@dataclass
class HoldSyncResult:
    set_apps: list[str] = field(default_factory=list)   # 注入或更新了 deploy.hold
    cleared: list[str] = field(default_factory=list)    # 移除了 deploy.hold（解除暂停）
    unchanged: list[str] = field(default_factory=list)  # 已一致，无需写入
    absent: list[str] = field(default_factory=list)     # R2 无 current.json（未发布过，无需暂停）
    notes: list[str] = field(default_factory=list)


def desired_holds(root: Path) -> dict[str, dict[str, Any]]:
    """apps/*.yaml 声明的 hold（唯一事实源）。返回 {app: {"reason": {en, zh}}}。"""
    out: dict[str, dict[str, Any]] = {}
    for name in appspec.all_apps(root):
        spec = appspec.load(name, root)
        hold = spec.g("deployment.hold")
        if isinstance(hold, dict) and isinstance(hold.get("reason"), dict) and hold["reason"].get("en"):
            out[name] = {
                "reason": {"en": hold["reason"]["en"], "zh": hold["reason"]["zh"]},
            }
    return out


def sync_holds(
    backend,
    root: Path,
    apps: list[str] | None = None,
) -> HoldSyncResult:
    """把 yaml 里的 hold 状态条件写进已发布的 current.json。

    只改动 deploy.hold 一个键，其它字段原样保留（不重写验证事实）。
    默认处理"声明了 hold 的应用"；显式 --app 可指定任意应用——yaml 无 hold 且
    current.json 有 hold 的应用会被清除（解除暂停的唯一途径）。
    """
    result = HoldSyncResult()
    wants = desired_holds(root)
    names = list(apps) if apps else sorted(wants)
    for app in names:
        key = f"verified/{app}/current.json"
        raw, etag = backend.get_with_etag(key)
        if not raw:
            result.absent.append(app)
            continue
        try:
            current = json.loads(raw)
        except json.JSONDecodeError:
            result.notes.append(f"{key} 损坏（非 JSON）→ 跳过")
            continue
        deploy = current.get("deploy")
        if not isinstance(deploy, dict):
            # 旧记录缺 deploy 段属于发布数据缺口，补写是验证流水线的职责，不在这里越权
            result.notes.append(f"{key} 无 deploy 段 → 跳过")
            continue
        want = wants.get(app)
        if deploy.get("hold") == want:
            result.unchanged.append(app)
            continue
        if want:
            deploy["hold"] = want
        else:
            deploy.pop("hold", None)
        payload = json.dumps(current, ensure_ascii=False, indent=2).encode() + b"\n"
        if backend.put_if_match(key, payload, etag):
            (result.set_apps if want else result.cleared).append(app)
            log(f"HOLD {'set' if want else 'cleared'}: {app}")
        else:
            result.notes.append(f"{key} 条件写冲突（并发修改）→ 本次跳过，重跑即可")
    return result
