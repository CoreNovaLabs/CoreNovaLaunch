import re

import requests
import yaml


def test_app_and_required_assets(base_url):
    response = requests.get(base_url + "/", timeout=30)
    assert response.status_code == 200
    assert "Homer" in response.text
    config = requests.get(base_url + "/assets/config.yml", timeout=20)
    assert config.status_code == 200
    assert isinstance(yaml.safe_load(config.text)["services"], list)
    version = requests.get(base_url + "/VERSION", timeout=20)
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.text.strip())
