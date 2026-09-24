"""2026-09 代码审查修复的回归测试。

每个测试类对应一个具体修复：回滚任何一处修复，这里必须立刻变红。
- TestClassifyRouting       → failure.classify 的路由顺序（H2）
- TestThrottleWindow        → check_versions.up_to_date_within_window 符号与时区（M2）
- TestAgeDays              → versioning.age_days 的 UTC 口径（M1）
- TestAssertVersionContract → runtime.assert_version 与校验器规则12 的字段一致性（H1）
- TestAppspecMalformedShapes→ appspec.validate 对畸形形状报违规而不是崩溃（M5）
- TestIdSanitization        → sanitize_for_id / DirBackend 的路径穿越防线（M8）
- TestAiWhitelistNormalize  → analyze_failure._normalize 的前缀剥离（M10）
- TestResolveFailures       → failure.resolve_failures 成功关闭台账的匹配口径（§7）
- TestLogGoesToStderr       → util.log 走 stderr，不污染 --json 数据通道（8/30 事故）
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import socket
import time
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

from corenova import appspec  # noqa: E402
from corenova.appspec import AppSpec  # noqa: E402
from corenova.backend import DirBackend  # noqa: E402
from corenova.failure import classify  # noqa: E402
from corenova.runtime import assert_version  # noqa: E402
from corenova.util import sanitize_for_id  # noqa: E402
from corenova.versioning import age_days  # noqa: E402
from tests.test_schema_rules import make as make_spec  # noqa: E402


def _load_script(relpath: str, name: str):
    """scripts/ 下的入口脚本不是包（目录含连字符），按文件路径加载。"""
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cv = _load_script("scripts/monitor/check_versions.py", "check_versions_under_test")
af = _load_script("scripts/ai-test/analyze_failure.py", "analyze_failure_under_test")


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    from corenova import failure

    def no_network(*args, **kwargs):
        pytest.fail("回归测试禁止真实网络请求")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(failure, "_headers", lambda: {})


@pytest.fixture
def tz_offset():
    """强制切到非 UTC 时区，把 time.mktime/timegm 的差异放大成可断言的偏移。"""
    if not hasattr(time, "tzset"):
        pytest.skip("平台不支持 time.tzset")
    old = os.environ.get("TZ")
    os.environ["TZ"] = "Asia/Shanghai"  # UTC+8
    time.tzset()
    yield
    if old is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = old
    time.tzset()


def _utc_iso(seconds_ago: int) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - seconds_ago))


# ------------------------------------------------------------------ H2: classify 路由


class TestClassifyRouting:
    def test_transient_wins_over_stage(self):
        assert classify("DEPLOYING", "cfn", RuntimeError("Read timed out")) == "TRANSIENT"

    def test_infrastructure_stage_and_checks(self):
        assert classify("DEPLOYING", "cfn") == "INFRASTRUCTURE"
        assert classify("DEPLOYED", "") == "INFRASTRUCTURE"
        assert classify("VERIFYING", "ami") == "INFRASTRUCTURE"

    def test_platform_contract_is_infrastructure_not_resolved_default(self):
        # 修复前：stage=RESOLVED 时落到 RESOLVED 分支，被误判成 APPLICATION
        assert classify("RESOLVED", "required_platform_contract_valid", None) == "INFRASTRUCTURE"
        assert classify("RESOLVED", "required_platform_contract_valid",
                        ValueError("contract expired")) == "INFRASTRUCTURE"

    def test_app_schema_is_application_even_with_error(self):
        # 修复前：stage=RESOLVED 且 err 非瞬时 → MANUAL_REQUIRED；
        # schema 违规是确定性的应用层问题，应走 FIX_PR 而不是人工
        assert classify("RESOLVED", "app_schema", ValueError("rule 12 violated")) == "APPLICATION"

    def test_resolved_deterministic_error_is_manual(self):
        assert classify("RESOLVED", "", ValueError("registry file missing")) == "MANUAL_REQUIRED"

    def test_resolved_without_error_is_application(self):
        assert classify("RESOLVED") == "APPLICATION"

    def test_publishing_defaults_to_transient(self):
        assert classify("PUBLISHING", "publish_commit",
                        RuntimeError("weird unknown error")) == "TRANSIENT"

    def test_verify_stage_check_routing(self):
        assert classify("VERIFYING", "tests_passed") == "TEST"
        assert classify("VERIFYING", "screenshots_generated") == "TEST"
        assert classify("VERIFYING", "health_check_passed") == "APPLICATION"
        assert classify("VERIFYING", "container_healthy") == "APPLICATION"
        assert classify("VERIFYING", "compose_started") == "APPLICATION"

    def test_unknown_falls_to_manual(self):
        assert classify("SOME_STAGE", "unknown_check") == "MANUAL_REQUIRED"


# ------------------------------------------------------------------ M2: 监控节流阀


class TestThrottleWindow:
    """语义：最近一次 release 已超出窗口 → True（窗口内没有新东西，可跳过扇出）。"""

    def test_old_release_returns_true(self, tz_offset):
        assert cv.up_to_date_within_window({"published_at": _utc_iso(10 * 86400)}, hours=24) is True

    def test_fresh_release_returns_false(self, tz_offset):
        assert cv.up_to_date_within_window({"published_at": _utc_iso(3600)}, hours=24) is False

    def test_tz_sensitive_boundary(self, tz_offset):
        # 2 小时前的 release、5 小时窗口 → 仍在窗口内 → False。
        # 修复前 time.mktime 按本地时区解释 UTC 字符串，UTC+8 上把年龄虚增 8 小时，
        # 误判为"超出窗口" → True，导致新版本被跳过验证。
        assert cv.up_to_date_within_window({"published_at": _utc_iso(2 * 3600)}, hours=5) is False

    def test_missing_or_disabled(self):
        assert cv.up_to_date_within_window({}, hours=24) is False
        assert cv.up_to_date_within_window({"published_at": ""}, hours=24) is False
        assert cv.up_to_date_within_window({"published_at": "2026-01-01T00:00:00Z"}, hours=0) is False
        assert cv.up_to_date_within_window({"published_at": "not-a-date"}, hours=24) is False


# ------------------------------------------------------------------ M1: 契约年龄时区


class TestAgeDays:
    def test_age_computed_in_utc(self, tz_offset):
        two_days_ago = _utc_iso(2 * 86400)
        # 修复前 time.mktime 在 UTC+8 上会给出 ~1.67 天
        assert abs(age_days(two_days_ago) - 2.0) < 0.1

    def test_future_clamps_to_zero(self):
        assert age_days(_utc_iso(-3600)) == 0.0

    def test_malformed_is_huge(self):
        assert age_days("not-a-date") == 1e9
        assert age_days("") == 1e9


# ------------------------------------------------------------------ H1: assert_version 契约一致性


def _spec_with_assertion(va: dict) -> AppSpec:
    return AppSpec(name="demo", path=pathlib.Path("apps/demo.yaml"), raw="",
                   data={"health_check": {"version_assertion": va}})


class TestAssertVersionContract:
    def test_header_kind_reads_probe_headers_lowercase(self):
        spec = _spec_with_assertion(
            {"kind": "header", "name": "X-App-Version", "expected": "{version}"})
        res = assert_version("", spec, "v1.2.3",
                             probe_headers={"x-app-version": "v1.2.3"})
        assert res.configured and res.ok, res.detail

    def test_header_kind_without_probe_headers_fails_clearly(self):
        spec = _spec_with_assertion(
            {"kind": "header", "name": "X-App-Version", "expected": "{version}"})
        res = assert_version("", spec, "v1.2.3")
        assert res.configured and not res.ok
        assert "响应头" in res.detail

    def test_api_json_path_uses_path_field_with_base_url(self, monkeypatch):
        captured: dict = {}

        class FakeResp:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self):
                return json.dumps({"info": {"version": "1.2.3"}}).encode()

        def fake_urlopen(url, timeout=None):
            captured["url"] = url
            return FakeResp()

        monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
        spec = _spec_with_assertion({
            "kind": "api_json_path", "path": "/api/version",
            "json_pointer": "/info/version", "expected": "{version_no_v}",
        })
        # 修复前运行时读的是 va["url"]（校验器规则12 只认 path）→ KeyError 断言失败
        res = assert_version("", spec, "v1.2.3", base_url="http://demo.local/")
        assert captured["url"] == "http://demo.local/api/version", captured
        assert res.ok, res.detail

    def test_api_json_path_without_base_url_fails_clearly(self):
        spec = _spec_with_assertion({
            "kind": "api_json_path", "path": "/api/version",
            "json_pointer": "/info/version", "expected": "1.2.3",
        })
        res = assert_version("", spec, "v1.2.3")
        assert res.configured and not res.ok
        assert "base_url" in res.detail


# ------------------------------------------------------------------ M5: 校验器畸形输入


class TestAppspecMalformedShapes:
    """畸形 YAML 形状必须产出违规清单，而不是让校验器抛异常。"""

    def _errs(self, tmp_path, mutate):
        spec = make_spec(tmp_path, mutate)
        return appspec.validate(spec, tmp_path, "us-east-1")

    def test_string_version_assertion(self, tmp_path):
        errs = self._errs(tmp_path, lambda d: d["health_check"].__setitem__(
            "version_assertion", "X-App-Version"))
        assert any("规则12" in e and "映射" in e for e in errs), errs

    def test_scenarios_non_dict_element(self, tmp_path):
        errs = self._errs(tmp_path, lambda d: d["tests"].__setitem__(
            "scenarios", ["home", 42]))
        assert any("规则8" in e and "映射" in e for e in errs), errs

    def test_scenarios_not_a_list(self, tmp_path):
        errs = self._errs(tmp_path, lambda d: d["tests"].__setitem__("scenarios", "home"))
        assert any("规则8" in e and "列表" in e for e in errs), errs

    def test_features_not_a_list(self, tmp_path):
        errs = self._errs(tmp_path, lambda d: d["website"].__setitem__(
            "features", {"en": "x"}))
        assert any("规则13" in e and "列表" in e for e in errs), errs

    def test_features_non_dict_element(self, tmp_path):
        errs = self._errs(tmp_path, lambda d: d["website"].__setitem__(
            "features", ["just a string"]))
        assert any("规则13" in e for e in errs), errs

    def test_unknown_app_type_no_crash(self, tmp_path):
        # 修复前 profiles.LADDER[spec.app_type] 直接 KeyError
        errs = self._errs(tmp_path, lambda d: d["app"].__setitem__("app_type", "toaster"))
        assert any("规则9" in e for e in errs), errs

    def test_extra_environment_not_a_list(self, tmp_path):
        errs = self._errs(tmp_path, lambda d: d["deploy"].__setitem__(
            "extra_environment", "FOO=bar"))
        assert any("规则16" in e and "列表" in e for e in errs), errs


# ------------------------------------------------------------------ M8: id 清洗与后端防线


class TestIdSanitization:
    def test_output_only_contains_safe_chars(self):
        out = sanitize_for_id("../etc/passwd")
        assert "/" not in out and "\\" not in out
        assert set(out) <= set("abcdefghijklmnopqrstuvwxyz0123456789._-")

    def test_uppercase_lowered(self):
        assert sanitize_for_id("V1.2.3-Beta") == "v1.2.3-beta"

    def test_dir_backend_rejects_traversal(self, tmp_path):
        (tmp_path / "data").mkdir()
        (tmp_path / "secret.txt").write_text("top secret")
        backend = DirBackend(tmp_path / "data")
        with pytest.raises(ValueError):
            backend.put("../evil.json", b"x")
        with pytest.raises(ValueError):
            backend.get("../../secret.txt")


# ------------------------------------------------------------------ M10: AI 白名单归一化


class TestAiWhitelistNormalize:
    def test_dot_slash_prefix_allowed(self):
        af.assert_whitelisted("ghost", "./apps/ghost.yaml")
        af.assert_whitelisted("ghost", "./apps/ghost/tests/test_a.py")

    def test_dotdot_prefix_rejected_not_stripped(self):
        # 修复前 lstrip("./") 按字符集剥离，"../apps/..." 会被剥成 "apps/..." 绕过白名单
        with pytest.raises(af.PathNotAllowed):
            af.assert_whitelisted("ghost", "../apps/ghost.yaml")

    def test_dotdot_segment_anywhere_rejected(self):
        with pytest.raises(af.PathNotAllowed):
            af.assert_whitelisted("ghost", "apps/ghost/tests/../../ghost.yaml")

    def test_outside_whitelist_still_rejected(self):
        with pytest.raises(af.PathNotAllowed):
            af.assert_whitelisted("ghost", "apps/other.yaml")


# ------------------------------------------------- 台账自动关闭（state-machine §7）


def _ledger_body(app_version: str, vid: str) -> str:
    return (
        "Application Verification 失败。\n\n```corenova-failure\n"
        + json.dumps({"app": "ghost", "app_version": app_version, "verification_id": vid})
        + "\n```\n"
    )


class TestResolveFailures:
    """resolve_failures：发布成功后按 app+版本/vid 关闭台账，"unknown" 与异版本不关。"""

    @pytest.fixture
    def ledger(self, monkeypatch):
        from corenova import failure

        calls: list[tuple[str, str, dict]] = []

        def fake_request(url, method="GET", headers=None, data=None):
            calls.append((method, url, data or {}))

        monkeypatch.setattr(failure, "http_request", fake_request)
        monkeypatch.setenv("GITHUB_REPOSITORY", "CoreNovaLabs/CoreNovaLaunch")
        return failure, calls

    def _items(self, failure, monkeypatch, bodies):
        monkeypatch.setattr(
            failure, "http_json",
            lambda url, headers=None: {"items": [
                {"number": i + 1, "body": b} for i, b in enumerate(bodies)
            ]},
        )

    def test_version_match_closes_with_comment(self, ledger, monkeypatch):
        failure, calls = ledger
        self._items(failure, monkeypatch, [_ledger_body("v6.61.0", "ghost-v6.61.0-20260831-001")])
        failure.resolve_failures("ghost", "v6.61.0", "ghost-v6.61.0-20260901-003")
        assert ("POST", "https://api.github.com/repos/CoreNovaLabs/CoreNovaLaunch/issues/1/comments") in [
            (m, u) for m, u, _ in calls]
        assert ("PATCH", "https://api.github.com/repos/CoreNovaLabs/CoreNovaLaunch/issues/1") in [
            (m, u) for m, u, _ in calls]

    def test_verification_id_match_closes(self, ledger, monkeypatch):
        failure, calls = ledger
        self._items(failure, monkeypatch, [_ledger_body("unknown", "ghost-v6.61.0-20260901-003")])
        failure.resolve_failures("ghost", "v6.62.0", "ghost-v6.61.0-20260901-003")
        assert any(m == "PATCH" for m, _, _ in calls)

    def test_unknown_version_not_closed(self, ledger, monkeypatch):
        failure, calls = ledger
        self._items(failure, monkeypatch, [_ledger_body("unknown", "pre-verification")])
        failure.resolve_failures("ghost", "v6.61.0", "ghost-v6.61.0-20260901-003")
        assert calls == []

    def test_other_version_not_closed(self, ledger, monkeypatch):
        failure, calls = ledger
        self._items(failure, monkeypatch, [_ledger_body("v6.62.0", "x")])
        failure.resolve_failures("ghost", "v6.61.0", "ghost-v6.61.0-20260901-003")
        assert calls == []

    def test_pre_verification_closes_only_known_matching_version(self, ledger, monkeypatch):
        failure, calls = ledger
        self._items(failure, monkeypatch, [
            _ledger_body("v6.61.0", "pre-verification-ghost-v6.61.0"),
            _ledger_body("unknown", "pre-verification-ghost-unknown-resolve_version"),
            _ledger_body("v6.62.0", "pre-verification-ghost-v6.62.0"),
        ])
        failure.resolve_failures("ghost", "v6.61.0", "ghost-v6.61.0-success")
        patches = [(url, data) for method, url, data in calls if method == "PATCH"]
        assert patches == [(
            "https://api.github.com/repos/CoreNovaLabs/CoreNovaLaunch/issues/1", {"state": "closed"},
        )]

    def test_no_repo_env_is_noop(self, ledger, monkeypatch):
        failure, calls = ledger
        monkeypatch.delenv("GITHUB_REPOSITORY")
        failure.resolve_failures("ghost", "v6.61.0", "vid")
        assert calls == []

    def test_meta_of_malformed_block(self):
        from corenova.failure import meta_from_body

        assert meta_from_body("```corenova-failure\n{not json}\n```") == {}
        assert meta_from_body("没有块的正文") == {}
        assert meta_from_body(_ledger_body("v1", "vid-1"))["app_version"] == "v1"


class TestStageErrorContext:
    @pytest.fixture
    def rig(self, monkeypatch, tmp_path):
        from corenova import pipeline

        cfg = SimpleNamespace(root=tmp_path, output_dir=tmp_path, region="test-region", registry_mirror="")
        spec = AppSpec(name="ghost", path=tmp_path / "ghost.yaml", raw="", data={})
        monkeypatch.setattr(pipeline.Config, "load", lambda: cfg)
        monkeypatch.setattr(pipeline.appspec, "load", lambda *args: spec)
        monkeypatch.setattr(pipeline.appspec, "validate", lambda *args: [])
        monkeypatch.setattr(pipeline, "make_backend", Mock(return_value=object()))
        monkeypatch.setattr(pipeline.platformref, "check", Mock(return_value=SimpleNamespace(
            valid=True, reasons=[], contract={},
        )))
        release = Mock(return_value=SimpleNamespace(app_version="v6.61.0"))
        digest = Mock(side_effect=ValueError("digest unavailable"))
        monkeypatch.setattr(pipeline.resolver, "pick_release", release)
        monkeypatch.setattr(pipeline.appspec, "render_image_ref", lambda spec, version: f"ghost:{version}")
        monkeypatch.setattr(pipeline.resolver, "resolve_digest", digest)
        env = object()
        monkeypatch.setattr(pipeline.runtime, "build_env", Mock(return_value=env))
        down = Mock()
        monkeypatch.setattr(pipeline.runtime, "down", down)
        records = []
        monkeypatch.setattr(pipeline, "record_failure", records.append)
        monkeypatch.setenv("GITHUB_REPOSITORY", "example/verify")
        monkeypatch.setenv("GITHUB_RUN_ID", "12345")
        monkeypatch.delenv("CORENOVA_KEEP_RUNNING", raising=False)
        monkeypatch.setattr(pipeline.sys, "argv", ["verify", "--app", "ghost", "--version", "unresolved-input"])
        return SimpleNamespace(
            pipeline=pipeline, release=release, digest=digest, records=records,
            down=down, env=env, spec=spec, cfg=cfg,
        )

    def test_digest_error_carries_actual_resolved_version(self, rig):
        with pytest.raises(rig.pipeline.StageError) as caught:
            rig.pipeline.run_verification("ghost", version="unresolved-input")
        exc = caught.value
        assert (exc.stage, exc.check) == ("RESOLVED", "resolve_digest")
        assert exc.app_version == "v6.61.0"
        assert exc.verification_id == "pre-verification-ghost-v6.61.0"
        assert exc.err is rig.digest.side_effect
        assert exc.__cause__ is exc.err
        rig.release.assert_called_once_with(rig.spec, wanted="unresolved-input")
        rig.digest.assert_called_once_with("ghost:v6.61.0", "")
        rig.down.assert_not_called()

    def test_main_digest_records_version_link_and_isolated_stable_ids(self, rig):
        for version in ("V6.61.0+Build", "v6.62.0", "V6.61.0+Build"):
            rig.release.return_value = SimpleNamespace(app_version=version)
            with pytest.raises(SystemExit) as caught:
                rig.pipeline.main()
            assert caught.value.code == 2
            rec = rig.records[-1]
            assert rec.app_version == version
            assert rec.verification_id == f"pre-verification-ghost-{sanitize_for_id(version)}"
            assert rec.run_url == "https://github.com/example/verify/actions/runs/12345"
            assert rec.failed_check == "resolve_digest"
        assert rig.records[0].verification_id != rig.records[1].verification_id
        assert rig.records[0].verification_id == rig.records[2].verification_id

    def test_resolution_failure_stays_unknown_despite_cli_version(self, rig):
        rig.release.side_effect = ValueError("release not found")
        with pytest.raises(SystemExit) as caught:
            rig.pipeline.main()
        assert caught.value.code == 2
        rec, = rig.records
        assert rec.app_version == "unknown"
        assert rec.verification_id == "pre-verification-ghost-unknown-resolve_version"
        assert rec.run_url == "https://github.com/example/verify/actions/runs/12345"
        rig.digest.assert_not_called()

    def test_unknown_context_defaults_and_check_isolation(self, rig, monkeypatch):
        for check in ("app_schema", "resolve_version"):
            exc = rig.pipeline.StageError("RESOLVED", check, ValueError("invalid"))
            assert exc.app_version == exc.verification_id == ""
            monkeypatch.setattr(rig.pipeline, "run_verification", Mock(side_effect=exc))
            with pytest.raises(SystemExit):
                rig.pipeline.main()
        assert {rec.app_version for rec in rig.records} == {"unknown"}
        assert len({rec.verification_id for rec in rig.records}) == 2
        assert all("ghost" in rec.verification_id for rec in rig.records)

    @pytest.mark.parametrize("failure_at", ["docker", "up"])
    def test_verifying_error_keeps_assigned_context_and_cleanup(self, rig, monkeypatch, failure_at):
        rig.digest.side_effect = None
        rig.digest.return_value = SimpleNamespace(image_ref="ghost:v6.61.0", pull_ref="ghost@sha256:abc", digest="sha256:abc")
        vid = "ghost-v6.61.0-assigned"
        monkeypatch.setattr(rig.pipeline.mf, "verification_id", lambda *args: vid)
        monkeypatch.setattr(rig.pipeline, "docker_available", lambda: failure_at != "docker")
        monkeypatch.setattr(rig.pipeline.runtime, "up", Mock(side_effect=ValueError("compose failed")))
        with pytest.raises(rig.pipeline.StageError) as caught:
            rig.pipeline.run_verification("ghost")
        exc = caught.value
        assert (exc.stage, exc.check) == ("VERIFYING", "compose_started")
        assert (exc.app_version, exc.verification_id) == ("v6.61.0", vid)
        rig.down.assert_called_once_with(rig.env, rig.spec, rig.cfg.root)

        rig.down.reset_mock()
        with pytest.raises(SystemExit) as caught:
            rig.pipeline.main()
        assert caught.value.code == 2
        rec, = rig.records
        assert (rec.app_version, rec.verification_id) == ("v6.61.0", vid)
        assert rec.run_url == "https://github.com/example/verify/actions/runs/12345"
        rig.down.assert_called_once_with(rig.env, rig.spec, rig.cfg.root)


class TestFailureIssueIdentity:
    @pytest.fixture
    def ledger(self, monkeypatch):
        from corenova import failure

        monkeypatch.setenv("GITHUB_REPOSITORY", "example/verify")
        record = failure.FailureRecord(
            app="ghost", app_version="v1", verification_id="pre-verification-ghost-v1",
            classification="MANUAL_REQUIRED", failed_stage="RESOLVED", failed_check="resolve_digest",
        )
        return failure, record

    @pytest.mark.parametrize("state", [None, "open"])
    def test_find_issue_matches_full_structured_id_only(self, ledger, monkeypatch, state):
        failure, record = ledger
        exact = {"number": 4, "body": record.body()}
        if state is not None:
            exact["state"] = state
        items = [
            {"number": 1, "body": _ledger_body("v10", record.verification_id + "0")},
            {"number": 2, "body": _ledger_body("v2", "other-id") + record.verification_id},
            {"number": 3, "body": record.verification_id},
            exact,
        ]
        search = Mock(return_value={"items": items})
        monkeypatch.setattr(failure, "http_json", search)
        assert failure.find_issue(record) is exact
        query = parse_qs(urlparse(search.call_args.args[0]).query)["q"][0].split()
        assert "is:open" in query
        assert "in:body" in query

    def test_find_issue_ignores_closed_even_if_search_returns_it(self, ledger, monkeypatch):
        failure, record = ledger
        closed = {"number": 1, "body": record.body(), "state": "closed"}
        opened = {"number": 2, "body": record.body(), "state": "open"}
        search = Mock(return_value={"items": [closed, opened]})
        monkeypatch.setattr(failure, "http_json", search)
        assert failure.find_issue(record) is opened
        search.return_value = {"items": [closed]}
        assert failure.find_issue(record) is None

    def test_update_synchronizes_title_and_increments_original_attempts(self, ledger, monkeypatch):
        failure, record = ledger
        previous = failure.FailureRecord(
            app="ghost", app_version="unknown", verification_id=record.verification_id,
            classification="TRANSIENT", failed_stage="RESOLVED", failed_check="resolve_digest", attempts=2,
        )
        monkeypatch.setattr(failure, "http_json", Mock(return_value={"items": [
            {"number": 7, "body": previous.body(), "state": "open", "title": previous.title()},
        ]}))
        request = Mock()
        monkeypatch.setattr(failure, "http_request", request)
        failure.record_failure(record)
        request.assert_called_once()
        assert request.call_args.args[0].endswith("/issues/7")
        assert request.call_args.kwargs["method"] == "PATCH"
        data = request.call_args.kwargs["data"]
        assert data["title"] == record.title() != previous.title()
        meta = failure.meta_from_body(data["body"])
        assert meta["attempts"] == 3
        assert meta["app_version"] == "v1"
        assert "needs-human" in data["labels"]


class TestLogGoesToStderr:
    """stdout 是脚本数据通道（--json）；诊断日志混入会炸 json.loads（8/30 事故）。"""

    def test_log_writes_stderr_not_stdout(self, capsys):
        from corenova.util import log

        log("诊断信息")
        out, err = capsys.readouterr()
        assert out == ""
        assert "[corenova] 诊断信息" in err
