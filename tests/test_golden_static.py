"""CFN 模板离线静态检查的回归用例。

这三类缺陷都是"validate-template 通过、真实建栈才发现"或"cfn-init 静空跑"的类型，
只能靠静态规则兜住；每一条都对应一次真实的线上失败。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import yaml

from corenova import golden

FIXTURES = Path(__file__).resolve().parents[1] / "templates" / "cloudformation" / "fixed"


def _instance_tpl(init: dict) -> dict:
    return {"Resources": {"Instance": {"Type": "AWS::EC2::Instance",
                                       "Metadata": {"AWS::CloudFormation::Init": init}}}}


def test_config_set_key_is_rejected():
    tpl = _instance_tpl({"config-set": {"default": ["a"]}, "config": {"a": {"files": {}}}})
    errs = golden._cfn_init_errors("app.yaml", tpl)
    assert any("非法键" in e for e in errs), errs


def test_missing_configsets_is_rejected():
    errs = golden._cfn_init_errors("app.yaml", _instance_tpl({"Comment": "x"}))
    assert any("缺少 configSets" in e for e in errs), errs


def test_dangling_configset_reference_is_rejected():
    tpl = _instance_tpl({"configSets": {"default": ["nope"]}, "present": {"files": {}}})
    errs = golden._cfn_init_errors("app.yaml", tpl)
    assert any("不存在的配置 'nope'" in e for e in errs), errs


def test_correct_init_shape_passes():
    tpl = _instance_tpl({"configSets": {"default": ["a"]}, "a": {"files": {}}})
    assert golden._cfn_init_errors("app.yaml", tpl) == []


def test_unknown_cfn_function_is_rejected():
    tpl = {"Resources": {"W": {"Type": "AWS::CloudFormation::WaitCondition",
                              "Properties": {"Timeout": {"Fn::MultiplyInt": [1, 60]}}}}}
    errs = golden._function_errors("app.yaml", tpl)
    assert any("Fn::MultiplyInt" in e for e in errs), errs


def test_shipped_templates_pass_static_rules():
    for name in ("network.yaml", "app.yaml", "canary.yaml"):
        tpl = yaml.safe_load((FIXTURES / name).read_text(encoding="utf-8"))
        assert golden._function_errors(name, tpl) == []
        assert golden._structure_errors(name, tpl) == []


def test_real_init_metadata_is_valid_and_assets_inlined():
    tpl = yaml.safe_load((FIXTURES / "app.yaml").read_text(encoding="utf-8"))
    init = tpl["Resources"]["Instance"]["Metadata"]["AWS::CloudFormation::Init"]
    assert "configSets" in init and "config" not in init and "config-set" not in init
    assert "default" in init["configSets"]


def test_inlined_assets_roundtrip(cfg=None):
    from corenova.config import Config

    c = cfg or Config.load()
    inlined = golden.inlined_assets(c, "app.yaml")
    assert set(inlined) >= {"00-packages-and-docker-runtime.sh", "10-nginx-base.sh",
                            "40-ready-and-signal.sh"}
    assert golden.asset_drift(c) == []


def test_app_runtime_receives_resolved_url_without_image_curl_dependency():
    tpl = yaml.safe_load((FIXTURES / "app.yaml").read_text(encoding="utf-8"))
    assert "AppUrlEnvironmentName" in tpl["Parameters"]
    init = tpl["Resources"]["Instance"]["Metadata"]["AWS::CloudFormation::Init"]
    env_content = init["20-assets"]["files"]["/opt/corenova/etc/init.env"]["content"]
    assert "CFNOVA_APP_URL_ENV_NAME" in str(env_content)
    app_asset = golden.inlined_assets(SimpleNamespace(root=FIXTURES.parents[2]), "app.yaml")[
        "30-app-container.sh"
    ]
    assert "latest/meta-data/public-hostname" not in app_asset
    assert ". /opt/corenova/bin/05-access-policy.sh" in app_asset
    assert "APP_URL_ENV_NAME" in app_asset
    assert "--health-cmd" not in app_asset


def test_retained_data_volume_is_discoverable_by_stack_tags():
    tpl = yaml.safe_load((FIXTURES / "app.yaml").read_text(encoding="utf-8"))
    props = tpl["Resources"]["Instance"]["Properties"]
    assert props["PropagateTagsToVolumeOnCreation"] is True
    condition, data, absent = props["BlockDeviceMappings"][1]["Fn::If"]
    assert condition == "HasDataVolume"
    assert absent == {"Ref": "AWS::NoValue"}
    assert data["DeviceName"] == "/dev/sdf"
    assert data["Ebs"]["DeleteOnTermination"] is False


# ------------------------------------------------------------------ 硬编码反模式：应用模式派生


def test_real_apps_derive_both_registrations():
    """真实注册表（ghost + uptime-kuma）都派生出镜像与端口两组模式。"""
    root = Path(__file__).resolve().parents[1]
    labels = {label for _app, label, _p in golden.app_hardcode_patterns(root)}
    assert {"ghost", "2368", "uptime-kuma", "3001"} <= labels


def test_app_hardcode_patterns_derived_from_registry(tmp_path):
    """镜像取最后路径段（registry/org 名不算），端口用 \b 防止撞进更长数字。"""
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "demo.yaml").write_text(
        yaml.safe_dump({"deploy": {"docker_image": "example/demo-app", "container_port": 3001}}),
        encoding="utf-8",
    )
    pats = {label: p for _app, label, p in golden.app_hardcode_patterns(tmp_path)}
    assert pats["demo-app"].search("docker run demo-app:1.2.3")
    assert not pats["demo-app"].search("image: example/demo-app")  # 无 :数字标签不算
    assert pats["3001"].search("-p 3001:3001")
    assert not pats["3001"].search("13001:30012")


def test_app_hardcode_patterns_skip_malformed_registration(tmp_path):
    """畸形注册文件跳过而不是崩溃：schema 违规由 appspec.validate 报告。"""
    apps = tmp_path / "apps"
    apps.mkdir()
    (apps / "broken.yaml").write_text("deploy: [not, a, mapping]\n", encoding="utf-8")
    (apps / "empty.yaml").write_text("app: {name: x}\n", encoding="utf-8")
    assert golden.app_hardcode_patterns(tmp_path) == []


def test_hardcoding_errors_catches_derived_app_pattern(tmp_path):
    """模板硬编码了未写死在 golden.py 里的应用镜像/端口 → 派生模式照常命中。"""
    (tmp_path / "apps").mkdir()
    (tmp_path / "apps" / "demo.yaml").write_text(
        "deploy:\n  docker_image: demo-app\n  container_port: 3001\n", encoding="utf-8"
    )
    tpl_dir = tmp_path / "templates" / "cloudformation" / "fixed"
    tpl_dir.mkdir(parents=True)
    (tpl_dir / "app.yaml").write_text(
        "Resources:\n  X: docker run -d demo-app:1.2.3 -p 3001:3001\n", encoding="utf-8"
    )
    errs = golden._hardcoding_errors(SimpleNamespace(root=tmp_path))
    assert any("demo-app:1" in e and "apps/demo.yaml" in e for e in errs), errs
    assert any("3001:3001" in e for e in errs), errs
