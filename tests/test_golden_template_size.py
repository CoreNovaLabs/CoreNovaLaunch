"""TemplateBody 51200 字节 API 上限的离线回归（全 mock，零 AWS 调用、零上传）。

canary.yaml / app.yaml 已超上限，只有 TemplateURL 分支能被 CFN 接受；若代码回退成内联
body，真实 Golden 会在 create_stack 处被拒，而单纯校验模板的测试照样通过。
"""

from __future__ import annotations

import pytest

from corenova import golden


class _FakeCfg:
    def __init__(self, root, *, bucket="corenova-templates-test", region="us-east-1",
                 s3_region="us-east-1"):
        self.root = root
        self.template_bucket = bucket
        self.region = region
        self.template_s3_region = s3_region


class _FakeS3:
    def __init__(self, log):
        self._log = log

    def put_object(self, *, Bucket, Key, Body, ContentType):  # noqa: N803
        self._log.append(("put", Bucket, Key, len(Body), ContentType))

    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self._log.append(("delete", Bucket, Key))


class _BoomS3(_FakeS3):
    def delete_object(self, *, Bucket, Key):  # noqa: N803
        self._log.append(("delete-failed", Bucket, Key))
        raise RuntimeError("AccessDenied")


class _FakeCfn:
    def __init__(self, log, *, validate_error=None, change_set_error=None):
        self._log = log
        self._validate_error = validate_error
        self._change_set_error = change_set_error

    def validate_template(self, **kwargs):
        self._log.append(("validate", kwargs))
        if self._validate_error:
            raise self._validate_error
        return {}

    def create_stack(self, **kwargs):
        self._log.append(("create_stack", kwargs))
        return {"StackId": "stack-1"}

    def update_stack(self, **kwargs):
        self._log.append(("update_stack", kwargs))
        return {"StackId": "stack-1"}

    def create_change_set(self, **kwargs):
        self._log.append(("change_set", kwargs))
        if self._change_set_error:
            raise self._change_set_error
        return {"Id": "change-set-arn"}

    def describe_change_set(self, **kwargs):
        # CFN 直到 change-set 就绪才拉 TemplateURL：此处即"服务端已消费"的时刻
        self._log.append(("consumed",))
        return {"Status": "CREATE_COMPLETE", "Changes": []}

    def describe_stacks(self, **kwargs):
        raise RuntimeError("Stack with id corenova-canary does not exist")


class _FakeAws:
    def __init__(self, cfg, log, **kw):
        self.cfg = cfg
        self.log = log
        self.cfn = _FakeCfn(log, **kw)


def _ops(log):
    return [entry[0] for entry in log]


def _sources(log, kind):
    return [entry[1] for entry in log if entry[0] == kind]


def _write_template(tmp_path, name, body):
    path = tmp_path / golden.TEMPLATE_DIR / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def _oversized():
    return "x" * (golden.TEMPLATE_BODY_API_LIMIT + 1)


@pytest.fixture
def log():
    return []


@pytest.fixture
def aws(tmp_path, monkeypatch, log):
    monkeypatch.setattr(golden, "_template_s3", lambda _aws: _FakeS3(log))
    return _FakeAws(_FakeCfg(tmp_path), log)


# --------------------------------------------------------------------------- 传输分支


def test_oversized_template_goes_through_template_url(aws, log, tmp_path):
    _write_template(tmp_path, "canary.yaml", _oversized())

    source, key = golden.template_argument(aws, "canary.yaml")

    assert key == "golden-validate/us-east-1/canary.yaml"
    assert source == {
        "TemplateURL": f"https://corenova-templates-test.s3.us-east-1.amazonaws.com/{key}"
    }
    assert _ops(log) == ["put"]


def test_small_template_keeps_template_body_and_touches_no_s3(aws, log, tmp_path):
    _write_template(tmp_path, "network.yaml", "Resources: {}\n")

    source, key = golden.template_argument(aws, "network.yaml")

    assert set(source) == {"TemplateBody"}
    assert key == ""
    assert log == []


def test_oversized_without_bucket_fails_loudly_instead_of_silently(tmp_path, monkeypatch, log):
    monkeypatch.setattr(golden, "_template_s3", lambda _aws: _FakeS3(log))
    aws = _FakeAws(_FakeCfg(tmp_path, bucket=""), log)
    _write_template(tmp_path, "app.yaml", _oversized())

    with pytest.raises(RuntimeError, match="TEMPLATE_S3_BUCKET"):
        golden.template_argument(aws, "app.yaml")

    assert log == []


def test_limit_boundary_is_inclusive(aws, log, tmp_path):
    at_limit = "A" * golden.TEMPLATE_BODY_API_LIMIT
    _write_template(tmp_path, "app.yaml", at_limit)
    source, key = golden.template_argument(aws, "app.yaml")
    assert set(source) == {"TemplateBody"} and key == ""

    _write_template(tmp_path, "app.yaml", at_limit + "B")
    source, key = golden.template_argument(aws, "app.yaml")
    assert set(source) == {"TemplateURL"} and key.endswith("/app.yaml")


