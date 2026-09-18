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
import time
import urllib.request

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


class TestLogGoesToStderr:
    """stdout 是脚本数据通道（--json）；诊断日志混入会炸 json.loads（8/30 事故）。"""

    def test_log_writes_stderr_not_stdout(self, capsys):
        from corenova.util import log

        log("诊断信息")
        out, err = capsys.readouterr()
        assert out == ""
        assert "[corenova] 诊断信息" in err
