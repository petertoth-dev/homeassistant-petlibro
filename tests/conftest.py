"""Shared test setup.

The integration is imported here, before any Home Assistant test instance exists: the HA test
harness puts its own `custom_components` package on the path later, and the first import wins.
"""

from pathlib import Path

import pytest

import custom_components.petlibro  # noqa: F401  (see module docstring)

ROOT = Path(__file__).resolve().parent.parent
assert Path(custom_components.petlibro.__file__).resolve().parent == ROOT / "custom_components" / "petlibro"


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Let Home Assistant load the integration in every test."""
    return


@pytest.fixture(autouse=True)
def broker(monkeypatch):
    """No test reaches a real broker: live updates connect to a fake one (stays open)."""
    from .fakes import FakeBroker

    fake = FakeBroker()
    monkeypatch.setattr("custom_components.petlibro.push.AiomqttTransport", lambda: fake)
    return fake


@pytest.fixture
def cloud(monkeypatch):
    """The whole integration runs against FakeCloud; `cloud["api"]` is the instance."""
    from .fakes import FakeCloud

    holder = {}

    def factory(*args, **kwargs):
        holder["api"] = FakeCloud()
        return holder["api"]

    monkeypatch.setattr("custom_components.petlibro.hub.PetLibroAPI", factory)
    return holder
