"""Observed n8n 2.38.6 owner, saved workflow and manual-execution APIs."""
import json
from urllib.parse import urlsplit
from uuid import uuid4

import requests

from corenova.business_fixtures import eventually

EMAIL = "verify@corenovalaunch.test"
PASSWORD = "CoreNova-Verify-2026!"
TRIGGER = "When clicking Execute workflow"
OUTPUT = "Verification output"


def owner_session(base_url):
    session = requests.Session()
    response = session.get(base_url + "/rest/settings", timeout=20)
    assert response.status_code == 200
    if response.json()["data"]["userManagement"]["showSetupOnFirstLoad"]:
        response = session.post(base_url + "/rest/owner/setup", json={"email": EMAIL,
                                "firstName": "CoreNova", "lastName": "Verify", "password": PASSWORD}, timeout=30)
        assert response.status_code == 200, response.text[:500]
        assert response.json()["data"]["email"] == EMAIL
    response = session.post(base_url + "/rest/login", json={"emailOrLdapLoginId": EMAIL,
                            "password": PASSWORD}, timeout=20)
    assert response.status_code == 200, response.text[:500]
    assert response.json()["data"]["email"] == EMAIL
    assert session.cookies.get("n8n-auth"), "No authenticated session established"
    return session


def save_workflow(session, base_url):
    marker = "p03-" + uuid4().hex
    workflow = {"name": "CoreNova verified workflow " + marker, "nodes": [
        {"id": "trigger", "name": TRIGGER, "type": "n8n-nodes-base.manualTrigger", "typeVersion": 1,
         "position": [0, 0], "parameters": {}},
        {"id": "result", "name": OUTPUT, "type": "n8n-nodes-base.set", "typeVersion": 3.4,
         "position": [260, 0], "parameters": {"assignments": {"assignments": [
             {"id": "marker", "name": "marker", "value": marker, "type": "string"}]}, "options": {}}},
    ], "connections": {TRIGGER: {"main": [[{"node": OUTPUT, "type": "main", "index": 0}]]}},
        "settings": {"executionOrder": "v1"}}
    response = session.post(base_url + "/rest/workflows", json=workflow, timeout=30)
    assert response.status_code == 200, response.text[:500]
    saved = response.json()["data"]
    assert saved["id"] and saved["name"] == workflow["name"]
    return saved, marker


def unflatten(value):
    """Decode n8n's observed flatted table; resolve references, not substring matches."""
    if not isinstance(value, str):
        return value
    table = json.loads(value)
    cache = {}

    def slot(index):
        if index in cache:
            return cache[index]
        raw = table[index]
        if isinstance(raw, dict):
            result = cache[index] = {}
            result.update({key: reference(item) for key, item in raw.items()})
        elif isinstance(raw, list):
            result = cache[index] = []
            result.extend(reference(item) for item in raw)
        else:
            result = cache[index] = raw
        return result

    def reference(item):
        return slot(int(item)) if isinstance(item, str) else item

    return slot(0)


def read_execution(session, base_url, execution_id, marker):
    response = session.get(base_url + "/rest/executions/" + execution_id, timeout=20)
    assert response.status_code == 200, response.text[:500]
    execution = response.json()["data"]
    assert execution["status"] == "success" and execution["finished"] is True
    result = unflatten(execution["data"])["resultData"]
    assert result["lastNodeExecuted"] == OUTPUT
    assert result["runData"][OUTPUT][0]["data"]["main"][0][0]["json"] == {"marker": marker}
    return execution


def execute(session, base_url, workflow, marker):
    response = session.post(base_url + "/rest/workflows/" + workflow["id"] + "/run", json={
        "workflowId": workflow["id"], "startNodes": [], "triggerToStartFrom": {"name": TRIGGER}}, timeout=30)
    assert response.status_code == 200, response.text[:500]
    execution_id = response.json()["data"]["executionId"]
    eventually(lambda: read_execution(session, base_url, execution_id, marker), timeout=60)
    return execution_id


def prepare(page, slug):
    parts = urlsplit(page.url)
    base_url = f"{parts.scheme}://{parts.netloc}"
    session = owner_session(base_url)
    response = session.get(base_url + "/rest/workflows", timeout=20)
    assert response.status_code == 200
    workflows = [w for w in response.json()["data"] if w["name"].startswith("CoreNova verified workflow p03-")]
    if workflows:
        workflow = workflows[0]
    else:
        workflow, _ = save_workflow(session, base_url)
    page.context.add_cookies([{"name": c.name, "value": c.value, "url": base_url} for c in session.cookies])
    page.goto(base_url + "/workflow/" + workflow["id"], wait_until="networkidle")
    later = page.get_by_text("Set up later in Settings", exact=True)
    if later.is_visible():
        later.click()
    page.locator('[data-test-id="execute-workflow-button"]').click()
    page.get_by_text("Workflow executed successfully", exact=True).wait_for(timeout=60_000)
