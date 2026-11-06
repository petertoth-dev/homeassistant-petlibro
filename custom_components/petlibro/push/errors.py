"""Errors of the live-updates connection, by what the client should do about them."""


class PushError(Exception):
    """Base class for live-update connection errors."""


class PushLoginRefused(PushError):
    """The broker refused the client id (stale or replaced API login): log in again."""


class PushCertificateError(PushError):
    """The certificate, key or CA cannot be used or is not accepted: issue a new one."""


class PushConnectionError(PushError):
    """Network or broker problem: try again later."""
