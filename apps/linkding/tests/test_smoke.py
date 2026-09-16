"""Linkding 真容器回归。"""

from __future__ import annotations


def test_login_page_answers(base_url):
    import requests

    r = requests.get(base_url + "/", timeout=20, allow_redirects=True)
    assert r.status_code == 200
    assert "Login - Linkding" in r.text
    assert "/login/" in r.url
