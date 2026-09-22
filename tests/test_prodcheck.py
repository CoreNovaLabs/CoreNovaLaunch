"""L1.5 生产核对离线单测（deployment-contract.md §2.6）。

零 AWS 调用：plan 是纯函数；探针全部经 CheckCtx.run_script/http_get 喂假响应，
poll_until monkeypatch 成"执行一次即定论"；hold 手术与 Manifest 投影只碰文本。
密码脱敏是安全边界，专设断言。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from corenova import holds, prodcheck
from corenova import manifest as mf
from corenova.appspec import AppSpec
from corenova.golden import Invocation
from corenova.prodcheck import CheckCtx, ProdPlan

REPO_ROOT = Path(__file__).resolve().parents[1]

DIGEST = "sha256:" + "a" * 64


def make_cfg(tmp_path: Path) -> SimpleNamespace:
    """最小 config 替身：plan/run 只读这几个字段。"""
    return SimpleNamespace(
        region="us-east-1",
        platform={},
        root=tmp_path,
        output_dir=tmp_path / "data",
        template_bucket="",
        template_s3_region="",
    )


def make_spec(checks: list[str] | None) -> AppSpec:
    data: dict = {"deployment": {}}
    if checks is not None:
        data["deployment"]["production_contract"] = {"checks": checks}
    return AppSpec(name="ghost", path=Path("apps/ghost.yaml"), raw="", data=data)


def make_manifest(**over) -> dict:
    m = {
        "app": "ghost",
        "app_version": "v6.61.0",
        "container": {"image": "ghost:6.61.0-alpine", "digest": DIGEST},
        "platform": {"ami_id": "ami-0abc123"},
        "website": {"deploy": {
            "container_port": 2368,
            "instance_type": "t3.small",
            "data_volume_gb": 30,
            "data_path": "/var/lib/ghost/content",
            "health_check_path": "/ghost/api/admin/site/",
            "app_url_env_name": "url",
            "extra_environment": ["GHOST_LOG=console", "PUBLIC_HOOK=${CORENOVA_APP_URL}/hook"],
        }},
    }
    m.update(over)
    return m


def make_plan(parameters: dict[str, str], checks: list[str]) -> ProdPlan:
    return ProdPlan(
        stack_name="corenova-prodcheck-ghost-v6610-000001",
        parameters=parameters,
        checks=checks,
        declared=checks,
        template_url="https://s3.example/app.yaml",
    )


# --------------------------------------------------------------------------- 钉扎与命名


def test_pinned_image_concatenates_digest():
    assert prodcheck.pinned_image(make_manifest()) == f"ghost:6.61.0-alpine@{DIGEST}"


@pytest.mark.parametrize("manifest", [
    {"container": {"image": "", "digest": DIGEST}},
    {"container": {"image": "ghost@sha256:x", "digest": DIGEST}},
    {"container": {"image": "ghost:6.61.0-alpine", "digest": "sha256:aaa"}},
    {"container": {"image": "ghost:6.61.0-alpine"}},
], ids=["no-image", "image-with-at", "floating-digest", "no-digest"])
def test_pinned_image_rejects(manifest):
    with pytest.raises(ValueError):
        prodcheck.pinned_image(manifest)


def test_stack_name_shape():
    name = prodcheck.stack_name_for("node-red", "v1.2.3", "123456789")
    assert name == "corenova-prodcheck-node-red-v123-456789"
    # run_id 清洗后为空（本地直跑）→ 固定尾标，不留悬空连字符
    assert prodcheck.stack_name_for("ghost", "v6.61.0", "?!").endswith("-v6610-local")


# --------------------------------------------------------------------------- plan()


def test_plan_admin_auth_branch(tmp_path):
    p = prodcheck.plan(
        make_cfg(tmp_path), make_spec(["admin_auth", "data_dir_write"]), make_manifest(),
        run_id="1", template_url="https://tpl",
    )
    assert p.parameters["AdminAuthEnabled"] == "true"
    assert p.parameters["AllowedWebCidr"] == "127.0.0.1/32"
    assert p.parameters["LaunchUrl"] == "http://localhost:8080"
    assert p.parameters["SelfSignedTls"] == "false"
    assert p.checks == ["health_external", "data_dir_write"]
    assert p.declared == ["admin_auth", "data_dir_write"]
    assert p.parameters["ImageReference"] == f"ghost:6.61.0-alpine@{DIGEST}"
    assert p.parameters["AmiId"] == "ami-0abc123"
    assert p.parameters["TerminationProtection"] == "Disabled"  # 删不掉的栈=持续计费
    assert prodcheck.STACK_TAG in p.tags
    assert p.template_url == "https://tpl"


def test_plan_closed_web_uses_loopback_branch(tmp_path):
    p = prodcheck.plan(
        make_cfg(tmp_path), make_spec(["data_dir_write"]), make_manifest(),
        run_id="1", template_url="https://tpl",
    )
    assert p.parameters["AdminAuthEnabled"] == "false"
    # 未声明 admin_auth → 对公网关闭（127.0.0.1/32），探针改走实例内回环
    assert p.parameters["AllowedWebCidr"] == "127.0.0.1/32"
    assert p.checks == ["health_external", "data_dir_write"]


def test_plan_host_metrics_flag(tmp_path):
    p = prodcheck.plan(
        make_cfg(tmp_path), make_spec(["admin_auth", "host_metrics", "data_dir_write"]),
        make_manifest(), run_id="1", template_url="https://tpl",
    )
    assert p.parameters["HostMetricsAccess"] == "true"
    # 固定执行序：health_external 恒先，其余按 EXECUTION_ORDER 排列
    assert p.checks == ["health_external", "host_metrics", "data_dir_write"]


def test_plan_projects_deploy_fields(tmp_path):
    p = prodcheck.plan(
        make_cfg(tmp_path), make_spec(["url_injection"]), make_manifest(),
        run_id="1", template_url="https://tpl",
    )
    q = p.parameters
    assert q["DataContainerPath"] == "/var/lib/ghost/content"
    assert q["HealthCheckPath"] == "/ghost/api/admin/site/"
    assert q["AppUrlEnvironmentName"] == "url"
    # 模板要求换行分隔的 KEY=VALUE 列表
    assert q["ExtraEnvironment"] == "GHOST_LOG=console\nPUBLIC_HOOK=${CORENOVA_APP_URL}/hook"
    assert q["CloudWatchLogGroupName"] == f"/corenova/prodcheck/{p.stack_name}"


def test_plan_one_click_template_is_self_contained(tmp_path):
    # 合并的 one-click 模板 DROP 掉这三个网络参数（usertemplate.DROP_PARAMS）；
    # 传给建栈请求会触发 CFN "unused parameters" 校验失败——plan 绝不能带它们。
    q = prodcheck.plan(make_cfg(tmp_path), make_spec(["data_dir_write"]), make_manifest(),
                       run_id="1", template_url="https://tpl").parameters
    for dropped in ("SubnetId", "SecurityGroupId", "NetworkStackName"):
        assert dropped not in q
    # 且不留任何空值参数
    assert "" not in q.values()


def test_plan_rejects_bad_inputs(tmp_path):
    cfg = make_cfg(tmp_path)
    with pytest.raises(ValueError, match="未知项"):
        prodcheck.plan(cfg, make_spec(["teleport"]), make_manifest(), template_url="")
    with pytest.raises(ValueError, match="为空"):
        prodcheck.plan(cfg, make_spec([]), make_manifest(), template_url="")
    with pytest.raises(ValueError, match="ami_id"):
        prodcheck.plan(cfg, make_spec(["data_dir_write"]),
                       make_manifest(platform={"ami_id": "latest"}), template_url="")


def test_url_env_names_from_manifest():
    names = prodcheck.url_env_names_from(make_manifest())
    assert names == ["url", "PUBLIC_HOOK"]  # 原生变量 + 引用 ${CORENOVA_APP_URL} 的 extra env
    m = make_manifest()
    del m["website"]["deploy"]["app_url_env_name"]
    m["website"]["deploy"]["extra_environment"] = ["A=1"]
    assert prodcheck.url_env_names_from(m) == []


# --------------------------------------------------------------------------- 探针判定


@pytest.fixture
def fast_poll(monkeypatch):
    """测试语义：probe 跑一次即定论，不等待。"""
    monkeypatch.setattr(prodcheck, "poll_until", lambda probe, **kw: probe())


def inv(stdout: str = "", code: int = 0, error: str = "") -> Invocation:
    return Invocation(exit_code=code, stdout=stdout, error=error)


def admin_ctx(scripts, http_status) -> tuple[CheckCtx, list]:
    seen_scripts: list[str] = []

    def run_script(script: str) -> Invocation:
        seen_scripts.append(script)
        return scripts(script)

    plan_ = make_plan({
        "AdminAuthEnabled": "true", "HealthCheckPath": "/",
        "ImageReference": f"ghost:6.61.0-alpine@{DIGEST}", "DataContainerPath": "/data",
    }, ["health_external"])
    ctx = CheckCtx(
        plan=plan_, instance_id="i-1", public_dns="ec2-1-2.compute-1.amazonaws.com",
        container="ghost", image_ref=plan_.parameters["ImageReference"], data_path="/data",
        run_script=run_script, http_get=lambda url, headers: (http_status(headers), {}, b""),
        tunnel_url="http://127.0.0.1:8080", session_id="test-session",
    )
    return ctx, seen_scripts


SECRET = "Sup3rS3cret!pw"


def test_health_admin_auth_green_and_redacted(fast_poll):
    ctx, _ = admin_ctx(
        lambda s: inv(f"corenova:{SECRET}\n"),
        lambda h: 401 if "Authorization" not in h else 200,
    )
    result = prodcheck.CHECK_FNS["health_external"](ctx)
    assert result.passed, result.detail
    assert "SSM port-forward" in result.detail and "200" in result.detail
    assert SECRET not in result.detail  # 密码只许活在内存
    assert SECRET not in ctx.plan.parameters.get("x", "") + result.name


def test_private_tunnel_loopback_exemption_needs_no_credentials(fast_poll):
    ctx, scripts = admin_ctx(lambda s: inv(f"corenova:{SECRET}\n"), lambda h: 200)
    result = prodcheck.CHECK_FNS["health_external"](ctx)
    assert result.passed
    assert scripts == []  # authenticated SSM access, no public Basic auth


def test_health_admin_auth_missing_credentials_file(fast_poll):
    ctx, _ = admin_ctx(lambda s: inv(code=1, error="No such file"), lambda h: 401)
    result = prodcheck.CHECK_FNS["health_external"](ctx)
    assert not result.passed
    assert "401" in result.detail
    assert ctx.secret == ""


def test_health_loopback_branch(fast_poll):
    plan_ = make_plan({"AdminAuthEnabled": "false", "HealthCheckPath": "/health"},
                      ["health_external"])
    seen: list[str] = []

    def run_script(script: str) -> Invocation:
        seen.append(script)
        return inv("code=200\n")

    urls = []
    def http_get(url, headers):
        urls.append(url)
        return 200, {}, b""
    ctx = CheckCtx(plan=plan_, instance_id="i-1", public_dns="", container="ghost",
                   image_ref="img", data_path="/data", run_script=run_script,
                   http_get=http_get, tunnel_url="http://127.0.0.1:8080", session_id="test-session")
    result = prodcheck.CHECK_FNS["health_external"](ctx)
    assert result.passed
    assert urls == ["http://127.0.0.1:8080/health"]
    assert seen == []  # instance-side curl is never actual access evidence


def test_health_loopback_502_fails(fast_poll):
    plan_ = make_plan({"AdminAuthEnabled": "false", "HealthCheckPath": "/"}, ["health_external"])
    ctx = CheckCtx(plan=plan_, instance_id="i-1", public_dns="", container="ghost",
                   image_ref="img", data_path="/data", run_script=lambda s: inv("code=502\n"),
                   http_get=lambda u, h: (502, {}, b""),
                   tunnel_url="http://127.0.0.1:8080", session_id="test-session")
    result = prodcheck.CHECK_FNS["health_external"](ctx)
    assert not result.passed and "502" in result.detail


def test_data_dir_write_script_and_pass(fast_poll):
    plan_ = make_plan({}, ["data_dir_write"])
    seen: list[str] = []

    def run_script(script: str) -> Invocation:
        seen.append(script)
        return inv("write-ok\n")

    ctx = CheckCtx(plan=plan_, instance_id="i-1", public_dns="", container="ghost",
                   image_ref=f"ghost:6.61.0-alpine@{DIGEST}", data_path="/var/lib/ghost/content",
                   run_script=run_script)
    result = prodcheck.CHECK_FNS["data_dir_write"](ctx)
    assert result.passed
    script = seen[0]
    # 必须以镜像自身用户执行（root 写永远成功，测不出 bind mount 权限）
    assert "{{.Config.User}}" in script
    assert 'docker exec -u "$uid_gid" ghost' in script
    assert "/var/lib/ghost/content/.corenova-probe" in script
    assert f"ghost:6.61.0-alpine@{DIGEST}" in script  # 钉扎镜像本体，不是浮动 tag


def test_data_dir_write_failure_detail(fast_poll):
    ctx = CheckCtx(plan=make_plan({}, ["data_dir_write"]), instance_id="i", public_dns="",
                   container="ghost", image_ref="img", data_path="/data",
                   run_script=lambda s: inv("touch: cannot touch ... Permission denied", code=1))
    result = prodcheck.CHECK_FNS["data_dir_write"](ctx)
    assert not result.passed
    assert "Permission denied" in result.detail


def test_url_injection_trailing_slash_normalized():
    dns = "ec2-1-2.compute-1.amazonaws.com"
    env_out = (
        "url=http://localhost:8080\n"
        "PUBLIC_HOOK=http://localhost:8080/hook\n"
        "OTHER=whatever\n"
    )
    ctx = CheckCtx(plan=make_plan({}, ["url_injection"]), instance_id="i", public_dns=dns,
                   container="ghost", image_ref="img", data_path="/data",
                   url_env_names=["url", "PUBLIC_HOOK"], run_script=lambda s: inv(env_out))
    result = prodcheck.CHECK_FNS["url_injection"](ctx)
    assert result.passed, result.detail


def test_url_injection_wrong_value_fails():
    ctx = CheckCtx(plan=make_plan({}, ["url_injection"]), instance_id="i",
                   public_dns="ec2-x.compute-1.amazonaws.com", container="ghost",
                   image_ref="img", data_path="/data", url_env_names=["url"],
                   run_script=lambda s: inv("url=http://localhost:2368\n"))
    result = prodcheck.CHECK_FNS["url_injection"](ctx)
    assert not result.passed
    assert "url=" in result.detail  # 指出哪个变量不符


def test_url_injection_needs_declared_names():
    ctx = CheckCtx(plan=make_plan({}, ["url_injection"]), instance_id="i", public_dns="d",
                   container="ghost", image_ref="img", data_path="/data",
                   run_script=lambda s: inv(""))
    assert not prodcheck.CHECK_FNS["url_injection"](ctx).passed


def test_host_metrics():
    ok = CheckCtx(plan=make_plan({}, ["host_metrics"]), instance_id="i", public_dns="",
                  container="netdata", image_ref="img", data_path="/data",
                  run_script=lambda s: inv("metrics-ok\n"))
    assert prodcheck.CHECK_FNS["host_metrics"](ok).passed
    bad = CheckCtx(plan=make_plan({}, ["host_metrics"]), instance_id="i", public_dns="",
                   container="netdata", image_ref="img", data_path="/data",
                   run_script=lambda s: inv("", code=1, error="exit 1"))
    assert not prodcheck.CHECK_FNS["host_metrics"](bad).passed


def test_run_checks_isolates_probe_crash(fast_poll, monkeypatch):
    def boom(_ctx):
        raise RuntimeError("SSM 超时")

    plan_ = make_plan({"AdminAuthEnabled": "false", "HealthCheckPath": "/"},
                      ["health_external", "data_dir_write"])
    ctx = CheckCtx(plan=plan_, instance_id="i", public_dns="", container="c", image_ref="i",
                   data_path="/data", run_script=lambda s: inv("write-ok"))
    monkeypatch.setattr(prodcheck, "CHECK_FNS", {
        "health_external": boom,
        "data_dir_write": prodcheck.CHECK_FNS["data_dir_write"],
    })
    results = prodcheck.run_checks(ctx)
    assert results[0].passed is False and "SSM 超时" in results[0].detail
    assert results[1].passed  # 单项异常不中断其余测量


# --------------------------------------------------------------------------- hold 文本手术


# 冻结 fixture：解除暂停是生产核对的正常结果，活跃 apps/*.yaml 迟早不再有 hold；
# 拿真实注册表文件当 strip_hold 的输入等于写一个会在成功那天变红的测试。
HOLD_SOURCE = (REPO_ROOT / "tests" / "fixtures" / "held-code-server.yaml").read_text(encoding="utf-8")


def test_strip_hold_on_real_app_file():
    lines = HOLD_SOURCE.splitlines(keepends=True)
    hold_i = next(i for i, line in enumerate(lines) if line.rstrip("\n") == "  hold:")
    start = hold_i - 2  # 两行"运维性部署暂停"说明注释
    end = hold_i + 4    # hold: + reason: + en + zh
    assert lines[start].lstrip().startswith("#") and "运维性部署暂停" in lines[start]
    assert "hold:" in "".join(lines[hold_i:end])

    result = holds.strip_hold(HOLD_SOURCE)
    assert result == "".join(lines[:start] + lines[end:])  # 其余字节原样
    data = yaml.safe_load(result)
    assert "hold" not in (data.get("deployment") or {})
    # 核对门禁的输入不能被顺手删掉
    assert data["deployment"]["production_contract"]["checks"]


def test_strip_hold_idempotent_and_noop():
    stripped = holds.strip_hold(HOLD_SOURCE)
    assert holds.strip_hold(stripped) == stripped


def test_strip_hold_rejects_malformed_structure():
    with pytest.raises(ValueError, match="2 处"):
        holds.strip_hold(HOLD_SOURCE + "x: 1\ndeployment:\n  hold:\n    reason: {}\n")
    with pytest.raises(ValueError, match="归属"):
        holds.strip_hold("other:\n  hold:\n    reason:\n      en: x\n")


def test_strip_hold_file(tmp_path):
    f = tmp_path / "app.yaml"
    f.write_text(HOLD_SOURCE, encoding="utf-8")
    assert holds.strip_hold_file(f) is True
    assert "hold:" not in f.read_text(encoding="utf-8")
    assert holds.strip_hold_file(f) is False  # 幂等：无改动不重写


# --------------------------------------------------------------------------- Manifest 投影


def _build_with(tmp_path, mutate=None):
    from tests.test_publish_two_phase import Cfg, _build_inputs

    make_spec_, resolved, image, platform, outcome = _build_inputs(tmp_path)
    spec = make_spec_(tmp_path, mutate)
    return mf.build(spec, tmp_path, resolved, image, platform, outcome, Cfg(), "local-1")


def test_projection_includes_production_contract(tmp_path):
    m = _build_with(tmp_path, lambda d: d["deployment"].__setitem__(
        "production_contract", {"checks": ["admin_auth", "data_dir_write"]}))
    assert m["website"]["deploy"]["production_contract"] == {
        "checks": ["admin_auth", "data_dir_write"]}
    mf.assert_projection(m)  # 合法形状不得被断言拒绝


def test_projection_omits_key_without_declaration(tmp_path):
    m = _build_with(tmp_path)
    assert "production_contract" not in m["website"]["deploy"]
    mf.assert_projection(m)


@pytest.mark.parametrize("pc", [
    {"checks": []},
    {"checks": ["data_dir_write"], "extra": 1},
    {"checks": [""]},
    ["data_dir_write"],
], ids=["empty-list", "extra-key", "blank-entry", "not-a-dict"])
def test_assert_projection_rejects_malformed(tmp_path, pc):
    # 用完整投影（含 config 段）作底，再注入非法形状——sample_manifest 缺 config
    # 会让 assert_projection 在更早的 template_revision 断言处 KeyError，测不到这里。
    m = _build_with(tmp_path)
    m["website"]["deploy"]["production_contract"] = pc
    with pytest.raises(AssertionError, match="production_contract"):
        mf.assert_projection(m)


@pytest.mark.parametrize("url", ["http://public.example/", "https://public.example/",
                                 "http://127.0.0.1:9999/", "http://127.0.0.1:8080.evil/"])
def test_credentials_never_leave_established_tunnel(url):
    ctx, _ = admin_ctx(lambda s: inv(""), lambda h: pytest.fail("HTTP must not be called"))
    with pytest.raises(ValueError, match="credentials"):
        prodcheck._http_status(ctx, url, {"Authorization": "Basic secret"})


def test_instance_curl_alone_never_counts_as_access(fast_poll):
    ctx = CheckCtx(plan=make_plan({"HealthCheckPath": "/"}, ["health_external"]),
                   instance_id="i-1", public_dns="public.example", container="app",
                   image_ref="app", data_path="/data",
                   run_script=lambda s: inv("code=200\n"))
    assert not prodcheck._check_health_external(ctx).passed


def test_auth_request_uses_only_ssm_loopback(fast_poll):
    ctx, _ = admin_ctx(lambda s: inv(f"corenova:{SECRET}\n"), lambda h: 200)
    seen = []
    def http_get(url, headers):
        seen.append((url, headers))
        return (200 if headers else 401), {}, b""
    ctx.http_get = http_get
    assert prodcheck._check_health_external(ctx).passed
    assert len(seen) == 2
    assert all(url == "http://127.0.0.1:8080/" for url, _ in seen)
    assert "Authorization" in seen[-1][1]


def test_auth_redirect_not_followed():
    assert prodcheck._NoRedirect().redirect_request(None, None, 302, "", {}, "http://evil/") is None


@pytest.mark.parametrize("open_port", [None, 80, 443])
def test_public_access_probe_is_credential_free_tcp(monkeypatch, open_port):
    from contextlib import nullcontext
    calls = []
    monkeypatch.setattr(prodcheck.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("8.8.8.8", 80))])
    def connect(address, timeout):
        calls.append(address)
        if address[1] == open_port:
            return nullcontext()
        raise TimeoutError("blocked by security group")
    monkeypatch.setattr(prodcheck.socket, "create_connection", connect)
    result = prodcheck.public_access_denied("ec2.example")
    assert result.passed is (open_port is None)
    assert calls
    assert all(len(address) == 2 for address in calls)  # no HTTP/auth payload is sent


def test_public_dns_failure_is_not_denial_evidence(monkeypatch):
    def fail(*a, **k):
        raise prodcheck.socket.gaierror("DNS failed")
    monkeypatch.setattr(prodcheck.socket, "getaddrinfo", fail)
    assert not prodcheck.public_access_denied("missing.example").passed


def test_aws_errors_are_not_cleanup_evidence():
    def denied(**kw):
        raise RuntimeError("AccessDenied")
    aws = SimpleNamespace(cfn=SimpleNamespace(describe_stacks=denied),
                          ec2=SimpleNamespace(describe_instances=denied))
    with pytest.raises(RuntimeError, match="AccessDenied"):
        prodcheck._stack_status(aws, "stack")
    with pytest.raises(RuntimeError, match="AccessDenied"):
        prodcheck._instance_running(aws, "i-1")


def test_ssm_tunnel_uses_real_document_and_closes_session(monkeypatch):
    from contextlib import nullcontext
    calls = []
    class Socket:
        def bind(self, address):
            assert address == ("127.0.0.1", 8080)
    class Process:
        def poll(self):
            return None
        def terminate(self):
            calls.append("terminate-plugin")
        def wait(self, timeout):
            calls.append("wait-plugin")
    def start(**kw):
        calls.append(kw)
        return {"SessionId": "ssm-session", "TokenValue": "secret-token", "StreamUrl": "wss://ssm"}
    def popen(argv, **kw):
        assert argv[0] == "session-manager-plugin"
        assert kw["stdout"] == prodcheck.subprocess.DEVNULL
        assert kw["stderr"] == prodcheck.subprocess.DEVNULL
        return Process()
    monkeypatch.setattr(prodcheck.socket, "socket", lambda: nullcontext(Socket()))
    monkeypatch.setattr(prodcheck.socket, "create_connection", lambda *a, **k: nullcontext())
    monkeypatch.setattr(prodcheck.subprocess, "Popen", popen)
    aws = SimpleNamespace(cfg=SimpleNamespace(region="us-east-1"), ssm=SimpleNamespace(
        start_session=start, terminate_session=lambda **kw: calls.append(kw)))
    with prodcheck.ssm_tunnel(aws, "i-123") as (url, session):
        assert url == "http://127.0.0.1:8080" and session == "ssm-session"
    assert calls[0]["DocumentName"] == "AWS-StartPortForwardingSession"
    assert calls[0]["Parameters"] == {"portNumber": ["80"], "localPortNumber": ["8080"]}
    assert calls[-1] == {"SessionId": "ssm-session"}


def test_cfn_template_fetch_race_fails_closed():
    p = make_plan({}, [])
    p.template_body = "expected public bytes"
    aws = SimpleNamespace(cfn=SimpleNamespace(get_template=lambda **kw: {"TemplateBody": "drifted bytes"}))
    assert not prodcheck.deployed_template_matches(aws, p)


def test_abort_record_names_refused_call_without_body():
    from botocore.exceptions import ClientError

    exc = ClientError({"Error": {"Code": "AccessDenied",
                                 "Message": "not allowed, token=abc123"}}, "GetTemplate")
    detail = prodcheck._api_error(exc)
    assert detail == "ClientError(GetTemplate=AccessDenied)"
    assert "abc123" not in detail
    assert prodcheck._api_error(RuntimeError("boom")) == "RuntimeError"


def test_volume_delete_must_be_confirmed_and_scoped(fast_poll):
    calls = []
    volume = {"VolumeId": "vol-1", "State": "available", "Tags": [
        {"Key": "corenova:prodcheck", "Value": "true"},
        {"Key": "corenova:prodcheck-stack", "Value": "our-stack"}]}
    def describe(**kw):
        calls.append(kw)
        return {"Volumes": [volume]}
    aws = SimpleNamespace(ec2=SimpleNamespace(describe_volumes=describe, delete_volume=lambda **kw: None))
    notes = []
    assert not prodcheck._cleanup_volumes(aws, ["vol-1"], notes, "our-stack")
    assert {"Name": "tag:corenova:prodcheck-stack", "Values": ["our-stack"]} in calls[0]["Filters"]
    assert any("未确认" in note for note in notes)
