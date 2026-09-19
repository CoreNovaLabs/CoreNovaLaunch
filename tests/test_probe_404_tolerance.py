"""wait_ready 对 404 的容忍语义（issue #17 行为锁定）。

背景：应用首启存在"端口已监听、路由尚未挂载完"的窗口（Nest/Express 先 listen
后挂路由，期间任何路径都 404）。nocodb 2026.09.0 首验被"404 立即快速失败"误杀
（同轮 pytest 后置通过佐证路径本身正确）。修复后：连续 404 达到阈值才判 endpoint
错配（保留防呆），窗口内暂时 404 可恢复；非 404 的 4xx 出现说明路由已挂载，
会重置连续计数。
"""

from __future__ import annotations

import io
import pathlib
import urllib.error
from typing import Any

import pytest

import corenova.runtime as rt
from corenova.appspec import AppSpec

CONSEC_404_LIMIT = 10  # 与实现一致；若实现调整阈值，此处锁定的行为需同步审阅


class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def time(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class FakeResponse:
    def __init__(self, status: int, text: str = ""):
        self.status = status
        self.text = text

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int) -> bytes:
        return self.text.encode()

    @property
    def headers(self):
        return {}


def _fake_urlopen(statuses: list[Any]):
    """按序返回 FakeResponse / 抛 HTTPError；序列耗尽后重复最后一项。"""
    seq = list(statuses)
    calls: list[int] = []

    def urlopen(req, timeout=None):
        idx = min(len(calls), len(seq) - 1)
        calls.append(len(calls))
        item = seq[min(idx, len(seq) - 1)]
        if isinstance(item, int):
            if item == 200:
                return FakeResponse(200, "ok")
            raise urllib.error.HTTPError(
                req.full_url if hasattr(req, "full_url") else "", item, "err", None, io.BytesIO(b"")
            )
        return item  # 预构造对象（如自定义响应）

    return urlopen


def _spec() -> AppSpec:
    return AppSpec(
        name="demo",
        path=pathlib.Path("apps/demo.yaml"),
        raw="",
        data={
            "health_check": {
                "endpoint": "/api/v1/health",
                "retries": 40,
                "interval_seconds": 3,
                "startup_timeout_seconds": 240,
            }
        },
    )


@pytest.fixture
def clock(monkeypatch):
    c = FakeClock()
    monkeypatch.setattr(rt.time, "time", c.time)
    monkeypatch.setattr(rt.time, "sleep", c.sleep)
    return c


class TestWaitReady404Tolerance:
    def test_404_within_window_recovers(self, clock, monkeypatch):
        # 启动窗口内 404 两次后路由挂载 → 200：必须 ready，而不是立即误杀
        monkeypatch.setattr(rt.urllib.request, "urlopen", _fake_urlopen([404, 404, 200]))
        probe = rt.wait_ready("http://127.0.0.1:8080", _spec())
        assert probe.ok, probe.detail
        assert probe.status == 200

    def test_persistent_404_fails_fast(self, clock, monkeypatch):
        # 持续 404（endpoint 真错配）：达到连续阈值即失败，不耗满 40 次重试
        monkeypatch.setattr(rt.urllib.request, "urlopen", _fake_urlopen([404]))
        probe = rt.wait_ready("http://127.0.0.1:8080", _spec())
        assert not probe.ok
        assert "404" in probe.detail
        assert probe.attempts == CONSEC_404_LIMIT

    def test_non404_4xx_resets_consecutive_counter(self, clock, monkeypatch):
        # 401 说明路由已挂载并应答 → 之后的 404 计数从头再来
        statuses = [404] * 5 + [401] + [404] * 5 + [200]
        monkeypatch.setattr(rt.urllib.request, "urlopen", _fake_urlopen(statuses))
        probe = rt.wait_ready("http://127.0.0.1:8080", _spec())
        assert probe.ok, probe.detail

    def test_counter_reset_extends_patience_correctly(self, clock, monkeypatch):
        # 重置后再连续 404 达到阈值：依然会快速失败（防呆不被"曾经 200 过的路由"绕过）
        statuses = [404] * 5 + [401] + [404] * CONSEC_404_LIMIT
        monkeypatch.setattr(rt.urllib.request, "urlopen", _fake_urlopen(statuses))
        probe = rt.wait_ready("http://127.0.0.1:8080", _spec())
        assert not probe.ok
        assert probe.attempts == 5 + 1 + CONSEC_404_LIMIT
