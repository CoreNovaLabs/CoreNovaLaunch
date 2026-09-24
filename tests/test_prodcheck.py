"""L1.5 生产核对离线单测（deployment-contract.md §2.6）。

零 AWS 调用：plan 是纯函数；探针全部经 CheckCtx.run_script/http_get 喂假响应，
poll_until monkeypatch 成"执行一次即定论"；hold 手术与 Manifest 投影只碰文本。
密码脱敏是安全边界，专设断言。
"""

from __future__ import annotations

import builtins
import io
import json
import os
import shlex
import subprocess
import sys
import uuid
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

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


@pytest.mark.parametrize("name", ["cyberchef", "drawio", "it-tools"])
def test_registered_none_manifest_to_plan(tmp_path, name):
    from corenova import appspec
    from tests.test_publish_two_phase import Cfg, _build_inputs

    spec = appspec.load(name, REPO_ROOT)
    assert appspec.validate(spec, REPO_ROOT, "us-east-1") == []
    # 三应用尚未声明生产核对清单；仅在内存中添加以测试参数投影。
    spec.data["deployment"]["production_contract"] = {"checks": ["admin_auth"]}
    _, resolved, image, platform, outcome = _build_inputs(tmp_path)
    image.digest = DIGEST
    m = mf.build(spec, REPO_ROOT, resolved, image, platform, outcome, Cfg(), "local-none")
    p = prodcheck.plan(make_cfg(tmp_path), spec, m, template_url="https://tpl")
    assert p.parameters["PersistenceMode"] == "none"
    assert p.parameters["DataVolumeSize"] == "0"
    assert p.parameters["DataContainerPath"] == ""
    assert p.parameters["InstanceType"] == spec.resources()[0]
    assert m["website"]["deploy"]["data_volume_gb"] == 0
    assert "data_path" not in m["website"]["deploy"]
    assert p.checks == ["health_external"]
    assert [key for key, value in p.parameters.items() if value == ""] == ["DataContainerPath"]


def none_plan_inputs():
    spec = make_spec(["admin_auth"])
    spec.data["app"] = {"app_type": "stateless_web"}
    spec.data["deployment"]["persistence"] = "none"
    m = make_manifest()
    deploy = m["website"]["deploy"]
    deploy.update(persistence="none", data_volume_gb=0)
    del deploy["data_path"]
    return spec, m


@pytest.mark.parametrize("field,value", [
    ("data_path", "/data"), ("data_path", ""), ("data_path", None),
    ("data_volume_gb", 20), ("data_volume_gb", None),
    ("data_volume_gb", False), ("data_volume_gb", "0"),
    ("volumes", []), ("production_contract", {"checks": ["data_dir_write"]}),
    ("persistence", "volume"), ("persistence", "unknown"),
    ("persistence", None), ("persistence", []),
])
def test_plan_none_rejects_conflicting_projection(tmp_path, field, value):
    spec, m = none_plan_inputs()
    m["website"]["deploy"][field] = value
    with pytest.raises(ValueError, match="persistence"):
        prodcheck.plan(make_cfg(tmp_path), spec, m, template_url="https://tpl")


@pytest.mark.parametrize("field,value", [
    ("data_path", "/data"), ("data_path", ""), ("data_path", None),
    ("volumes", []), ("production_contract", {"checks": ["data_dir_write"]}),
    ("persistence", "volume"), ("persistence", "unknown"), ("persistence", None),
])
def test_plan_none_rejects_conflicting_spec(tmp_path, field, value):
    spec, m = none_plan_inputs()
    spec.data["deployment"][field] = value
    with pytest.raises(ValueError, match="persistence"):
        prodcheck.plan(make_cfg(tmp_path), spec, m, template_url="https://tpl")


def test_plan_none_rejects_wrong_type_or_missing_projection(tmp_path):
    spec, m = none_plan_inputs()
    spec.data["app"]["app_type"] = "stateful_app"
    with pytest.raises(ValueError, match="stateless_web"):
        prodcheck.plan(make_cfg(tmp_path), spec, m, template_url="https://tpl")
    spec.data["app"]["app_type"] = "stateless_web"
    del m["website"]["deploy"]["persistence"]
    with pytest.raises(ValueError, match="不一致"):
        prodcheck.plan(make_cfg(tmp_path), spec, m, template_url="https://tpl")


