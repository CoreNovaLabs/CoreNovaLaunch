"""Real loopback HTTP outage/recovery + delivered webhooks, restart and cold restore."""
import json
import tarfile
from uuid import uuid4

from scenario_setup import ROOT, owner_page, socket_call

from corenova.business_fixtures import LocalWebhook, eventually


def test_monitor_notifications_restart_and_cold_restore(base_url, browser_page, docker_app):
    name = "P03 " + uuid4().hex
    page = browser_page
    owner_page(page, base_url)
    with LocalWebhook(docker_app) as sink:
        notification = socket_call(page, "addNotification", {
            "name": name + " local webhook", "type": "webhook", "isDefault": False,
            "httpMethod": "post", "webhookURL": sink.url + "/hook", "webhookContentType": "json",
        }, None)
        notification_id = str(notification["id"])
        result = socket_call(page, "add", {
            "type": "http", "name": name, "url": sink.url + "/target", "method": "GET",
            "interval": 20, "retryInterval": 20, "maxretries": 0, "resendInterval": 0,
            "notificationIDList": {notification_id: True}, "accepted_statuscodes": ["200-299"],
            "timeout": 5, "maxredirects": 0, "expiryNotification": False, "domainExpiryNotification": False,
            "conditions": [], "kafkaProducerBrokers": [], "kafkaProducerSaslOptions": {"mechanism": "None"},
            "rabbitmqNodes": [],
        })
        monitor_id = result["monitorID"]
        _failure_recovery(page, sink, monitor_id, docker_app, "initial")
    docker_app.restart()
    with LocalWebhook(docker_app) as sink:
        with page.context.browser.new_context() as context:
            fresh_page = context.new_page()
            owner_page(fresh_page, base_url)
            _saved(fresh_page, monitor_id, name, notification_id)
            _failure_recovery(fresh_page, sink, monitor_id, docker_app, "restart")
    with docker_app.restored() as replica:
        with tarfile.open(replica.archive) as archive:
            members = archive.getmembers()
            assert any(m.name.endswith("kuma.db") for m in members)
            config = next(m for m in members if m.name.endswith("db-config.json"))
            assert json.load(archive.extractfile(config))["type"] == "sqlite"
        with LocalWebhook(replica) as sink, page.context.browser.new_context() as context:
            restored_page = context.new_page()
            owner_page(restored_page, replica.base_url)
            monitor = _saved(restored_page, monitor_id, name, notification_id)
            _failure_recovery(restored_page, sink, monitor_id, docker_app, "restore")
            socket_call(restored_page, "editMonitor", {**monitor, "name": name + " restored"})
            assert socket_call(restored_page, "getMonitor", monitor_id)["monitor"]["name"] == name + " restored"
    # Keep only this owned loopback fixture alive for runner screenshots; compose
    # teardown kills it. No notifications go to any external service.
    with LocalWebhook(docker_app, keep_running=True) as sink:
        owner_page(page, base_url)
        _saved(page, monitor_id, name, notification_id)
        _wait_status(page, monitor_id, 1)


def _saved(page, monitor_id, name, notification_id):
    monitor = socket_call(page, "getMonitor", monitor_id)["monitor"]
    assert monitor["name"] == name and monitor["url"] == LocalWebhook.url + "/target"
    assert monitor["interval"] == 20 and monitor["notificationIDList"][notification_id] is True
    page.wait_for_function(f"{ROOT}.notificationList.some(n=>String(n.id)==='{notification_id}')")
    config = page.evaluate(f"JSON.parse({ROOT}.notificationList.find(n=>String(n.id)==='{notification_id}').config)")
    assert config["type"] == "webhook" and config["webhookURL"] == LocalWebhook.url + "/hook"
    return monitor


def _wait_status(page, monitor_id, status):
    page.wait_for_function(f"{ROOT}.lastHeartbeatList[{monitor_id}]?.status === {status}", timeout=90_000)


def _failure_recovery(page, sink, monitor_id, app, phase):
    _wait_status(page, monitor_id, 1)
    start = len(sink.events())
    sink.request("/down")  # The real HTTP target now returns 503, not a mocked heartbeat.
    _wait_status(page, monitor_id, 0)

    def delivered(status):
        return [event for event in sink.events()[start:]
                if event.get("monitor", {}).get("id") == monitor_id
                and event.get("heartbeat", {}).get("status") == status]

    down = eventually(lambda: delivered(0), timeout=45)[-1]
    assert "503" in down["heartbeat"]["msg"]
    sink.request("/up")
    _wait_status(page, monitor_id, 1)
    up = eventually(lambda: delivered(1), timeout=45)[-1]
    assert up["heartbeat"]["msg"] == "200 - OK"
    assert up["heartbeat"]["time"] > down["heartbeat"]["time"]
    (app.data_dir.parent / f"webhook-{monitor_id}-{phase}.json").write_text(
        json.dumps({"down": down, "up": up}, ensure_ascii=False, indent=2), encoding="utf-8")
