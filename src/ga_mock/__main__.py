"""Entry point: `python -m ga_mock` (or the `ga-mock` console script).

HOST/PORT stay out of `Settings`: they only concern the server process
(compose publishes 8012→8000, the Tekton sidecar rebinds via
`GA_MOCK_PORT`), never the mock's logic.
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
