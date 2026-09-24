"""corenova.versioning 单一事实源的等价性测试。

版本裁决有三处历史实现：publish.may_update_current（P5 覆盖保护）、
check_versions.compare（监控扇出）、golden/platformref 的 _age_days（契约年龄）。
本文件锁定收敛后的口径：

- compare() 的 relation 值必须逐对等于 versioning.relation()；
- 监控判 newer 的版本，P5 覆盖保护必须放行；判 older 的必须拒绝
  （口径漂移 = "扇出了验证、发布端却拒绝覆盖" 的空转，或更糟的反向）；
- age_days 的哨兵语义：解析失败 = 无限老（过期判定必须成立）。
"""

from __future__ import annotations

import importlib.util
import io
import json
import pathlib
import urllib.error
from types import SimpleNamespace

import pytest

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

from corenova import publish, versioning  # noqa: E402
from corenova.backend import DirBackend  # noqa: E402


def _load_script(relpath: str, name: str):
    """scripts/ 下的入口脚本不是包（目录含连字符），按文件路径加载。"""
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cv = _load_script("scripts/monitor/check_versions.py", "check_versions_versioning_test")


# ------------------------------------------------------------------ semver_relation


class TestSemverRelation:
    @pytest.mark.parametrize("candidate,current,expected", [
        ("v1.3.0", "v1.2.0", "newer"),
        ("1.3.0", "v1.2.0", "newer"),       # v 前缀等价
        ("v1.2.0", "v1.2.0", "same"),
        ("v1.2.3-rc.1", "v1.2.3", "same"),  # prerelease 后缀被忽略（口径锁定，勿"顺手修复"）
        ("v1.1.0", "v1.2.0", "older"),
        ("v1.10.0", "v1.9.0", "newer"),     # 数值比较，不是字符串比较
    ])
    def test_pairs(self, candidate, current, expected):
        assert versioning.semver_relation(candidate, current) == expected

    @pytest.mark.parametrize("candidate,current", [
        ("main", "v1.2.0"),   # 候选非 semver
        ("v1.2.0", "main"),   # 当前非 semver
        ("main", "main"),     # 双方都非 semver
        ("", "v1.2.0"),
    ])
    def test_unparseable_returns_none(self, candidate, current):
        assert versioning.semver_relation(candidate, current) is None


class TestRelation:
    def test_missing_current_is_initial(self):
        assert versioning.relation("v1.0.0", None) == "initial"
        assert versioning.relation("v1.0.0", "") == "initial"

    def test_non_semver_string_equality_is_same(self):
        assert versioning.relation("main", "main") == "same"

    def test_non_semver_difference_is_unknown(self):
        assert versioning.relation("main", "dev") == "unknown"
        assert versioning.relation("abc123def", "v1.0.0") == "unknown"
        assert versioning.relation("v1.0.0", "20260901") == "unknown"

    def test_semver_pairs_delegate(self):
        assert versioning.relation("v1.3.0", "v1.2.0") == "newer"
        assert versioning.relation("v1.1.0", "v1.2.0") == "older"


# ------------------------------------------------------------------ age_days 哨兵


class TestAgeDaysSentinel:
    def test_non_string_is_unparseable(self):
        # 契约 JSON 里时间戳字段坏了（数字/None）→ 无限老，过期判定成立
        assert versioning.age_days(None) == versioning.UNPARSEABLE_AGE_DAYS
        assert versioning.age_days(20260901) == versioning.UNPARSEABLE_AGE_DAYS

    def test_future_clamps_to_zero(self):
        assert versioning.age_days("2999-01-01T00:00:00Z") == 0.0


# ------------------------------------------------------------------ compare() 等价


COMPARE_CASES = [
    ("v6.62.0", "v6.61.0"),   # newer
    ("v6.61.0", "v6.61.0"),   # same（semver）
    ("v6.60.0", "v6.61.0"),   # older
    ("main", "main"),          # same（字符串等值）
    ("main", "dev"),           # unknown
    ("abc123def", "v1.0.0"),   # unknown（候选非 semver）
    ("v1.0.0", "20260901"),    # unknown（当前非 semver）
]


