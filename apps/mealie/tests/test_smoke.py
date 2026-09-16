"""Mealie 真容器回归。"""

from __future__ import annotations


def test_public_about_and_root(base_url):
    import requests

    about = requests.get(base_url + "/api/app/about", timeout=20)
    assert about.status_code == 200
    assert about.json()["version"].startswith("v")
    root = requests.get(base_url + "/", timeout=20)
    assert root.status_code == 200
