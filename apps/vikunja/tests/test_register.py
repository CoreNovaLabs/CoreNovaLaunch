"""Vikunja API、注册页及容器重建后的数据库与附件回读。"""

from __future__ import annotations

from uuid import uuid4

import requests


def test_info_endpoint(base_url):
    r = requests.get(base_url + "/api/v1/info", timeout=20)
    assert r.status_code == 200, f"/api/v1/info 返回 {r.status_code}"
    info = r.json()
    assert info.get("version"), "缺少 version 字段"
    assert info["frontend_url"] == base_url.rstrip("/") + "/"


def test_register_renders(base_url, browser_page):
    page = browser_page
    page.goto(base_url + "/register", wait_until="domcontentloaded")
    page.wait_for_selector("input", timeout=90_000)
    text = page.locator("body").inner_text()
    assert text.strip(), "注册页渲染为空白"
    assert "vikunja" in text.lower() or "register" in text.lower() or "创建账户" in text, "页面缺少 Vikunja 注册标识"


def test_project_and_attachment_survive_recreate(docker_app):
    before, destination = docker_app.owned_container()
    assert before["State"]["Running"]
    assert destination == "/db"
    environment = dict(item.split("=", 1) for item in before["Config"]["Env"] if "=" in item)
    assert environment["VIKUNJA_FILES_BASEPATH"] == "/db"
    assert environment["VIKUNJA_SERVICE_PUBLICURL"] == docker_app.base_url + "/"
    suffix = uuid4().hex
    credentials = {"username": "cn" + suffix[:12], "password": uuid4().hex + "Aa1!"}
    project_title = "CoreNova project " + suffix
    task_title = "CoreNova task " + suffix
    payload = ("CoreNova 附件 " + suffix).encode() + b"\x00\xff\n"

    with requests.Session() as api:
        def request(method, path, expected=200, **kwargs):
            response = api.request(method, docker_app.base_url + "/api/v1" + path,
                                   timeout=20, **kwargs)
            assert response.status_code == expected, f"{method} {path}: HTTP {response.status_code}"
            return response

        user = request("POST", "/register", json={
            **credentials, "email": credentials["username"] + "@example.invalid",
        }).json()
        assert user["username"] == credentials["username"]
        token = request("POST", "/login", json=credentials).json()["token"]
        assert token
        api.headers["Authorization"] = "Bearer " + token
        project = request("PUT", "/projects", 201, json={"title": project_title}).json()
        assert project["title"] == project_title
        project_id = project["id"]
        task = request("PUT", f"/projects/{project_id}/tasks", 201, json={"title": task_title}).json()
        task_id = task["id"]
        assert task["project_id"] == project_id
        upload = request("PUT", f"/tasks/{task_id}/attachments", files={
            "files": ("evidence.bin", payload, "application/octet-stream"),
        }).json()
        assert not upload["errors"]
        assert len(upload["success"]) == 1
        attachment = upload["success"][0]
        attachment_id = attachment["id"]
        assert attachment["task_id"] == task_id
        assert attachment["file"]["size"] == len(payload)
        download_path = f"/tasks/{task_id}/attachments/{attachment_id}"
        assert request("GET", download_path).content == payload

        owned, _ = docker_app.owned_container()
        assert owned["Id"] == before["Id"] and owned["Image"] == before["Image"]
        docker_app.compose("up", "-d", "--force-recreate", "--pull", "never")
        docker_app.ready()
        after, after_destination = docker_app.owned_container()
        assert after["Id"] != before["Id"], "必须重建容器，不能只重启进程"
        assert after["Image"] == before["Image"]
        assert after["State"]["Running"] and after_destination == destination

        api.headers.pop("Authorization")
        token = request("POST", "/login", json=credentials).json()["token"]
        assert token
        api.headers["Authorization"] = "Bearer " + token
        saved_project = request("GET", f"/projects/{project_id}").json()
        assert saved_project["id"] == project_id and saved_project["title"] == project_title
        saved_task = request("GET", f"/tasks/{task_id}").json()
        assert saved_task["id"] == task_id and saved_task["title"] == task_title
        assert saved_task["project_id"] == project_id
        attachments = request("GET", f"/tasks/{task_id}/attachments").json()
        assert any(item["id"] == attachment_id and item["task_id"] == task_id for item in attachments)
        assert request("GET", download_path).content == payload
