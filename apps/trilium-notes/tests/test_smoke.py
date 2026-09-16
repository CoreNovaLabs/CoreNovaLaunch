"""Trilium Notes 真容器回归。"""

from __future__ import annotations


def test_health_and_setup_page(base_url):
    import requests

    health = requests.get(base_url + "/api/health-check", timeout=20)
    assert health.status_code == 200
    assert health.json() == {"status": "ok"}
    root = requests.get(base_url + "/", timeout=20)
    assert root.status_code == 200
    assert "trilium" in root.text.lower()
