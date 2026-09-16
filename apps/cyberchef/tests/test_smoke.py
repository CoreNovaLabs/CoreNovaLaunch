import re

import requests


def test_app_and_required_assets(base_url):
    response = requests.get(base_url + "/", timeout=30)
    assert response.status_code == 200
    assert "CyberChef" in response.text
    digest = requests.get(base_url + "/sha256digest.txt", timeout=20)
    assert digest.status_code == 200
    assert re.fullmatch(r"[0-9a-f]{64}", digest.text.strip())
