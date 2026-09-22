"""Ghost 6.61.0: setup status + cookie session, not OAuth password grants."""
from urllib.parse import urlsplit
from uuid import uuid4

import requests

EMAIL = "verify@corenovalaunch.test"
PASSWORD = "CoreNova-Verify-2026!"
NAME = "CoreNova Verify"


def owner_session(base_url):
    session = requests.Session()
    session.headers.update({"Origin": base_url, "Accept": "application/json"})
    api = base_url + "/ghost/api/admin"
    response = session.get(api + "/authentication/setup/", timeout=20)
    assert response.status_code == 200, response.text[:500]
    if response.json()["setup"][0]["status"] is False:
        response = session.post(api + "/authentication/setup/", json={"setup": [{
            "name": NAME, "email": EMAIL, "password": PASSWORD,
            "blogTitle": "CoreNova Verification",
        }]}, timeout=60)
        assert response.status_code == 201, response.text[:500]
        assert response.json()["users"][0]["email"] == EMAIL
    response = session.post(api + "/session/", json={"username": EMAIL, "password": PASSWORD}, timeout=30)
    assert response.status_code == 201, response.text[:500]
    assert session.cookies.get("ghost-admin-api-session"), "Ghost did not establish a cookie session"
    response = session.get(api + "/users/me/", timeout=20)
    assert response.status_code == 200, response.text[:500]
    assert response.json()["users"][0]["email"] == EMAIL
    return session


def publish(session, base_url, marker=None):
    marker = marker or ("p03-" + uuid4().hex)
    response = session.post(base_url + "/ghost/api/admin/posts/?source=html", json={"posts": [{
        "title": "CoreNova verified article " + marker, "slug": marker,
        "html": f"<p>Verified publishing, persistence and recovery: {marker}</p>", "status": "published",
    }]}, timeout=30)
    assert response.status_code == 201, response.text[:500]
    post = response.json()["posts"][0]
    assert post["status"] == "published" and post["slug"] == marker
    return post


def prepare(page, slug):
    parts = urlsplit(page.url)
    base_url = f"{parts.scheme}://{parts.netloc}"
    session = owner_session(base_url)
    response = session.get(base_url + "/ghost/api/admin/posts/?filter=status:published&limit=50", timeout=20)
    assert response.status_code == 200
    posts = [p for p in response.json()["posts"] if p["slug"].startswith("p03-")]
    post = posts[0] if posts else publish(session, base_url)
    if "/ghost" in parts.path or "admin" in slug:
        page.context.add_cookies([{"name": c.name, "value": c.value, "url": base_url + "/ghost/"}
                                  for c in session.cookies])
        page.goto(base_url + "/ghost/#/posts/", wait_until="networkidle")
        page.get_by_text(post["title"], exact=True).first.wait_for()
    else:
        page.goto(base_url + "/" + post["slug"] + "/", wait_until="networkidle")
        page.get_by_role("heading", name=post["title"], exact=True).wait_for()
