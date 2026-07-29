from types import MappingProxyType

import pytest


_RATE_LIMIT_SETTING_NAMES = (
    "_FAILURE_LIMIT",
    "_LOCKOUT_SECONDS",
    "_FAILURE_BUCKET_LIMIT",
    "_FAILURE_OVERFLOW_BUCKET_LIMIT",
)


@pytest.fixture(scope="session")
def _rate_limit_defaults():
    import server

    with server._FAILURE_STATE_LOCK:
        defaults = {
            name: getattr(server, name) for name in _RATE_LIMIT_SETTING_NAMES
        }
    return MappingProxyType(defaults)


def _reset_rate_limit_state(rate_limit_defaults):
    import server

    with server._FAILURE_STATE_LOCK:
        server._AUTH_FAILURES.clear()
        server._SHARE_UNLOCK_FAILURES.clear()
        server._AUTH_FAILURE_OVERFLOW.clear()
        server._SHARE_UNLOCK_FAILURE_OVERFLOW.clear()
        for name, default in rate_limit_defaults.items():
            setattr(server, name, default)


@pytest.fixture(autouse=True)
def _isolate_rate_limit_state(_rate_limit_defaults):
    _reset_rate_limit_state(_rate_limit_defaults)
    try:
        yield
    finally:
        _reset_rate_limit_state(_rate_limit_defaults)
