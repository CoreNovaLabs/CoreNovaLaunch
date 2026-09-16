"""navidrome 预写测试的共用夹具（scripts/dev/new_app.py 生成）。

容器是每次验证从零启动、跑完销毁的一次性环境。
提供 HTTP 与浏览器回归共用夹具。
"""

from __future__ import annotations

import os

import pytest

BASE_URL = os.environ.get("CORENOVA_APP_URL", "http://localhost:4533")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture(scope="session")
def browser_page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_context(viewport={"width": 1440, "height": 900}).new_page()
        page.set_default_timeout(60_000)
        try:
            yield page
        finally:
            browser.close()
