"""Topics and messages of PETLIBRO's app MQTT channel.

Topics (from the PETLIBRO app):
- ``dl/member/<memberId>/sub`` — account-wide messages.
- ``dl/<productIdentifier>/<deviceSn>/app/<channel>/sub`` — per device, e.g. ``.../app/event/sub``.

Payloads are JSON objects with ``cmd`` (e.g. ``APP_REFRESH_EVENT``), ``eventKeys``, ``msgId``,
``linkId`` and ``ts``. ``APP_REFRESH_EVENT`` tells the app to re-read the device.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
import json
from typing import Any

TOPIC_PREFIX = "dl"


class TopicScope(StrEnum):
    """Who a topic is about."""

    MEMBER = "member"
    DEVICE = "device"


def member_topic(member_id: int | str) -> str:
    """Subscription for the account's own messages."""
    return f"{TOPIC_PREFIX}/member/{member_id}/sub"


def device_topic(product_identifier: str, serial: str) -> str:
    """Subscription for every app channel of one device."""
    return f"{TOPIC_PREFIX}/{product_identifier}/{serial}/app/+/sub"


@dataclass(frozen=True)
class TopicInfo:
    """A parsed topic."""

    scope: TopicScope
    member_id: str | None = None
    product_identifier: str | None = None
    serial: str | None = None
    channel: str | None = None


def parse_topic(topic: str) -> TopicInfo | None:
    """Parse a PETLIBRO app topic; None for anything else."""
    parts = topic.split("/")
    if len(parts) == 4 and parts[0] == TOPIC_PREFIX and parts[1] == "member" and parts[3] == "sub":
        return TopicInfo(scope=TopicScope.MEMBER, member_id=parts[2])
    if len(parts) == 6 and parts[0] == TOPIC_PREFIX and parts[3] == "app" and parts[5] == "sub" and all(parts[1:3]):
        return TopicInfo(
            scope=TopicScope.DEVICE, product_identifier=parts[1], serial=parts[2], channel=parts[4]
        )
    return None


@dataclass(frozen=True)
class PushMessage:
    """One message from the app channel."""

    topic: TopicInfo
    command: str | None
    event_keys: tuple[str, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def serial(self) -> str | None:
        """Serial of the device the message is about (None for member messages)."""
        return self.topic.serial


def parse_message(topic: str, payload: bytes | str) -> PushMessage | None:
    """Parse a raw MQTT message; None when the topic or payload is not a PETLIBRO app message."""
    if (info := parse_topic(topic)) is None:
        return None
    try:
        data = json.loads(payload)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    command = data.get("cmd") if isinstance(data.get("cmd"), str) else None
    keys = data.get("eventKeys")
    event_keys = tuple(str(key) for key in keys) if isinstance(keys, list) else ()
    return PushMessage(topic=info, command=command, event_keys=event_keys, payload=data)
