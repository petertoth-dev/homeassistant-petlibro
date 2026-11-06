"""Test doubles: a certificate authority, the PETLIBRO cloud and an MQTT broker."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

MEMBER_ID = 100000001
DUO_SERIAL = "AF0901030388412DUO1"
GRANARY_SERIAL = "AF01GRANARY0AD74"
POLAR_SERIAL = "AF05POLAR00017SL"
LUNA, MILO = 1001, 1002


def ms(moment: datetime) -> int:
    """Epoch milliseconds."""
    return int(moment.timestamp() * 1000)


class TestAuthority:
    """A CA that signs CSRs the way PETLIBRO's cloud does, with chosen validity."""

    __test__ = False  # not a pytest test class

    def __init__(self, ca_valid_days: int = 900) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "DesignLibroCA")])
        now = datetime.now(timezone.utc)
        self.certificate = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=ca_valid_days))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .sign(self.key, hashes.SHA256())
        )
        self.signed: list[x509.CertificateSigningRequest] = []

    @property
    def ca_pem(self) -> str:
        return self.certificate.public_bytes(serialization.Encoding.PEM).decode()

    def sign(self, csr_pem: str, valid_days: int = 1460) -> str:
        csr = x509.load_pem_x509_csr(csr_pem.encode())
        self.signed.append(csr)
        now = datetime.now(timezone.utc)
        certificate = (
            x509.CertificateBuilder()
            .subject_name(csr.subject)
            .issuer_name(self.certificate.subject)
            .public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=valid_days))
            .sign(self.key, hashes.SHA256())
        )
        return certificate.public_bytes(serialization.Encoding.PEM).decode()


class FakeSession:
    """The bits of PetLibroSession the live-update code uses."""

    def __init__(self, client_id: str | None = None) -> None:
        self.client_id = client_id
        self.logins = 0
        self.login_required = False
        self.on_login_required = None

    async def re_login(self) -> str:
        self.logins += 1
        self.client_id = f"client{self.logins:07d}"
        return f"token-{self.logins}"

    async def async_ensure_client_id(self) -> str:
        if not self.client_id:
            await self.re_login()
        return self.client_id

    async def async_refresh_login(self, stale_client_id: str) -> str:
        if self.client_id == stale_client_id:
            await self.re_login()
        return self.client_id


@dataclass
class FakeCertificateApi:
    """`PetLibroAPI` stand-in for certificate and broker calls."""

    authority: TestAuthority = field(default_factory=TestAuthority)
    certificate_days: int = 1460
    ca_header: str = "Subject: CN=DesignLibroCA\nIssuer: CN=DesignLibroCA\n"
    session: FakeSession = field(default_factory=FakeSession)
    ca_calls: int = 0
    generate_calls: int = 0

    async def certificate_authority(self) -> dict[str, Any]:
        self.ca_calls += 1
        return {"caCertificate": self.ca_header + self.authority.ca_pem, "expireTime": 0}

    async def generate_certificate(self, csr_pem: str) -> dict[str, Any]:
        self.generate_calls += 1
        return {
            "certificate": self.authority.sign(csr_pem, self.certificate_days),
            "serialNumber": f"serial-{self.generate_calls}",
            "responseFormat": "PEM",
        }

    async def app_config(self) -> dict[str, Any]:
        return {"mqttTlsEnable": True, "appMqttsHosts": [{"host": "mqtt.example.invalid", "port": 8883}]}


def meal_record(record_id, start: datetime, seconds: int, intake: float, pets_info, show_ups=None) -> dict[str, Any]:
    """A MEAL_RECORD shaped like the ones read from Peter's Vision Duo."""
    end = start + timedelta(seconds=seconds)
    record = {
        "id": record_id, "type": "MEAL_RECORD", "eventType": "PET_EATING_RECORD_EVENT",
        "recordTime": ms(start), "petEatingStartTime": ms(start), "eatingEndTime": ms(end),
        "duration": seconds // 60, "intake": intake, "petsInfo": pets_info,
    }
    if show_ups is not None:
        record["showUpPetsInfo"] = show_ups
    return record


