"""Disposable owner fixtures: all authentication failures are hard failures."""
import os

import pytest
import requests
from scenario_setup import owner_session

from corenova.business_fixtures import docker_app  # noqa: F401


@pytest.fixture(scope="session")
def base_url():
    return os.environ.get("CORENOVA_APP_URL", "http://localhost:2368").rstrip("/")


@pytest.fixture(scope="session")
def api():
    with requests.Session() as session:
        yield session


@pytest.fixture(scope="session")
def auth_api(base_url):
    with owner_session(base_url) as session:
        yield session


@pytest.fixture(scope="session")
def browser_page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        browser = pw.chromium.launch(args=["--no-sandbox", "--disable-dev-shm-usage"])
        page = browser.new_page(viewport={"width": 1440, "height": 900})
        page.set_default_timeout(60_000)
        try:
            yield page
        finally:
            browser.close()
