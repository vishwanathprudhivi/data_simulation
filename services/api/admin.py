"""Admin tasks: ``python -m services.api.admin create-user NAME PASSWORD --workspace WS``."""

from __future__ import annotations

import argparse

from services.platform.auth import hash_password
from services.platform.metadata import MetadataStore
from services.platform.settings import Settings


def create_user(store: MetadataStore, name: str, password: str, workspaces: list[str]) -> None:
    store.create_user(name, hash_password(password))
    for ws in workspaces or ["default"]:
        store.add_member(name, ws)


def main() -> None:  # pragma: no cover - CLI
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    cu = sub.add_parser("create-user")
    cu.add_argument("name")
    cu.add_argument("password")
    cu.add_argument("--workspace", action="append", default=[])
    args = parser.parse_args()
    create_user(MetadataStore(Settings().metadata), args.name, args.password, args.workspace)


if __name__ == "__main__":  # pragma: no cover
    main()
