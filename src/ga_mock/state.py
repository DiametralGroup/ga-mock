"""État mutable du serveur.

Un singleton de module : la même instance pour l'app FastAPI, pour `/__admin`
et pour les tests in-process. `reset()` est le SEUL point de reconstruction —
démarrage, admin et tests passent tous par lui, sinon deux chemins de remise à
zéro finissent par diverger.
"""

from __future__ import annotations

from .clock import horloge
from .settings import settings


class MockState:
    def __init__(self) -> None:
        self.seed: int = settings.seed

    def reset(self, seed: int | None = None) -> None:
        """Relit l'environnement puis rebâtit l'état.

        `seed` explicite (venu de /__admin/reset) prime sur l'environnement ;
        sans lui on revient à la configuration de déploiement, pas à un état
        magique mémorisé. L'horloge virtuelle fait partie de l'état : un reset
        ramène aussi le temps à l'ancre.
        """
        settings.reload()
        self.seed = settings.seed if seed is None else seed
        horloge.offset_secondes = 0.0


state = MockState()
