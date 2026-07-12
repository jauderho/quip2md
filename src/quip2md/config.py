"""Configuration loading for quip2md.

Reads the Quip API token and related settings from a `.env` file and the
process environment. The token is never logged or printed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import dotenv_values

DEFAULT_BASE_URL = "https://platform.quip.com"
DEFAULT_OUTPUT_DIR = Path("export")
DEFAULT_STATE_PATH = Path(".quip2md/state.json")


class ConfigError(RuntimeError):
    """Raised when configuration cannot be loaded (e.g. QUIP_TOKEN is missing)."""


@dataclass(slots=True, frozen=True)
class Config:
    token: str
    output_dir: Path
    state_path: Path
    dry_run: bool
    verbose: bool
    include_chats: bool
    force: bool
    base_url: str = DEFAULT_BASE_URL


def load_config(
    *,
    env_path: Path | None = None,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
    state_path: Path = DEFAULT_STATE_PATH,
    dry_run: bool = False,
    verbose: bool = False,
    include_chats: bool = False,
    force: bool = False,
) -> Config:
    """Build a `Config` from a `.env` file and the process environment.

    `QUIP_TOKEN` (and `QUIP_BASE_URL`) are read from the process environment
    first; if unset there, they fall back to the `.env` file at `env_path`
    (default: `.env` in the current working directory). Process-env values
    always take precedence over `.env` values.

    Raises:
        ConfigError: if `QUIP_TOKEN` is missing or empty in both sources.
    """
    dotenv_path = env_path if env_path is not None else Path(".env")
    file_values = dotenv_values(dotenv_path) if dotenv_path.is_file() else {}

    token = (os.environ.get("QUIP_TOKEN") or file_values.get("QUIP_TOKEN") or "").strip()
    if not token:
        raise ConfigError(
            "QUIP_TOKEN is missing or empty. Set it in the process environment "
            "or in a .env file."
        )

    base_url = (
        os.environ.get("QUIP_BASE_URL") or file_values.get("QUIP_BASE_URL") or DEFAULT_BASE_URL
    )

    return Config(
        token=token,
        base_url=base_url,
        output_dir=output_dir,
        state_path=state_path,
        dry_run=dry_run,
        verbose=verbose,
        include_chats=include_chats,
        force=force,
    )
