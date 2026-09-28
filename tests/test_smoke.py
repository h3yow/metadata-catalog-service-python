"""Trivial sanity test kept as a fast boot check."""


def test_app_builds():
    from app.api import create_app

    app = create_app()
    assert app.title == "metadata-catalog-service"
