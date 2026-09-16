"""Actual Budget 真容器回归。"""

from __future__ import annotations


def test_health_info_and_root(base_url):
    import requests

    health = requests.get(base_url + "/health", timeout=20)
    assert health.status_code == 200
    assert health.json() == {"status": "UP"}
    info = requests.get(base_url + "/info", timeout=20)
    assert info.status_code == 200
    assert info.json()["build"]["version"]
    root = requests.get(base_url + "/", timeout=20)
    assert root.status_code == 200
    assert "<title>Actual</title>" in root.text
