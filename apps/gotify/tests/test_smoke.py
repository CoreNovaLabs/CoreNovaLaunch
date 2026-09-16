"""Gotify 真容器回归。"""

from __future__ import annotations


def test_health_and_login_page(base_url):
    import requests

    health = requests.get(base_url + "/health", timeout=20)
    assert health.status_code == 200
    assert health.json() == {"health": "green", "database": "green"}
    root = requests.get(base_url + "/", timeout=20)
    assert root.status_code == 200
    assert "gotify" in root.text.lower()
