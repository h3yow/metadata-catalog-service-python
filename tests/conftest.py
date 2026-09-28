"""Shared fixtures and logging: every test prints inputs and the decision basis."""
from __future__ import annotations

import logging

import pytest
from fastapi.testclient import TestClient

from app.api import create_app
from app.db import reset

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
log = logging.getLogger("test")


@pytest.fixture(autouse=True)
def fresh_db():
    reset(":memory:")
    yield


@pytest.fixture
def client():
    return TestClient(create_app())


def logged(label: str, **data):
    """Log the input and the expected decision basis for one test step."""
    log.info("CASE %s | input=%s", label, data)
