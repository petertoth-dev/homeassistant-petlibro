"""Topics and payloads of the app's MQTT channel; reconnect delays; error translation."""

import ssl

import aiomqtt
import pytest

from custom_components.petlibro.push.backoff import ExponentialBackoff
from custom_components.petlibro.push.messages import (
    TopicScope,
    device_topic,
    member_topic,
    parse_message,
    parse_topic,
)
from custom_components.petlibro.push.transport import (
    PushCertificateError,
    PushConnectionError,
    PushLoginRefused,
    translate_error,
)
from paho.mqtt.client import convert_connack_rc_to_reason_code


def test_subscription_topics():
    assert member_topic(190705099) == "dl/member/190705099/sub"
    assert device_topic("PLAF205", "SN1") == "dl/PLAF205/SN1/app/+/sub"


@pytest.mark.parametrize(("topic", "expected"), [
    ("dl/member/190705099/sub", (TopicScope.MEMBER, None, None, None)),
    ("dl/PLAF109/SN1/app/event/sub", (TopicScope.DEVICE, "PLAF109", "SN1", "event")),
])
def test_parse_topic(topic, expected):
    info = parse_topic(topic)
    assert (info.scope, info.product_identifier, info.serial, info.channel) == expected


@pytest.mark.parametrize("topic", ["dl/member/1/post", "dl/PLAF109/SN1/device/event/sub", "x/y", "dl//SN1/app/e/sub", ""])
def test_other_topics_are_not_ours(topic):
    assert parse_topic(topic) is None


def test_parse_refresh_message():
    message = parse_message(
        "dl/PLAF109/SN1/app/event/sub",
        b'{"cmd":"APP_REFRESH_EVENT","eventKeys":["REAL_INFO","WORK_RECORD"],"msgId":"1","linkId":"2","ts":3}',
    )
    assert message.command == "APP_REFRESH_EVENT"
    assert message.event_keys == ("REAL_INFO", "WORK_RECORD")
    assert message.serial == "SN1"


@pytest.mark.parametrize(("topic", "payload"), [
    ("dl/PLAF109/SN1/app/event/sub", b"not json"),
    ("dl/PLAF109/SN1/app/event/sub", b"[1, 2]"),
    ("some/other/topic", b'{"cmd": "X"}'),
])
def test_unusable_messages_are_dropped(topic, payload):
    assert parse_message(topic, payload) is None


def test_message_without_command_or_keys():
    message = parse_message("dl/member/1/sub", b'{"eventKeys": "not a list"}')
    assert message.command is None and message.event_keys == () and message.serial is None


def test_backoff_doubles_to_the_cap_and_resets():
    backoff = ExponentialBackoff(minimum=5, maximum=30)
    assert [backoff.next_delay() for _ in range(5)] == [5, 10, 20, 30, 30]
    backoff.reset()
    assert backoff.next_delay() == 5
    with pytest.raises(ValueError):
        ExponentialBackoff(minimum=10, maximum=5)


@pytest.mark.parametrize("code", [4, 5, convert_connack_rc_to_reason_code(4), convert_connack_rc_to_reason_code(5)])
def test_refused_login_is_recognised(code):
    assert isinstance(translate_error(aiomqtt.MqttCodeError(code)), PushLoginRefused)


def test_tls_failure_is_a_certificate_error():
    try:
        try:
            raise ssl.SSLError("certificate verify failed")
        except ssl.SSLError as err:
            raise aiomqtt.MqttError(str(err)) from None  # aiomqtt hides the cause like this
    except aiomqtt.MqttError as error:
        assert isinstance(translate_error(error), PushCertificateError)


@pytest.mark.parametrize("error", [aiomqtt.MqttError("Disconnected"), aiomqtt.MqttCodeError(3, "Server unavailable")])
def test_everything_else_is_a_connection_error(error):
    assert isinstance(translate_error(error), PushConnectionError)
