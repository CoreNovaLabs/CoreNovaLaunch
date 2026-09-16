"""Node-RED 真容器回归。"""

from __future__ import annotations


def test_editor_answers(base_url):
    import requests

    r = requests.get(base_url + "/", timeout=20, allow_redirects=True)
    assert r.status_code == 200
    assert "Node-RED" in r.text
