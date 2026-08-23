"""The Expo push client (UC23).

The thing worth testing here is the reading of a response, not the sending of
a request: the scheduler decides whether an item decays based on what this
module says happened, so mistaking a refusal for a delivery would decay items
that were never told about.
"""

import httpx
import pytest

from backend import push
from backend.config import settings


class FakeTransport(httpx.AsyncBaseTransport):
    """Answers with a canned response and records what was asked."""

    def __init__(
        self, status: int = 200, json_body: object = None, raw: str = ""
    ) -> None:
        self.status = status
        self.json_body = json_body
        self.raw = raw
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.json_body is not None:
            return httpx.Response(self.status, json=self.json_body, request=request)
        return httpx.Response(self.status, text=self.raw, request=request)


@pytest.fixture
def transport(monkeypatch):
    """Route `push.send` through a transport we control."""
    holder: dict[str, FakeTransport] = {}

    def install(**kwargs) -> FakeTransport:
        fake = FakeTransport(**kwargs)
        holder["fake"] = fake
        original = httpx.AsyncClient

        def build(*args, **client_kwargs):
            client_kwargs["transport"] = fake
            return original(*args, **client_kwargs)

        monkeypatch.setattr(push.httpx, "AsyncClient", build)
        return fake

    return install


def message(token: str = "ExponentPushToken[abc123]") -> push.PushMessage:
    """One message, with the fields the scheduler always sets."""
    return push.PushMessage(
        token=token,
        title="Call the insurance guy",
        body="Due 3:00 pm",
        data={"itemId": "x"},
    )


# ------------------------------------------------------------ token shape


@pytest.mark.parametrize(
    "token,valid",
    [
        ("ExponentPushToken[xxxxxxxxxxxxxxxxxxxxxx]", True),
        ("ExpoPushToken[xxxxxxxxxxxxxxxxxxxxxx]", True),
        ("ExponentPushToken[]", False),
        ("fcm-registration-id", False),
        ("", False),
        ("ExponentPushToken[unclosed", False),
    ],
)
def test_token_shape_is_checked_at_the_edge(token: str, valid: bool):
    """A malformed token stored now is a push that vanishes later."""
    assert push.is_expo_token(token) is valid


# ---------------------------------------------------------------- payload


def test_payload_names_the_channel_and_the_category():
    """The done/snooze buttons only appear if the category name matches (UC15, UC17)."""
    payload = message().payload()

    assert payload["categoryId"] == settings.push_category_id
    assert payload["channelId"] == settings.push_channel_id
    assert payload["to"] == "ExponentPushToken[abc123]"
    assert payload["priority"] == "high"
    assert payload["data"] == {"itemId": "x"}


# ---------------------------------------------------------------- tickets


async def test_an_accepted_message_comes_back_as_a_ticket(transport):
    """The happy path: Expo took it, and it has a receipt id."""
    transport(json_body={"data": [{"status": "ok", "id": "ticket-1"}]})

    tickets = await push.send([message()])

    assert len(tickets) == 1
    assert tickets[0].ok is True
    assert tickets[0].ticket_id == "ticket-1"
    assert tickets[0].token_is_dead is False


async def test_a_dead_token_is_reported_as_dead(transport):
    """`DeviceNotRegistered` is the one error that means stop using this token."""
    transport(
        json_body={
            "data": [
                {
                    "status": "error",
                    "message": "not a registered device",
                    "details": {"error": "DeviceNotRegistered"},
                }
            ]
        }
    )

    tickets = await push.send([message()])

    assert tickets[0].ok is False
    assert tickets[0].token_is_dead is True


async def test_other_errors_do_not_kill_the_token(transport):
    """A message Expo would not take is not a device that has gone away."""
    transport(
        json_body={
            "data": [
                {
                    "status": "error",
                    "message": "message too big",
                    "details": {"error": "MessageTooBig"},
                }
            ]
        }
    )

    tickets = await push.send([message()])

    assert tickets[0].ok is False
    assert tickets[0].token_is_dead is False


async def test_tickets_are_matched_to_messages_by_position(transport):
    """Expo does not echo the token, so a short array must not be guessed at.

    Mis-pairing here would disable a device that is working perfectly.
    """
    transport(json_body={"data": [{"status": "ok", "id": "only-one"}]})

    with pytest.raises(push.PushError, match="1 tickets for 2 messages"):
        await push.send(
            [message("ExponentPushToken[a]"), message("ExponentPushToken[b]")]
        )


# ------------------------------------------------------- request failures


async def test_a_request_level_error_raises_rather_than_returning_tickets(transport):
    """A refused request says nothing about any single message."""
    transport(json_body={"errors": [{"code": "PUSH_TOO_MANY_EXPERIENCE_IDS"}]})

    with pytest.raises(push.PushError, match="refused the request"):
        await push.send([message()])


async def test_an_http_error_raises(transport):
    """A 500 from the push service is not a delivery."""
    transport(status=503, raw="upstream unavailable")

    with pytest.raises(push.PushError, match="503"):
        await push.send([message()])


async def test_an_unreachable_service_raises(monkeypatch):
    """No socket, no delivery — and the caller has to be able to tell."""

    class DeadTransport(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("no route to host", request=request)

    original = httpx.AsyncClient
    monkeypatch.setattr(
        push.httpx,
        "AsyncClient",
        lambda *a, **k: original(*a, **{**k, "transport": DeadTransport()}),
    )

    with pytest.raises(push.PushError, match="Could not reach"):
        await push.send([message()])


async def test_a_body_that_is_not_json_raises(transport):
    """A 200 carrying HTML is a broken host, not a delivered push."""
    transport(raw="<html>gateway</html>")

    with pytest.raises(push.PushError, match="not JSON"):
        await push.send([message()])


async def test_nothing_to_send_is_not_a_request(transport):
    """An empty batch must not cost an HTTP call."""
    fake = transport(json_body={"data": []})

    assert await push.send([]) == []
    assert fake.requests == []


async def test_a_batch_beyond_expos_limit_is_refused(transport):
    """Better to raise than to have Expo silently drop the tail."""
    fake = transport(json_body={"data": []})
    too_many = [message() for _ in range(push.MAX_MESSAGES_PER_REQUEST + 1)]

    with pytest.raises(push.PushError, match="exceeds Expo's limit"):
        await push.send(too_many)
    assert fake.requests == []
