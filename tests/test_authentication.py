import time

from cryptography.fernet import Fernet
import pytest
import requests

from authentication import TOKEN_URL, discover_client, request_tokens
from http_client import HttpLidlClient
from lidl_client import AuthenticationExpiredError, RequestTimeoutError, UnexpectedResponseError
from session_store import SessionStateError, SessionStore, SessionTokens
from test_http_client import FakeResponse, FakeSession, allowance


def token_response(**changes):
    data = {"token_type": "Bearer", "expires_in": 3600, "access_token": "new-access", "refresh_token": "new-refresh"}
    data.update(changes)
    return FakeResponse(data=data)


def renewable_client(tmp_path, replies, *, expired=False):
    store = SessionStore(tmp_path / "session.sqlite3", Fernet.generate_key().decode())
    store.save(SessionTokens("old-access", "old-refresh", "test-client", "test-secret", 0 if expired else time.time() + 3600))
    client = HttpLidlClient(session_store=store)
    client._session.close()
    client._session = FakeSession(replies)
    return client, store


def test_expiration_renews_before_read_and_persists_new_pair(tmp_path):
    client, store = renewable_client(tmp_path, [token_response(), FakeResponse(data={"data": allowance()})], expired=True)
    assert client.get_remaining_data() == 0.20
    calls = client._session.calls
    assert calls[0][0] == TOKEN_URL
    assert calls[0][1]["json"] == {"grant_type": "refresh_token", "client_id": "test-client", "client_secret": "test-secret", "refresh_token": "old-refresh"}
    assert calls[0][1]["allow_redirects"] is False
    assert calls[1][1]["headers"]["Authorization"] == "Bearer new-access"
    assert store.access_token(lambda _: pytest.fail("Renewed session must be retained")) == "new-access"


def test_401_read_renews_once_and_retries_only_the_read(tmp_path):
    client, _ = renewable_client(tmp_path, [FakeResponse(401), token_response(), FakeResponse(data={"data": allowance()})])
    assert client.get_remaining_data() == 0.20
    assert [call[1]["json"].get("operationName", "renew") for call in client._session.calls] == ["consumptions", "renew", "consumptions"]


def test_repeated_401_read_stops_after_one_renewal(tmp_path):
    client, _ = renewable_client(tmp_path, [FakeResponse(401), token_response(), FakeResponse(401)])
    with pytest.raises(AuthenticationExpiredError):
        client.get_remaining_data()
    assert len(client._session.calls) == 3


def test_403_is_not_treated_as_permission_to_renew(tmp_path):
    client, _ = renewable_client(tmp_path, [FakeResponse(403)])
    with pytest.raises(AuthenticationExpiredError):
        client.get_remaining_data()
    assert len(client._session.calls) == 1


def test_mutation_401_never_replays_mutation_and_verification_can_renew(tmp_path):
    client, _ = renewable_client(tmp_path, [FakeResponse(401), FakeResponse(401), token_response(), FakeResponse(data={"data": allowance(refill=1)})])
    with pytest.raises(AuthenticationExpiredError):
        client.activate_refill()
    assert len(client._session.calls) == 1
    assert client.get_remaining_data() == 1
    operations = [call[1]["json"].get("operationName", "renew") for call in client._session.calls]
    assert operations == ["bookTariffOptionsDirect", "consumptions", "renew", "consumptions"]


def test_refresh_timeout_blocks_future_network_and_never_reaches_mutation(tmp_path):
    client, _ = renewable_client(tmp_path, [requests.Timeout("private-token")], expired=True)
    with pytest.raises(RequestTimeoutError) as caught:
        client.activate_refill()
    assert "private-token" not in str(caught.value)
    with pytest.raises(SessionStateError, match="interrupted"):
        client.activate_refill()
    assert len(client._session.calls) == 1
    assert client._session.calls[0][0] == TOKEN_URL


@pytest.mark.parametrize("changes", [
    {"token_type": None}, {"token_type": "unknown"}, {"expires_in": True},
    {"expires_in": "3600"}, {"expires_in": float("nan")}, {"expires_in": 0},
    {"access_token": None}, {"refresh_token": "private\nvalue"},
    {"needs_multifactor_authentication": True}, {"needs_multifactor_authentication": 0},
])
def test_invalid_or_challenged_refresh_response_is_sanitized(changes):
    response = token_response(**changes)
    with pytest.raises((UnexpectedResponseError, AuthenticationExpiredError)) as caught:
        request_tokens(FakeSession([response]), {}, timeout=20, client_id="test-client", client_secret="test-secret")
    assert "private" not in str(caught.value)
    assert response.closed


def test_login_requires_explicitly_absent_multifactor_challenge():
    for response in [token_response(), token_response(needs_multifactor_authentication=True)]:
        with pytest.raises(AuthenticationExpiredError):
            request_tokens(FakeSession([response]), {}, timeout=20, client_id="test-client", client_secret="test-secret", login=True)
    tokens = request_tokens(FakeSession([token_response(needs_multifactor_authentication=False)]), {}, timeout=20, client_id="test-client", client_secret="test-secret", login=True)
    assert tokens.access_token == "new-access"


class PublicSession:
    def __init__(self, sources, bundle):
        self.sources, self.bundle, self.calls = sources, bundle, []

    def get(self, url, **kwargs):
        self.calls.append(url)
        text = self.sources if len(self.calls) == 1 else self.bundle
        response = FakeResponse()
        response.text, response.content = text, text.encode()
        return response


def test_public_client_discovery_reads_current_script_without_executing_it():
    session = PublicSession('<script src="/assets/brfrontend/dist/production/js/lidl.js?v=1"></script>', '.commit("selectClient",{id:"published-client",secret:"published-value"})')
    assert discover_client(session, timeout=20) == ("published-client", "published-value")
    assert session.calls[1].endswith("lidl.js?v=1")


@pytest.mark.parametrize("sources", [
    '<script src="https://unexpected.example/assets/brfrontend/dist/production/js/lidl.js"></script>',
    '<script src="/changed.js"></script>', '',
])
def test_changed_script_location_or_host_stops_discovery(sources):
    session = PublicSession(sources, "")
    with pytest.raises(UnexpectedResponseError):
        discover_client(session, timeout=20)
    assert len(session.calls) == 1


def test_changed_public_client_configuration_stops_discovery():
    session = PublicSession('<script src="/assets/brfrontend/dist/production/js/lidl.js"></script>', 'changed configuration')
    with pytest.raises(UnexpectedResponseError):
        discover_client(session, timeout=20)