def test_token_scopes_keys_so_concurrent_steps_never_clobber(aws, log, tmp_path):
    _write_template(tmp_path, "canary.yaml", _oversized())

    _, plan_key = golden.template_argument(aws, "canary.yaml", "plan-120000")
    _, deploy_key = golden.template_argument(aws, "canary.yaml", "deploy-120000")

    assert plan_key.endswith("canary.yaml-plan-120000")
    assert deploy_key.endswith("canary.yaml-deploy-120000")


# --------------------------------------------------------------------------- 校验 / 建栈


def test_validate_template_uses_url_and_removes_the_public_temp_object(aws, log, tmp_path):
    _write_template(tmp_path, "app.yaml", _oversized())

    golden._validate_template(aws, "app.yaml")

    assert "TemplateURL" in _sources(log, "validate")[0]
    assert _ops(log) == ["put", "validate", "delete"]


def test_validate_template_cleans_up_even_when_cfn_rejects(aws, log, tmp_path):
    aws.cfn = _FakeCfn(log, validate_error=ValueError("Template format error"))
    _write_template(tmp_path, "app.yaml", _oversized())

    with pytest.raises(ValueError, match="Template format error"):
        golden._validate_template(aws, "app.yaml")

    assert _ops(log) == ["put", "validate", "delete"]


def test_small_template_validation_keeps_the_old_body_path(aws, log, tmp_path):
    _write_template(tmp_path, "network.yaml", "Resources: {}\n")

    golden._validate_template(aws, "network.yaml")

    assert "TemplateBody" in _sources(log, "validate")[0]
    assert _ops(log) == ["validate"]


@pytest.mark.parametrize("create", [True, False])
def test_deploy_canary_forwards_the_source_to_create_and_update(aws, log, tmp_path, create):
    _write_template(tmp_path, "canary.yaml", _oversized())
    source, key = golden.template_argument(aws, "canary.yaml", "deploy-120000")

    golden.deploy_canary(aws, "corenova-canary", {"AmiId": "ami-1"}, create=create, source=source)
    golden.cleanup_template(aws, key)

    kwargs = _sources(log, "create_stack" if create else "update_stack")[0]
    assert "TemplateURL" in kwargs and "TemplateBody" not in kwargs
    assert _ops(log)[-1] == "delete"


def test_change_set_keeps_the_object_until_cfn_has_consumed_it(aws, log, tmp_path):
    _write_template(tmp_path, "canary.yaml", _oversized())

    change_set_id, summary = golden.plan_change_set(aws, "corenova-canary", {"AmiId": "ami-1"}, "120000")

    assert change_set_id == "change-set-arn"
    assert "0 项变更" in summary
    kwargs = _sources(log, "change_set")[0]
    assert "TemplateURL" in kwargs and "TemplateBody" not in kwargs
    # put → 提交 change-set → CFN 读取 → 才删；顺序颠倒会让 CFN 报 AccessDenied
    assert _ops(log) == ["put", "change_set", "consumed", "consumed", "delete"]


def test_change_set_failure_still_cleans_up(aws, log, tmp_path):
    aws.cfn = _FakeCfn(log, change_set_error=ValueError("Early validation failed"))
    _write_template(tmp_path, "canary.yaml", _oversized())

    with pytest.raises(ValueError, match="Early validation failed"):
        golden.plan_change_set(aws, "corenova-canary", {"AmiId": "ami-1"}, "120000")

    assert _ops(log) == ["put", "change_set", "delete"]


# --------------------------------------------------------------------------- 清理


def test_cleanup_of_a_body_template_is_a_noop(aws, log):
    golden.cleanup_template(aws, "")

    assert log == []


def test_cleanup_failure_is_reported_but_never_masks_the_verdict(aws, monkeypatch, log):
    monkeypatch.setattr(golden, "_template_s3", lambda _aws: _BoomS3(log))

    golden.cleanup_template(aws, "golden-validate/us-east-1/app.yaml")

    assert _ops(log) == ["delete-failed"]


# --------------------------------------------------------------------------- 现网模板


def test_shipped_templates_pick_a_transport_cfn_accepts(monkeypatch, log):
    """真实模板：canary/app 必须走 URL，network 必须走 body——否则门禁与线上不一致。"""
    from corenova.config import Config

    cfg = Config.load()
    monkeypatch.setenv("TEMPLATE_S3_BUCKET", cfg.template_bucket or "corenova-templates-test")
    monkeypatch.setattr(golden, "_template_s3", lambda _aws: _FakeS3(log))
    aws = _FakeAws(cfg, log)

    for name in golden.TEMPLATES:
        size = len(golden.template_path(cfg, name).read_bytes())
        over_limit = size > golden.TEMPLATE_BODY_API_LIMIT
        source, key = golden.template_argument(aws, name)
        assert ("TemplateURL" in source) is over_limit, f"{name} ({size} bytes)"
        assert bool(key) is over_limit, f"{name} ({size} bytes)"
        golden.cleanup_template(aws, key)
