import json

import httpx
import pytest

from commute_tracker.notify import (
    NotifyError,
    NtfyNotifier,
    PushoverNotifier,
    deliver,
    notifiers_from_env,
)
from commute_tracker.report import Report


def make_report(severity="red") -> Report:
    return Report(
        route_id="morning-commute",
        route_name="Morning commute",
        local_date="2026-02-10",
        severity=severity,
        samples=8,
        avg_seconds=2400,
        min_seconds=2100,
        max_seconds=2700,
        baseline_avg_seconds=1800,
        baseline_stdev_seconds=120,
        baseline_days=20,
        z_score=5.0,
        maps_url="https://www.google.com/maps/dir/?api=1&origin=A&destination=B",
    )


def transport(recorder, status_code=200):
    """A mock transport that records the request and returns ``status_code``."""

    def handler(request: httpx.Request) -> httpx.Response:
        recorder.append(request)
        return httpx.Response(status_code, text="")

    return httpx.MockTransport(handler)


async def test_ntfy_sends_title_click_and_a_colored_tag():
    sent = []
    notifier = NtfyNotifier(topic="my-commute")
    async with httpx.AsyncClient(transport=transport(sent)) as client:
        await notifier.send(make_report(), client)

    request = sent[0]
    body = json.loads(request.content)
    assert str(request.url) == "https://ntfy.sh"
    assert body["topic"] == "my-commute"
    assert body["tags"] == ["red_circle"]
    assert body["priority"] == 4
    assert body["click"].startswith("https://www.google.com/maps/dir/")
    assert body["actions"][0]["label"] == "Open in Maps"
    assert "Morning commute" in body["title"]
    assert "Busier than normal" in body["message"]


async def test_ntfy_title_may_carry_non_ascii():
    """HTTP header values must be ASCII, so the severity emoji goes in the JSON body."""
    sent = []
    async with httpx.AsyncClient(transport=transport(sent)) as client:
        await NtfyNotifier(topic="t").send(make_report(), client)

    assert json.loads(sent[0].content)["title"].startswith("🔴")


async def test_ntfy_uses_a_custom_server_and_bearer_token():
    sent = []
    notifier = NtfyNotifier(topic="t", server="https://ntfy.example.com/", token="tk_abc")
    async with httpx.AsyncClient(transport=transport(sent)) as client:
        await notifier.send(make_report("yellow"), client)

    body = json.loads(sent[0].content)
    assert str(sent[0].url) == "https://ntfy.example.com"
    assert sent[0].headers["Authorization"] == "Bearer tk_abc"
    assert body["topic"] == "t"
    assert body["tags"] == ["yellow_circle"]
    assert body["priority"] == 3


async def test_ntfy_raises_on_an_http_error():
    notifier = NtfyNotifier(topic="t")
    async with httpx.AsyncClient(transport=transport([], status_code=403)) as client:
        with pytest.raises(NotifyError, match="403"):
            await notifier.send(make_report(), client)


async def test_pushover_posts_credentials_message_and_supplementary_url():
    sent = []
    notifier = PushoverNotifier(token="app-token", user="user-key")
    async with httpx.AsyncClient(transport=transport(sent)) as client:
        await notifier.send(make_report(), client)

    body = sent[0].content.decode()
    assert "token=app-token" in body
    assert "user=user-key" in body
    assert "priority=1" in body  # red bypasses quiet hours
    assert "url=https" in body


async def test_deliver_reports_per_channel_and_survives_one_failure():
    good, bad = [], []

    class Good(NtfyNotifier):
        name = "good"

        async def send(self, report, client):
            good.append(report)

    class Bad(NtfyNotifier):
        name = "bad"

        async def send(self, report, client):
            bad.append(report)
            raise NotifyError("nope")

    results = await deliver(make_report(), [Good(topic="a"), Bad(topic="b")])
    assert results["good"] == "sent"
    assert results["bad"].startswith("failed:")
    assert len(good) == 1 and len(bad) == 1


async def test_deliver_with_no_notifiers_is_a_no_op():
    assert await deliver(make_report(), []) == {}


def test_notifiers_are_built_only_from_complete_credentials(monkeypatch):
    for key in ("NTFY_TOPIC", "NTFY_SERVER", "NTFY_TOKEN", "PUSHOVER_TOKEN", "PUSHOVER_USER"):
        monkeypatch.delenv(key, raising=False)
    assert notifiers_from_env() == []

    monkeypatch.setenv("NTFY_TOPIC", "my-commute")
    monkeypatch.setenv("PUSHOVER_TOKEN", "only-half")
    built = notifiers_from_env()
    assert [n.name for n in built] == ["ntfy"]  # Pushover needs the user key too

    monkeypatch.setenv("PUSHOVER_USER", "user-key")
    assert [n.name for n in notifiers_from_env()] == ["ntfy", "pushover"]
