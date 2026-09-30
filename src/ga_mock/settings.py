"""Configuration, read exclusively from the environment.

A single mechanism for docker compose, Kubernetes Deployment and the Tekton
sidecar: `GA_MOCK_*` variables, read at import time and reloadable via
`reload()` (tests change the environment then reload). No configuration
file: a mock must start identically wherever it's deployed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_SA_EMAIL = "insights360@boreal-conseil-mock.iam.gserviceaccount.example"


def _boolean(raw: str) -> bool:
    """Ecosystem rule (same as boondmanager-mock): 1/true/yes/on."""
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    """The defaults are those of the README; the environment overrides them."""

    seed: int = 42
    property_id: str = "424242001"
    sa_email: str = DEFAULT_SA_EMAIL
    freshness_hours: float = 48.0
    admin_enabled: bool = False
    admin_token: str = "mock-admin-token"
    quota_tokens_per_day: int = 200_000
    quota_tokens_per_hour: int = 40_000
    # 35% of the hourly bucket — the rule is written in black and white in
    # Google's discovery document: "Analytics Properties can use up to 35% of
    # their tokens per project per hour", i.e. 14,000 for a standard property.
    quota_tokens_per_project_per_hour: int = 14_000
    rate_limit_after: int | None = None
    retry_after: int = 1

    def reload(self) -> None:
        env = os.environ
        self.seed = int(env.get("GA_MOCK_SEED", "42"))
        self.property_id = env.get("GA_MOCK_PROPERTY_ID", "424242001")
        self.sa_email = env.get("GA_MOCK_SA_EMAIL", DEFAULT_SA_EMAIL)
        self.freshness_hours = float(env.get("GA_MOCK_FRESHNESS_HOURS", "48"))
        self.admin_enabled = _boolean(env.get("GA_MOCK_ADMIN_ENABLED", "false"))
        self.admin_token = env.get("GA_MOCK_ADMIN_TOKEN", "mock-admin-token")
        self.quota_tokens_per_day = int(env.get("GA_MOCK_QUOTA_TOKENS_PER_DAY", "200000"))
        self.quota_tokens_per_hour = int(env.get("GA_MOCK_QUOTA_TOKENS_PER_HOUR", "40000"))
        self.quota_tokens_per_project_per_hour = int(
            env.get("GA_MOCK_QUOTA_TOKENS_PER_PROJECT_PER_HOUR", "14000")
        )
        raw = env.get("GA_MOCK_RATE_LIMIT_AFTER", "").strip()
        self.rate_limit_after = int(raw) if raw else None
        self.retry_after = int(env.get("GA_MOCK_RETRY_AFTER", "1"))


settings = Settings()
settings.reload()

# PROPERTY configuration, not server configuration: fixed like the generated
# world. Changing it would change the responses — hence the de facto contract
# of consumer tests. No environment variable for this, on purpose.
CURRENCY_CODE = "EUR"
TIME_ZONE = "Europe/Paris"
BEARER_TTL_SECONDS = 3600
