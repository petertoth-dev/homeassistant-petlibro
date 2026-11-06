"""Client certificate and CA: issue, store, reuse, renew; credentials and TLS context."""

from datetime import timedelta
import ssl

from cryptography import x509
from cryptography.x509.oid import NameOID
import pytest
from homeassistant.util import dt as dt_util

from custom_components.petlibro.exceptions import PetLibroAPIError
from custom_components.petlibro.push.certificates import (
    CertificateManager,
    CertificateStore,
    ClientCertificate,
    build_key_and_csr,
    normalize_pem,
)
from custom_components.petlibro.push.credentials import (
    BrokerEndpoint,
    PushCredentialProvider,
    build_tls_context,
)

from .fakes import MEMBER_ID, FakeCertificateApi, FakeSession, TestAuthority


def manager(hass, api, entry_id="entry1"):
    return CertificateManager(hass, api, CertificateStore(hass, entry_id))


def test_csr_has_the_subject_petlibro_signs():
    key_pem, csr_pem = build_key_and_csr(MEMBER_ID)
    csr = x509.load_pem_x509_csr(csr_pem.encode())
    assert [(a.oid, a.value) for a in csr.subject] == [
        (NameOID.SERIAL_NUMBER, str(MEMBER_ID)), (NameOID.COMMON_NAME, "DesignLibro"),
    ]
    assert csr.is_signature_valid and csr.public_key().key_size == 2048
    assert key_pem.startswith("-----BEGIN PRIVATE KEY-----")


def test_normalize_pem_drops_the_text_header():
    assert normalize_pem("Subject: CN=x\n-----BEGIN CERTIFICATE-----\nAA\n").startswith("-----BEGIN")
    with pytest.raises(ValueError):
        normalize_pem("no pem here")


async def test_first_use_issues_a_certificate_and_stores_it_privately(hass, hass_storage):
    api = FakeCertificateApi()
    certificate = await manager(hass, api).async_get(MEMBER_ID)
    assert (api.ca_calls, api.generate_calls) == (1, 1)
    assert certificate.member_id == MEMBER_ID and certificate.serial_number == "serial-1"
    assert certificate.ca_pem.startswith("-----BEGIN")  # header from the API stripped
    assert x509.load_pem_x509_certificate(certificate.certificate_pem.encode()).subject.get_attributes_for_oid(
        NameOID.SERIAL_NUMBER)[0].value == str(MEMBER_ID)
    stored = hass_storage["petlibro.entry1.mqtt_certificate"]["data"]
    assert stored["certificate_pem"] == certificate.certificate_pem


def test_store_is_private_and_atomic(hass):
    # private=True makes Home Assistant write the file with mode 0600 (helpers/json.py);
    # the test harness keeps Store data in memory, so the flags are checked directly.
    store = CertificateStore(hass, "entry1")._store
    assert store._private and store._atomic_writes
    assert store.key == "petlibro.entry1.mqtt_certificate"


async def test_a_valid_certificate_is_reused(hass, hass_storage):
    api = FakeCertificateApi()
    first = await manager(hass, api).async_get(MEMBER_ID)
    second_manager = manager(hass, api)  # e.g. after a restart: loads from the store
    assert await second_manager.async_get(MEMBER_ID) == first
    assert (api.ca_calls, api.generate_calls) == (1, 1)


async def test_an_expiring_certificate_is_renewed(hass, hass_storage):
    api = FakeCertificateApi(certificate_days=20)  # inside the 30-day window
    certificates = manager(hass, api)
    first = await certificates.async_get(MEMBER_ID)
    assert certificates.renewal_due()
    api.certificate_days = 1460
    renewed = await certificates.async_get(MEMBER_ID)
    assert renewed.certificate_pem != first.certificate_pem and api.generate_calls == 2
    assert not certificates.renewal_due()


async def test_an_expiring_ca_is_refreshed_without_a_new_certificate(hass, hass_storage):
    api = FakeCertificateApi(authority=TestAuthority(ca_valid_days=10))
    certificates = manager(hass, api)
    first = await certificates.async_get(MEMBER_ID)
    assert certificates.renewal_due()
    refreshed = await certificates.async_get(MEMBER_ID)
    assert api.ca_calls == 2 and api.generate_calls == 1
    assert refreshed.certificate_pem == first.certificate_pem


async def test_a_different_account_gets_its_own_certificate(hass, hass_storage):
    api = FakeCertificateApi()
    certificates = manager(hass, api)
    await certificates.async_get(MEMBER_ID)
    other = await certificates.async_get(123)
    assert other.member_id == 123 and api.generate_calls == 2