class FakeCloud:
    """Stands in for PetLibroAPI in whole-integration tests; anything not modelled answers {}."""

    def __init__(self, *args, **kwargs) -> None:
        self.session = FakeSession()
        self.certificates = FakeCertificateApi(session=self.session)
        self.meals: list[dict[str, Any]] = []
        self.messages: list[dict[str, Any]] = []
        self.manual_feed_id: int | None = None
        self.refresh_calls: dict[str, int] = {}
        pets = [
            {"id": LUNA, "name": "Luna", "memberId": 1, "type": 2, "breedId": 7, "breedName": "Domestic Shorthair"},
            {"id": MILO, "name": "Milo", "memberId": 1, "type": 2, "breedId": 7, "breedName": "Domestic Shorthair"},
        ]
        self.pets = SimpleNamespace(
            get_list=AsyncMock(return_value={"petList": pets}),
            get_details=AsyncMock(side_effect=lambda pet_id: next(p for p in pets if p["id"] == pet_id)),
            get_bound_devices=AsyncMock(return_value=[]),
        )
        self.time_zone = "America/Los_Angeles"

    async def member_info(self):
        return {"email": "tester@example.invalid", "memberId": MEMBER_ID, "id": MEMBER_ID}

    async def list_devices(self):
        return [
            {"deviceSn": DUO_SERIAL, "productName": "Granary 2 Vision Duo", "productIdentifier": "PLAF205",
             "name": "Duo", "mac": "AA:BB:CC:DD:EE:01", "online": True},
            {"deviceSn": POLAR_SERIAL, "productName": "Polar Wet Food Feeder", "productIdentifier": "PLAF109",
             "name": "Wet Food Feeder", "mac": "AA:BB:CC:DD:EE:02", "online": True},
        ]

    async def device_real_info(self, serial):
        self.refresh_calls[serial] = self.refresh_calls.get(serial, 0) + 1
        return {"online": True}

    async def device_data_real_info(self, serial):
        return {"bowlMode": "DOUBLE_BOWL", "leftRemainingGrain": 10, "rightRemainingGrain": 20}

    async def device_wet_feeding_plan(self, serial):
        return {"manualFeedId": self.manual_feed_id} if self.manual_feed_id else {}

    async def get_device_work_record(self, serial):
        return []

    async def device_feeding_plan_list(self, serial):
        return []

    async def device_get_bound_pets(self, serial):
        return []

    async def get_device_meal_records(self, serial, start, end):
        return [record for record in self.meals if start <= record["recordTime"] <= end]

    async def device_messages(self):
        return self.messages

    async def app_config(self):
        return await self.certificates.app_config()

    async def certificate_authority(self):
        return await self.certificates.certificate_authority()

    async def generate_certificate(self, csr_pem):
        return await self.certificates.generate_certificate(csr_pem)

    def __getattr__(self, name):
        return AsyncMock(return_value={})


# --- MQTT broker -----------------------------------------------------------------------------

def push_payload(command: str = "APP_REFRESH_EVENT", event_keys: Sequence[str] = ("REAL_INFO",)) -> bytes:
    return json.dumps({"cmd": command, "eventKeys": list(event_keys), "msgId": "m1", "linkId": "l1", "ts": 1}).encode()


@dataclass
class Session:
    """What one connection attempt does: fail on connect, or deliver messages then end."""

    fail_with: Exception | None = None
    messages: list[tuple[str, bytes]] = field(default_factory=list)
    end_with: Exception | None = None
    stay_open: bool = False  # keep the connection until it is cancelled


class FakeConnection:
    def __init__(self, session: Session, broker: FakeBroker) -> None:
        self._session = session
        self._broker = broker

    async def subscribe(self, topics: Sequence[str]) -> None:
        self._broker.subscriptions.append(list(topics))

    async def messages(self) -> AsyncIterator[tuple[str, bytes]]:
        for message in self._session.messages:
            yield message
            await asyncio.sleep(0)
        if self._session.end_with is not None:
            raise self._session.end_with
        if self._session.stay_open:
            self._broker.open.set()
            await asyncio.Event().wait()


class FakeBroker:
    """`MqttTransport` that plays scripted sessions; after the script, connections stay open."""

    def __init__(self, *sessions: Session) -> None:
        self.sessions = list(sessions)
        self.credentials: list[Any] = []
        self.subscriptions: list[list[str]] = []
        self.open = asyncio.Event()

    @asynccontextmanager
    async def connect(self, credentials) -> AsyncIterator[FakeConnection]:
        self.credentials.append(credentials)
        session = self.sessions.pop(0) if self.sessions else Session(stay_open=True)
        if session.fail_with is not None:
            raise session.fail_with
        yield FakeConnection(session, self)
