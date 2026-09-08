"""One-time-code bindings between QQ OpenIDs and locally imported RizLine saves.

The database deliberately stores only a QQ OpenID, a local player alias, and
short-lived *hashed* codes.  It never receives or stores a game login,
password, SMS code, or game token.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


PLAYER_ALIAS_RE = re.compile(r"^[\w-]{1,40}$", re.UNICODE)
MAX_OPENID_LENGTH = 128
MAX_CODE_LENGTH = 128


@dataclass(frozen=True)
class BindingRedemption:
    status: str
    alias: str | None = None


@dataclass(frozen=True)
class BindingState:
    alias: str | None
    revision: str


class RizlineBindingStore:
    """Manage per-QQ aliases with atomic updates guarded by a local lock."""

    def __init__(self, path: Path | str, *, now: Callable[[], float] = time.time,
                 credential_revoker: Callable[[str], None] | None = None) -> None:
        self._path = Path(path)
        self._now = now
        self.credential_revoker = credential_revoker

    def issue_code(self, alias: str, *, expires_seconds: int = 900) -> str:
        self._validate_alias(alias)
        if not 60 <= expires_seconds <= 3600:
            raise ValueError("绑定码有效期应为 1 到 60 分钟")

        def update(data: dict[str, Any]) -> tuple[str, bool]:
            codes = data["codes"]
            code = ""
            digest = ""
            while not code or digest in codes:
                code = secrets.token_urlsafe(15)
                digest = self._digest(code)
            codes[digest] = {"alias": alias, "expires_at": self._now() + expires_seconds}
            return code, True

        return self._with_lock(update)

    def redeem(self, code: str, openid: str) -> BindingRedemption:
        normalized_code = code.strip()
        if not normalized_code or len(normalized_code) > MAX_CODE_LENGTH:
            return BindingRedemption("invalid")
        if not self._valid_openid(openid):
            return BindingRedemption("invalid_openid")
        digest = self._digest(normalized_code)

        def update(data: dict[str, Any]) -> tuple[BindingRedemption, bool]:
            payload = data["codes"].pop(digest, None)
            if not isinstance(payload, dict):
                return BindingRedemption("invalid"), False
            alias = payload.get("alias")
            if not isinstance(alias, str) or not PLAYER_ALIAS_RE.fullmatch(alias):
                return BindingRedemption("invalid"), True
            if self.credential_revoker:
                self.credential_revoker(openid)
            data["bindings"][openid] = alias
            data.setdefault("binding_revisions", {})[openid] = secrets.token_hex(16)
            return BindingRedemption("bound", alias), True

        return self._with_lock(update)

    def alias_for(self, openid: str) -> str | None:
        if not self._valid_openid(openid):
            return None

        def read(data: dict[str, Any]) -> tuple[str | None, bool]:
            alias = data["bindings"].get(openid)
            if isinstance(alias, str) and PLAYER_ALIAS_RE.fullmatch(alias):
                return alias, False
            return None, False

        return self._with_lock(read)

    def state_for(self, openid: str) -> BindingState:
        if not self._valid_openid(openid):
            return BindingState(None, "")

        def read(data: dict[str, Any]) -> tuple[BindingState, bool]:
            alias = data["bindings"].get(openid)
            if not isinstance(alias, str) or not PLAYER_ALIAS_RE.fullmatch(alias):
                alias = None
            revision = data.get("binding_revisions", {}).get(openid, "")
            return BindingState(alias, revision), False

        return self._with_lock(read)

    def bind_verified(self, openid: str, alias: str, *, expected_alias: str | None,
                      expected_revision: str | None = None,
                      prepare: Callable[[str], None] | None = None) -> bool:
        self._validate_alias(alias)
        if not self._valid_openid(openid):
            raise ValueError("无法识别 QQ 身份")

        def update(data: dict[str, Any]) -> tuple[bool, bool]:
            if data["bindings"].get(openid) != expected_alias:
                return False, False
            revisions = data.setdefault("binding_revisions", {})
            if expected_revision is not None and revisions.get(openid, "") != expected_revision:
                return False, False
            revision = secrets.token_hex(16)
            if prepare:
                prepare(revision)
            data["bindings"][openid] = alias
            revisions[openid] = revision
            return True, True

        return self._with_lock(update)

    def unbind(self, openid: str) -> bool:
        if not self._valid_openid(openid):
            return False

        def update(data: dict[str, Any]) -> tuple[bool, bool]:
            if self.credential_revoker:
                self.credential_revoker(openid)
            removed = data["bindings"].pop(openid, None) is not None
            data.setdefault("binding_revisions", {})[openid] = secrets.token_hex(16)
            return removed, True

        return self._with_lock(update)

    def while_current(self, openid: str, alias: str, revision: str, operation: Callable[[], None]) -> bool:
        """Run credential cleanup under the same lock, only for this binding version."""
        def check(data: dict[str, Any]) -> tuple[bool, bool]:
            if (data["bindings"].get(openid) != alias
                    or data.get("binding_revisions", {}).get(openid, "") != revision):
                return False, False
            operation()
            return True, False

        return self._with_lock(check)

    def _with_lock(self, operation: Callable[[dict[str, Any]], tuple[Any, bool]]) -> Any:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self._path.with_name(f".{self._path.name}.lock")
        try:
            with lock_path.open("a+", encoding="utf-8") as lock_file:
                os.chmod(lock_path, 0o600)
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    data = self._read_data()
                    changed = self._prune_expired_codes(data)
                    result, operation_changed = operation(data)
                    if changed or operation_changed:
                        self._write_data(data)
                    return result
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except OSError as exc:
            raise RuntimeError("无法访问 RizLine 绑定数据") from exc

    def _read_data(self) -> dict[str, Any]:
        try:
            payload = self._path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return {"format_version": 1, "bindings": {}, "codes": {}}
        except OSError as exc:
            raise RuntimeError("无法读取 RizLine 绑定数据") from exc
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise RuntimeError("RizLine 绑定数据格式无效，已拒绝覆盖") from exc
        if not isinstance(data, dict) or not isinstance(data.get("bindings"), dict) or not isinstance(data.get("codes"), dict):
            raise RuntimeError("RizLine 绑定数据格式无效，已拒绝覆盖")
        revisions = data.get("binding_revisions", {})
        if not isinstance(revisions, dict) or any(not isinstance(value, str) for value in revisions.values()):
            raise RuntimeError("RizLine 绑定版本数据格式无效，已拒绝覆盖")
        return data

    def _write_data(self, data: dict[str, Any]) -> None:
        data["format_version"] = 1
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n"
        temporary = self._path.with_name(f".{self._path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")
        try:
            temporary.write_text(encoded, encoding="utf-8")
            os.chmod(temporary, 0o600)
            temporary.replace(self._path)
        except OSError as exc:
            raise RuntimeError("无法写入 RizLine 绑定数据") from exc

    def _prune_expired_codes(self, data: dict[str, Any]) -> bool:
        now = self._now()
        expired = [
            digest
            for digest, payload in data["codes"].items()
            if not isinstance(payload, dict)
            or not isinstance(payload.get("expires_at"), (int, float))
            or float(payload["expires_at"]) <= now
        ]
        for digest in expired:
            del data["codes"][digest]
        return bool(expired)

    @staticmethod
    def _digest(code: str) -> str:
        return hashlib.sha256(code.encode("utf-8")).hexdigest()

    @staticmethod
    def _validate_alias(alias: str) -> None:
        if not PLAYER_ALIAS_RE.fullmatch(alias):
            raise ValueError("玩家别名只能包含汉字、字母、数字、下划线或连字符，且不超过 40 个字符")

    @staticmethod
    def _valid_openid(openid: str) -> bool:
        return bool(openid) and len(openid) <= MAX_OPENID_LENGTH
