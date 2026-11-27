"""Run with the separately installed baseline wheel. Its code and templates own this render."""

import sys

from starlette.testclient import TestClient

from genealogy.web.app import create_app
from genealogy.web.repository import PostgresStore

public, family, page, derivative = sys.argv[1:]
client = TestClient(
    create_app(store=PostgresStore(public, family), authorize_family=lambda _: True)
)
response = client.get(page)
response.raise_for_status()
assert client.get("/assets/" + derivative).status_code == 404
sys.stdout.buffer.write(response.content)
