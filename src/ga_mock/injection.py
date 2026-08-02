"""Moteur d'injection de pannes.

« The point of the mock is to reproduce failure modes, not just happy paths »
— même règle que boondmanager-mock. Les règles sont évaluées à UN SEUL point,
AVANT l'authentification (une `auth_reject` doit pouvoir préempter un bearer
valide), avec un scope glob (`*:runReport`, `/v1beta/*`, `/token`, `*`) et un
compteur `times` optionnel (panne transitoire vs persistante).

Le moteur enregistre aussi, par chemin, le nombre de requêtes et le RÉSUMÉ du
dernier corps de rapport reçu : c'est l'affordance qui permet à insights360 de
PROUVER, dans ses tests, la fenêtre de dates réellement envoyée.
"""

from __future__ import annotations

import itertools
import time
from dataclasses import dataclass
from fnmatch import fnmatch
from typing import Any, Literal

from fastapi.responses import JSONResponse

from .errors import MESSAGE_401, MESSAGE_429_JOUR, erreur
from .settings import settings

Kind = Literal["rate_limit", "status", "latency", "auth_reject", "quota_exhausted"]

KINDS: tuple[Kind, ...] = ("rate_limit", "status", "latency", "auth_reject", "quota_exhausted")

MESSAGE_RATE_LIMIT = "Resource has been exhausted (e.g. check quota)."

_MESSAGES_STATUS: dict[int, str] = {
    500: "Internal error encountered.",
    502: "Bad gateway.",
    503: "The service is currently unavailable.",
    504: "Deadline expired before operation could complete.",
}


@dataclass(slots=True)
class Regle:
    rule_id: str
    kind: Kind
    scope: str = "*"
    status: int = 503
    times: int | None = None
    seconds: float = 1.0
    retry_after_seconds: int = 1
    after_requests: int = 0

    def resume(self) -> dict[str, Any]:
        corps: dict[str, Any] = {"rule_id": self.rule_id, "kind": self.kind, "scope": self.scope}
        if self.kind == "status":
            corps["status"] = self.status
        if self.kind == "latency":
            corps["seconds"] = self.seconds
        if self.kind == "rate_limit":
            corps["after_requests"] = self.after_requests
            corps["retry_after_seconds"] = self.retry_after_seconds
        if self.times is not None:
            corps["times"] = self.times
        return corps


class MoteurInjection:
    def __init__(self) -> None:
        self.regles: list[Regle] = []
        self._ids = itertools.count(1)
        self.request_counts: dict[str, int] = {}
        self.last_request_by_path: dict[str, dict[str, Any]] = {}

    # ── observation ──────────────────────────────────────────────────────────

    def observer(self, chemin: str) -> None:
        self.request_counts[chemin] = self.request_counts.get(chemin, 0) + 1

    def noter_corps(self, chemin: str, resume: dict[str, Any]) -> None:
        """Le résumé n'est noté qu'après un parse réussi : ce que le mock
        atteste, c'est ce qu'un client BIEN FORMÉ a demandé."""
        self.last_request_by_path[chemin] = resume

    # ── gestion des règles ───────────────────────────────────────────────────

    def ajouter(self, kind: str, **params: Any) -> Regle:
        if kind not in KINDS:
            raise ValueError(f"unknown injection kind: {kind}")
        regle = Regle(rule_id=f"rule-{next(self._ids)}", kind=kind, **params)
        self.regles.append(regle)
        return regle

    def retirer(self, rule_id: str) -> bool:
        avant = len(self.regles)
        self.regles = [r for r in self.regles if r.rule_id != rule_id]
        return len(self.regles) != avant

    def vider(self) -> None:
        self.regles.clear()

    def reinitialiser(self) -> None:
        """Retour au BASELINE de l'environnement, pas à zéro : une injection
        posée par variable d'env (déploiement) doit survivre aux resets des
        tests — règle héritée de boondmanager-mock."""
        self.vider()
        self.request_counts.clear()
        self.last_request_by_path.clear()
        if settings.rate_limit_after is not None:
            self.ajouter(
                "rate_limit",
                scope="/v1beta/*",
                after_requests=settings.rate_limit_after,
                retry_after_seconds=settings.retry_after,
            )

    # ── évaluation ───────────────────────────────────────────────────────────

    def _consommer(self, regle: Regle) -> None:
        if regle.times is None:
            return
        regle.times -= 1
        if regle.times <= 0:
            self.retirer(regle.rule_id)

    def evaluer(self, chemin: str) -> JSONResponse | None:
        """Ordre fixe : auth_reject, latency, rate_limit, quota_exhausted,
        status — pour qu'une latence s'applique même quand une autre règle
        répond ensuite."""
        actives = [r for r in self.regles if fnmatch(chemin, r.scope)]
        for regle in (r for r in actives if r.kind == "auth_reject"):
            self._consommer(regle)
            return erreur(401, MESSAGE_401)
        for regle in (r for r in actives if r.kind == "latency"):
            time.sleep(regle.seconds)
            self._consommer(regle)
        for regle in (r for r in actives if r.kind == "rate_limit"):
            if self.request_counts.get(chemin, 0) > regle.after_requests:
                self._consommer(regle)
                return erreur(
                    429,
                    MESSAGE_RATE_LIMIT,
                    headers={"Retry-After": str(regle.retry_after_seconds)},
                )
        for regle in (r for r in actives if r.kind == "quota_exhausted"):
            self._consommer(regle)
            return erreur(429, MESSAGE_429_JOUR)
        for regle in (r for r in actives if r.kind == "status"):
            self._consommer(regle)
            code = regle.status
            return erreur(code, _MESSAGES_STATUS.get(code, f"Injected status {code}."))
        return None


engine = MoteurInjection()
