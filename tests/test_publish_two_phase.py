"""两阶段提交与投影一致性的行为测试（verification-manifest.md §6 / §4）。

关键断言：上传探测未通过时 **绝不** 写 current.json —— 这是整个发布门禁的落点。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from corenova import manifest as mf
from corenova import publish
from corenova.backend import DirBackend
from corenova.config import Config

LOCAL_TRUE = {
    "compose_started": True,
    "container_healthy": True,
    "health_check_passed": True,
    "tests_passed": True,
    "screenshots_generated": True,
    "required_platform_contract_valid": True,
}


class Cfg:
    """最小 config 替身：只暴露 publish 需要的字段。"""

    verified_backend = "dir"
    r2_public_base_url = ""
    region = "us-east-1"
    architecture = "x86_64"
    base_ami_source = "public"


def sample_manifest(**over) -> dict:
    m = {
        "schema_version": "1.0",
        "verification_id": "ghost-v6.61.0-20260829-001",
        "app": "ghost",
        "app_version": "v6.61.0",
        "verification_run_id": "100",
        "verified_at": "2026-08-29T10:00:00Z",
        "_strategy": "release_tag",
        "checks": {**LOCAL_TRUE, "screenshots_uploaded": False, "report_uploaded": False,
                   "verification_manifest_uploaded": False},
        "verification": {"application": "passed", "platform": "referenced", "tests": "passed"},
        "container": {"image": "ghost:6.61.0-alpine", "digest": "sha256:aaa",
                      "manifest_digest": "sha256:bbb", "platform": "linux/amd64"},
        "platform": {"platform_verification_id": "plat-x", "ami_id": "ami-0abc",
                     "base_ami_source": "public", "source_ami_name": "ubuntu-noble-24.04-amd64-server-*",
                     "region": "us-east-1", "architecture": "x86_64"},
        "artifacts": {"screenshots": [
            {"scenario": "home", "file": "home.png", "url": "/screenshots/ghost/v6.61.0/home.png",
             "caption": {"en": "Home", "zh": "首页"}}],
            "report_url": "", "workflow_run_url": ""},
        "website": {
            "app": "ghost", "app_version": "v6.61.0",
            "verification_id": "ghost-v6.61.0-20260829-001", "verification_run_id": "100",
            "verified_at": "2026-08-29T10:00:00Z", "platform_verification_id": "plat-x",
            "ami_id": "ami-0abc", "region": "us-east-1", "architecture": "x86_64",
            "health": "passed", "status": "verified",
            "screenshots_order": ["home"],
            "screenshots": [
                {"scenario": "home", "file": "home.png",
                 "url": "/screenshots/ghost/v6.61.0/home.png", "caption": {"en": "Home", "zh": "首页"}}],
            "deploy": {"docker_image": "ghost:6.61.0-alpine", "regions": ["us-east-1"],
                       "instance_type": "t3.small", "container_port": 2368,
                       "data_volume_gb": 30, "health_check_path": "/",
                       "documentation_url": "https://ghost.org/docs/",
                       "post_deploy": {
                           "admin_path": "/ghost/",
                           "admin_setup": {"en": "Setup wizard on first visit.",
                                           "zh": "首次访问进入初始化向导。"},
                           "notes": [{"en": "Back up the data volume.", "zh": "备份数据卷。"}],
                       },
                       "cost_estimate": {"monthly_usd": 18,
                                         "note": {"en": "t3.small + 30 GB gp3.",
                                                  "zh": "t3.small + 30GB gp3。"}},
                       "data_path": "/var/lib/ghost/content", "app_url_env_name": "url"},
            "release": {"type": "initial", "previous_version": "", "type_evidence": "rule1"},
        },
    }
    m["website"].update(over.pop("website", {}))
    m.update(over)
    return m


@pytest.fixture
def shots(tmp_path: Path) -> Path:
    d = tmp_path / "screenshots"
    d.mkdir()
    (d / "home.png").write_bytes(b"\x89PNG fake")
    return d


def test_publish_commit_point_writes_all_three(tmp_path, shots):
    out = tmp_path / "data"
    backend = DirBackend(out)
    m = sample_manifest()
    res = publish.publish(backend, Cfg(), m, shots, "<html/>")

    assert res.current_written and res.committed
    assert all(m["checks"][c] for c in mf.CHECKS), m["checks"]
    current = json.loads((out / "verified/ghost/current.json").read_text())
    version = json.loads((out / "verified/ghost/versions/v6.61.0.json").read_text())
    index = json.loads((out / "verified/index.json").read_text())

    # current.json 必须逐字段等于 Manifest 的 website 段（不增不减）
    assert current == version["website"]
    assert index["apps"][0]["verification_id"] == m["verification_id"]
    assert (out / "screenshots/ghost/v6.61.0/home.png").exists()
    assert (out / "reports/ghost-v6.61.0-20260829-001.html").exists()
    # 上传成功后 report_url 才被回填（P2 之后才知道键名）
    assert current["report_url"] == "/reports/ghost-v6.61.0-20260829-001.html"


def test_versions_index_tracks_committed_versions(tmp_path, shots):
    """versions/index.json：只收录过 P5 门禁的版本，按 verified_at 倒序、同版本去重。"""
    out = tmp_path / "data"
    backend = DirBackend(out)
    cfg = Cfg()
    first = sample_manifest()
    publish.publish(backend, cfg, first, shots, "<html/>")

    # 旧版本被覆盖保护拒绝 → 不进清单
    older = sample_manifest(app_version="v6.60.0",
                            verification_id="ghost-v6.60.0-20260829-001",
                            verified_at="2026-08-24T10:00:00Z")
    older["website"]["app_version"] = "v6.60.0"
    older["website"]["verification_id"] = older["verification_id"]
    res = publish.publish(backend, cfg, older, shots, "<html/>")
    assert not res.current_written

    # 新版本提交 → 清单倒序收录
    newer = sample_manifest(app_version="v6.62.0",
                            verification_id="ghost-v6.62.0-20260830-001",
                            verified_at="2026-08-30T10:00:00Z",
                            verification_run_id="200")
    newer["website"]["app_version"] = "v6.62.0"
    newer["website"]["verification_id"] = newer["verification_id"]
    publish.publish(backend, cfg, newer, shots, "<html/>")

    vindex = json.loads((out / "verified/ghost/versions/index.json").read_text())
    assert vindex["app"] == "ghost"
    assert [e["app_version"] for e in vindex["versions"]] == ["v6.62.0", "v6.61.0"]
    assert vindex["versions"][0]["verification_id"] == "ghost-v6.62.0-20260830-001"
    assert vindex["versions"][0]["status"] == "verified"

    # 同一版本重验（run 更新）→ 清单里替换而非追加
    again = sample_manifest(app_version="v6.62.0",
                            verification_id="ghost-v6.62.0-20260830-002",
                            verified_at="2026-08-30T12:00:00Z",
                            verification_run_id="201")
    again["website"]["verification_id"] = again["verification_id"]
    publish.publish(backend, cfg, again, shots, "<html/>")
    vindex = json.loads((out / "verified/ghost/versions/index.json").read_text())
    assert [e["app_version"] for e in vindex["versions"]] == ["v6.62.0", "v6.61.0"]
    assert vindex["versions"][0]["verification_id"].endswith("-002")

    # P5 门禁失败（旧版本拒绝）时清单保持不变
    assert json.loads((out / "verified/ghost/versions/index.json").read_text())["versions"][0][
        "app_version"
    ] == "v6.62.0"


# ------------------------------------------------------------------ 索引并发写保护


class RacingDirBackend(DirBackend):
    """模拟跨应用并发：条件写前被并发方抢先写入 index.json（ETag 失配）。"""

    def __init__(self, root: Path, races: int = 1):
        super().__init__(root)
        self._races = races

    def put_if_match(self, key, data, etag):
        if key == "verified/index.json" and self._races > 0:
            self._races -= 1
            other = {
                "schema_version": "1.0",
                "apps": [{
                    "app": "other-app", "app_version": "v1.0.0",
                    "verification_id": "other-app-v1.0.0-20260829-001",
                    "status": "verified", "health": "passed",
                    "verified_at": "2026-08-29T10:00:00Z",
                }],
                "generated_at": "2026-08-29T10:00:00Z",
            }
            super().put(key, json.dumps(other).encode())
            return False  # 本方的 ETag 已失配
        return super().put_if_match(key, data, etag)


def test_index_conditional_write_merges_concurrent_app(tmp_path, shots):
    """并发写冲突 → 重读重合，绝不丢掉并发方的条目（last-writer-wins 反例）。"""
    out = tmp_path / "data"
    backend = RacingDirBackend(out, races=2)
    res = publish.publish(backend, Cfg(), sample_manifest(), shots, "<html/>")
    assert res.committed

    index = json.loads((out / "verified/index.json").read_text())
    apps = {a["app"] for a in index["apps"]}
    assert apps == {"ghost", "other-app"}, "并发方的条目必须保留"


def test_index_persistent_conflict_fails_loudly(tmp_path, shots, monkeypatch):
    """条件写连续失配 → 发布失败，而不是静默丢数据。"""
    out = tmp_path / "data"
    backend = DirBackend(out)
    monkeypatch.setattr(publish, "_INDEX_WRITE_ATTEMPTS", 2)

    def always_lose(key, data, etag):
        return False

    monkeypatch.setattr(backend, "put_if_match", always_lose)
    with pytest.raises(RuntimeError, match="条件写连续"):
        publish.publish(backend, Cfg(), sample_manifest(), shots, "<html/>")


def test_missing_screenshot_blocks_commit(tmp_path, shots):
    """截图对象探测不通过 → 只留占位记录，current.json 不得出现。"""
    out = tmp_path / "data"
    backend = DirBackend(out)
    m = sample_manifest()
    shots_file = shots / "home.png"
    shots_file.unlink()  # 模拟上传后探测失败

    res = publish.publish(backend, Cfg(), m, shots, "<html/>")
    assert not res.current_written
    assert res.checks["screenshots_uploaded"] is False

    version = json.loads((out / "verified/ghost/versions/v6.61.0.json").read_text())
    assert version["checks"]["screenshots_uploaded"] is False
    assert not (out / "verified/ghost/current.json").exists()
    assert not (out / "verified/index.json").exists()


def test_local_gate_failure_writes_nothing(tmp_path, shots):
    out = tmp_path / "data"
    backend = DirBackend(out)
    m = sample_manifest()
    m["checks"]["health_check_passed"] = False
    res = publish.publish(backend, Cfg(), m, shots, "<html/>")
    assert not res.current_written
    assert list(out.rglob("*.json")) == [], "P0 未过时不得产生任何写入"


def test_scratch_keys_never_uploaded(tmp_path, shots):
    out = tmp_path / "data"
    backend = DirBackend(out)
    publish.publish(backend, Cfg(), sample_manifest(), shots, "<html/>")
    version = json.loads((out / "verified/ghost/versions/v6.61.0.json").read_text())
    assert not [k for k in version if k.startswith("_")], "契约外的临时键泄漏到事实源"
    current = json.loads((out / "verified/ghost/current.json").read_text())
    assert not [k for k in current if k.startswith("_")]


def test_reverify_same_day_bumps_seq(tmp_path, shots):
    out = tmp_path / "data"
    backend = DirBackend(out)
    first = sample_manifest()
    publish.publish(backend, Cfg(), first, shots, "<html/>")

    second = sample_manifest(verification_run_id="101")  # 另一次运行
    res = publish.publish(backend, Cfg(), second, shots, "<html/>")
    assert second["verification_id"].endswith("-002"), second["verification_id"]
    assert res.current_written
    index = json.loads((out / "verified/index.json").read_text())
    assert index["apps"][0]["verification_id"].endswith("-002")


# ------------------------------------------------------------------ 版本覆盖保护


def test_old_version_cannot_overwrite_current(tmp_path, shots):
    out = tmp_path / "data"
    backend = DirBackend(out)
    cfg = Cfg()
    publish.publish(backend, cfg, sample_manifest(), shots, "<html/>")

    older = sample_manifest(app_version="v6.60.0",
                            verification_id="ghost-v6.60.0-20260829-001")
    older["website"]["app_version"] = "v6.60.0"
    older["website"]["verification_id"] = older["verification_id"]
    res = publish.publish(backend, cfg, older, shots, "<html/>")
    assert not res.current_written
    assert any("版本覆盖保护" in n for n in res.notes), res.notes
    current = json.loads((out / "verified/ghost/current.json").read_text())
    assert current["app_version"] == "v6.61.0"


def test_non_semver_falls_back_to_run_id(tmp_path):
    backend = DirBackend(tmp_path)
    backend.put("verified/ghost/current.json",
                json.dumps({"app_version": "main", "verification_run_id": "500"}).encode())
    ok, why = publish.may_update_current(backend, "ghost", "main", "400", "git_branch")
    assert not ok and "不新于" in why
    ok2, _ = publish.may_update_current(backend, "ghost", "main", "600", "git_branch")
    assert ok2
    ok3, _ = publish.may_update_current(backend, "ghost", "main", "600", "git_branch", force=True)
    assert ok3


# ------------------------------------------------------------------ 投影断言


def test_projection_drift_detected():
    m = sample_manifest()
    m["website"]["ami_id"] = "ami-drifted"
    with pytest.raises(AssertionError, match="漂移"):
        mf.assert_projection(m)


def test_health_must_be_projection():
    m = sample_manifest()
    m["website"]["health"] = "failed"
    with pytest.raises(AssertionError):
        mf.assert_projection(m)


def test_screenshot_order_must_match():
    m = sample_manifest()
    m["website"]["screenshots_order"] = ["admin"]
    with pytest.raises(AssertionError):
        mf.assert_projection(m)


# ------------------------------------------------------------------ 部署后指引投影（app-schema 规则17）


def _build_inputs(tmp_path):
    from corenova.manifest import VerifyOutcome
    from corenova.resolver import ResolvedImage, ResolvedVersion
    from tests.test_schema_rules import make as make_spec

    resolved = ResolvedVersion(
        app_version="v6.61.0", release_tag="v6.61.0", source_revision="abc123",
        release_type="initial", type_evidence="rule1", release_body="", published_at="",
    )
    image = ResolvedImage(
        image_ref="ghost:6.61.0-alpine", pull_ref="ghost:6.61.0-alpine@sha256:aaa",
        digest="sha256:aaa", manifest_digest="sha256:bbb",
        repo="ghost", tag="6.61.0-alpine", host="docker.io", upstream_host="docker.io",
    )
    platform = {"platform_verification_id": "plat-x", "ami_id": "ami-0abc",
                "architecture": "x86_64", "region": "us-east-1"}
    outcome = VerifyOutcome(
        checks={c: True for c in mf.CHECKS},
        verification={"application": "passed", "platform": "referenced", "tests": "passed"},
        screenshots=[
            {"slug": "home", "file": "home.png", "caption": {"en": "Home", "zh": "首页"}},
            {"slug": "admin", "file": "admin.png", "caption": {"en": "Admin", "zh": "后台"}},
        ],
    )
    return make_spec, resolved, image, platform, outcome


def test_deploy_guide_projected_when_registered(tmp_path):
    make_spec, resolved, image, platform, outcome = _build_inputs(tmp_path)
    spec = make_spec(tmp_path)
    m = mf.build(spec, tmp_path, resolved, image, platform, outcome, Cfg(), "local-1")
    assert m["website"]["deploy"]["post_deploy"] == spec.g("deployment.post_deploy")


def test_deploy_guide_key_absent_when_not_registered(tmp_path):
    make_spec, resolved, image, platform, outcome = _build_inputs(tmp_path)
    spec = make_spec(tmp_path, lambda d: d["deployment"].__delitem__("post_deploy"))
    m = mf.build(spec, tmp_path, resolved, image, platform, outcome, Cfg(), "local-2")
    assert "post_deploy" not in m["website"]["deploy"]


def test_cost_estimate_projected_when_registered(tmp_path):
    make_spec, resolved, image, platform, outcome = _build_inputs(tmp_path)
    spec = make_spec(tmp_path)
    m = mf.build(spec, tmp_path, resolved, image, platform, outcome, Cfg(), "local-3")
    assert m["website"]["deploy"]["cost_estimate"] == spec.g("deployment.cost_estimate")


def test_cost_estimate_key_absent_when_not_registered(tmp_path):
    make_spec, resolved, image, platform, outcome = _build_inputs(tmp_path)
    spec = make_spec(tmp_path, lambda d: d["deployment"].__delitem__("cost_estimate"))
    m = mf.build(spec, tmp_path, resolved, image, platform, outcome, Cfg(), "local-4")
    assert "cost_estimate" not in m["website"]["deploy"]


def test_data_path_projected_when_registered(tmp_path):
    make_spec, resolved, image, platform, outcome = _build_inputs(tmp_path)
    spec = make_spec(tmp_path)
    m = mf.build(spec, tmp_path, resolved, image, platform, outcome, Cfg(), "local-5")
    assert m["website"]["deploy"]["data_path"] == "/var/lib/ghost/content"


def test_data_path_key_absent_when_not_registered(tmp_path):
    make_spec, resolved, image, platform, outcome = _build_inputs(tmp_path)
    spec = make_spec(tmp_path, lambda d: d["deployment"].__delitem__("data_path"))
    m = mf.build(spec, tmp_path, resolved, image, platform, outcome, Cfg(), "local-6")
    assert "data_path" not in m["website"]["deploy"]


def test_deploy_runtime_contract_is_projected(tmp_path):
    make_spec, resolved, image, platform, outcome = _build_inputs(tmp_path)
    spec = make_spec(tmp_path)
    m = mf.build(spec, tmp_path, resolved, image, platform, outcome, Cfg(), "local-7")
    deploy = m["website"]["deploy"]
    assert deploy["data_volume_gb"] == 30
    assert deploy["health_check_path"] == "/"
    assert deploy["app_url_env_name"] == "url"


def test_app_url_env_name_absent_when_not_registered(tmp_path):
    make_spec, resolved, image, platform, outcome = _build_inputs(tmp_path)
    spec = make_spec(tmp_path, lambda d: d["deployment"].__delitem__("app_url_env_name"))
    m = mf.build(spec, tmp_path, resolved, image, platform, outcome, Cfg(), "local-8")
    assert "app_url_env_name" not in m["website"]["deploy"]


def test_config_defaults_present(monkeypatch):
    # Config.region/base_ami_source 优先读环境变量；不清掉的话，本机/CI 上
    # 带 AWS_REGION 的开发环境会把断言带偏（测的是仓库默认值，不是环境覆盖）
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("BASE_AMI_SOURCE", raising=False)
    cfg = Config.load()
    assert cfg.region == "us-east-1" and cfg.architecture == "x86_64"
    assert cfg.base_ami_source == "public"
    assert cfg.ami_ssm_parameter().startswith("/aws/service/")


def test_hold_is_live_state_not_baked_into_version_record(tmp_path, shots):
    """deployment-contract.md §2.5：hold 只活在 current.json。

    冻结进不可变版本证据会双向出错：解除暂停后该版本仍被旧快照拦；
    hold 之前验证的版本没有快照，暂停期间反而看起来可部署。"""
    out = tmp_path / "data"
    backend = DirBackend(out)
    m = sample_manifest()
    hold = {"reason": {"en": "Paused pending production check.",
                       "zh": "暂停待生产核对。"}}
    m["website"]["deploy"]["hold"] = hold
    res = publish.publish(backend, Cfg(), m, shots, "<html/>")
    assert res.committed

    current = json.loads((out / "verified/ghost/current.json").read_text())
    version = json.loads((out / "verified/ghost/versions/v6.61.0.json").read_text())
    assert current["deploy"]["hold"] == hold
    assert "hold" not in version["website"]["deploy"]
    # 剥离只发生在版本记录副本——就地 pop 会污染内存 manifest
    assert m["website"]["deploy"]["hold"] == hold
