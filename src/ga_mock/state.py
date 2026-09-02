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
from .errors import (
    MESSAGE_429_HEURE,
    MESSAGE_429_JOUR,
    MESSAGE_429_PROJET_HEURE,
    ErreurQuota,
)
from .injection import engine
from .settings import settings


class MockState:
    def __init__(self) -> None:
        self.seed: int = settings.seed
        self._jours: dict[date, tuple[Session, ...]] = {}
        self.quota_jour_consomme: int = 0
        self.quota_heure_consomme: int = 0
        self.quota_projet_heure_consomme: int = 0
        # UNE seule voie de construction : l'init passe par reset(), sinon le
        # baseline d'injection de l'environnement ne serait appliqué qu'aux
        # resets explicites et jamais au démarrage du conteneur.
        self.reset()

    def consommer_quota(self, jetons: int) -> None:
        """Décompte des jetons — l'épuisement NATUREL produit la même 429 que le
        vrai service. Rare avec les plafonds par défaut ; l'injection
        `quota_exhausted` force le cas sans attendre.

        TROIS seaux, comme chez le vendeur : « An API request consumes a single
        number of tokens, and that number is deducted from all of the hourly,
        daily, and per project hourly quotas. » Le seau projet/heure (35 % de
        l'horaire) est donc celui qui s'épuise EN PREMIER aux plafonds par
        défaut — un consommateur qui ne surveille que `tokensPerHour` sera
        surpris ici plutôt qu'en prod.
        """
        if self.quota_jour_consomme + jetons > settings.quota_tokens_per_day:
            raise ErreurQuota(MESSAGE_429_JOUR)
        if self.quota_heure_consomme + jetons > settings.quota_tokens_per_hour:
            raise ErreurQuota(MESSAGE_429_HEURE)
        if self.quota_projet_heure_consomme + jetons > settings.quota_tokens_per_project_per_hour:
            raise ErreurQuota(MESSAGE_429_PROJET_HEURE)
        self.quota_jour_consomme += jetons
        self.quota_heure_consomme += jetons
        self.quota_projet_heure_consomme += jetons

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
        self.quota_jour_consomme = 0
        self.quota_heure_consomme = 0
        self.quota_projet_heure_consomme = 0
        horloge.offset_secondes = 0.0
        engine.reinitialiser()


state = MockState()
