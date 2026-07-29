import pytest


_FAILURE_STATE_NAMES = (
    "_AUTH_FAILURES",
    "_SHARE_UNLOCK_FAILURES",
    "_AUTH_FAILURE_OVERFLOW",
    "_SHARE_UNLOCK_FAILURE_OVERFLOW",
)


def _pollute_rate_limit_state(server, rate_limit_defaults):
    with server._FAILURE_STATE_LOCK:
        for name in _FAILURE_STATE_NAMES:
            getattr(server, name)[name] = (1, 1.0)
        for name in rate_limit_defaults:
            setattr(server, name, 1)


def _assert_rate_limit_defaults(server, rate_limit_defaults):
    with server._FAILURE_STATE_LOCK:
        for name in _FAILURE_STATE_NAMES:
            assert getattr(server, name) == {}
        for name, default in rate_limit_defaults.items():
            assert getattr(server, name) == default


@pytest.fixture(scope="module", autouse=True)
def _pollute_before_autouse_fixture_setup(_rate_limit_defaults):
    import server

    _pollute_rate_limit_state(server, _rate_limit_defaults)


def test_autouse_fixture_setup_restores_server_defaults(_rate_limit_defaults):
    import server

    _assert_rate_limit_defaults(server, _rate_limit_defaults)


def test_autouse_fixture_teardown_restores_server_defaults(_rate_limit_defaults):
    import conftest
    import server

    fixture_runner = conftest._isolate_rate_limit_state.__wrapped__(
        _rate_limit_defaults,
    )
    next(fixture_runner)
    _pollute_rate_limit_state(server, _rate_limit_defaults)

    with pytest.raises(StopIteration):
        next(fixture_runner)

    _assert_rate_limit_defaults(server, _rate_limit_defaults)
