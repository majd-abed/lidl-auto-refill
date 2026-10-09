import pytest

from config import Config, ConfigurationError


def test_safe_defaults():
    config = Config.from_env({})
    assert config.dry_run is True
    assert config.client_mode == "mock"
    assert config.threshold_gb == 0.30
    assert config.cooldown_seconds == 600


def test_environment_overrides():
    config = Config.from_env({"DRY_RUN": "FALSE", "REFILL_THRESHOLD_GB": "0.20", "VERIFY_ATTEMPTS": "2"})
    assert config.dry_run is False
    assert config.threshold_gb == 0.20
    assert config.verification_attempts == 2


def test_access_token_is_read_from_environment_and_hidden_from_repr():
    config = Config.from_env({"LIDL_ACCESS_TOKEN": "placeholder-private-token"})
    assert config.access_token == "placeholder-private-token"
    assert "placeholder-private-token" not in repr(config)


def test_session_token_alias():
    assert Config.from_env({"LIDL_SESSION_TOKEN": "placeholder-token"}).access_token == "placeholder-token"


def test_session_encryption_key_is_hidden_from_repr():
    config = Config.from_env({"LIDL_TOKEN_STATE_KEY": "private-encryption-key"})
    assert config.token_state_key == "private-encryption-key"
    assert "private-encryption-key" not in repr(config)


@pytest.mark.parametrize("path", ["", ":memory:", ".state/refill.sqlite3"])
def test_session_state_requires_a_separate_persistent_file(path):
    with pytest.raises(ConfigurationError):
        Config.from_env({"LIDL_TOKEN_STATE_PATH": path})


@pytest.mark.parametrize(
    ("name", "value"),
    [("DRY_RUN", "yes"), ("CLIENT_MODE", "browser"), ("REFILL_THRESHOLD_GB", "NaN"),
     ("REFILL_THRESHOLD_GB", "inf"), ("REFILL_THRESHOLD_GB", "-0.1"),
     ("REFILL_THRESHOLD_GB", "secret-value"), ("REFILL_COOLDOWN_SECONDS", "599"),
     ("VERIFY_ATTEMPTS", "0"), ("VERIFY_ATTEMPTS", "11"),
     ("VERIFY_INITIAL_DELAY_SECONDS", "301"), ("STATE_DB_PATH", ""),
     ("STATE_DB_PATH", ":memory:"), ("MOCK_REFILL_OUTCOME", "unknown"),
     ("LIDL_ACCESS_TOKEN", "private\nvalue"), ("HTTP_TIMEOUT_SECONDS", "0"),
     ("HTTP_TIMEOUT_SECONDS", "121")],
)
def test_invalid_configuration(name, value):
    with pytest.raises(ConfigurationError) as error:
        Config.from_env({name: value})
    assert name in str(error.value) or "verification wait" in str(error.value)
    assert "secret-value" not in str(error.value)
