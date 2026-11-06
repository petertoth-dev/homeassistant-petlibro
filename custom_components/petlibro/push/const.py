"""Constants for PETLIBRO live updates (the app's MQTT push channel)."""

from datetime import timedelta
from typing import Final

# Used when /member/app/config does not name a TLS broker.
DEFAULT_BROKER_HOST: Final = "mqtt.us.petlibro.com"
DEFAULT_BROKER_TLS_PORT: Final = 8883

KEEPALIVE_SECONDS: Final = 60

# Reconnect delays: doubled after every failed attempt, reset once connected.
RECONNECT_MIN_SECONDS: Final = 5.0
RECONNECT_MAX_SECONDS: Final = 300.0

# The CSR subject PETLIBRO signs: SERIALNUMBER=<memberId>, CN=DesignLibro (from the app).
CSR_COMMON_NAME: Final = "DesignLibro"
RSA_KEY_SIZE: Final = 2048

# Renew the client certificate or refresh the CA this long before either expires.
CERTIFICATE_RENEW_BEFORE: Final = timedelta(days=30)
CERTIFICATE_CHECK_INTERVAL: Final = timedelta(hours=24)

# A burst of pushes for one feeder becomes one refresh.
REFRESH_COOLDOWN_SECONDS: Final = 1.0

# Commands seen on the app topics.
CMD_APP_REFRESH: Final = "APP_REFRESH_EVENT"
