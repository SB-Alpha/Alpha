"""Secret values for the platform: encrypted in the SQLite file, passed around as references.

Modules exchange only references like secret://mig-001/source-db-admin. The encryption key
comes from ALPHA_SECRETS_KEY, or else from the OS keychain (created on first use).
"""

import json
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import data_dir

PREFIX = "secret://"
KEYRING_SERVICE = "alpha-platform"
KEYRING_USER = "secrets-key"

_key_lock = threading.Lock()


def _db_path() -> Path:
    path = data_dir() / "alpha.db"
    if not path.exists():
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
    return path


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path(), timeout=10)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS secrets (name TEXT PRIMARY KEY, value BLOB NOT NULL, created_at TEXT NOT NULL)"
    )
    return conn


def _fernet() -> Fernet:
    key = os.getenv("ALPHA_SECRETS_KEY")
    if key:
        return Fernet(key)
    import keyring  # only needed when the key is not given by environment

    with _key_lock:
        key = keyring.get_password(KEYRING_SERVICE, KEYRING_USER)
        if key is None:
            key = Fernet.generate_key().decode()
            keyring.set_password(KEYRING_SERVICE, KEYRING_USER, key)
    return Fernet(key)


def _name(ref: str) -> str:
    return ref[len(PREFIX):] if ref.startswith(PREFIX) else ref


def put_secret(name: str, value: dict[str, str]) -> str:
    """Store and return the reference. The same name overwrites."""
    name = _name(name)
    token = _fernet().encrypt(json.dumps(value).encode())
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    with closing(_connect()) as conn, conn:
        conn.execute(
            "INSERT INTO secrets (name, value, created_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value",
            (name, token, now),
        )
    return PREFIX + name


def resolve(ref: str) -> dict[str, str]:
    """Raises KeyError naming the reference when it is missing."""
    with closing(_connect()) as conn:
        row = conn.execute("SELECT value FROM secrets WHERE name = ?", (_name(ref),)).fetchone()
    if row is None:
        raise KeyError(ref)
    try:
        return json.loads(_fernet().decrypt(row[0]))
    except InvalidToken:
        raise RuntimeError(f"{ref} does not decrypt with the current key") from None


def has_secret(ref: str) -> bool:
    with closing(_connect()) as conn:
        return conn.execute("SELECT 1 FROM secrets WHERE name = ?", (_name(ref),)).fetchone() is not None


def delete_secret(ref: str) -> None:
    with closing(_connect()) as conn, conn:
        conn.execute("DELETE FROM secrets WHERE name = ?", (_name(ref),))
