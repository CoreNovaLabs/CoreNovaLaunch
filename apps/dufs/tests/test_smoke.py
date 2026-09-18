import re
import uuid

import requests


def test_browser_and_assets(base_url):
    response = requests.get(base_url + "/", headers={"User-Agent": "Mozilla/5.0"}, timeout=20)
    assert response.status_code == 200
    assets = re.findall(r'(?:src|href)="([^\"]+\.(?:js|css))"', response.text)
    assert assets
    for path in assets:
        assert requests.get(base_url + path, timeout=20).status_code == 200


def test_public_read_only_contract(base_url):
    response = requests.get(base_url + "/?json", timeout=20)
    assert response.status_code == 200
    listing = response.json()
    assert listing["dir_exists"] is True
    assert listing["allow_upload"] is False
    assert listing["allow_delete"] is False
    assert listing["auth"] is False
    target = base_url + "/corenova-write-probe-" + uuid.uuid4().hex
    assert requests.put(target, data=b"must not be stored", timeout=20).status_code in (403, 405)
    assert requests.delete(target, timeout=20).status_code in (403, 405)
    assert requests.get(target, timeout=20).status_code == 404
