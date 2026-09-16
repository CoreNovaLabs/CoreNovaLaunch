import requests


def test_app_and_required_assets(base_url):
    response = requests.get(base_url + "/", timeout=30)
    assert response.status_code == 200
    assert "Flowchart Maker" in response.text
    bundle = requests.get(base_url + "/js/app.min.js", timeout=30)
    assert bundle.status_code == 200
    assert 'EditorUi.VERSION=' in bundle.text