@pytest.mark.parametrize("explicit", [False, True])
def test_plan_volume_keeps_existing_parameters(tmp_path, explicit):
    spec, m = make_spec(["admin_auth"]), make_manifest()
    if explicit:
        spec.data["deployment"].update(persistence="volume", data_path="/var/lib/ghost/content")
        m["website"]["deploy"]["persistence"] = "volume"
    q = prodcheck.plan(make_cfg(tmp_path), spec, m, template_url="https://tpl").parameters
    assert q["DataVolumeSize"] == "30"
    assert q["DataContainerPath"] == "/var/lib/ghost/content"
    if explicit:
        assert q["PersistenceMode"] == "volume"
    else:
        assert "PersistenceMode" not in q
        del m["website"]["deploy"]["data_path"]
        q = prodcheck.plan(make_cfg(tmp_path), spec, m, template_url="https://tpl").parameters
        assert q["DataContainerPath"] == "/data"
        assert q["DataVolumeSize"] == "30"


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


def _redirect_ctx(http_get, health_path="/"):
    plan_ = make_plan({"AdminAuthEnabled": "false", "HealthCheckPath": health_path},
                      ["health_external"])
    ctx = CheckCtx(plan=plan_, instance_id="i-1", public_dns="ec2-1-2.compute-1.amazonaws.com",
                   container="app", image_ref="app", data_path="/data",
                   run_script=lambda script: inv(""), http_get=http_get,
                   tunnel_url="http://127.0.0.1:8080", session_id="test-session")
    return ctx


def test_health_follows_redirects_that_stay_in_the_tunnel(fast_poll):
    # code-server answers / with 302 -> /login. The container stage has always let urllib chase
    # that, so L1.5 must not be stricter about the same declared endpoint.
    routes = {"http://127.0.0.1:8080/": (302, {"location": "/login"}, b""),
              "http://127.0.0.1:8080/login": (200, {}, b"<html>")}
    result = prodcheck.CHECK_FNS["health_external"](
        _redirect_ctx(lambda url, h: routes[url]))
    assert result.passed, result.detail
    assert "HTTP=200 -> /login" in result.detail  # evidence says which hop answered


def test_health_never_chases_a_redirect_out_of_the_tunnel(fast_poll):
    calls = []

    def http_get(url, headers):
        calls.append(url)
        return 302, {"location": "https://public.example/login"}, b""

    result = prodcheck.CHECK_FNS["health_external"](_redirect_ctx(http_get))
    assert not result.passed
    assert "HTTP=302 (refused hop to public.example)" in result.detail
    assert set(calls) == {"http://127.0.0.1:8080/"}  # refused at the origin, nothing fetched off-tunnel


def test_tunnel_http_get_keeps_the_location_of_a_refused_redirect(monkeypatch):
    # _NoRedirect turns a 3xx into HTTPError. If that branch swallowed its headers, the hop loop
    # in _followed_status would see a redirect with no target and every app that redirects at the
    # root would go red again -- and no fake http_get would notice, because they bypass this helper.
    err = prodcheck.urllib.error.HTTPError(
        "http://127.0.0.1:8080/", 302, "Found", {"location": "/login"}, None)

    class Opener:
        def open(self, req, timeout=None):
            raise err

    monkeypatch.setattr(prodcheck.urllib.request, "build_opener", lambda *a: Opener())
    assert prodcheck.tunnel_http_get("http://127.0.0.1:8080/", {}) == (302, {"location": "/login"}, b"")


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
    argv = shlex.split(script)
    assert argv[:2] == ["python3", "-c"]
    assert argv[3:] == ["ghost", f"ghost:6.61.0-alpine@{DIGEST}", "/var/lib/ghost/content"]
    # 从真实进程取身份，不依赖镜像内 shell/env 或 Config.User。
    source = argv[2]
    assert 'proc / "status"' in source
    assert "os.setgroups(" in source and "os.setgid(" in source and "os.setuid(" in source
    assert '".corenova-probe-" + uuid.uuid4().hex' in source
    assert "os.O_EXCL" in source and "os.O_NOFOLLOW" in source
    assert "docker exec" not in script and "sh -c" not in script
    assert "Config.User" not in script


