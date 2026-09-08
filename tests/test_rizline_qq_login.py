from __future__ import annotations

import asyncio
import io
import json
import logging
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from Crypto.Cipher import AES

from bot import CredentialSafeSdkFilter, QQOfficialBot, ReplyService
from rizline import RizlineScoreService
from rizline_bindings import RizlineBindingStore
from rizline_qq_login import GameLoginError, QQLoginService, RizlineSmsClient, minimized_save


PHONE = "13800000000"
CODE = "654321"
TOKEN = "never-persist-this-token"


def sample_save(username: str = "Test Player") -> dict:
    return {
        "username": username, "userId": "test-account-id", "totalRks": 123.4, "phone": PHONE, "token": TOKEN,
        "mails": [{"content": "private-mail"}], "coin": 42,
        "myBest": [{"trackAssetId": "track.Test.artist.0", "difficultyClassName": "IN",
                    "score": 1001234, "completeRate": 120.2, "isFullCombo": True, "isClear": True,
                    "unknown_private_field": TOKEN}],
        "levelsRks": [{"trackId": "track.Test.artist.0", "difficultyClassName": "IN", "rks": 140.3}],
        "rizcard": {"avatarId": "illustration.Test.artist.0", "backgroundId": "illustration.bg.0",
                    "layoutId": "layout.00001", "bioId1": "bio.first", "bioId2": "bio.second",
                    "avatarPos": {"x": 0.2, "y": 0.8}, "extra": TOKEN},
    }


class QQLoginFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.clock = 1000.0
        self.bindings = RizlineBindingStore(self.root / "bindings.json")
        self.client = Mock()
        self.client.send_code = AsyncMock(return_value=True)
        self.client.verify_and_fetch = AsyncMock(return_value=minimized_save(sample_save()))
        self.login = QQLoginService(self.bindings, self.root / "saves", self.client, now=lambda: self.clock)
        self.replies = ReplyService(RizlineScoreService(self.root / "saves"), bindings=self.bindings, login=self.login)

    def tearDown(self) -> None:
        for openid in list(self.login._sessions):
            self.login.cancel(openid)
        self.temporary.cleanup()

    async def reply(self, text: str, openid: str = "player-one", source: str = "c2c") -> str:
        return (await self.replies.response_for(text, source=source, user_openid=openid)).content

    async def start_sms(self, openid: str = "player-one", phone: str = PHONE) -> None:
        self.assertIn("同意", await self.reply("/riz login", openid))
        self.assertIn("手机号", await self.reply("同意", openid))
        self.assertIn("验证码已发送", await self.reply(phone, openid))

    async def test_consent_is_required_before_sms_and_no_secrets_are_echoed(self) -> None:
        for text in (PHONE, CODE, f"/riz login {PHONE}", TOKEN):
            response = await self.reply(text)
            self.assertNotIn(text, response)
        await self.reply("/riz login")
        self.assertIn("先阅读", await self.reply(PHONE))
        self.client.send_code.assert_not_awaited()

    async def test_complete_login_requires_confirmation_and_saves_only_score_fields(self) -> None:
        await self.start_sms()
        self.assertIn("Test Player", await self.reply(CODE))
        self.assertIsNone(self.bindings.alias_for("player-one"))
        self.assertFalse((self.root / "saves").exists())
        self.assertEqual(self.login._sessions["player-one"].phone, "")

        self.assertIn("已绑定", await self.reply("确认"))
        alias = self.bindings.alias_for("player-one")
        self.assertTrue(alias.startswith("qq_"))
        target = self.root / "saves" / f"{alias}.json"
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)
        snapshot = json.loads(target.read_text())
        self.assertEqual(set(snapshot), {"username", "totalRks", "myBest", "levelsRks", "rizcard"})
        self.assertEqual(snapshot["rizcard"]["bioId1"], "bio.first")
        for path in self.root.rglob("*.json"):
            for secret in (PHONE, CODE, TOKEN, "private-mail", "unknown_private_field"):
                self.assertNotIn(secret, path.read_text())
        self.assertFalse(list((self.root / "saves").glob(".qq-login-*")))
        self.assertNotIn("player-one", self.login._sessions)
        self.assertIn("Test Player", await self.reply("/riz profile"))
        self.assertIn("不能查询其他玩家", await self.reply("/riz b40 someone_else"))

    async def test_group_messages_cannot_create_advance_confirm_or_cancel_login(self) -> None:
        self.assertIn("C2C 私聊", await self.reply("/riz login", source="group"))
        self.assertFalse(self.login._sessions)
        await self.start_sms()
        for text in (CODE, PHONE, "确认", "/riz confirm", "/riz cancel", "/riz resend"):
            response = await self.reply(text, source="group")
            self.assertNotIn(CODE, response)
            self.assertNotIn(PHONE, response)
        self.assertEqual(self.login._sessions["player-one"].stage, "sms")
        self.client.verify_and_fetch.assert_not_awaited()
        self.assertEqual(self.client.send_code.await_count, 1)

    async def test_other_player_cannot_consume_session_and_each_login_has_own_device(self) -> None:
        await self.start_sms()
        self.assertIn("没有有效", await self.reply(CODE, "player-two"))
        await self.start_sms("player-two", "13900000000")
        self.assertNotEqual(self.client.send_code.await_args_list[0].args[1],
                            self.client.send_code.await_args_list[1].args[1])
        await self.reply(CODE)
        await self.reply("确认", "player-two")
        self.assertIsNone(self.bindings.alias_for("player-two"))

    async def test_cancel_expiry_and_restarts_do_not_echo_late_codes(self) -> None:
        await self.start_sms()
        session = self.login._sessions["player-one"]
        await self.reply("/riz cancel")
        self.assertEqual(session.phone, "")
        self.assertNotIn(CODE, await self.reply(CODE))
        self.clock += 61
        await self.start_sms()
        self.clock += 601
        self.assertIn("没有有效", await self.reply(CODE))
        self.assertFalse(self.login._sessions)
        fresh_reply_service = ReplyService(RizlineScoreService(self.root / "saves"))
        self.assertNotIn(CODE, await fresh_reply_service.reply_for(CODE, source="c2c"))
        self.client.verify_and_fetch.assert_not_awaited()

    async def test_sms_cooldown_and_phone_limit_survive_new_sessions_and_users(self) -> None:
        await self.start_sms()
        self.assertIn("频繁", await self.reply("/riz resend"))
        self.clock += 61
        self.assertIn("已发送", await self.reply("/riz resend"))
        self.clock += 61
        self.assertIn("已发送", await self.reply("/riz resend"))
        self.clock += 61
        await self.reply("/riz login", "player-two")
        await self.reply("同意", "player-two")
        self.assertIn("频繁", await self.reply(PHONE, "player-two"))
        self.assertEqual(self.client.send_code.await_count, 3)
        self.assertNotIn(PHONE, repr(self.login._rate_history))

    async def test_sms_expiry_does_not_submit_code(self) -> None:
        await self.start_sms()
        self.clock += 121
        self.assertIn("等待时间已过", await self.reply(CODE))
        self.client.verify_and_fetch.assert_not_awaited()
        self.assertIn("已发送", await self.reply("/riz resend"))

    async def test_wrong_code_limit_and_exception_details_are_redacted(self) -> None:
        await self.start_sms()
        self.client.verify_and_fetch.side_effect = RuntimeError(f"{PHONE} {CODE} {TOKEN}")
        with self.assertLogs("qq_official_bot.rizline_qq_login", level="WARNING") as logs:
            responses = [await self.reply(CODE) for _ in range(5)]
        for secret in (PHONE, CODE, TOKEN):
            self.assertNotIn(secret, " ".join(responses + logs.output))
        self.assertIn("上限", responses[-1])
        self.assertNotIn("player-one", self.login._sessions)

    async def test_failed_sms_still_consumes_cooldown(self) -> None:
        self.client.send_code.side_effect = GameLoginError("sms")
        await self.reply("/riz login")
        await self.reply("同意")
        self.assertIn("未接受", await self.reply(PHONE))
        self.assertIn("频繁", await self.reply(PHONE))
        self.assertEqual(self.client.send_code.await_count, 1)

    async def test_uncertain_sms_response_allows_received_code_without_resending(self) -> None:
        self.client.send_code.return_value = False
        await self.reply("/riz login")
        await self.reply("同意")
        response = await self.reply(PHONE)
        self.assertIn("发送结果暂时无法确认", response)
        self.assertNotIn("验证码已发送", response)
        self.assertEqual(self.login._sessions["player-one"].stage, "sms")
        self.assertIn("频繁", await self.reply("/riz resend"))
        self.assertIn("Test Player", await self.reply(CODE))
        self.assertIsNone(self.bindings.alias_for("player-one"))
        self.assertIn("已绑定", await self.reply("确认"))
        self.assertEqual(self.client.send_code.await_count, 1)
        self.assertEqual(self.client.verify_and_fetch.await_count, 1)

    async def test_uncertain_resend_refreshes_sms_window_without_resetting_attempts(self) -> None:
        await self.start_sms()
        self.login._sessions["player-one"].attempts = 2
        self.clock += 121
        self.client.send_code.return_value = False
        self.assertIn("发送结果暂时无法确认", await self.reply("/riz resend"))
        self.assertEqual(self.login._sessions["player-one"].attempts, 2)
        self.assertIn("Test Player", await self.reply(CODE))

    async def test_duplicate_requests_and_cancel_during_fetch(self) -> None:
        await self.start_sms()
        entered = asyncio.Event()
        release = asyncio.Event()

        async def delayed_fetch(*args):
            entered.set()
            await release.wait()
            return minimized_save(sample_save())

        self.client.verify_and_fetch.side_effect = delayed_fetch
        first = asyncio.create_task(self.reply(CODE))
        await asyncio.wait_for(entered.wait(), 1)
        self.assertIn("正在处理", await self.reply(CODE))
        await self.reply("/riz cancel")
        release.set()
        self.assertIn("取消", await first)
        self.assertIsNone(self.bindings.alias_for("player-one"))
        self.assertFalse((self.root / "saves").exists())
        self.assertEqual(self.client.verify_and_fetch.await_count, 1)

    async def test_operation_timeout_has_safe_reply_and_releases_concurrency(self) -> None:
        await self.start_sms()

        async def wait_forever(*args):
            await asyncio.Event().wait()

        self.client.verify_and_fetch.side_effect = wait_forever
        with patch("rizline_qq_login.OPERATION_SECONDS", 0.01):
            self.assertIn("超时", await self.reply(CODE))
        self.assertEqual(self.login._active_operations, 0)
        self.assertIsNone(self.login._sessions["player-one"].task)

    async def test_changed_binding_is_not_overwritten_and_new_snapshot_is_removed(self) -> None:
        self.bindings.bind_verified("player-one", "old_player", expected_alias=None)
        await self.start_sms()
        await self.reply(CODE)
        self.bindings.bind_verified("player-one", "new_player", expected_alias="old_player")
        self.assertIn("发生了变化", await self.reply("确认"))
        self.assertEqual(self.bindings.alias_for("player-one"), "new_player")
        self.assertFalse(list((self.root / "saves").iterdir()))

    async def test_refresh_creates_new_snapshot_without_overwriting_legacy_save(self) -> None:
        saves = self.root / "saves"
        saves.mkdir()
        original = saves / "legacy.json"
        original.write_text("do-not-touch-this-existing-save")
        self.bindings.bind_verified("player-one", "legacy", expected_alias=None)
        await self.start_sms()
        await self.reply(CODE)
        self.assertIn("已绑定", await self.reply("确认"))
        self.assertNotEqual(self.bindings.alias_for("player-one"), "legacy")
        self.assertEqual(original.read_text(), "do-not-touch-this-existing-save")

    async def test_unbind_cancels_pending_login_and_cannot_be_undone_by_confirm(self) -> None:
        self.bindings.bind_verified("player-one", "legacy", expected_alias=None)
        await self.start_sms()
        await self.reply(CODE)
        await self.reply("/riz unbind")
        self.assertIn("没有有效", await self.reply("/riz confirm"))
        self.assertIsNone(self.bindings.alias_for("player-one"))

    async def test_session_capacity_is_a_hard_limit_and_expiry_timer_scrubs_phone(self) -> None:
        await self.start_sms()
        with patch("rizline_qq_login.MAX_SESSIONS", 1):
            self.assertIn("人数较多", await self.reply("/riz login", "other"))
        session = self.login._sessions["player-one"]
        self.login._expire("player-one", session)
        self.assertEqual(session.phone, "")
        self.assertFalse(self.login._sessions)

    async def test_concurrency_limit_does_not_make_network_calls(self) -> None:
        await self.reply("/riz login")
        await self.reply("同意")
        self.login._active_operations = 4
        self.assertIn("正忙", await self.reply(PHONE))
        self.client.send_code.assert_not_awaited()

    async def test_disk_failure_preserves_binding_and_cleans_staging_files(self) -> None:
        await self.start_sms()
        await self.reply(CODE)
        with patch.object(self.bindings, "bind_verified", side_effect=RuntimeError("write failed")):
            self.assertIn("未能完成", await self.reply("确认"))
        self.assertIsNone(self.bindings.alias_for("player-one"))
        self.assertFalse(list((self.root / "saves").iterdir()))

    async def test_qq_and_global_sms_limits_cannot_be_bypassed_by_changing_phones(self) -> None:
        for index in range(5):
            self.assertTrue(self.login._reserve_sms("same-qq", f"1380000{index:04d}"))
            self.clock += 61
        self.assertFalse(self.login._reserve_sms("same-qq", "13900000000"))
        for index in range(15):
            self.assertTrue(self.login._reserve_sms(f"other-{index}", f"1390000{index:04d}"))
        self.assertFalse(self.login._reserve_sms("new-qq", "13700000000"))

    async def test_rebinding_cannot_overwrite_the_previous_snapshot_on_cancel(self) -> None:
        await self.start_sms()
        await self.reply(CODE)
        await self.reply("确认")
        original_alias = self.bindings.alias_for("player-one")
        self.clock += 61
        await self.start_sms()
        await self.reply(CODE)
        await self.reply("/riz cancel")
        self.assertEqual(self.bindings.alias_for("player-one"), original_alias)
        self.assertEqual(len(list((self.root / "saves").glob("*.json"))), 1)