async def test_invalidate_forces_a_new_certificate_and_ca(hass, hass_storage):
    api = FakeCertificateApi()
    certificates = manager(hass, api)
    await certificates.async_get(MEMBER_ID)
    certificates.invalidate()
    assert certificates.renewal_due()
    await certificates.async_get(MEMBER_ID)
    assert (api.ca_calls, api.generate_calls) == (2, 2)
    assert not certificates.renewal_due()


async def test_an_unreadable_store_means_a_new_certificate(hass, hass_storage):
    hass_storage["petlibro.entry1.mqtt_certificate"] = {"version": 1, "key": "petlibro.entry1.mqtt_certificate",
                                                        "data": {"member_id": MEMBER_ID, "certificate_pem": "junk"}}
    api = FakeCertificateApi()
    await manager(hass, api).async_get(MEMBER_ID)
    assert api.generate_calls == 1


@pytest.mark.parametrize("answer", [{}, {"certificate": "no pem"}])
async def test_a_bad_certificate_answer_raises(hass, hass_storage, answer):
    api = FakeCertificateApi()

    async def generate(_csr):
        return answer
    api.generate_certificate = generate
    with pytest.raises(PetLibroAPIError):
        await manager(hass, api).async_get(MEMBER_ID)


async def test_a_bad_ca_answer_raises(hass, hass_storage):
    api = FakeCertificateApi()

    async def authority():
        return {"caCertificate": None}
    api.certificate_authority = authority
    with pytest.raises(PetLibroAPIError):
        await manager(hass, api).async_get(MEMBER_ID)


async def test_remove_deletes_the_stored_certificate(hass, hass_storage):
    await manager(hass, FakeCertificateApi()).async_get(MEMBER_ID)
    await CertificateStore(hass, "entry1").async_remove()
    assert "petlibro.entry1.mqtt_certificate" not in hass_storage


def test_certificate_repr_hides_the_secrets():
    certificate = ClientCertificate(MEMBER_ID, "s", private_key_pem="KEY", certificate_pem="CERT", ca_pem="CA")
    assert "KEY" not in repr(certificate) and "CERT" not in repr(certificate)


# --- credentials ---------------------------------------------------------------------------

@pytest.mark.parametrize(("config", "expected"), [
    ({"appMqttsHosts": [{"host": "broker.example", "port": 8884}]}, BrokerEndpoint("broker.example", 8884)),
    ({"appMqttsHosts": [{"host": 1}]}, BrokerEndpoint("mqtt.us.petlibro.com", 8883)),
    ({}, BrokerEndpoint("mqtt.us.petlibro.com", 8883)),
])
def test_broker_from_app_config(config, expected):
    assert BrokerEndpoint.from_app_config(config) == expected


async def test_tls_context_trusts_the_ca_and_presents_the_certificate(hass, hass_storage):
    certificate = await manager(hass, FakeCertificateApi()).async_get(MEMBER_ID)
    context = build_tls_context(certificate)
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert [c["subject"] for c in context.get_ca_certs()] == [((("commonName", "DesignLibroCA"),),)]


async def test_provider_builds_credentials_from_the_login_and_certificate(hass, hass_storage):
    api = FakeCertificateApi(session=FakeSession())
    provider = PushCredentialProvider(hass, api, manager(hass, api), MEMBER_ID)
    credentials = await provider.async_get()
    assert credentials.client_id == "client0000001" and api.session.logins == 1
    assert credentials.endpoint == BrokerEndpoint("mqtt.example.invalid", 8883)
    assert "client0000001" not in repr(credentials)

    await provider.async_login_rejected(credentials)
    assert api.session.logins == 2  # stale id → new login
    await provider.async_login_rejected(credentials)
    assert api.session.logins == 2  # already replaced → no third login

    provider.certificate_rejected()
    await provider.async_get()
    assert api.generate_calls == 2 and api.ca_calls == 2


async def test_certificate_due_follows_the_clock(hass, hass_storage):
    certificate = await manager(hass, FakeCertificateApi(certificate_days=60)).async_get(MEMBER_ID)
    now = dt_util.utcnow()
    assert not certificate.certificate_due(now)
    assert certificate.certificate_due(now + timedelta(days=31))


async def test_an_unusable_key_is_a_certificate_error_and_gets_reissued(hass, hass_storage):
    from dataclasses import replace

    from custom_components.petlibro.push.errors import PushCertificateError

    api = FakeCertificateApi(session=FakeSession())
    certificates = manager(hass, api)
    provider = PushCredentialProvider(hass, api, certificates, MEMBER_ID)
    await provider.async_get()
    certificates._current = replace(certificates._current, private_key_pem="-----BEGIN PRIVATE KEY-----\nbroken\n")
    with pytest.raises(PushCertificateError):
        await provider.async_get()
    provider.certificate_rejected()
    assert (await provider.async_get()).tls_context is not None
    assert api.generate_calls == 2