def test_data_dir_write_failure_detail(fast_poll):
    ctx = CheckCtx(plan=make_plan({}, ["data_dir_write"]), instance_id="i", public_dns="",
                   container="ghost", image_ref="img", data_path="/data",
                   run_script=lambda s: inv("touch: cannot touch ... Permission denied", code=1))
    result = prodcheck.CHECK_FNS["data_dir_write"](ctx)
    assert not result.passed
    assert "Permission denied" in result.detail


def test_url_injection_trailing_slash_normalized():
    dns = "ec2-1-2.compute-1.amazonaws.com"
    env_out = json.dumps({"url": True, "PUBLIC_HOOK": True})
    ctx = CheckCtx(plan=make_plan({}, ["url_injection"]), instance_id="i", public_dns=dns,
                   container="ghost", image_ref="img", data_path="/data",
                   url_env_names=["url", "PUBLIC_HOOK"], run_script=lambda s: inv(env_out))
    result = prodcheck.CHECK_FNS["url_injection"](ctx)
    assert result.passed, result.detail


def test_url_injection_wrong_value_fails():
    ctx = CheckCtx(plan=make_plan({}, ["url_injection"]), instance_id="i",
                   public_dns="ec2-x.compute-1.amazonaws.com", container="ghost",
                   image_ref="img", data_path="/data", url_env_names=["url"],
                   run_script=lambda s: inv(json.dumps({"url": False})))
    result = prodcheck.CHECK_FNS["url_injection"](ctx)
    assert not result.passed
    assert "url=" in result.detail  # 指出哪个变量不符


def test_url_injection_needs_declared_names():
    ctx = CheckCtx(plan=make_plan({}, ["url_injection"]), instance_id="i", public_dns="d",
                   container="ghost", image_ref="img", data_path="/data",
                   run_script=lambda s: inv(""))
    assert not prodcheck.CHECK_FNS["url_injection"](ctx).passed


def _execute_host_probe(command, modules):
    """执行真实 -c 内容；导入白名单避免碰宿主 Docker、/proc 或身份。"""
    argv = shlex.split(command)
    assert argv[:2] == ["python3", "-c"]
    assert "docker exec" not in command and "sh -c" not in command
    imports = {"json": json, "sys": SimpleNamespace(argv=["-c", *argv[3:]], exit=sys.exit),
               **modules}

    def sandbox_import(name, *args, **kwargs):
        assert name in imports, f"unexpected script import: {name}"
        return imports[name]

    stdout = io.StringIO()
    code, error = 0, ""
    with redirect_stdout(stdout):
        try:
            exec(compile(argv[2], "<host-probe>", "exec"),
                 {"__builtins__": {**vars(builtins), "__import__": sandbox_import}})
        except SystemExit as exc:
            code = exc.code
        except (RuntimeError, OSError) as exc:
            code, error = 1, str(exc)
    return inv(stdout.getvalue(), code=code, error=error)


