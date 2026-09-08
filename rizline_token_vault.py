"""Authenticated encryption for revocable, per-binding game credentials."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
import stat
import time
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Callable

from Crypto.Cipher import AES


MAX_RETENTION_SECONDS = 7 * 24 * 3600
MAX_CREDENTIAL_BYTES = 16384


class TokenVaultError(RuntimeError):
    def __init__(self) -> None:
        super().__init__("Encrypted credential storage unavailable")


@dataclass(frozen=True, repr=False)
class GameCredential:
    token: str
    phone: str
    device_id: str
    channel_id: str
    expires_at: float
    account_id: str = ""


def token_deadline(token: str, *, now: float | None = None) -> float:
    current = time.time() if now is None else now
    deadline = current + MAX_RETENTION_SECONDS
    try:
        segment = token.split(".")[1]
        payload = json.loads(base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4)))
        expiry = payload.get("exp")
        if type(expiry) in (int, float) and math.isfinite(expiry):
            deadline = min(deadline, float(expiry))
    except (ValueError, IndexError, TypeError, AttributeError, OverflowError):
        pass
    return deadline


def validate_credential(credential: GameCredential) -> None:
    fields = ((credential.token, 8192), (credential.phone, 32), (credential.device_id, 128),
              (credential.channel_id, 2), (credential.account_id, 128))
    if any(not isinstance(value, str) or len(value) > limit or any(ord(char) < 32 for char in value)
           for value, limit in fields):
        raise TokenVaultError()
    if not credential.token or not credential.phone or not credential.device_id:
        raise TokenVaultError()
    if credential.channel_id not in {str(number) for number in range(1, 12)}:
        raise TokenVaultError()
    if type(credential.expires_at) not in (int, float) or not math.isfinite(credential.expires_at):
        raise TokenVaultError()


class TokenVault:
    def __init__(self, database: Path, key_file: Path, *, now: Callable[[], float] = time.time) -> None:
        self._database = Path(database)
        self._key_file = Path(key_file)
        self._now = now
        try:
            database_existed = self._database.exists()
            self._key = self._load_key(allow_create=not database_existed)
            self._database.parent.mkdir(parents=True, exist_ok=True)
            if not database_existed:
                try:
                    descriptor = os.open(self._database, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
                    os.close(descriptor)
                except FileExistsError:
                    pass
            self._check_private_file(self._database)
            with self._connect() as connection:
                connection.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value BLOB NOT NULL)")
                connection.execute("""CREATE TABLE IF NOT EXISTS credentials (
                    owner TEXT NOT NULL, alias TEXT NOT NULL, revision TEXT NOT NULL, envelope BLOB NOT NULL,
                    PRIMARY KEY (owner, alias))""")
                marker = hmac.digest(self._key, b"RizLine token vault key check v1", "sha256")
                stored = connection.execute("SELECT value FROM metadata WHERE name='key_check'").fetchone()
                if stored is None:
                    if database_existed:
                        raise TokenVaultError()
                    connection.execute("INSERT INTO metadata VALUES ('key_check', ?)", (marker,))
                elif not hmac.compare_digest(stored[0], marker):
                    raise TokenVaultError()
        except (OSError, sqlite3.Error, ValueError):
            raise TokenVaultError() from None

    @staticmethod
    def _check_private_file(path: Path) -> None:
        metadata = path.lstat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid() or stat.S_IMODE(metadata.st_mode) & 0o077:
            raise TokenVaultError()

    def _load_key(self, *, allow_create: bool) -> bytes:
        if not self._key_file.exists():
            if not allow_create:
                raise TokenVaultError()
            self._key_file.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                descriptor = os.open(self._key_file, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
            except FileExistsError:
                pass
            else:
                with os.fdopen(descriptor, "wb") as output:
                    output.write(secrets.token_bytes(32))
                    output.flush()
                    os.fsync(output.fileno())
        self._check_private_file(self._key_file)
        descriptor = os.open(self._key_file, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            key = source.read(33)
        if len(key) != 32:
            raise TokenVaultError()
        return key

    @contextmanager
    def _connect(self):
        connection = None
        try:
            self._check_private_file(self._database)
            connection = sqlite3.connect(self._database, timeout=5)
            connection.execute("PRAGMA secure_delete=ON")
            with connection:
                yield connection
        except (OSError, sqlite3.Error):
            raise TokenVaultError() from None
        finally:
            if connection:
                connection.close()

    def _owner(self, openid: str) -> str:
        if not isinstance(openid, str) or not 0 < len(openid) <= 128:
            raise TokenVaultError()
        return hmac.new(self._key, b"owner:" + openid.encode(), hashlib.sha256).hexdigest()

    @staticmethod
    def _aad(owner: str, alias: str, revision: str) -> bytes:
        if not isinstance(alias, str) or not 0 < len(alias) <= 40 or not isinstance(revision, str) or len(revision) > 128:
            raise TokenVaultError()
        return json.dumps(["rizline-credentials-v1", owner, alias, revision], separators=(",", ":")).encode()

    def put(self, openid: str, alias: str, revision: str, credential: GameCredential) -> None:
        validate_credential(credential)
        if credential.expires_at <= self._now():
            raise TokenVaultError()
        owner = self._owner(openid)
        plaintext = json.dumps(asdict(credential), separators=(",", ":"), allow_nan=False).encode()
        if len(plaintext) > MAX_CREDENTIAL_BYTES:
            raise TokenVaultError()
        nonce = secrets.token_bytes(12)
        cipher = AES.new(self._key, AES.MODE_GCM, nonce=nonce)
        cipher.update(self._aad(owner, alias, revision))
        ciphertext, tag = cipher.encrypt_and_digest(plaintext)
        envelope = b"RZV1" + nonce + tag + ciphertext
        with self._connect() as connection:
            connection.execute("INSERT OR REPLACE INTO credentials VALUES (?, ?, ?, ?)",
                               (owner, alias, revision, envelope))

    def get(self, openid: str, alias: str, revision: str) -> GameCredential | None:
        owner = self._owner(openid)
        with self._connect() as connection:
            row = connection.execute("SELECT revision, envelope FROM credentials WHERE owner=? AND alias=?",
                                     (owner, alias)).fetchone()
        if row is None or row[0] != revision:
            return None
        envelope = row[1]
        try:
            if not isinstance(envelope, bytes) or not 32 < len(envelope) <= MAX_CREDENTIAL_BYTES + 32 or envelope[:4] != b"RZV1":
                raise TokenVaultError()
            cipher = AES.new(self._key, AES.MODE_GCM, nonce=envelope[4:16])
            cipher.update(self._aad(owner, alias, revision))
            plaintext = cipher.decrypt_and_verify(envelope[32:], envelope[16:32])
            credential = GameCredential(**json.loads(plaintext))
            validate_credential(credential)
        except (ValueError, TypeError, KeyError):
            raise TokenVaultError() from None
        if credential.expires_at <= self._now():
            self.delete(openid, alias=alias, revision=revision)
            return None
        return credential

    def delete(self, openid: str, *, alias: str | None = None, revision: str | None = None) -> None:
        query = "DELETE FROM credentials WHERE owner=?"
        parameters = [self._owner(openid)]
        if alias is not None:
            query += " AND alias=?"
            parameters.append(alias)
        if revision is not None:
            query += " AND revision=?"
            parameters.append(revision)
        with self._connect() as connection:
            connection.execute(query, parameters)

    def retain_only(self, openid: str, alias: str) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM credentials WHERE owner=? AND alias<>?", (self._owner(openid), alias))
