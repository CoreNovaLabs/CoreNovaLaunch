import requests


def test_app_and_required_assets(base_url):
    response = requests.get(base_url + "/", timeout=30)
    assert response.status_code == 200
    assert "IT Tools" in response.text
    manifest = requests.get(base_url + "/manifest.webmanifest", timeout=20)
    assert manifest.status_code == 200
    assert manifest.json()["name"] == "IT Tools"