class FakeResponse:
    def __init__(self, body: bytes, status: int = 200, headers: dict | None = None) -> None:
        self.body = body
        self.status = status
        self.headers = headers or {}
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def iter_chunked(self, size):
        yield self.body


class SmsClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.client = object.__new__(RizlineSmsClient)
        self.client._key = bytes(range(32))
        self.client._as_bytes = lambda payload: payload
        self.client._channel_id = "1"

    async def test_sms_rejects_explicit_business_errors(self) -> None:
        for body in (b'{"code": 3}', b'{"code": false}', b'{"code": "3"}', b'{"code":"-1"}'):
            with self.subTest(body=body):
                with patch.object(self.client, "_post", new=AsyncMock(return_value=(body, TOKEN))):
                    with self.assertRaises(GameLoginError):
                        await self.client.send_code(PHONE, "device")

    async def test_login_still_requires_strict_business_success_and_token(self) -> None:
        for body, token in ((b'{"code":0}', ""), (b'{"code":3}', TOKEN),
                            (b'{"code":false}', TOKEN), (b'', TOKEN),
                            (b'not-json', TOKEN), (b'{}', TOKEN), (b'{"code":"0"}', TOKEN)):
            with self.subTest(body=body):
                fake_post = AsyncMock(return_value=(body, token))
                with patch.object(self.client, "_post", new=fake_post):
                    with self.assertRaises(GameLoginError):
                        await self.client.verify_and_fetch(PHONE, CODE, "device")
                self.assertEqual(fake_post.await_count, 1)

    async def test_sms_empty_text_or_unrecognized_json_is_uncertain_not_confirmed(self) -> None:
        for body in (b'', b' \n', b'OK', b'{}', b'null', b'{"code":"0"}', TOKEN.encode()):
            with self.subTest(body=body):
                with patch.object(self.client, "_post", new=AsyncMock(return_value=(body, ""))):
                    with self.assertLogs("qq_official_bot.rizline_qq_login", level="WARNING") as logs:
                        self.assertIs(await self.client.send_code(PHONE, "device"), False)
                self.assertNotIn(TOKEN, " ".join(logs.output))
                self.assertNotIn(PHONE, " ".join(logs.output))
                self.assertNotIn(CODE, " ".join(logs.output))

    async def test_sms_http_204_can_wait_for_verification_without_asserting_delivery(self) -> None:
        http = Mock()
        http.post.return_value = FakeResponse(b'', status=204)
        body, token = await self.client._post(http, "/account/send_verify_code", PHONE, "device", {})
        self.assertIs(self.client._sms_dispatch_confirmed(body), False)
        self.assertEqual(token, "")

    async def test_sms_http_errors_remain_errors_and_do_not_log_response_content(self) -> None:
        http = Mock()
        for status in (302, 400, 401, 429, 500):
            http.post.return_value = FakeResponse(TOKEN.encode(), status=status)
            with self.assertRaises(GameLoginError):
                await self.client._post(http, "/account/send_verify_code", PHONE, "device", {})

    async def test_authenticated_save_is_decrypted_and_minimized_without_output(self) -> None:
        cipher = AES.new(self.client._key, AES.MODE_GCM, nonce=b"012345678901")
        ciphertext, tag = cipher.encrypt_and_digest(json.dumps(sample_save()).encode())
        encrypted = cipher.nonce + ciphertext + tag
        fake_post = AsyncMock(side_effect=[(b'{"code":0}', TOKEN), (encrypted, "")])
        with patch.object(self.client, "_post", new=fake_post), patch("sys.stdout", new_callable=io.StringIO) as output:
            snapshot = await self.client.verify_and_fetch(PHONE, CODE, "device")
        self.assertEqual(snapshot.username, "Test Player")
        self.assertEqual(output.getvalue(), "")
        self.assertEqual(fake_post.await_args_list[1].kwargs["token"], TOKEN)
        self.assertNotIn(TOKEN.encode(), snapshot.payload)
        self.assertEqual(fake_post.await_args_list[0].args[1], "/account/login")
        self.assertEqual(fake_post.await_args_list[1].args[1], "/game/rn_login")

    async def test_bad_encrypted_save_is_rejected_without_raw_output(self) -> None:
        fake_post = AsyncMock(side_effect=[(b'{"code":0}', TOKEN), (b"bad-save" * 20, "")])
        with patch.object(self.client, "_post", new=fake_post), patch("sys.stdout", new_callable=io.StringIO) as output:
            with self.assertRaises(GameLoginError) as raised:
                await self.client.verify_and_fetch(PHONE, CODE, "device")
        self.assertEqual(str(raised.exception), "save")
        self.assertEqual(output.getvalue(), "")

    async def test_request_uses_fixed_https_host_tls_verification_and_no_redirects(self) -> None:
        http = Mock()
        http.post.return_value = FakeResponse(b'{"code":0}')
        await self.client._post(http, "/account/send_verify_code", PHONE, "device", {"phone": PHONE})
        self.assertEqual(http.post.call_args.args[0], "https://rizserver.pigeongames.net/account/send_verify_code")
        self.assertIs(http.post.call_args.kwargs["allow_redirects"], False)
        self.assertIs(http.post.call_args.kwargs["ssl"], True)
        for status in (302, 401, 429, 500):
            http.post.return_value = FakeResponse(b"sensitive-response", status=status)
            with self.assertRaises(GameLoginError):
                await self.client._post(http, "/account/login", PHONE, "device", {})

    async def test_oversized_responses_and_non_login_routes_are_rejected(self) -> None:
        http = Mock()
        http.post.return_value = FakeResponse(b"a" * 101)
        with patch("rizline_qq_login.MAX_RESPONSE_BYTES", 100):
            with self.assertRaises(GameLoginError):
                await self.client._post(http, "/game/rn_login", PHONE, "device", {})
        with self.assertRaises(GameLoginError):
            await self.client._post(http, "/account/change_password", PHONE, "device", {})


