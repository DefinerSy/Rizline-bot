from __future__ import annotations

import base64
import json
import sqlite3
import stat
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from pathlib import Path

from rizline_token_vault import GameCredential, TokenVault, TokenVaultError, token_deadline


class TokenVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.db = self.root / "tokens.sqlite3"
        self.key = self.root / "secrets" / "master.key"
        self.clock = 1000.0
        self.vault = TokenVault(self.db, self.key, now=lambda: self.clock)
        self.credential = GameCredential("synthetic-secret-token", "13800000000", "private-device", "1", 2000, "game-user")

    def test_reopen_roundtrip_permissions_and_no_plaintext(self):
        self.vault.put("qq-player", "snapshot", "rev1", self.credential)
        reopened = TokenVault(self.db, self.key, now=lambda: self.clock)
        self.assertEqual(reopened.get("qq-player", "snapshot", "rev1"), self.credential)
        for path in (self.db, self.key):
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.key.parent.stat().st_mode), 0o700)
        self.assertEqual(self.key.stat().st_size, 32)
        for value in ("synthetic-secret-token", "13800000000", "private-device", "game-user", "qq-player"):
            self.assertNotIn(value.encode(), self.db.read_bytes())
            self.assertNotIn(value, repr(self.credential))

    def test_randomized_encryption_and_owner_revision_isolation(self):
        self.vault.put("qq-player", "snapshot", "rev1", self.credential)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            first = conn.execute("SELECT envelope FROM credentials").fetchone()[0]
        self.vault.put("qq-player", "snapshot", "rev1", self.credential)
        with closing(sqlite3.connect(self.db)) as conn, conn:
            second = conn.execute("SELECT envelope FROM credentials").fetchone()[0]
        self.assertNotEqual(first, second)
        self.assertIsNone(self.vault.get("other-qq", "snapshot", "rev1"))
        self.assertIsNone(self.vault.get("qq-player", "snapshot", "rev2"))
        self.assertIsNone(self.vault.get("qq-player", "other-alias", "rev1"))

    def test_tamper_and_relabel_fail_authentication(self):
        for column, new_value in (("envelope", b"RZV1" + b"x" * 100), ("alias", "other"), ("revision", "rev2"),
                                  ("owner", self.vault._owner("other-qq"))):
            with self.subTest(column=column):
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute("DELETE FROM credentials")
                self.vault.put("qq-player", "snapshot", "rev1", self.credential)
                with closing(sqlite3.connect(self.db)) as conn, conn:
                    conn.execute(f"UPDATE credentials SET {column}=?", (new_value,))
                with self.assertRaises(TokenVaultError):
                    self.vault.get("other-qq" if column == "owner" else "qq-player",
                                   "other" if column == "alias" else "snapshot",
                                   "rev2" if column == "revision" else "rev1")

    def test_wrong_missing_and_public_key_fail_without_replacing_database(self):
        self.vault.put("qq-player", "snapshot", "rev1", self.credential)
        original = self.db.read_bytes()
        correct_key = self.key.read_bytes()
        self.key.write_bytes(b"x" * 32)
        with self.assertRaises(TokenVaultError):
            TokenVault(self.db, self.key)
        self.assertEqual(self.db.read_bytes(), original)
        self.key.unlink()
        with self.assertRaises(TokenVaultError):
            TokenVault(self.db, self.key)
        self.assertFalse(self.key.exists())
        self.key.write_bytes(correct_key)
        self.key.chmod(0o644)
        with self.assertRaises(TokenVaultError):
            TokenVault(self.db, self.key)

    def test_symlink_key_or_database_rejected(self):
        linked_key = self.root / "linked.key"
        linked_key.symlink_to(self.key)
        with self.assertRaises(TokenVaultError):
            TokenVault(self.db, linked_key)
        linked_db = self.root / "linked.sqlite3"
        linked_db.symlink_to(self.db)
        with self.assertRaises(TokenVaultError):
            TokenVault(linked_db, self.key)

    def test_expiry_is_removed_and_cannot_be_saved(self):
        self.vault.put("qq-player", "snapshot", "rev1", self.credential)
        self.clock = 2000
        self.assertIsNone(self.vault.get("qq-player", "snapshot", "rev1"))
        with closing(sqlite3.connect(self.db)) as conn, conn:
            self.assertEqual(conn.execute("SELECT count(*) FROM credentials").fetchone()[0], 0)
        with self.assertRaises(TokenVaultError):
            self.vault.put("qq-player", "snapshot", "rev1", self.credential)

    def test_scoped_cleanup_cannot_remove_another_player_or_new_revision(self):
        for owner, alias in (("qq-player", "old"), ("qq-player", "new"), ("other-qq", "old")):
            self.vault.put(owner, alias, "rev2", self.credential)
        self.vault.delete("qq-player", alias="old", revision="rev1")
        self.assertIsNotNone(self.vault.get("qq-player", "old", "rev2"))
        self.vault.retain_only("qq-player", "new")
        self.assertIsNone(self.vault.get("qq-player", "old", "rev2"))
        self.assertIsNotNone(self.vault.get("other-qq", "old", "rev2"))
        self.vault.delete("qq-player")
        self.assertIsNone(self.vault.get("qq-player", "new", "rev2"))

    def test_expiry_hint_cannot_extend_local_limit(self):
        def jwt(exp):
            encoded = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
            return f"header.{encoded}.signature"
        self.assertEqual(token_deadline(jwt(1500), now=1000), 1500)
        self.assertEqual(token_deadline(jwt(99999999), now=1000), 1000 + 7 * 86400)
        self.assertEqual(token_deadline("opaque", now=1000), 1000 + 7 * 86400)
        self.assertEqual(token_deadline(jwt(True), now=1000), 1000 + 7 * 86400)

    def test_invalid_header_credential_not_persisted(self):
        with self.assertRaises(TokenVaultError):
            self.vault.put("qq-player", "alias", "rev1", replace(self.credential, token="bad\r\nheader"))


if __name__ == "__main__":
    unittest.main()