@pytest.fixture
def write_probe_sandbox(tmp_path):
    source = tmp_path / "bind source"
    source.mkdir()
    proc = tmp_path / "proc"
    proc.mkdir()
    identity_map = "         0          0 4294967295\n"
    for name in ("uid_map", "gid_map"):
        (proc / name).write_text(identity_map)
    # Config.User 故意与真实进程不同，且附加组不能丢失。
    (proc / "status").write_text(
        "Name:\tapp\nUid:\t1001\t1001\t1001\t1001\n"
        "Gid:\t1002\t1002\t1002\t1002\nGroups:\t1002 44 55\n")
    info = {"Image": DIGEST, "State": {"Running": True, "Pid": 321},
            "Config": {"User": "0:0"},
            "Mounts": [{"Destination": "/data", "Source": str(source),
                        "Type": "bind", "RW": True}]}
    calls, events, opened, streams, outputs = [], [], [], [], []
    image_ref = f"ghost:6.61.0-alpine@{DIGEST}"

    def check_output(argv, **kwargs):
        calls.append(argv)
        if argv == ["docker", "inspect", "ghost"]:
            assert kwargs == {}
            return json.dumps([info]).encode()
        assert argv == ["docker", "image", "inspect", "--format", "{{.Id}}", image_ref]
        assert kwargs == {"text": True}
        return DIGEST + "\n"

    def sandbox_path(path):
        # 不允许读取真实 /proc；其余唯一路径是测试创建的 bind source。
        if path == "/proc/321":
            return proc
        assert path == str(source)
        return source

    def open_marker(path, flags, mode):
        assert events == [("setgroups", [1002, 44, 55]), ("setgid", 1002), ("setuid", 1001)]
        assert path.parent == source
        assert path.name.startswith(".corenova-probe-")
        assert uuid.UUID(hex=path.name.removeprefix(".corenova-probe-")).version == 4
        assert flags == os.O_CREAT | os.O_EXCL | os.O_RDWR | os.O_NOFOLLOW
        assert mode == 0o600
        opened.append(path)
        return os.open(path, flags, mode)

    @contextmanager
    def fdopen(fd, mode):
        assert mode == "w+b"
        with os.fdopen(fd, mode) as stream:
            wrapped = Mock(wraps=stream)
            streams.append(wrapped)
            yield wrapped
            # 使用独立文件描述符确认落盘内容，不仅仅检查 mock 调用。
            assert opened[-1].read_bytes() == b"corenova-write-probe"

    fake_os = SimpleNamespace(
        setgroups=lambda groups: events.append(("setgroups", groups)),
        setgid=lambda gid: events.append(("setgid", gid)),
        setuid=lambda uid: events.append(("setuid", uid)),
        open=open_marker, fdopen=fdopen,
        O_CREAT=os.O_CREAT, O_EXCL=os.O_EXCL, O_RDWR=os.O_RDWR, O_NOFOLLOW=os.O_NOFOLLOW,
    )
    modules = {"os": fake_os, "pathlib": SimpleNamespace(Path=sandbox_path),
               "subprocess": SimpleNamespace(check_output=check_output), "uuid": uuid}

    def run_script(command):
        result = _execute_host_probe(command, modules)
        outputs.append(result)
        return result

    ctx = CheckCtx(plan=make_plan({}, ["data_dir_write"]), instance_id="i", public_dns="",
                   container="ghost", image_ref=image_ref, data_path="/data", run_script=run_script)
    return SimpleNamespace(ctx=ctx, info=info, source=source, proc=proc, events=events,
                           opened=opened, streams=streams, calls=calls, outputs=outputs)


def test_data_dir_write_real_script_identity_and_file_lifecycle(write_probe_sandbox):
    sandbox = write_probe_sandbox
    result = prodcheck.CHECK_FNS["data_dir_write"](sandbox.ctx)
    assert result.passed, result.detail
    assert sandbox.events == [("setgroups", [1002, 44, 55]), ("setgid", 1002), ("setuid", 1001)]
    assert len(sandbox.calls) == 2  # 严格 mock 只允许两次 inspect，绝无 exec/sh/env。
    assert len(sandbox.opened) == 1
    assert list(sandbox.source.iterdir()) == []
    assert not sandbox.opened[0].exists()
    stream = sandbox.streams[0]
    assert [call[0] for call in stream.mock_calls] == ["write", "flush", "seek", "read"]
    stream.write.assert_called_once_with(b"corenova-write-probe")
    stream.seek.assert_called_once_with(0)
    stream.read.assert_called_once_with()
    assert sandbox.outputs[0].stdout == "write-ok\n"


