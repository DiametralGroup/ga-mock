"""Configuration, lue exclusivement dans l'environnement.

Une seule mécanique pour docker compose, Deployment Kubernetes et sidecar
Tekton : des variables `GA_MOCK_*`, lues à l'import et rechargeables par
`reload()` (les tests changent l'environnement puis rechargent). Pas de
fichier de configuration : un mock doit démarrer identique partout où on le
pose.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

EMAIL_SA_DEFAUT = "insights360@boreal-conseil-mock.iam.gserviceaccount.example"


def _booleen(brut: str) -> bool:
    """Règle de l'écosystème (identique à boondmanager-mock) : 1/true/yes/on."""
    return brut.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """Les défauts sont ceux du README ; l'environnement les écrase."""

    seed: int = 42
    property_id: str = "424242001"
    sa_email: str = EMAIL_SA_DEFAUT
    freshness_hours: float = 48.0
    admin_enabled: bool = False
    admin_token: str = "mock-admin-token"
    quota_tokens_per_day: int = 200_000
    quota_tokens_per_hour: int = 40_000
    rate_limit_after: int | None = None
    retry_after: int = 1

    def reload(self) -> None:
        env = os.environ
        self.seed = int(env.get("GA_MOCK_SEED", "42"))
        self.property_id = env.get("GA_MOCK_PROPERTY_ID", "424242001")
        self.sa_email = env.get("GA_MOCK_SA_EMAIL", EMAIL_SA_DEFAUT)
        self.freshness_hours = float(env.get("GA_MOCK_FRESHNESS_HOURS", "48"))
        self.admin_enabled = _booleen(env.get("GA_MOCK_ADMIN_ENABLED", "false"))
        self.admin_token = env.get("GA_MOCK_ADMIN_TOKEN", "mock-admin-token")
        self.quota_tokens_per_day = int(env.get("GA_MOCK_QUOTA_TOKENS_PER_DAY", "200000"))
        self.quota_tokens_per_hour = int(env.get("GA_MOCK_QUOTA_TOKENS_PER_HOUR", "40000"))
        brut = env.get("GA_MOCK_RATE_LIMIT_AFTER", "").strip()
        self.rate_limit_after = int(brut) if brut else None
        self.retry_after = int(env.get("GA_MOCK_RETRY_AFTER", "1"))


settings = Settings()
settings.reload()

# Configuration de la PROPRIÉTÉ, pas du serveur : figée comme le monde généré.
# Les changer changerait les réponses — donc le contrat de fait des tests
# consommateurs. Pas de variable d'environnement pour ça, c'est voulu.
CURRENCY_CODE = "EUR"
TIME_ZONE = "Europe/Paris"
BEARER_TTL_SECONDES = 3600
