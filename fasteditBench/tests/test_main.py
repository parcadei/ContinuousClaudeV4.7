"""Smoke tests for the FastAPI app."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fasteditbench.main import app


def test_app_exists() -> None:
    """The FastAPI application instance should be importable and of the correct type."""
    assert app is not None
    assert isinstance(app, FastAPI)


def test_app_responds_to_requests() -> None:
    """The app should be wired up as a WSGI/ASGI-compatible handler via TestClient."""
    client = TestClient(app)
    # No routes are registered yet, so any path should return 404 (not 500).
    response = client.get("/")
    assert response.status_code == 404