@pytest.mark.parametrize("fault, message", [
    ("image", "image or running state mismatch"),
    ("stopped", "image or running state mismatch"),
    ("readonly", "writable data bind mount"),
    ("nonbind", "writable data bind mount"),
    ("missingmount", "writable data bind mount"),
    ("destination-prefix", "writable data bind mount"),
    ("duplicate-mount", "writable data bind mount"),
    ("pid", "invalid container pid"),
    ("uid_map", "namespace mapping"),
    ("gid_map", "namespace mapping"),
    ("mixeduids", "mixed process credentials"),
    ("mixedgids", "mixed process credentials"),
])
def test_data_dir_write_real_script_fails_closed(write_probe_sandbox, fault, message):
    sandbox = write_probe_sandbox
    info = sandbox.info
    if fault == "image":
        info["Image"] = "sha256:" + "b" * 64
    elif fault == "stopped":
        info["State"]["Running"] = False
    elif fault == "readonly":
        info["Mounts"][0]["RW"] = False
    elif fault == "nonbind":
        info["Mounts"][0]["Type"] = "volume"
    elif fault == "missingmount":
        info["Mounts"] = []
    elif fault == "destination-prefix":
        info["Mounts"][0]["Destination"] = "/data/child"
    elif fault == "duplicate-mount":
        info["Mounts"] *= 2
    elif fault == "pid":
        info["State"]["Pid"] = 0
    elif fault in ("uid_map", "gid_map"):
        (sandbox.proc / fault).write_text("0 100000 65536\n")
    else:
        status = sandbox.proc / "status"
        old = "1001\t1001\t1001\t1001" if fault == "mixeduids" else "1002\t1002\t1002\t1002"
        status.write_text(status.read_text().replace(old, "0\t1001\t1001\t1001"))
    result = prodcheck.CHECK_FNS["data_dir_write"](sandbox.ctx)
    assert not result.passed
    assert message in result.detail
    assert sandbox.outputs[0].exit_code != 0
    assert sandbox.events == [] and sandbox.opened == []
    assert list(sandbox.source.iterdir()) == []


@pytest.mark.parametrize("value, allowed", [
    ("http://localhost:8080", True),
    ("http://localhost:8080/", True),
    ("http://localhost:8080/hook/nested/", True),
    ("http://localhost:8080/hook?token=" + SECRET, True),
    ("http://localhost:2368/" + SECRET, False),
    ("https://localhost:8080/" + SECRET, False),
    ("http://localhost:8080.evil/" + SECRET, False),
    ("http://localhost:8080@evil/" + SECRET, False),
    ("http://localhost:8080?token=" + SECRET, False),
    (None, False),
])
def test_url_injection_real_script_boolean_only(value, allowed):
    calls, outputs = [], []
    environment = ["OTHER_SECRET=unrelated-secret-value",
                   "PUBLIC_HOOK=http://localhost:8080/hook"]
    if value is not None:
        environment.append("url=" + value)

    def check_output(argv, **kwargs):
        calls.append(argv)
        assert argv == ["docker", "inspect", "--format", "{{json .Config.Env}}", "ghost"]
        assert kwargs == {}
        return json.dumps(environment).encode()

    def run_script(command):
        result = _execute_host_probe(command, {"subprocess": SimpleNamespace(check_output=check_output)})
        outputs.append(result)
        return result

    ctx = CheckCtx(plan=make_plan({}, ["url_injection"]), instance_id="i", public_dns="",
                   container="ghost", image_ref="img", data_path="/data",
                   url_env_names=["url", "PUBLIC_HOOK"], run_script=run_script)
    result = prodcheck.CHECK_FNS["url_injection"](ctx)
    assert result.passed is allowed
    assert len(calls) == 1
    assert json.loads(outputs[0].stdout) == {"url": allowed, "PUBLIC_HOOK": True}
    evidence = outputs[0].stdout + repr(result)
    for private in (SECRET, "OTHER_SECRET", "unrelated-secret-value", "http://localhost"):
        assert private not in evidence
    if not allowed:
        assert "url=不符或缺失" in result.detail


