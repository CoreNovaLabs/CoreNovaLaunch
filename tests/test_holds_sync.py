"""holds 同步的行为测试：hold 独立于验证结果的发布/解除路径（deployment-contract.md §2.5）。

关键断言：同步只改动 deploy.hold 一个键，验证事实字段原样保留。
"""

from __future__ import annotations

import json
from pathlib import Path

import yaml

from corenova import holds
from corenova.backend import DirBackend

HOLD = {"reason": {"en": "Deployment paused pending verification.", "zh": "暂停部署：待验证。"}}


def seed_repo(root: Path, apps: dict[str, dict | None]) -> None:
    """apps/<name>.yaml（hold 可为 None 表示不声明）+ 已发布的 current.json fixtures。"""
    apps_dir = root / "apps"
    apps_dir.mkdir(parents=True, exist_ok=True)
    for app, hold in apps.items():
        deployment: dict = {"size": "small"}
        if hold is not None:
            deployment["hold"] = hold
        (apps_dir / f"{app}.yaml").write_text(
            yaml.safe_dump({"deployment": deployment}, allow_unicode=True),
            encoding="utf-8",
        )
        current = {
            "app": app,
            "app_version": "v1.0.0",
            "status": "verified",
            "deploy": {"container_port": 8080, "data_path": "/data"},
        }
        key = f"verified/{app}/current.json"
        (root / key).parent.mkdir(parents=True, exist_ok=True)
        (root / key).write_bytes(json.dumps(current, ensure_ascii=False, indent=2).encode() + b"\n")


def read_current(backend: DirBackend, app: str) -> dict:
    return json.loads(backend.get(f"verified/{app}/current.json"))


def test_desired_holds_reads_yaml(tmp_path):
    seed_repo(tmp_path, {"held": HOLD, "clean": None})
    backend = DirBackend(tmp_path)
    want = holds.desired_holds(tmp_path)
    assert want == {"held": HOLD}
    assert holds.sync_holds(backend, tmp_path).set_apps == ["held"]


def test_sync_injects_hold_and_preserves_rest(tmp_path):
    seed_repo(tmp_path, {"ghost": HOLD})
    backend = DirBackend(tmp_path)
    result = holds.sync_holds(backend, tmp_path)

    assert result.set_apps == ["ghost"]
    current = read_current(backend, "ghost")
    assert current["deploy"]["hold"] == HOLD
    # 验证事实字段不被触碰
    assert current["status"] == "verified"
    assert current["deploy"]["container_port"] == 8080
    assert current["deploy"]["data_path"] == "/data"


def test_sync_is_idempotent(tmp_path):
    seed_repo(tmp_path, {"ghost": HOLD})
    backend = DirBackend(tmp_path)
    holds.sync_holds(backend, tmp_path)
    again = holds.sync_holds(backend, tmp_path)
    assert again.unchanged == ["ghost"]
    assert again.set_apps == []


def test_clear_only_via_explicit_app(tmp_path):
    """解除暂停必须显式 --app：yaml 已无 hold + current 仍有 hold → 清除。"""
    seed_repo(tmp_path, {"ghost": HOLD})
    backend = DirBackend(tmp_path)
    holds.sync_holds(backend, tmp_path)

    # 移除 yaml 里的 hold（模拟解除暂停后的注册状态）
    (tmp_path / "apps" / "ghost.yaml").write_text(
        yaml.safe_dump({"deployment": {"size": "small"}}, allow_unicode=True),
        encoding="utf-8",
    )
    result = holds.sync_holds(backend, tmp_path, apps=["ghost"])
    assert result.cleared == ["ghost"]
    assert "hold" not in read_current(backend, "ghost")["deploy"]


def test_absent_current_is_skipped(tmp_path):
    seed_repo(tmp_path, {"ghost": HOLD})
    (tmp_path / "verified" / "ghost" / "current.json").unlink()
    result = holds.sync_holds(DirBackend(tmp_path), tmp_path)
    assert result.absent == ["ghost"]


def test_corrupt_current_is_reported_not_raised(tmp_path):
    seed_repo(tmp_path, {"ghost": HOLD})
    (tmp_path / "verified" / "ghost" / "current.json").write_bytes(b"{not json")
    result = holds.sync_holds(DirBackend(tmp_path), tmp_path)
    assert result.set_apps == []
    assert any("损坏" in n for n in result.notes)
