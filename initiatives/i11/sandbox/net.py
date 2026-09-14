#!/usr/bin/env python3
"""Sandboxed network: extensions may only reach declared destinations.

Deny-by-default. The only network primitive an extension receives is
``SandboxHTTP``, constructed with the manifest's explicit destination
allowlist (bare hostnames). Any request to a host not on the list raises
``SandboxViolation`` before any socket is opened — exfiltration to an
undeclared host fails closed.

Per-destination circuit breakers and a per-extension rate limiter are
enforced here, in the host, where extension code cannot weaken them.
"""

from __future__ import annotations

import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, field


class SandboxViolation(PermissionError):
    """An extension attempted network access outside its declared destinations."""


class RateLimitExceeded(RuntimeError):
    """The extension exceeded its manifest-declared call rate."""


class CircuitOpen(RuntimeError):
    """The destination's circuit breaker is open after repeated failures."""


@dataclass
class _CircuitState:
    failures: int = 0
    opened_at: float = 0.0
    cooldown_s: float = 60.0

    def record_failure(self, now: float) -> None:
        self.failures += 1
        if self.failures >= 3:
            self.opened_at = now
            # Exponential backoff: 1m, 2m, 4m ... capped at 1h.
            self.cooldown_s = min(3600.0, 60.0 * (2 ** (self.failures - 3)))

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = 0.0
        self.cooldown_s = 60.0

    def is_open(self, now: float) -> bool:
        return (
            self.failures >= 3
            and (now - self.opened_at) < self.cooldown_s
        )


class _TokenBucket:
    """Simple per-extension token bucket. Host-enforced; not reachable
    from extension code."""

    def __init__(self, calls_per_minute: int) -> None:
        self._capacity = float(calls_per_minute)
        self._tokens = float(calls_per_minute)
        self._refill_per_s = calls_per_minute / 60.0
        self._last = time.monotonic()

    def take(self) -> None:
        now = time.monotonic()
        self._tokens = min(
            self._capacity,
            self._tokens + (now - self._last) * self._refill_per_s,
        )
        self._last = now
        if self._tokens < 1.0:
            raise RateLimitExceeded(
                "extension exceeded its manifest rate limit "
                f"({int(self._capacity)}/min)"
            )
        self._tokens -= 1.0


@dataclass
class HttpRequest:
    method: str
    url: str
    headers: dict[str, str] = field(default_factory=dict)
    body: bytes | None = None
    timeout_s: float = 10.0


@dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class SandboxHTTP:
    """Allowlisted HTTP client for one loaded extension.

    ``transport`` is a host-supplied callable
    ``(HttpRequest) -> HttpResponse`` so tests can run without real
    network. The allowlist, rate limit, and circuit breaker checks all
    happen here, before the transport is invoked.
    """

    def __init__(
        self,
        destinations: list[str],
        *,
        calls_per_minute: int = 60,
        transport=None,
    ) -> None:
        self._allowlist = {d.lower() for d in destinations}
        self._bucket = _TokenBucket(calls_per_minute)
        self._circuits: dict[str, _CircuitState] = {}
        self._transport = transport or self._no_transport

    @staticmethod
    def _no_transport(request: HttpRequest) -> HttpResponse:  # pragma: no cover
        raise RuntimeError(
            "no HTTP transport configured on this host; "
            "network calls are disabled"
        )

    def _check_host(self, url: str) -> str:
        try:
            host = (urllib.parse.urlparse(url).hostname or "").lower()
        except Exception:
            host = ""
        if not host or host not in self._allowlist:
            raise SandboxViolation(
                f"destination {host or url!r} is not in the manifest's "
                f"declared network destinations {sorted(self._allowlist)}"
            )
        return host

    def request(self, request: HttpRequest) -> HttpResponse:
        host = self._check_host(request.url)
        self._bucket.take()  # rate limit enforced before any network use
        circuit = self._circuits.setdefault(host, _CircuitState())
        now = time.monotonic()
        if circuit.is_open(now):
            raise CircuitOpen(
                f"circuit open for {host}: cooling down after repeated failures"
            )
        try:
            response = self._transport(request)
        except (SandboxViolation, RateLimitExceeded, CircuitOpen):
            raise
        except Exception as exc:
            circuit.record_failure(now)
            raise RuntimeError(f"network call to {host} failed: {exc}") from exc
        if 500 <= response.status < 600:
            circuit.record_failure(now)
        else:
            circuit.record_success()
        return response

    def get(self, url: str, **kwargs) -> HttpResponse:
        return self.request(HttpRequest(method="GET", url=url, **kwargs))

    def post(self, url: str, body: bytes | None = None, **kwargs) -> HttpResponse:
        return self.request(
            HttpRequest(method="POST", url=url, body=body, **kwargs)
        )

    @property
    def destinations(self) -> list[str]:
        return sorted(self._allowlist)
