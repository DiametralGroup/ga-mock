"""Point d'entrée : `python -m ga_mock` (ou le script console `ga-mock`).

HOST/PORT restent hors de `Settings` : ils n'intéressent que le processus
serveur (compose publie 8012→8000, le sidecar Tekton rebinde via
`GA_MOCK_PORT`), jamais la logique du mock.
"""

from __future__ import annotations

import os

import uvicorn


def main() -> None:
    from .app import app

    uvicorn.run(
        app,
        host=os.environ.get("GA_MOCK_HOST", "0.0.0.0"),
        port=int(os.environ.get("GA_MOCK_PORT", "8000")),
    )


if __name__ == "__main__":
    main()
