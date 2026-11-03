"""Local settings and credential storage for the reMarkable Linux client.

Layout (XDG-aware, defaults shown):
    ~/.config/remarkable-linux/            directory, mode 0700
    ~/.config/remarkable-linux/config.json non-secret settings
    ~/.config/remarkable-linux/auth.json   device/user tokens, mode 0600

Both files are written atomically (temp file + rename) so a crash can never leave
a half-written token file behind.
"""

from __future__ import annotations

import json
import os
import stat
import tempfile
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

APP_DIR_NAME = "remarkable-linux"
CONFIG_FILE = "config.json"
AUTH_FILE = "auth.json"


def config_dir() -> Path:
    """Return the per-user config directory, honouring $XDG_CONFIG_HOME."""
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.join(Path.home(), ".config")
    return Path(base) / APP_DIR_NAME


def cache_dir() -> Path:
    """Return the per-user cache directory, honouring $XDG_CACHE_HOME."""
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(Path.home(), ".cache")
    return Path(base) / APP_DIR_NAME


def ensure_private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    # mkdir's mode is masked by umask and ignored for existing dirs; enforce it.
    if stat.S_IMODE(path.stat().st_mode) != 0o700:
        path.chmod(0o700)


def atomic_write_json(path: Path, data: dict[str, Any], mode: int = 0o600) -> None:
    ensure_private_dir(path.parent)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def read_json(path: Path) -> dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return data


@dataclass
class Settings:
    """Non-secret client settings (config.json).

    Host values are bare hostnames or full https:// URLs. Empty means "resolve
    automatically" (see core.auth.HostResolver and ARCHITECTURE.md §3.2).
    """

    # Overrides the resolved auth ("webapp" service) host when set.
    auth_host: str = ""
    # Gateway used before a user token tells us our tectonic region.
    default_gateway: str = "internal.cloud.remarkable.com"
    # deviceDesc sent on pairing. Matches what the Windows app identifies as; the
    # server's accepted set is not visible in the binary (ARCHITECTURE.md §6).
    device_desc: str = "desktop-windows"
    # Refresh the user token this many seconds before its JWT `exp`.
    refresh_margin_s: int = 300
    http_timeout_s: float = 30.0
    library_dir: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, directory: Path | None = None) -> "Settings":
        raw = read_json((directory or config_dir()) / CONFIG_FILE)
        known = {f.name for f in fields(cls)}
        settings = cls(**{k: v for k, v in raw.items() if k in known})
        settings.extra.update({k: v for k, v in raw.items() if k not in known})
        return settings

    def save(self, directory: Path | None = None) -> None:
        data = asdict(self)
        extra = data.pop("extra")
        data.update(extra)
        atomic_write_json((directory or config_dir()) / CONFIG_FILE, data, 0o600)


@dataclass
class AuthState:
    """Credentials persisted in auth.json (mode 0600)."""

    device_id: str = ""
    device_token: str = ""
    user_token: str = ""
    # Host that issued the device token; user tokens are requested from the same one.
    auth_host: str = ""

    @property
    def is_paired(self) -> bool:
        return bool(self.device_token)


class AuthStore:
    """Reads and writes auth.json, refusing to trust a world/group-readable file."""

    def __init__(self, directory: Path | None = None) -> None:
        self.path = (directory or config_dir()) / AUTH_FILE

    def load(self) -> AuthState:
        if self.path.exists():
            file_mode = stat.S_IMODE(self.path.stat().st_mode)
            if file_mode & 0o077:
                # Tighten rather than fail: the file is ours, permissions drifted.
                self.path.chmod(0o600)
        raw = read_json(self.path)
        known = {f.name for f in fields(AuthState)}
        state = AuthState(**{k: v for k, v in raw.items() if k in known})
        if not state.device_id:
            # deviceID is a stable per-installation UUID, generated once.
            state.device_id = str(uuid.uuid4())
        return state

    def save(self, state: AuthState) -> None:
        atomic_write_json(self.path, asdict(state), 0o600)

    def clear_tokens(self) -> AuthState:
        state = self.load()
        state.device_token = ""
        state.user_token = ""
        state.auth_host = ""
        self.save(state)
        return state
