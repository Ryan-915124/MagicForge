"""Explicit, non-interactive-safe bootstrap command for the first admin.

The command never accepts a password argument because command-line arguments
are commonly retained in process listings and shell history.  Use the hidden
prompt or explicitly name an environment variable containing the password.
"""

from __future__ import annotations

import argparse
import getpass
import math
import os
import sys
from collections.abc import Sequence

from app.config import get_settings
from persistence import (
    DatabaseInitializationError,
    DatabaseManager,
    EXPECTED_DATABASE_REVISION,
)
from security.crypto import PasswordPolicyError
from security.services import AdminService, SecurityServiceError


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m security.admin_cli",
        description="Explicit MagicForge administrator provisioning.",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser(
        "create-initial-admin",
        help="Create the first administrator only when the user table is empty.",
    )
    create.add_argument("--username", required=True)
    create.add_argument("--email")
    create.add_argument(
        "--password-env",
        metavar="ENV_NAME",
        help="Read the password from this explicitly named environment variable.",
    )
    create.add_argument(
        "--reason",
        default="Explicitly bootstrap the initial administrator from the local CLI.",
    )
    return parser


def _password(password_environment_name: str | None) -> str:
    if password_environment_name:
        if not password_environment_name.isidentifier():
            raise ValueError("password environment variable name is invalid")
        password = os.environ.pop(password_environment_name, None)
        if password is None:
            raise ValueError("password environment variable is not set")
        return password
    first = getpass.getpass("Administrator password: ")
    second = getpass.getpass("Confirm administrator password: ")
    if first != second:
        raise ValueError("password confirmation does not match")
    return first


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    manager: DatabaseManager | None = None
    try:
        password = _password(arguments.password_env)
        settings = get_settings()
        manager = DatabaseManager(
            settings.database_url,
            engine_options={
                "connect_args": {
                    "connect_timeout": max(
                        1, math.ceil(settings.database_connect_timeout_seconds)
                    )
                }
            },
            expected_revision=EXPECTED_DATABASE_REVISION,
        )
        manager.start()
        service = AdminService(manager.require_ready())
        user = service.bootstrap_initial_admin(
            username=arguments.username,
            password=password,
            email=arguments.email,
            reason=arguments.reason,
        )
    except (
        DatabaseInitializationError,
        PasswordPolicyError,
        SecurityServiceError,
        ValueError,
    ) as exc:
        message = getattr(exc, "public_message", str(exc))
        print(f"Administrator provisioning failed: {message}", file=sys.stderr)
        return 1
    finally:
        # Do not retain the raw password longer than the operation requires.
        if "password" in locals():
            password = ""  # noqa: F841 - explicit best-effort reference release
        if manager is not None:
            manager.close()

    print(f"Created initial administrator {user.username!r} ({user.id}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
