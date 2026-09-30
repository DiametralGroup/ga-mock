"""Fault injection engine.

"The point of the mock is to reproduce failure modes, not just happy paths"
— same rule as boondmanager-mock. Rules are evaluated at A SINGLE point,
BEFORE authentication (an `auth_reject` must be able to preempt a valid
bearer), with a glob scope (`*:runReport`, `/v1beta/*`, `/token`, `*`) and an
optional `times` counter (transient vs persistent failure).

The engine also records, per path, the request count and the SUMMARY of the
last report body received: this is the affordance that lets insights360
PROVE, in its tests, the date window it actually sent.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Literal

from fastapi.responses import JSONResponse

from .errors import MESSAGE_401_INVALID, MESSAGE_429_DAY, error
from .settings import settings

Kind = Literal["rate_limit", "status", "latency", "auth_reject", "quota_exhausted"]

KINDS: tuple[Kind, ...] = ("rate_limit", "status", "latency", "auth_reject", "quota_exhausted")

MESSAGE_RATE_LIMIT = "Resource has been exhausted (e.g. check quota)."

_STATUS_MESSAGES: dict[int, str] = {
    500: "Internal error encountered.",
    502: "Bad gateway.",
    503: "The service is currently unavailable.",
    504: "Deadline expired before operation could complete.",
}


@dataclass(slots=True)
class Rule:
    rule_id: str
    kind: Kind
    scope: str = "*"
    status: int = 503
    times: int | None = None
    seconds: float = 1.0
    retry_after_seconds: int = 1
    after_requests: int = 0

    def summary(self) -> dict[str, Any]:
        body: dict[str, Any] = {"rule_id": self.rule_id, "kind": self.kind, "scope": self.scope}
        if self.kind == "status":
            body["status"] = self.status
        if self.kind == "latency":
            body["seconds"] = self.seconds
        if self.kind == "rate_limit":
            body["after_requests"] = self.after_requests
            body["retry_after_seconds"] = self.retry_after_seconds
        if self.times is not None:
            body["times"] = self.times
        return body


class InjectionEngine:
    def __init__(self) -> None:
        self.rules: list[Rule] = []
        self._ids = itertools.count(1)
        self.request_counts: dict[str, int] = {}
        self.last_request_by_path: dict[str, dict[str, Any]] = {}

    # ── observation ──────────────────────────────────────────────────────────

    def observe(self, path: str) -> None:
        self.request_counts[path] = self.request_counts.get(path, 0) + 1

    def record_body(self, path: str, summary: dict[str, Any]) -> None:
        """The summary is only recorded after a successful parse: what the
        mock attests is what a WELL-FORMED client requested."""
        self.last_request_by_path[path] = summary

    # ── rule management ──────────────────────────────────────────────────────

    def add(self, kind: str, **params: Any) -> Rule:
        if kind not in KINDS:
            raise ValueError(f"unknown injection kind: {kind}")
        rule = Rule(rule_id=f"rule-{next(self._ids)}", kind=kind, **params)
        self.rules.append(rule)
        return rule

    def remove(self, rule_id: str) -> bool:
        before = len(self.rules)
        self.rules = [r for r in self.rules if r.rule_id != rule_id]
        return len(self.rules) != before

    def clear(self) -> None:
        self.rules.clear()

    def reset(self) -> None:
        """Back to the environment's BASELINE, not to zero: an injection set
        via an env variable (deployment) must survive test resets — rule
        inherited from boondmanager-mock."""
        self.clear()
        self.request_counts.clear()
        self.last_request_by_path.clear()
        if settings.rate_limit_after is not None:
            self.add(
                "rate_limit",
                scope="/v1beta/*",
                after_requests=settings.rate_limit_after,
                retry_after_seconds=settings.retry_after,
            )

    # ── evaluation ───────────────────────────────────────────────────────────

    def _consume(self, rule: Rule) -> None:
        if rule.times is None:
            return
        rule.times -= 1
        if rule.times <= 0:
            self.remove(rule.rule_id)

    def evaluate(self, path: str) -> JSONResponse | None:
        """Fixed order: auth_reject, latency, rate_limit, quota_exhausted,
        status — so that latency applies even when another rule answers
        next."""
        active = [r for r in self.rules if fnmatch(path, r.scope)]
        for rule in (r for r in active if r.kind == "auth_reject"):
            self._consume(rule)
            # "Rejected token" variant: an injected auth failure simulates a
            # vendor rejecting PRESENT credentials, not a client that forgot
            # its header.
            return error(401, MESSAGE_401_INVALID)
        for rule in (r for r in active if r.kind == "latency"):
            time.sleep(rule.seconds)
            self._consume(rule)
        for rule in (r for r in active if r.kind == "rate_limit"):
            if self.request_counts.get(path, 0) > rule.after_requests:
                self._consume(rule)
                return error(
                    429,
                    MESSAGE_RATE_LIMIT,
                    headers={"Retry-After": str(rule.retry_after_seconds)},
                )
        for rule in (r for r in active if r.kind == "quota_exhausted"):
            self._consume(rule)
            return error(429, MESSAGE_429_DAY)
        for rule in (r for r in active if r.kind == "status"):
            self._consume(rule)
            code = rule.status
            return error(code, _STATUS_MESSAGES.get(code, f"Injected status {code}."))
        return None


engine = InjectionEngine()
