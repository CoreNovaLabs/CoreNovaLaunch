"""首启页面：断言均为 n8nio/n8n:2.38.6 真容器内实测事实。

全新容器首启：/healthz 200 {"status":"ok"}；/ 返回编辑器 SPA 外壳
（<title>n8n.io - Workflow Automation</title> + <div id="app">）。

CI 实测（runner 34569985745 / 34571582970）：在 pytest 的首个浏览器会话里，
n8n 前端可能长时间不完成客户端挂载（>180s 无 <input>），而晚于本测试启动的
管线截图浏览器（同参数）总能渲染出 "Set up owner account"——属于 CI 环境
的首屏冷启动病态，不是应用缺陷。因此视觉证据由截图门禁承担（截图是硬门禁，
漏拍/错拍都会让 verification 失败），本测试退守确定性 HTTP 层断言。
"""

from __future__ import annotations


def test_healthz_ready(base_url):
    import requests

    r = requests.get(base_url + "/healthz", timeout=20)
    assert r.status_code == 200, f"/healthz 返回 {r.status_code}"
    assert r.json().get("status") == "ok", f"/healthz 状态异常: {r.text[:100]}"


def test_editor_shell_served(base_url):
    # CI 实测：n8n 首启要跑数十条 SQLite 迁移，期间 / 在 404("Cannot GET /")
    # 与 200 之间闪断（healthz 先行），探针可能恰好命中 200 而 pytest 命中 404。
    # 因此这里轮询等待外壳稳定，而不是单次请求定生死。
    import time

    import requests

    deadline = time.time() + 240
    last_status = None
    while time.time() < deadline:
        try:
            r = requests.get(base_url + "/", timeout=10)
            last_status = r.status_code
            if r.status_code == 200 and '<div id="app">' in r.text:
                assert "n8n" in r.text.lower(), "返回的不是 n8n 页面"
                return
        except requests.RequestException:
            pass
        time.sleep(5)
    raise AssertionError(f"n8n 编辑器外壳 240s 内未稳定可访问（最后状态 {last_status}）")


def test_owner_workflow_execution_restart_and_cold_restore(base_url, docker_app):
    import hashlib
    import tarfile

    from scenario_setup import execute, owner_session, read_execution, save_workflow

    from corenova.util import run

    session = owner_session(base_url)
    workflow, marker = save_workflow(session, base_url)
    execution_id = execute(session, base_url, workflow, marker)

    def verify_saved_and_run(url):
        api = owner_session(url)
        response = api.get(url + "/rest/workflows/" + workflow["id"], timeout=20)
        assert response.status_code == 200
        saved = response.json()["data"]
        assert saved["name"] == workflow["name"]
        assert saved["settings"]["executionOrder"] == "v1"
        assert saved["nodes"] == workflow["nodes"]
        read_execution(api, url, execution_id, marker)
        new_id = execute(api, url, saved, marker)
        assert new_id != execution_id
        return api

    docker_app.restart()
    verify_saved_and_run(base_url)
    with docker_app.restored() as replica:
        with tarfile.open(replica.archive) as archive:
            members = archive.getmembers()
            assert any(m.name.endswith("database.sqlite") for m in members)
            config = next(m for m in members if m.name.rstrip("/").split("/")[-1] == "config")
            config_hash = hashlib.sha256(archive.extractfile(config).read()).hexdigest()
        result = run(["docker", "exec", replica.cid, "sha256sum", "/home/node/.n8n/config"])
        assert result.stdout.split()[0] == config_hash, "Encryption config must survive full-data restore"
        api = verify_saved_and_run(replica.base_url)
        response = api.patch(replica.base_url + "/rest/workflows/" + workflow["id"],
                             json={"name": workflow["name"] + " restored"}, timeout=20)
        assert response.status_code == 200, response.text[:500]
        assert response.json()["data"]["name"].endswith(" restored")
    response = session.get(base_url + "/rest/workflows/" + workflow["id"], timeout=20)
    assert response.status_code == 200 and response.json()["data"]["name"] == workflow["name"]
