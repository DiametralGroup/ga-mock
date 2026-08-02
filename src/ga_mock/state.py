"""État mutable du serveur.

Un singleton de module : la même instance pour l'app FastAPI, pour `/__admin`
et pour les tests in-process. `reset()` est le SEUL point de reconstruction —
démarrage, admin et tests passent tous par lui, sinon deux chemins de remise à
zéro finissent par diverger.
"""

from __future__ import annotations

from datetime import date, timedelta

from .clock import horloge, virtual_now
from .dataset.sessions import Session, build_day
from .settings import settings


class MockState:
    def __init__(self) -> None:
        self.seed: int = settings.seed
        self._jours: dict[date, tuple[Session, ...]] = {}

    def day(self, d: date) -> tuple[Session, ...]:
        """Matérialisation paresseuse + cache.

        Un jour est une fonction pure de (seed, jour) : le cache n'est donc
        invalidé QUE par `reset()` — jamais par le temps qui passe, seule la
        VISIBILITÉ des sessions dépend de l'horloge.
        """
        if d not in self._jours:
            self._jours[d] = build_day(self.seed, d)
        return self._jours[d]

    def visible_sessions(self, d: date) -> tuple[Session, ...]:
        """Le sous-ensemble « déjà traité » du jour.

        Modèle de fraîcheur GA4 : une session n'existe pour l'API qu'une fois
        sa latence de traitement (`lag_hours`) écoulée. Les jours récents
        grossissent donc de façon monotone quand l'horloge avance — c'est le
        levier qui permet aux consommateurs de tester leur ré-extraction des
        N derniers jours.
        """
        limite = virtual_now()
        return tuple(s for s in self.day(d) if s.ts + timedelta(hours=s.lag_hours) <= limite)

    def jours_materialises(self) -> int:
        return len(self._jours)

    def reset(self, seed: int | None = None) -> None:
        """Relit l'environnement puis rebâtit l'état.

        `seed` explicite (venu de /__admin/reset) prime sur l'environnement ;
        sans lui on revient à la configuration de déploiement, pas à un état
        magique mémorisé. L'horloge virtuelle fait partie de l'état : un reset
        ramène aussi le temps à l'ancre.
        """
        settings.reload()
        self.seed = settings.seed if seed is None else seed
        self._jours.clear()
        horloge.offset_secondes = 0.0


state = MockState()
