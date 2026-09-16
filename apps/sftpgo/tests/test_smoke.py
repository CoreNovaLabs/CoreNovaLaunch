"""SFTPGo 真容器回归。"""

from __future__ import annotations


def test_health_and_setup_page(base_url):
    import requests

    health = requests.get(base_url + "/healthz", timeout=20)
    assert health.status_code == 200
    assert health.text.strip() == "ok"
    root = requests.get(base_url + "/", timeout=20, allow_redirects=True)
    assert root.status_code == 200
    assert "SFTPGo" in root.text
    assert "/web/admin/setup" in root.url
