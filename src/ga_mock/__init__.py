"""ga-mock — a fake Google Analytics 4 that speaks the Data API v1beta dialect.

Two usage modes, both maintained:
  • in-process: `TestClient(ga_mock.app)` in a test suite;
  • container: docker image for compose and CI sidecars.

The application the stack talks to IS the one the tests exercise.
"""

from .app import app, contract_openapi
from .auth import build_assertion
from .clock import virtual_now
from .injection import engine
from .settings import settings
from .state import state

__all__ = [
    "app",
    "build_assertion",
    "contract_openapi",
    "engine",
    "settings",
    "state",
    "virtual_now",
]
