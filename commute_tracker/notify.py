"""Push notification delivery.

Two providers ship today -- ntfy and Pushover -- and both are optional: whichever
ones are configured get the daily report, and if none are, the digest job simply
does not run. Adding another provider means one more :class:`Notifier` subclass.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

import httpx

from .report import Report

log = logging.getLogger(__name__)

# Pushover's own scale: 1 = high (bypasses quiet hours), 0 = normal, -1 = quiet.
# ntfy uses 1-5, so each provider maps severity itself.
_LOUD = {"red"}
_QUIET = {"grey"}


class NotifyError(RuntimeError):
    """A notification could not be delivered."""


class Notifier:
    """One delivery channel."""

    name = "notifier"

    async def send(self, report: Report, client: httpx.AsyncClient) -> None:
        raise NotImplementedError


@dataclass(frozen=True)
class NtfyNotifier(Notifier):
    """https://ntfy.sh -- topic-based push, no account required."""

    topic: str
    server: str = "https://ntfy.sh"
    token: str | None = None

    name = "ntfy"

    def _priority(self, severity: str) -> int:
        """ntfy's 1-5 scale: 4 = high, 3 = default, 2 = low."""
        if severity in _LOUD:
            return 4
        if severity in _QUIET:
            return 2
        return 3

    async def send(self, report: Report, client: httpx.AsyncClient) -> None:
        # ntfy's JSON publishing endpoint rather than the header-based one: HTTP
        # header values must be ASCII, and the title carries a severity emoji.
        payload = {
            "topic": self.topic,
            "title": report.title(),
            "message": report.body(),
            "priority": self._priority(report.severity),
            # The tag renders as a colored dot in the notification list.
            "tags": [
                {"red": "red_circle", "yellow": "yellow_circle", "green": "green_circle"}.get(
                    report.severity, "white_circle"
                )
            ],
            "click": report.maps_url,
            "actions": [{"action": "view", "label": "Open in Maps", "url": report.maps_url}],
        }
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        response = await client.post(self.server.rstrip("/"), json=payload, headers=headers)
        if response.status_code >= 400:
            raise NotifyError(f"ntfy returned HTTP {response.status_code}: {response.text[:300]}")


@dataclass(frozen=True)
class PushoverNotifier(Notifier):
    """https://pushover.net -- app token plus user key."""

    token: str
    user: str
    api_url: str = "https://api.pushover.net/1/messages.json"

    name = "pushover"

    async def send(self, report: Report, client: httpx.AsyncClient) -> None:
        payload = {
            "token": self.token,
            "user": self.user,
            "title": report.title(),
            "message": report.body(),
            "url": report.maps_url,
            "url_title": "Open in Google Maps",
            "priority": 1 if report.severity in _LOUD else (-1 if report.severity in _QUIET else 0),
        }
        response = await client.post(self.api_url, data=payload)
        if response.status_code >= 400:
            raise NotifyError(
                f"Pushover returned HTTP {response.status_code}: {response.text[:300]}"
            )


def notifiers_from_env() -> list[Notifier]:
    """Build whichever notifiers the environment has credentials for."""
    configured: list[Notifier] = []

    topic = os.getenv("NTFY_TOPIC", "").strip()
    if topic:
        configured.append(
            NtfyNotifier(
                topic=topic,
                server=os.getenv("NTFY_SERVER", "https://ntfy.sh").strip(),
                token=os.getenv("NTFY_TOKEN", "").strip() or None,
            )
        )

    pushover_token = os.getenv("PUSHOVER_TOKEN", "").strip()
    pushover_user = os.getenv("PUSHOVER_USER", "").strip()
    if pushover_token and pushover_user:
        configured.append(PushoverNotifier(token=pushover_token, user=pushover_user))
    elif pushover_token or pushover_user:
        log.warning("Pushover needs both PUSHOVER_TOKEN and PUSHOVER_USER; skipping it")

    return configured


async def deliver(
    report: Report, notifiers: list[Notifier], *, timeout: float = 15.0
) -> dict[str, str]:
    """Send one report through every notifier. Returns per-provider status.

    A provider that fails is logged and reported, never raised: one dead channel
    must not stop the others, nor take the scheduler down with it.
    """
    results: dict[str, str] = {}
    if not notifiers:
        return results
    async with httpx.AsyncClient(timeout=timeout) as client:
        for notifier in notifiers:
            try:
                await notifier.send(report, client)
            except (NotifyError, httpx.HTTPError) as exc:
                log.error("[%s] notification failed: %s", notifier.name, exc)
                results[notifier.name] = f"failed: {exc}"
            else:
                log.info(
                    "[%s] sent %s report for %s", notifier.name, report.severity, report.route_id
                )
                results[notifier.name] = "sent"
    return results
