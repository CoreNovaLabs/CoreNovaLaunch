"""resolver.semver_latest 策略测试。

背景（app-schema.md §4）：semver_latest = "从 tag 中取满足 semver 的最新稳定版"。
上游可能发布非版本号命名的最新 release（如 n8n 的 "stable"），或 tag 带仓库前缀
（如 "n8n@2.38.6"）——策略必须扫描 release 列表并宽容提取 semver，而不是只看
/releases/latest。
"""

from __future__ import annotations

import pathlib

import pytest

from corenova import resolver
from corenova.appspec import AppSpec
from corenova.resolver import extract_semver, pick_release


def test_extract_semver_plain():
    assert extract_semver("1.37.2") == (1, 37, 2)


def test_extract_semver_v_prefix():
    assert extract_semver("v1.27.3") == (1, 27, 3)


def test_extract_semver_repo_prefix():
    assert extract_semver("n8n@2.38.6") == (2, 38, 6)


def test_extract_semver_suffix():
    assert extract_semver("1.37.2-alpine") == (1, 37, 2)
    assert extract_semver("v0.30.0-rc.1") == (0, 30, 0)


def test_extract_semver_non_semver_returns_none():
    assert extract_semver("stable") is None
    assert extract_semver("2024-10-22") is None
    assert extract_semver("") is None


# ------------------------------------------------------------------ pick_release: wanted 匹配


class FakeGitHub:
    """只实现 pick_release 需要的两个方法；tag_sha 返回固定值（evidence，非门禁）。"""

    def __init__(self, tags: list[str]):
        self.releases_list = [
            {"tag_name": t, "draft": False, "prerelease": False, "name": t, "body": "", "published_at": ""}
            for t in tags
        ]

    def releases(self, repo: str, per_page: int = 30):
        return self.releases_list

    def tag_sha(self, repo: str, ref: str) -> str:
        return "f" * 40


def _n8n_spec() -> AppSpec:
    return AppSpec(
        name="n8n",
        path=pathlib.Path("apps/n8n.yaml"),
        raw="",
        data={
            "source": {
                "repo": "n8n-io/n8n",
                "version_strategy": "semver_latest",
                "release_filter": {"prerelease": False, "draft": False},
            }
        },
    )


@pytest.fixture(autouse=True)
def _no_previous_version(monkeypatch):
    # 避免测试读发布后端（issue #16 修复的行为锁定，与后端状态无关）
    monkeypatch.setattr(resolver, "_previous_published_version", lambda spec: None)


class TestWantedFallback:
    def test_wanted_semver_fallback_matches_prefixed_tag(self):
        # issue #16 根因：重验传入规范化 app_version（v2.39.8），上游 tag 是
        # "n8n@2.39.8" —— 精确匹配失败后必须退回 semver 匹配，否则永远 404
        gh = FakeGitHub(["stable", "n8n@2.39.8", "n8n@2.39.7"])
        resolved = pick_release(_n8n_spec(), gh=gh, wanted="v2.39.8")
        assert resolved.release_tag == "n8n@2.39.8"
        assert resolved.app_version == "v2.39.8"

    def test_wanted_exact_tag_still_matches(self):
        gh = FakeGitHub(["stable", "n8n@2.39.8"])
        resolved = pick_release(_n8n_spec(), gh=gh, wanted="n8n@2.39.8")
        assert resolved.release_tag == "n8n@2.39.8"

    def test_wanted_semver_with_no_upstream_match_raises(self):
        gh = FakeGitHub(["stable", "n8n@2.39.8"])
        with pytest.raises(ValueError, match="不存在 release tag"):
            pick_release(_n8n_spec(), gh=gh, wanted="v9.9.9")