class LoginPrivacyTests(unittest.TestCase):
    def test_sdk_filter_removes_debug_events_and_error_response_bodies(self) -> None:
        privacy_filter = CredentialSafeSdkFilter()
        debug_record = logging.LogRecord("botpy", logging.DEBUG, "gateway.py", 1, PHONE, (), None)
        self.assertFalse(privacy_filter.filter(debug_record))
        record = logging.LogRecord("botpy", logging.ERROR, "http.py", 1, "body: %s", (TOKEN,), None)
        self.assertTrue(privacy_filter.filter(record))
        self.assertNotIn(TOKEN, record.getMessage())
        self.assertEqual(record.args, ())

    def test_c2c_identity_never_falls_back_to_group_identifier(self) -> None:
        message = Mock()
        message.author.user_openid = None
        message.author.member_openid = "group-only-id"
        self.assertIsNone(QQOfficialBot._sender_openid(message, source="c2c"))
        self.assertEqual(QQOfficialBot._sender_openid(message, source="group"), "group-only-id")

    def test_minimized_save_preserves_scores_and_removes_sensitive_fields(self) -> None:
        result = minimized_save(sample_save("Player\n<@123>"))
        self.assertNotIn("\n", result.username)
        self.assertNotIn("<", result.username)
        profile = RizlineScoreService._parse_profile("test", json.loads(result.payload))
        self.assertEqual(profile.records[0].score, 1001234)
        self.assertAlmostEqual(profile.records[0].rks, 140.3)
        self.assertEqual(profile.card.avatar_x, 0.2)

    def test_empty_save_has_a_specific_error_without_treating_it_as_network_failure(self) -> None:
        with self.assertRaises(GameLoginError) as raised:
            minimized_save({"username": "New Player", "myBest": []})
        self.assertEqual(str(raised.exception), "empty")


if __name__ == "__main__":
    unittest.main()
