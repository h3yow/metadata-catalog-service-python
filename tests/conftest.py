"""公共夹具：每个用例使用独立的临时 JSON 文件存储，并可同时拿到 client/service。"""

from __future__ import annotations

import logging
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from main import create_app  # noqa: E402
from app.service import CatalogService  # noqa: E402
from app.storage import CatalogStore  # noqa: E402


@pytest.fixture
def tmp_db(tmp_path: Path) -> Path:
    return tmp_path / "catalog.json"


@pytest.fixture
def service(tmp_db: Path) -> CatalogService:
    return CatalogService(CatalogStore(str(tmp_db)))


@pytest.fixture
def client(tmp_db: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("CATALOG_DB", str(tmp_db))
    monkeypatch.delenv("CATALOG_MEM", raising=False)
    with TestClient(create_app()) as c:
        yield c


@pytest.fixture(autouse=True)
def _service_logs(caplog: pytest.LogCaptureFixture) -> Iterator[None]:
    """让单测日志打印输入与判定依据。"""
    caplog.set_level(logging.INFO, logger="catalog.service")
    yield