class TestCompareEquivalence:
    def test_relation_matches_single_source(self):
        for candidate, current in COMPARE_CASES:
            verdict = cv.compare(candidate, current, "release_tag")
            assert verdict["relation"] == versioning.relation(candidate, current)

    def test_decision_follows_relation(self):
        for candidate, current in COMPARE_CASES:
            rel = versioning.relation(candidate, current)
            verdict = cv.compare(candidate, current, "release_tag")
            expected = "dispatch" if rel in ("newer", "unknown") else "skip"
            assert verdict["decision"] == expected, (candidate, current, verdict)

    def test_no_current_is_initial_dispatch(self):
        for current in (None, ""):
            verdict = cv.compare("v1.0.0", current, "release_tag")
            assert verdict["relation"] == "initial"
            assert verdict["decision"] == "dispatch"


# ------------------------------------------------------- may_update_current() 等价


SEMVER_GATE_CASES = [
    ("v1.3.0", "v1.2.0", "newer", True),
    ("v1.2.0", "v1.2.0", "same", True),
    ("v1.1.0", "v1.2.0", "older", False),
]


def _backend_with_current(tmp_path: pathlib.Path, name: str, version: str, run_id: str) -> DirBackend:
    backend = DirBackend(tmp_path / name)
    backend.put(
        "verified/app/current.json",
        json.dumps({"app_version": version, "verification_run_id": run_id}).encode(),
    )
    return backend


class TestMayUpdateEquivalence:
    @pytest.mark.parametrize("strategy", ["release_tag", "semver_latest"])
    @pytest.mark.parametrize("candidate,current,rel,expected", SEMVER_GATE_CASES)
    def test_gated_strategy_follows_relation(self, tmp_path, strategy, candidate, current, rel, expected):
        assert versioning.semver_relation(candidate, current) == rel
        backend = _backend_with_current(tmp_path, f"g-{strategy}-{candidate}", current, "500")
        ok, why = publish.may_update_current(backend, "app", candidate, "1", strategy)
        assert ok is expected, why

    @pytest.mark.parametrize("candidate,current,rel,_", SEMVER_GATE_CASES)
    def test_ungated_strategy_ignores_semver(self, tmp_path, candidate, current, rel, _):
        """strategy 不在 gate 内时，semver 关系不参与裁决，落到 run_id 比较。"""
        backend = _backend_with_current(tmp_path, f"u-{candidate}", current, "500")
        ok_newer_run, _ = publish.may_update_current(backend, "app", candidate, "600", "git_branch")
        ok_older_run, _ = publish.may_update_current(backend, "app", candidate, "100", "git_branch")
        assert ok_newer_run and not ok_older_run

    def test_no_current_always_allows(self, tmp_path):
        backend = DirBackend(tmp_path / "empty")
        ok, why = publish.may_update_current(backend, "app", "v1.0.0", "1", "release_tag")
        assert ok and "尚无" in why

    def test_force_overrides_everything(self, tmp_path):
        backend = _backend_with_current(tmp_path, "forced", "v9.9.9", "5000")
        ok, _ = publish.may_update_current(backend, "app", "v0.0.1", "1", "release_tag", force=True)
        assert ok


class TestMonitorPublishConsistency:
    """端到端一致性：监控会扇出的版本，P5 覆盖保护不得拒绝（older/unknown 分开断言）。"""

    def test_newer_relation_passes_publish_gate(self, tmp_path):
        for i, (candidate, current) in enumerate(COMPARE_CASES):
            if versioning.relation(candidate, current) != "newer":
                continue
            backend = _backend_with_current(tmp_path, f"c-{i}", current, "1000")
            ok, why = publish.may_update_current(backend, "app", candidate, "1", "release_tag")
            assert ok, f"监控判 newer 但 P5 拒绝：{candidate} vs {current} —— {why}"

    def test_older_relation_blocked_by_publish_gate(self, tmp_path):
        for i, (candidate, current) in enumerate(COMPARE_CASES):
            if versioning.relation(candidate, current) != "older":
                continue
            backend = _backend_with_current(tmp_path, f"b-{i}", current, "1000")
            ok, why = publish.may_update_current(backend, "app", candidate, "999", "release_tag")
            assert not ok, f"监控判 older 但 P5 放行回退：{candidate} vs {current} —— {why}"


