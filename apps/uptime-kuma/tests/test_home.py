"""Kuma 2.5.3 public shell and order-independent database/owner onboarding."""

from __future__ import annotations


def test_root_reaches_first_run_page(base_url):
    import requests

    r = requests.get(base_url + "/", timeout=20, allow_redirects=True)
    assert r.status_code == 200, f"/ 重定向落点返回 {r.status_code}"
    assert "<html" in r.text.lower(), "落点不是 HTML"
    assert "<title>Uptime Kuma</title>" in r.text, "页面标题不是 Uptime Kuma"


def test_database_owner_or_existing_dashboard(base_url, browser_page):
    from scenario_setup import owner_page

    owner_page(browser_page, base_url)
    assert "Quick Stats" in browser_page.locator("body").inner_text()
