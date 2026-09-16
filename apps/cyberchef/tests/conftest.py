import os

import pytest


@pytest.fixture(scope="session")
def base_url():
    return os.environ["CORENOVA_APP_URL"]