class TestImagePreflight:
    ref = "jgraph/drawio:31.5.2"
    url = "https://registry-1.docker.io/v2/jgraph/drawio/manifests/31.5.2"

    @pytest.fixture
    def probe(self, monkeypatch):
        calls, waits = [], []
        replies = []

        def resolve(ref, mirror):
            calls.append((ref, mirror))
            reply = replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply

        monkeypatch.setattr(cv.resolver, "resolve_digest", resolve)
        monkeypatch.setattr(cv.time, "sleep", waits.append)
        return replies, calls, waits

    def missing(self, url=None, body=None):
        return cv.HttpError(404, url or self.url, body or json.dumps({
            "errors": [{"code": "MANIFEST_UNKNOWN"}],
        }))

    def test_missing_manifest_is_bounded_and_held(self, probe):
        replies, calls, waits = probe
        replies.extend([self.missing()] * 3)
        result = cv.image_preflight(self.ref, "")
        assert result["decision"] == "hold"
        assert result["classification"] == "MANUAL_REQUIRED"
        assert "下轮监控" in result["reason"]
        assert calls == [(self.ref, "")] * 3
        assert waits == [5, 10]

    def test_eventual_image_publication_allows_dispatch(self, probe):
        replies, calls, waits = probe
        replies.extend([self.missing(), self.missing(), object()])
        assert cv.image_preflight(self.ref, "") == {}
        assert len(calls) == 3
        assert waits == [5, 10]

    @pytest.mark.parametrize("body", ["Not Found", "[]", '{"errors":{}}',
                                      '{"errors":[{"code":"NAME_UNKNOWN"}]}'])
    def test_other_404_is_not_publication_delay(self, probe, body):
        replies, calls, waits = probe
        replies.append(self.missing(body=body))
        result = cv.image_preflight(self.ref, "")
        assert result["decision"] == "error"
        assert len(calls) == 1 and waits == []

    def test_token_endpoint_404_is_not_missing_manifest(self, probe):
        replies, calls, waits = probe
        replies.append(self.missing(url="https://auth.docker.io/token"))
        assert cv.image_preflight(self.ref, "")["decision"] == "error"
        assert len(calls) == 1 and waits == []

    def test_full_error_body_is_parsed_not_truncated_message(self, probe):
        replies, _, waits = probe
        body = json.dumps({"padding": "x" * 500, "errors": [{"code": "MANIFEST_UNKNOWN"}]})
        replies.extend([self.missing(body=body)] * 3)
        assert cv.image_preflight(self.ref, "")["decision"] == "hold"
        assert waits == [5, 10]

    @pytest.mark.parametrize("error,classification", [
        (cv.HttpError(401, url, "unauthorized"), "MANUAL_REQUIRED"),
        (cv.HttpError(429, url, "rate limited"), "TRANSIENT"),
        (TimeoutError("timed out"), "TRANSIENT"),
        (ValueError("platform linux/amd64 missing"), "MANUAL_REQUIRED"),
    ])
    def test_other_errors_are_visible_without_nested_retries(self, probe, error, classification):
        replies, calls, waits = probe
        replies.append(error)
        result = cv.image_preflight(self.ref, "")
        assert result["decision"] == "error"
        assert result["classification"] == classification
        assert len(calls) == 1 and waits == []

    def test_missing_then_rate_limited_stops_waiting(self, probe):
        replies, calls, waits = probe
        replies.extend([self.missing(), cv.HttpError(429, self.url, "rate limited")])
        result = cv.image_preflight(self.ref, "")
        assert result["decision"] == "error" and result["classification"] == "TRANSIENT"
        assert len(calls) == 2 and waits == [5]

    def test_http_retry_budget_is_not_multiplied(self, monkeypatch):
        from corenova import util

        calls, waits = [], []

        def open_request(request, timeout):
            calls.append(request.full_url)
            raise urllib.error.HTTPError(request.full_url, 429, "rate limited", {}, io.BytesIO(b"rate limited"))

        monkeypatch.setattr(cv.resolver, "_registry_token", lambda *a: "")
        monkeypatch.setattr(util, "_opener", lambda: SimpleNamespace(open=open_request))
        monkeypatch.setattr(util.time, "sleep", waits.append)
        result = cv.image_preflight(self.ref, "")
        assert result["decision"] == "error" and result["classification"] == "TRANSIENT"
        assert calls == [self.url] * 3
        assert waits == [1, 2]

    def test_mirror_is_preserved(self, probe):
        replies, calls, waits = probe
        mirror = "mirror.example.test"
        replies.extend([self.missing(url=f"https://{mirror}/v2/jgraph/drawio/manifests/31.5.2"), object()])
        assert cv.image_preflight(self.ref, mirror) == {}
        assert calls == [(self.ref, mirror)] * 2
        assert waits == [5]

    @pytest.fixture
    def monitor(self, monkeypatch, tmp_path):
        cfg = SimpleNamespace(root=REPO_ROOT, registry_mirror="", verified_backend="dir", output_dir=tmp_path)
        resolved = SimpleNamespace(app_version="v31.5.2", release_tag="v31.5.2", release_type="new_version",
                                   type_evidence="test", published_at="2026-09-23T18:13:03Z", source_revision="abc")
        monkeypatch.setattr(cv.publish, "current_version", lambda *a: "v31.5.1")
        monkeypatch.setattr(cv.resolver, "pick_release", lambda *a, **kw: resolved)
        gh = SimpleNamespace(latest_release=lambda *a: {"published_at": resolved.published_at})
        return cfg, gh

    def test_inspect_never_dispatches_unavailable_image(self, monitor, probe):
        cfg, gh = monitor
        replies, calls, _ = probe
        replies.extend([self.missing()] * 3)
        entry = cv.inspect_app("drawio", cfg, gh, 6, durable=True)
        assert entry["decision"] == "hold" and not entry["dispatch"]
        assert entry["classification"] == "MANUAL_REQUIRED"
        assert entry["app_version"] == "v31.5.2"
        assert entry["image_ref"] == self.ref
        assert len(calls) == 3
        summary = cv.render_markdown({"entries": [entry], "backend": "r2", "apps_checked": 1,
                                      "errors": 0, "durable_current_source": True})
        assert "不代表验证通过" in summary and "MANIFEST_UNKNOWN" in summary
        assert "暂缓（无持久事实源）" not in summary

    def test_next_monitor_round_can_recover_without_state(self, monitor, probe):
        cfg, gh = monitor
        replies, calls, waits = probe
        replies.extend([self.missing()] * 3 + [object()])
        held = cv.inspect_app("drawio", cfg, gh, 6, durable=True)
        ready = cv.inspect_app("drawio", cfg, gh, 6, durable=True)
        assert held["decision"] == "hold" and not held["dispatch"]
        assert ready["decision"] == "dispatch" and ready["dispatch"]
        assert len(calls) == 4 and waits == [5, 10]

    def test_inspect_ready_image_remains_dispatchable(self, monitor, probe):
        cfg, gh = monitor
        replies, calls, _ = probe
        replies.append(object())
        entry = cv.inspect_app("drawio", cfg, gh, 6, durable=True)
        assert entry["dispatch"] and entry["decision"] == "dispatch"
        assert entry["classification"] == ""
        assert calls == [(self.ref, "")]

    @pytest.mark.parametrize("current,durable,decision", [
        ("v31.5.2", True, "skip"), ("v31.5.3", True, "skip"), (None, False, "hold"),
    ])
    def test_non_dispatch_decisions_do_not_query_registry(self, monitor, probe, monkeypatch,
                                                        current, durable, decision):
        cfg, gh = monitor
        _, calls, _ = probe
        monkeypatch.setattr(cv.publish, "current_version", lambda *a: current)
        entry = cv.inspect_app("drawio", cfg, gh, 6, durable=durable)
        assert entry["decision"] == decision and not entry["dispatch"]
        assert calls == []