@pytest.mark.parametrize("failure", ["exec", "malformed-env"])
def test_url_injection_real_script_error_does_not_leak(failure):
    outputs = []

    def check_output(argv, **kwargs):
        assert argv == ["docker", "inspect", "--format", "{{json .Config.Env}}", "ghost"]
        if failure == "exec":
            raise subprocess.CalledProcessError(1, argv, output=SECRET, stderr="OTHER_SECRET=" + SECRET)
        return ("malformed OTHER_SECRET=" + SECRET).encode()

    def run_script(command):
        result = _execute_host_probe(command, {"subprocess": SimpleNamespace(check_output=check_output)})
        outputs.append(result)
        return result

    ctx = CheckCtx(plan=make_plan({}, ["url_injection"]), instance_id="i", public_dns="",
                   container="ghost", image_ref="img", data_path="/data",
                   url_env_names=["url"], run_script=run_script)
    result = prodcheck.CHECK_FNS["url_injection"](ctx)
    assert not result.passed
    assert outputs[0].exit_code == 1
    assert outputs[0].stdout.strip() == ("CalledProcessError" if failure == "exec" else "JSONDecodeError")
    assert SECRET not in outputs[0].stdout + repr(result)
    assert "OTHER_SECRET" not in outputs[0].stdout + repr(result)


@pytest.mark.parametrize("stdout, code", [
    ('{"url": true}', 1),
    ("url=http://localhost:8080?token=" + SECRET, 0),
    ("not-json " + SECRET, 0),
    ("[]", 0),
    ("null", 0),
    ("{}", 0),
    ('{"url": "true"}', 0),
    ('{"url": 1}', 0),
    ('{"url": null}', 0),
    (json.dumps({"url": "http://wrong.example/" + SECRET, "OTHER_SECRET": SECRET}), 0),
])
def test_url_injection_rejects_failed_or_malformed_results(stdout, code):
    ctx = CheckCtx(plan=make_plan({}, ["url_injection"]), instance_id="i", public_dns="",
                   container="ghost", image_ref="img", data_path="/data", url_env_names=["url"],
                   run_script=lambda s: inv(stdout, code=code, error="OTHER_SECRET=" + SECRET))
    result = prodcheck.CHECK_FNS["url_injection"](ctx)
    assert not result.passed
    assert SECRET not in repr(result) and "OTHER_SECRET" not in repr(result)


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
        prodcheck._followed_status(ctx, url, {"Authorization": "Basic secret"})


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


def _cfn(body):
    return SimpleNamespace(cfn=SimpleNamespace(get_template=lambda **kw: {"TemplateBody": body}))


def _plan(template_body: str) -> ProdPlan:
    p = make_plan({}, [])
    p.template_body = template_body
    return p


def test_cfn_template_fetch_race_fails_closed():
    drift = prodcheck.deployed_template_diff(_cfn("drifted bytes"), _plan("expected public bytes"))
    assert drift
    # The evidence has to name the disagreement: one byte of drift must not need another
    # billable stack run to diagnose. Both sides here are already-public template bytes.
    assert "@0" in drift and "expected" in drift and "drifted" in drift


def test_cfn_stores_non_ascii_as_question_mark_and_nothing_else_changes():
    # Measured 2026-09-23 against corenova-network: CFN keeps the submitted body verbatim
    # (6673 bytes both sides, same order, indent and comments) but writes each non-ASCII
    # character as '?' at the same offset. Without tolerating exactly that, template_match
    # can never pass for a template carrying Chinese prose.
    published = "Description: 契约 §9 — one-click\nResources: {}\n"
    stored = published.encode("ascii", "replace").decode("ascii")
    assert prodcheck.deployed_template_diff(_cfn(stored), _plan(published)) == ""
    # A same-offset swap that CFN *can* see still fails, so the tolerance buys formatting,
    # not silence: non-ASCII text is pinned by the §2.4 public-object SHA, not by this comparison.
    assert prodcheck.deployed_template_diff(_cfn(stored.replace("one-click", "one-kl1ck")),
                                           _plan(published))


def test_cfn_body_that_is_not_text_fails_closed():
    assert prodcheck.deployed_template_diff(_cfn({"AWSTemplateFormatVersion": "2010-09-09"}),
                                            _plan("x")).startswith("CFN returned dict")


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
