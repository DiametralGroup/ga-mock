"""ga-mock — un faux Google Analytics 4 qui parle le dialecte Data API v1beta.

Deux modes d'usage, tous deux maintenus :
  • in-process : `TestClient(ga_mock.app)` dans une suite de tests ;
  • conteneur : image docker pour compose et les sidecars CI.

L'application que la stack interroge EST celle que les tests exercent.
"""

from .app import app
from .settings import settings
from .state import state

__all__ = ["app", "settings", "state"]
