from __future__ import annotations

import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from Crypto.Cipher import AES

from bot import QQOfficialBot, ReplyService
from rizline import RizlineScoreService
from rizline_bindings import RizlineBindingStore
from rizline_qq_login import GameLoginError, QQLoginService, RizlineSmsClient, minimized_save
from rizline_token_vault import GameCredential, TokenVault, TokenVaultError
from test_rizline_qq_login import CODE, PHONE, TOKEN, FakeResponse, sample_save


class EncryptedLoginTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.clock = 1000.0
        self.wall = time.time()
        self.vault = TokenVault(self.root / "tokens.sqlite3", self.root / "secrets" / "key", now=lambda: self.wall)
        self.bindings = RizlineBindingStore(self.root / "bindings.json")
        self.credential = GameCredential(TOKEN, PHONE, "device", "1", self.wall + 3600, "test-account-id")
        self.snapshot = replace(minimized_save(sample_save()), credential=self.credential)
        self.client = Mock(send_code=AsyncMock(return_value=True),
                           verify_and_fetch=AsyncMock(return_value=self.snapshot),
                           fetch_with_token=AsyncMock(return_value=self.snapshot))
        self.start_service()

    def start_service(self):
        self.login = QQLoginService(self.bindings, self.root / "saves", self.client,
                                    vault=self.vault, now=lambda: self.clock)
        self.replies = ReplyService(RizlineScoreService(self.root / "saves"), bindings=self.bindings, login=self.login)

    def tearDown(self):
        for owner in list(self.login._sessions):
            self.login.cancel(owner)
        self.temp.cleanup()

    async def reply(self, text, owner="player", source="c2c"):
        return await self.replies.reply_for(text, source=source, user_openid=owner)

    def credential_for(self, owner="player"):
        state = self.bindings.state_for(owner)
        return self.vault.get(owner, state.alias, state.revision) if state.alias else None

    async def authorize(self, *, consent="同意并保存", confirm=True, owner="player"):
        self.assertIn("加密", await self.reply("/riz login", owner))
        await self.reply(consent, owner)
        await self.reply(PHONE, owner)
        self.assertIn("Test Player", await self.reply(CODE, owner))
        if confirm:
            self.assertIn("已绑定", await self.reply("确认", owner))

    def row_count(self):
        with closing(sqlite3.connect(self.root / "tokens.sqlite3")) as conn:
            return conn.execute("SELECT count(*) FROM credentials").fetchone()[0]

    async def test_credentials_only_saved_after_explicit_consent_and_confirmation(self):
        await self.authorize(confirm=False)
        self.assertEqual(self.row_count(), 0)
        self.assertIsNone(self.bindings.alias_for("player"))
        self.assertIn("加密保存", await self.reply("确认"))
        self.assertEqual(self.credential_for(), self.credential)
        for path in (self.root / "saves").glob("*.json"):
            payload = path.read_text()
            for secret in (TOKEN, PHONE, CODE, "test-account-id"):
                self.assertNotIn(secret, payload)

    async def test_cancel_before_confirm_does_not_persist(self):
        await self.authorize(confirm=False)
        await self.reply("/riz cancel")
        self.assertEqual(self.row_count(), 0)
        self.assertIsNone(self.bindings.alias_for("player"))

    async def test_one_time_login_and_legacy_users_do_not_silently_opt_in(self):
        await self.authorize(consent="同意")
        self.assertEqual(self.row_count(), 0)
        self.assertIn("同意并保存", await self.reply("/riz update"))
        self.client.fetch_with_token.assert_not_awaited()

    async def test_update_survives_restart_uses_no_sms_and_keeps_old_snapshot(self):
        await self.authorize()
        original = self.bindings.state_for("player")
        path = self.root / "saves" / f"{original.alias}.json"
        old_bytes = path.read_bytes()
        self.bindings = RizlineBindingStore(self.root / "bindings.json")
        self.vault = TokenVault(self.root / "tokens.sqlite3", self.root / "secrets" / "key")
        self.start_service()
        self.client.send_code.reset_mock()
        self.client.verify_and_fetch.reset_mock()
        rotated = replace(self.credential, token="rotated-synthetic-token")
        self.client.fetch_with_token.return_value = replace(self.snapshot, credential=rotated)
        self.assertIn("已更新", await self.reply("/riz update"))
        self.client.fetch_with_token.assert_awaited_once_with(self.credential)
        self.client.send_code.assert_not_awaited()
        self.client.verify_and_fetch.assert_not_awaited()
        self.assertNotEqual(self.bindings.state_for("player"), original)
        self.assertEqual(path.read_bytes(), old_bytes)
        self.assertEqual(self.credential_for(), rotated)
        self.assertEqual(self.row_count(), 1)

    async def test_transient_and_format_errors_preserve_credentials_and_binding(self):
        await self.authorize()
        original = self.bindings.state_for("player")
        for error in (TimeoutError(), GameLoginError("upstream"), GameLoginError("limited"), GameLoginError("save")):
            self.client.fetch_with_token.side_effect = error
            await self.reply("/riz update")
            self.clock += 61
            self.assertEqual(self.bindings.state_for("player"), original)
            self.assertEqual(self.credential_for(), self.credential)
        self.assertEqual(len(list((self.root / "saves").glob("*.json"))), 1)

    async def test_upstream_expired_token_is_revoked_but_old_scores_remain(self):
        await self.authorize()
        original = self.bindings.state_for("player")
        self.client.fetch_with_token.side_effect = GameLoginError("expired")
        self.assertIn("已失效", await self.reply("/riz update"))
        self.assertEqual(self.bindings.state_for("player"), original)
        self.assertIsNone(self.credential_for())

    async def test_local_expiry_never_calls_game_or_sms(self):
        await self.authorize()
        self.wall += 4000
        self.assertIn("未找到", await self.reply("/riz update"))
        self.client.fetch_with_token.assert_not_awaited()
        self.assertEqual(self.row_count(), 0)

    async def test_group_update_shares_c2c_cooldown_but_not_sms_quota(self):
        await self.authorize()
        self.assertIn("已更新", await self.reply("<@!1234> /riz update", source="group"))
        self.assertIn("60 秒", await self.reply("/riz update"))
        self.clock += 61
        self.assertIn("已更新", await self.reply("/riz update"))
        self.assertIn("60 秒", await self.reply("/riz update", source="group"))
        self.assertEqual(len(self.login._rate_history["global"]), 1)
        self.assertIn("未找到", await self.reply("/riz update", owner="someone-else"))

    async def test_group_update_never_starts_sms_or_accepts_account_arguments(self):
        await self.authorize()
        self.client.send_code.reset_mock()
        self.client.verify_and_fetch.reset_mock()
        for owner in (None, "", "unbound-player"):
            result = await self.reply("/riz update", owner=owner, source="group")
            self.assertNotIn("已更新", result)
        for argument in ("player", PHONE, CODE, TOKEN):
            result = await self.reply("/riz update " + argument, source="group")
            self.assertIn("不接受", result)
            self.assertNotIn(argument, result)
        self.client.fetch_with_token.assert_not_awaited()
        self.assertIn("已更新", await self.reply("/riz 更新", source="group"))
        self.client.send_code.assert_not_awaited()
        self.client.verify_and_fetch.assert_not_awaited()

    async def test_group_update_does_not_advance_or_cancel_private_login(self):
        await self.reply("/riz login")
        session = self.login._sessions["player"]
        for text in ("/riz update", "/riz login", "/riz confirm", "/riz resend", "/riz cancel",
                     "同意并保存", PHONE, CODE, "确认", "取消"):
            await self.reply(text, source="group")
            self.assertIs(self.login._sessions["player"], session)
            self.assertEqual(session.stage, "consent")
        self.client.send_code.assert_not_awaited()
        self.client.verify_and_fetch.assert_not_awaited()
        self.client.fetch_with_token.assert_not_awaited()

    async def test_expired_group_update_redirects_to_private_login_without_secrets(self):
        await self.authorize()
        self.client.send_code.reset_mock()
        self.client.fetch_with_token.side_effect = GameLoginError("expired")
        result = await self.reply("/riz update", source="group")
        self.assertIn("私聊", result)
        for secret in (PHONE, CODE, TOKEN):
            self.assertNotIn(secret, result)
        self.assertIsNone(self.credential_for())
        self.client.send_code.assert_not_awaited()

    async def test_group_callback_uses_sender_not_group_or_other_author_identifiers(self):
        await self.authorize()
        original = self.bindings.state_for("player")
        bot = SimpleNamespace(_replies=self.replies, _sender_openid=QQOfficialBot._sender_openid)
        message = SimpleNamespace(content="<@1234> /riz update", group_openid="player",
                                  author=SimpleNamespace(member_openid="other-player", user_openid="player"),
                                  reply=AsyncMock())
        await QQOfficialBot._reply(bot, message, source="group")
        self.client.fetch_with_token.assert_not_awaited()
        self.assertEqual(self.bindings.state_for("player"), original)
        message.author = SimpleNamespace(member_openid="player")
        await QQOfficialBot._reply(bot, message, source="group")
        self.assertIn("已更新", message.reply.call_args.kwargs["content"])
        self.client.fetch_with_token.assert_awaited_once_with(self.credential)

    async def test_unbind_and_valid_manual_rebind_revoke_but_invalid_code_does_not(self):
        await self.authorize()
        original = self.bindings.state_for("player")
        self.bindings.redeem("bad-code", "player")
        self.assertEqual(self.credential_for(), self.credential)
        self.bindings.redeem(self.bindings.issue_code(original.alias), "player")
        self.assertNotEqual(self.bindings.state_for("player").revision, original.revision)
        self.assertIsNone(self.credential_for())
        self.clock += 61
        await self.authorize()
        self.assertIn("凭据已删除", await self.reply("/riz unbind"))
        self.assertEqual(self.row_count(), 0)
        self.assertIsNone(self.bindings.alias_for("player"))

    async def test_one_time_relogin_revokes_previously_saved_credentials(self):
        await self.authorize()
        self.clock += 61
        await self.authorize(consent="仅本次登录")
        self.assertEqual(self.row_count(), 0)

    async def test_failed_commit_retains_old_binding_and_credentials(self):
        await self.authorize()
        original = self.bindings.state_for("player")
        for target, error in ((self.vault, TokenVaultError()), (self.bindings, RuntimeError("synthetic write failure"))):
            method = "put" if target is self.vault else "_write_data"
            with patch.object(target, method, side_effect=error):
                self.assertIn("未能完成保存", await self.reply("/riz update"))
            self.clock += 61
            self.assertEqual(self.bindings.state_for("player"), original)
            self.assertEqual(self.credential_for(), self.credential)
            self.assertEqual(self.row_count(), 1)
            self.assertEqual(len(list((self.root / "saves").iterdir())), 1)

    async def test_inflight_update_cannot_restore_unbound_or_same_alias_rebound_account(self):
        await self.authorize()
        original = self.bindings.state_for("player")
        started, release = asyncio.Event(), asyncio.Event()
        async def fetch(credential):
            started.set()
            await release.wait()
            return self.snapshot
        self.client.fetch_with_token.side_effect = fetch
        task = asyncio.create_task(self.reply("/riz update", source="group"))
        await started.wait()
        self.bindings.unbind("player")
        self.bindings.redeem(self.bindings.issue_code(original.alias), "player")
        rebound = self.bindings.state_for("player")
        release.set()
        self.assertIn("发生了变化", await task)
        self.assertEqual(self.bindings.state_for("player"), rebound)
        self.assertEqual(self.row_count(), 0)
        self.assertEqual(len(list((self.root / "saves").glob("*.json"))), 1)

    async def test_late_result_after_cancel_cannot_commit_or_cancel_new_session(self):
        await self.authorize()
        original = self.bindings.state_for("player")
        started, release = asyncio.Event(), asyncio.Event()
        async def fetch(credential):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            return self.snapshot
        self.client.fetch_with_token.side_effect = fetch
        task = asyncio.create_task(self.reply("/riz update"))
        await started.wait()
        await self.reply("/riz cancel")
        await self.reply("/riz login")
        newer = self.login._sessions["player"]
        release.set()
        self.assertIn("已取消", await task)
        self.assertIs(self.login._sessions["player"], newer)
        self.assertEqual(self.bindings.state_for("player"), original)
        self.assertEqual(self.credential_for(), self.credential)

    async def test_late_unauthorized_response_cannot_delete_new_login_credentials(self):
        await self.authorize()
        started, release = asyncio.Event(), asyncio.Event()
        async def fetch(credential):
            started.set()
            try:
                await release.wait()
            except asyncio.CancelledError:
                await release.wait()
            raise GameLoginError("expired")
        self.client.fetch_with_token.side_effect = fetch
        task = asyncio.create_task(self.reply("/riz update"))
        await started.wait()
        await self.reply("/riz cancel")
        self.clock += 61
        newer_credential = replace(self.credential, token="new-login-token")
        self.client.verify_and_fetch.return_value = replace(self.snapshot, credential=newer_credential)
        await self.authorize()
        release.set()
        await task
        self.assertEqual(self.credential_for(), newer_credential)

    async def test_update_timeout_and_global_capacity_do_not_modify_state(self):
        await self.authorize()
        original = self.bindings.state_for("player")
        self.login._active_operations = 4
        self.assertIn("正忙", await self.reply("/riz update"))
        self.login._active_operations = 0
        async def hang(credential):
            await asyncio.Event().wait()
        self.client.fetch_with_token.side_effect = hang
        with patch("rizline_qq_login.OPERATION_SECONDS", 0.01):
            self.assertIn("超时", await self.reply("/riz update"))
        self.assertFalse(self.login._sessions)
        self.assertEqual(self.login._active_operations, 0)
        self.assertEqual(self.bindings.state_for("player"), original)
        self.assertEqual(self.credential_for(), self.credential)

    async def test_unbind_before_first_confirmation_invalidates_empty_binding_revision(self):
        await self.authorize(confirm=False)
        self.assertFalse(self.bindings.unbind("player"))
        self.assertIn("发生了变化", await self.reply("确认"))
        self.assertEqual(self.row_count(), 0)

    async def test_old_cleanup_cannot_delete_newer_credentials(self):
        await self.authorize()
        original = self.bindings.state_for("player")
        self.assertIn("已更新", await self.reply("/riz update"))
        ran = self.bindings.while_current("player", original.alias, original.revision,
                                          lambda: self.vault.retain_only("player", original.alias))
        self.assertFalse(ran)
        self.assertEqual(self.credential_for(), self.credential)

    async def test_failed_manual_binding_write_fails_closed_and_explains_revocation(self):
        for action in ("unbind", "bind"):
            with self.subTest(action=action):
                await self.authorize()
                original = self.bindings.state_for("player")
                command = "/riz unbind" if action == "unbind" else "/riz bind " + self.bindings.issue_code(original.alias)
                with patch.object(self.bindings, "_write_data", side_effect=RuntimeError("disk failure")):
                    self.assertIn("凭据可能已清除", await self.reply(command))
                self.assertEqual(self.bindings.state_for("player"), original)
                self.assertIsNone(self.credential_for())
                self.assertTrue((self.root / "saves" / f"{original.alias}.json").exists())
                self.clock += 61


class TokenClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = object.__new__(RizlineSmsClient)
        self.client._key = bytes(range(32))
        self.client._as_bytes = lambda blob: blob
        self.client._channel_id = "1"
        self.credential = GameCredential(TOKEN, PHONE, "saved-device", "3", time.time() + 3600, "test-account-id")

    def encrypted(self, raw):
        cipher = AES.new(self.client._key, AES.MODE_GCM, nonce=b"x" * 12)
        ciphertext, tag = cipher.encrypt_and_digest(json.dumps(raw).encode())
        return b"x" * 12 + ciphertext + tag

    async def test_refresh_only_uses_game_endpoint_and_preserves_saved_headers_and_expiry(self):
        with patch.object(self.client, "_post", new=AsyncMock(return_value=(self.encrypted(sample_save()), ""))) as post:
            result = await self.client.fetch_with_token(self.credential)
        self.assertEqual(post.await_count, 1)
        self.assertEqual(post.call_args.args[1:], ("/game/rn_login", PHONE, "saved-device", {}))
        self.assertEqual(post.call_args.kwargs, {"token": TOKEN, "channel_id": "3"})
        self.assertEqual(result.credential, self.credential)
        self.assertNotIn(TOKEN.encode(), result.payload)

    async def test_rotated_token_is_returned_and_wrong_account_is_rejected(self):
        with patch.object(self.client, "_post", new=AsyncMock(return_value=(self.encrypted(sample_save()), "new-token"))):
            result = await self.client.fetch_with_token(self.credential)
        self.assertEqual(result.credential.token, "new-token")
        with patch.object(self.client, "_post", new=AsyncMock(return_value=(self.encrypted(sample_save()), ""))):
            with self.assertRaisesRegex(GameLoginError, "account"):
                await self.client.fetch_with_token(replace(self.credential, account_id="different-account"))

    async def test_missing_account_and_tampered_save_do_not_update(self):
        raw = sample_save()
        raw.pop("userId")
        for blob in (self.encrypted(raw), self.encrypted(sample_save())[:-1] + b"z"):
            with patch.object(self.client, "_post", new=AsyncMock(return_value=(blob, ""))):
                with self.assertRaisesRegex(GameLoginError, "save"):
                    await self.client.fetch_with_token(self.credential)

    async def test_only_authenticated_401_is_expired_other_failures_are_transient(self):
        for status, category in ((401, "expired"), (403, "upstream"), (429, "limited"), (500, "upstream")):
            with self.subTest(status=status):
                http = Mock(post=Mock(return_value=FakeResponse(b"do not log body", status)))
                with self.assertRaisesRegex(GameLoginError, category):
                    await self.client._post(http, "/game/rn_login", PHONE, "device", {}, token=TOKEN)


if __name__ == "__main__":
    unittest.main()
