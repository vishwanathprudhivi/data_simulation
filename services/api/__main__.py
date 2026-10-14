"""``python -m services.api`` runs the API with uvicorn (``make api``)."""

from __future__ import annotations

import os


def main() -> None:  # pragma: no cover - process entrypoint
    import uvicorn

    from services.api.app import create_app

    uvicorn.run(
        create_app(),
        host=os.environ.get("SIMCORE_HOST", "127.0.0.1"),
        port=int(os.environ.get("SIMCORE_PORT", "8000")),
    )


if __name__ == "__main__":  # pragma: no cover
    main()
