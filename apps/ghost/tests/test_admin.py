"""Ghost 6.61.0 real setup/session, publication and cold recovery assertions."""

from __future__ import annotations

PROTECTED = ("/ghost/api/admin/users/me/", "/ghost/api/admin/posts/", "/ghost/api/admin/newsletters/")


def test_public_admin_site_endpoint_answers(api, base_url):
    r = api.get(base_url.rstrip("/") + "/ghost/api/admin/site/", timeout=20)
    assert r.status_code == 200, f"admin/site 返回 {r.status_code}"
    assert (r.json().get("site") or {}).get("title"), "响应缺少 site.title"


def test_protected_admin_endpoints_reject_unauthenticated(api, base_url):
    for path in PROTECTED:
        r = api.get(base_url.rstrip("/") + path, timeout=20)
        assert r.status_code in (401, 403), f"{path} 未鉴权却返回 {r.status_code}（安全边界异常）"


def test_admin_spa_is_served(base_url, browser_page):
    page = browser_page
    resp = page.goto(base_url.rstrip("/") + "/ghost/", wait_until="networkidle")
    assert resp and resp.status == 200, f"后台入口返回 {resp.status if resp else 'None'}"
    assert page.locator("body").inner_text().strip(), "后台页面渲染为空白"


def test_owner_publish_restart_and_cold_restore(auth_api, base_url, docker_app, browser_page):
    import tarfile

    import requests
    from scenario_setup import owner_session, publish

    post = publish(auth_api, base_url)

    def check_and_edit(url, suffix):
        session = owner_session(url)  # Fresh login proves account/password persistence.
        response = session.get(url + "/ghost/api/admin/posts/" + post["id"] + "/", timeout=20)
        assert response.status_code == 200
        saved = response.json()["posts"][0]
        assert saved["slug"] == post["slug"] and saved["status"] == "published"
        response = session.get(url + "/ghost/api/admin/site/", timeout=20)
        assert response.status_code == 200
        assert response.json()["site"]["title"] == "CoreNova Verification"
        response = requests.get(url + "/" + post["slug"] + "/", timeout=20)
        assert response.status_code == 200
        assert "Verified publishing, persistence and recovery: " + post["slug"] in response.text
        response = session.put(url + "/ghost/api/admin/posts/" + post["id"] + "/",
                               json={"posts": [{"title": post["title"] + suffix,
                                                "updated_at": saved["updated_at"]}]}, timeout=20)
        assert response.status_code == 200, response.text[:500]
        assert response.json()["posts"][0]["title"] == post["title"] + suffix
        return session

    browser_page.goto(base_url + "/" + post["slug"] + "/", wait_until="networkidle")
    browser_page.get_by_role("heading", name=post["title"], exact=True).wait_for()
    docker_app.restart()
    check_and_edit(base_url, " — restarted")
    with docker_app.restored() as replica:
        with tarfile.open(replica.archive) as archive:
            assert any(m.name.endswith("data/ghost.db") for m in archive.getmembers())
        check_and_edit(replica.base_url, " — restored independently")
    response = requests.get(base_url + "/" + post["slug"] + "/", timeout=20)
    assert "restarted" in response.text and "restored independently" not in response.text
