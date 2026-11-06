"""The live-updates connection loop against a scripted broker."""

import asyncio
from dataclasses import dataclass, field

import pytest

from custom_components.petlibro.exceptions import PetLibroAPIError
from custom_components.petlibro.push.backoff import ExponentialBackoff
from custom_components.petlibro.push.client import PushClient
from custom_components.petlibro.push.transport import (
    PushCertificateError,
    PushConnectionError,
    PushLoginRefused,
)

from .fakes import FakeBroker, Session, push_payload

TOPIC = "dl/PLAF109/SN1/app/event/sub"


@dataclass
class FakeProvider:
    """Hands out numbered credentials and records what the client reported."""

    fail_with: list[Exception] = field(default_factory=list)
    issued: int = 0
    rejected_logins: list[str] = field(default_factory=list)
    rejected_certificates: int = 0

    async def async_get(self):
        if self.fail_with:
            raise self.fail_with.pop(0)
        self.issued += 1
        return f"credentials-{self.issued}"

    async def async_login_rejected(self, credentials):
        self.rejected_logins.append(credentials)

    def certificate_rejected(self):
        self.rejected_certificates += 1


class Harness:
    """Runs a PushClient in a task with an instant, recorded sleep."""

    def __init__(self, broker, provider=None, topics=("dl/member/1/sub", "dl/PLAF109/SN1/app/+/sub")):
        self.broker = broker
        self.provider = provider or FakeProvider()
        self.received = []
        self.delays = []
        self.states = []
        self.client = PushClient(
            broker, self.provider, lambda: list(topics), self._on_message,
            backoff=ExponentialBackoff(minimum=5, maximum=40), sleep=self._sleep,
        )
        self.client.add_connected_listener(self.states.append)
        self.task = None
        self.fail_handler = False

    async def _on_message(self, message):
        if self.fail_handler:
            self.fail_handler = False
            raise RuntimeError("handler bug")
        self.received.append(message)

    async def _sleep(self, delay):
        self.delays.append(delay)
        await asyncio.sleep(0)

    async def __aenter__(self):
        self.task = asyncio.ensure_future(self.client.async_run())
        return self

    async def __aexit__(self, *exc):
        self.task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await self.task
        assert not self.client.connected

    async def until(self, condition, rounds=200):
        for _ in range(rounds):
            if condition():
                return
            await asyncio.sleep(0)
        raise AssertionError("condition not reached")


async def test_connects_subscribes_and_dispatches(hass):
    broker = FakeBroker(Session(messages=[(TOPIC, push_payload()), (TOPIC, b"junk"), ("other/topic", b"{}")],
                                stay_open=True))
    async with Harness(broker) as harness:
        await harness.until(lambda: broker.open.is_set())
        assert harness.client.connected and harness.states == [True]
        assert broker.subscriptions == [["dl/member/1/sub", "dl/PLAF109/SN1/app/+/sub"]]
        assert [m.command for m in harness.received] == ["APP_REFRESH_EVENT"]  # junk and foreign topics dropped
        assert broker.credentials == ["credentials-1"]
    assert harness.states == [True, False]


async def test_a_failing_handler_does_not_end_the_connection(hass):
    broker = FakeBroker(Session(messages=[(TOPIC, push_payload("A")), (TOPIC, push_payload("B"))], stay_open=True))
    harness = Harness(broker)
    harness.fail_handler = True
    async with harness:
        await harness.until(lambda: broker.open.is_set())
        assert [m.command for m in harness.received] == ["B"]
        assert len(broker.credentials) == 1


async def test_reconnects_after_the_broker_closes_and_resets_the_backoff(hass):
    broker = FakeBroker(
        Session(fail_with=PushConnectionError("down")),
        Session(fail_with=PushConnectionError("down")),
        Session(messages=[(TOPIC, push_payload())]),  # connects, then the broker closes it
        Session(stay_open=True),
    )
    async with Harness(broker) as harness:
        await harness.until(lambda: broker.open.is_set())
        assert harness.delays == [5, 10, 5]  # doubled while failing, reset after a good connection
        assert len(broker.credentials) == 4
        assert harness.states == [True, False, True]


async def test_a_dropped_connection_while_listening_is_retried(hass):
    broker = FakeBroker(Session(end_with=PushConnectionError("socket closed")), Session(stay_open=True))
    async with Harness(broker) as harness:
        await harness.until(lambda: broker.open.is_set())
        assert harness.delays == [5]


async def test_a_refused_login_refreshes_the_login(hass):
    broker = FakeBroker(Session(fail_with=PushLoginRefused("Not authorized")), Session(stay_open=True))
    async with Harness(broker) as harness:
        await harness.until(lambda: broker.open.is_set())
        assert harness.provider.rejected_logins == ["credentials-1"]
        assert broker.credentials == ["credentials-1", "credentials-2"]


async def test_a_refused_login_whose_refresh_fails_is_retried_later(hass):
    provider = FakeProvider()

    async def failing_refresh(credentials):
        raise PetLibroAPIError("login failed")
    provider.async_login_rejected = failing_refresh
    broker = FakeBroker(Session(fail_with=PushLoginRefused("Not authorized")), Session(stay_open=True))
    async with Harness(broker, provider) as harness:
        await harness.until(lambda: broker.open.is_set())
        assert harness.delays == [5]


async def test_a_tls_failure_renews_the_certificate(hass):
    broker = FakeBroker(Session(fail_with=PushCertificateError("bad certificate")), Session(stay_open=True))
    async with Harness(broker) as harness:
        await harness.until(lambda: broker.open.is_set())
        assert harness.provider.rejected_certificates == 1


async def test_credentials_that_cannot_be_built_are_retried(hass):
    provider = FakeProvider(fail_with=[PetLibroAPIError("cloud down"), OSError("dns")])
    broker = FakeBroker(Session(stay_open=True))
    async with Harness(broker, provider) as harness:
        await harness.until(lambda: broker.open.is_set())
        assert harness.delays == [5, 10] and broker.credentials == ["credentials-1"]


async def test_request_reconnect_drops_and_reconnects(hass):
    broker = FakeBroker(Session(stay_open=True), Session(stay_open=True))
    async with Harness(broker) as harness:
        await harness.until(lambda: broker.open.is_set())
        broker.open.clear()
        harness.client.request_reconnect()
        await harness.until(lambda: len(broker.credentials) == 2 and broker.open.is_set())
        assert harness.states == [True, False, True]


async def test_removing_a_connected_listener(hass):
    broker = FakeBroker(Session(stay_open=True))
    harness = Harness(broker)
    calls = []
    remove = harness.client.add_connected_listener(calls.append)
    remove()
    async with harness:
        await harness.until(lambda: broker.open.is_set())
    assert calls == []
