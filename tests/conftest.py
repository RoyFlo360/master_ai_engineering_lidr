"""Shared fixtures.

Every test gets the same hermetic ``Settings`` instance, injected into the
modules under test through ``get_settings``. Nothing here touches the network.
"""

from types import SimpleNamespace

import openai
import pytest
from fastapi.testclient import TestClient

import app.main as main
from app.config import Settings
from app.services import llm_service
from tests.factories import FakeLLM, build_settings


@pytest.fixture(autouse=True)
def settings_holder(monkeypatch) -> SimpleNamespace:
    """Point every module at one Settings instance, independent of the local .env.

    Assign ``settings_holder.current = build_settings(...)`` inside a test to
    exercise a different configuration.
    """
    holder = SimpleNamespace(current=build_settings())

    def current_settings() -> Settings:
        return holder.current

    monkeypatch.setattr(llm_service, "get_settings", current_settings)
    monkeypatch.setattr(main, "get_settings", current_settings)
    return holder


@pytest.fixture
def settings(settings_holder) -> Settings:
    """The settings the application code will see."""
    return settings_holder.current


@pytest.fixture
def llm(monkeypatch) -> FakeLLM:
    """Replace the OpenAI client with a recorder that never leaves the process."""
    fake = FakeLLM()
    monkeypatch.setattr(openai, "OpenAI", fake)
    return fake


@pytest.fixture
def client(settings_holder) -> TestClient:
    """A TestClient with the application's lifespan (startup/shutdown) run."""
    with TestClient(main.app) as test_client:
        yield test_client
