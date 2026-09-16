"""Audiobookshelf 真容器回归。"""

from __future__ import annotations


def test_health_and_root(base_url):
    import requests

    health = requests.get(base_url + "/healthcheck", timeout=20)
    assert health.status_code == 200
    assert health.text.strip() == "OK"
    root = requests.get(base_url + "/", timeout=20)
    assert root.status_code == 200
    assert "Audiobookshelf" in root.text
