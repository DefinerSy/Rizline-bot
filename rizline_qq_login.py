"""Opt-in C2C SMS login and encrypted, per-binding token refresh."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import importlib.util
import json
import logging
import os
import re
import secrets
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

import aiohttp
from Crypto.Cipher import AES

from rizline import MAX_SAVE_BYTES, RizlineScoreService
from rizline_bindings import MAX_OPENID_LENGTH, RizlineBindingStore
from rizline_token_vault import GameCredential, TokenVault, token_deadline, validate_credential


LOGGER = logging.getLogger("qq_official_bot.rizline_qq_login")
GAME_BASE = "https://rizserver.pigeongames.net"
PHONE_RE = re.compile(r"1[3-9][0-9]{9}")
SMS_RE = re.compile(r"[0-9]{4,8}")
SESSION_SECONDS = 600
SMS_SECONDS = 120
MAX_SESSIONS = 32
MAX_ATTEMPTS = 5
MAX_OPERATIONS = 4
OPERATION_SECONDS = 30
MAX_RESPONSE_BYTES = MAX_SAVE_BYTES + 2 * 1024 * 1024
LOGIN_ACTIONS = {
    "login": "login", "登录": "login", "update": "update", "更新": "update",
    "cancel": "cancel", "取消": "cancel", "confirm": "confirm", "确认": "confirm",
    "resend": "resend", "重发": "resend",
}


class GameLoginError(Exception):
    """A fixed error category, never an upstream response or credential."""


@dataclass(frozen=True, repr=False)
class VerifiedSave:
    username: str
    record_count: int
    payload: bytes
    credential: GameCredential | None = None


def minimized_save(raw: Any) -> VerifiedSave:
    if isinstance(raw, dict):
        data = raw.get("data") if isinstance(raw.get("data"), dict) and "myBest" not in raw else raw
        if data.get("myBest") == []:
            raise GameLoginError("empty")
    profile = RizlineScoreService._parse_profile("preview", raw)
    username = "".join(
        character for character in profile.username
        if not unicodedata.category(character).startswith("C") and character not in "<>&"
    )[:64] or "RizLine 玩家"
    card = profile.card
    snapshot = {
        "username": username,
        "totalRks": profile.total_rks,
        "myBest": [
            {"trackAssetId": record.track_id, "difficultyClassName": record.difficulty,
             "score": record.score, "completeRate": record.complete_rate,
             "isFullCombo": record.full_combo, "isClear": record.cleared}
            for record in profile.records
        ],
        "levelsRks": [
            {"trackId": record.track_id, "difficultyClassName": record.difficulty, "rks": record.rks}
            for record in profile.records if record.rks is not None
        ],
        "rizcard": {
            "avatarId": card.avatar_id, "backgroundId": card.background_id,
            "layoutId": card.layout_id, "bioId1": card.bio_id1, "bioId2": card.bio_id2,
            "avatarPos": {"x": card.avatar_x, "y": card.avatar_y},
        },
    }
    payload = json.dumps(snapshot, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) > MAX_SAVE_BYTES:
        raise GameLoginError("save")
    return VerifiedSave(username, len(profile.records), payload)


class RizlineSmsClient:
    """Use only the three fixed game endpoints needed for SMS and a score snapshot."""

    def __init__(self, decryptor_path: Path, *, channel_id: str = "1") -> None:
        if channel_id not in {str(number) for number in range(1, 12)}:
            raise ValueError("RizLine channel must be between 1 and 11")
        spec = importlib.util.spec_from_file_location("rizline_local_save_decoder", decryptor_path)
        if not spec or not spec.loader:
            raise RuntimeError("RizLine decoder unavailable")
        decoder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(decoder)
        self._key = decoder.AES_KEY
        self._as_bytes = decoder._as_bytes
        self._channel_id = channel_id

    async def _post(
        self, http: aiohttp.ClientSession, route: str, phone: str, device_id: str,
        payload: dict[str, str], *, token: str = "", channel_id: str | None = None,
    ) -> tuple[bytes, str]:
        if route not in {"/account/send_verify_code", "/account/login", "/game/rn_login"}:
            raise GameLoginError("request")
        headers = {
            "User-Agent": "UnityPlayer/2022.3.62f2 (UnityWebRequest/1.0, libcurl/8.10.1-DEV)",
            "game_id": "pigeongames.rizline", "device_id": device_id,
            "channel_id": channel_id or self._channel_id, "i18n": "zh-CN", "phone": phone,
            "X-Unity-Version": "2022.3.62f2",
        }
        if token:
            headers["token"] = token
        async with http.post(GAME_BASE + route, json=payload, headers=headers,
                             allow_redirects=False, ssl=True) as response:
            if response.status == 429:
                raise GameLoginError("limited")
            if response.status == 401 and route == "/game/rn_login" and token:
                raise GameLoginError("expired")
            if not 200 <= response.status < 300:
                if route == "/account/send_verify_code":
                    LOGGER.warning("RizLine SMS request rejected (HTTP status=%d)", response.status)
                raise GameLoginError("upstream")
            result = bytearray()
            async for chunk in response.content.iter_chunked(65536):
                result.extend(chunk)
                if len(result) > MAX_RESPONSE_BYTES:
                    raise GameLoginError("save")
            result_token = next((response.headers.get(name, "") for name in
                                 ("set_token", "set-token", "token") if response.headers.get(name)), "")
            if len(result_token) > 8192 or "\r" in result_token or "\n" in result_token:
                raise GameLoginError("upstream")
            return bytes(result), result_token

    @staticmethod
    def _require_success(body: bytes, category: str) -> None:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            raise GameLoginError("upstream") from None
        if not isinstance(payload, dict) or type(payload.get("code")) is not int or payload["code"] != 0:
            raise GameLoginError(category)

    @staticmethod
    def _sms_dispatch_confirmed(body: bytes) -> bool:
        try:
            payload = json.loads(body)
        except (ValueError, UnicodeError):
            LOGGER.warning("RizLine SMS dispatch not confirmed (body format=%s)",
                           "empty" if not body.strip() else "non_json")
            return False
        if isinstance(payload, dict) and "code" in payload:
            code = payload["code"]
            if type(code) is int:
                if code != 0:
                    raise GameLoginError("sms")
                return True
            if isinstance(code, bool) or (isinstance(code, str) and re.fullmatch(r"[+-]?[0-9]+", code.strip())
                                          and code.strip().lstrip("+-0")):
                raise GameLoginError("sms")
        LOGGER.warning("RizLine SMS dispatch not confirmed (body format=unrecognized_json)")
        return False

    async def send_code(self, phone: str, device_id: str) -> bool:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as http:
            body, _ = await self._post(http, "/account/send_verify_code", phone, device_id,
                                       {"phone": phone, "transaction": "login"})
            return self._sms_dispatch_confirmed(body)

    async def verify_and_fetch(self, phone: str, code: str, device_id: str) -> VerifiedSave:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as http:
            body, token = await self._post(http, "/account/login", phone, device_id, {"phone": phone, "code": code})
            self._require_success(body, "code")
            if not token:
                raise GameLoginError("upstream")
        credential = GameCredential(token, phone, device_id, self._channel_id, token_deadline(token))
        return await self.fetch_with_token(credential)

    async def fetch_with_token(self, credential: GameCredential) -> VerifiedSave:
        validate_credential(credential)
        if credential.expires_at <= time.time():
            raise GameLoginError("expired")
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20), trust_env=False) as http:
            encrypted, rotated = await self._post(
                http, "/game/rn_login", credential.phone, credential.device_id, {},
                token=credential.token, channel_id=credential.channel_id,
            )
        try:
            blob = self._as_bytes(encrypted)
            if len(blob) < 28:
                raise ValueError("Invalid encrypted save")
            cipher = AES.new(self._key, AES.MODE_GCM, nonce=blob[:12])
            plaintext = cipher.decrypt_and_verify(blob[12:-16], blob[-16:])
            raw = json.loads(plaintext.decode("utf-8"))
            snapshot = minimized_save(raw)
            data = raw.get("data") if isinstance(raw.get("data"), dict) and "myBest" not in raw else raw
            account_id = data.get("userId", "")
            if not isinstance(account_id, str) or not account_id or len(account_id) > 128:
                raise GameLoginError("save")
            if credential.account_id and account_id != credential.account_id:
                raise GameLoginError("account")
            refreshed = replace(credential, account_id=account_id)
            if rotated and rotated != credential.token:
                refreshed = replace(refreshed, token=rotated, expires_at=token_deadline(rotated))
            validate_credential(refreshed)
            return replace(snapshot, credential=refreshed)
        except (ValueError, TypeError, KeyError, OverflowError):
            raise GameLoginError("save") from None


@dataclass(repr=False)
class LoginSession:
    expires_at: float
    expected_alias: str | None
    expected_revision: str = ""
    remember_token: bool = False
    device_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    stage: str = "consent"
    phone: str = ""
    sms_expires_at: float = 0
    attempts: int = 0
    snapshot: VerifiedSave | None = None
    task: asyncio.Task | None = None
    timer: asyncio.TimerHandle | None = None


class QQLoginService:
    def __init__(
        self, bindings: RizlineBindingStore, save_dir: Path, client: RizlineSmsClient,
        *, vault: TokenVault | None = None, now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._bindings = bindings
        self._save_dir = Path(save_dir)
        self._client = client
        self._vault = vault
        if vault:
            bindings.credential_revoker = vault.delete
        self._now = now
        self._sessions: dict[str, LoginSession] = {}
        self._rate_history: dict[str, list[float]] = {}
        self._update_history: dict[str, float] = {}
        self._global_updates: list[float] = []
        self._rate_secret = secrets.token_bytes(32)
        self._active_operations = 0

    def cancel(self, openid: str) -> None:
        session = self._sessions.pop(openid, None)
        if session:
            if session.task:
                session.task.cancel()
            if session.timer:
                session.timer.cancel()
            session.phone = ""
            session.snapshot = None

    def _expire(self, openid: str, session: LoginSession) -> None:
        if self._sessions.get(openid) is session:
            self.cancel(openid)

    def _current(self, openid: str, session: LoginSession) -> bool:
        return self._sessions.get(openid) is session and self._now() < session.expires_at

    def _reserve_sms(self, openid: str, phone: str) -> bool:
        now = self._now()
        self._rate_history = {
            key: [stamp for stamp in stamps if now - stamp < 3600]
            for key, stamps in self._rate_history.items()
            if stamps and now - stamps[-1] < 3600
        }
        user_key = "qq:" + hmac.new(self._rate_secret, openid.encode(), hashlib.sha256).hexdigest()
        phone_key = "phone:" + hmac.new(self._rate_secret, phone.encode(), hashlib.sha256).hexdigest()
        limits = ((user_key, 5, 60), (phone_key, 3, 60), ("global", 20, 0))
        for key, limit, cooldown in limits:
            stamps = self._rate_history.get(key, [])
            if len(stamps) >= limit or (stamps and now - stamps[-1] < cooldown):
                return False
        for key, _, _ in limits:
            self._rate_history.setdefault(key, []).append(now)
        return True

    async def _operate(self, session: LoginSession, operation: Callable) -> Any:
        if self._active_operations >= MAX_OPERATIONS:
            raise GameLoginError("busy")
        self._active_operations += 1
        task = asyncio.create_task(operation())
        session.task = task
        try:
            async with asyncio.timeout(OPERATION_SECONDS):
                return await task
        finally:
            self._active_operations -= 1
            session.task = None

    async def handle(self, text: str, *, source: str, openid: str | None) -> str | None:
        tokens = text.strip().split()
        action = (LOGIN_ACTIONS.get(tokens[1].casefold()) if len(tokens) >= 2
                  and tokens[0].casefold() in {"/riz", "riz"} else None)
        if source != "c2c" and not (source == "group" and action == "update"):
            return "登录只支持与机器人的 C2C 私聊，请勿在群里发送手机号或验证码。" if action else None
        if not openid or len(openid) > MAX_OPENID_LENGTH:
            return "无法识别你的 QQ 身份，不能进行登录或更新。" if action else None
        for expired_id, session in list(self._sessions.items()):
            if self._now() >= session.expires_at:
                self.cancel(expired_id)
        session = self._sessions.get(openid)
        if action and len(tokens) != 2:
            if action == "update":
                return "请单独发送 /riz update，只能更新你自己的绑定，不接受玩家别名或登录详情。首次绑定请私聊 /riz login。"
            return "请单独发送 /riz login，按提示分步操作；不要在命令后附带登录详情。"
        if action == "cancel" or (session and text.strip() == "取消"):
            self.cancel(openid)
            return "已取消登录或更新并清理临时会话，原有绑定不变。"
        if action == "update":
            if session:
                return "你已有登录或更新会话，请等待完成，或回到私聊用 /riz cancel 取消后再更新。"
            return await self._update(openid)
        if action == "login":
            if session:
                return "你已有登录会话，请按上一条提示操作；重开请先发送 /riz cancel。"
            if len(self._sessions) >= MAX_SESSIONS:
                return "当前登录人数较多，请稍后重试。"
            try:
                state = self._bindings.state_for(openid)
            except RuntimeError:
                return "绑定服务暂时不可用，请稍后重试。"
            session = LoginSession(self._now() + SESSION_SECONDS, state.alias, state.revision)
            self._sessions[openid] = session
            session.timer = asyncio.get_running_loop().call_later(SESSION_SECONDS, self._expire, openid, session)
            storage_notice = (
                "回复“同意并保存”：加密保存登录令牌、手机号及拉档必需信息，以后 /riz update 可直接更新；令牌失效仍需重新登录。\n"
                "回复“仅本次登录”或“同意”：只保存成绩，不保留令牌。\n"
                "加密密钥留在本机；能控制本机的管理员仍可能使用令牌。/riz unbind 解绑会删除本机凭据。"
                if self._vault else
                "当前只保存成绩、不保存登录令牌；更新需再次登录。接受请回复：同意"
            )
            return (
                "RizLine 自助短信登录（手机号账号）\n"
                "这是社区机器人查分功能，不是游戏官方授权入口。\n"
                "手机号与验证码会经过 QQ 聊天；机器人不记录登录详情到日志、不保存验证码，不能删除 QQ 端聊天记录。\n"
                "仅为你本人账号拉取成绩、头像及名片数据。"
                + ("确认成功后会替换你目前的绑定。" if state.alias else "")
                + "\n" + storage_notice + "\n会话 10 分钟内有效；随时可发送 /riz cancel 取消。"
            )
        if not session:
            return "没有有效登录会话，请先私聊发送 /riz login。" if action or PHONE_RE.fullmatch(text) or SMS_RE.fullmatch(text) else None
        if action is None and text.startswith(("/", "riz ")):
            return None
        if session.task:
            return "正在处理上一项登录请求，请稍候；可发送 /riz cancel 取消。"
        if len(text) > 256:
            return "输入过长。请只发送当前步骤需要的内容，不要转发整段短信或存档。"
        if session.stage == "consent":
            consent = text.strip()
            if consent not in {"同意", "仅本次登录", "同意并保存"}:
                return "请先阅读登录说明，回复“同意并保存”或“仅本次登录”继续，或 /riz cancel 取消。" if self._vault else "请先阅读登录说明，回复“同意”继续，或 /riz cancel 取消。"
            if consent == "同意并保存" and not self._vault:
                return "加密保存未启用；可回复“仅本次登录”继续，或 /riz cancel 取消。"
            session.remember_token = consent == "同意并保存"
            session.stage = "phone"
            return "请发送你本人的 11 位手机号。下一步将请求一条游戏登录短信；不要发送密码。"
        if session.stage == "phone":
            if action or not PHONE_RE.fullmatch(text):
                return "请只发送 11 位手机号，不要附带密码或其他信息；/riz cancel 可取消。"
            session.phone = text
            return await self._send_sms(openid, session)
        if session.stage == "sms":
            if action == "resend" or text == "重发验证码":
                return await self._send_sms(openid, session)
            if action or not SMS_RE.fullmatch(text):
                return "请只发送短信中的 4～8 位数字验证码，不要转发整段短信；重发用 /riz resend。"
            if self._now() >= session.sms_expires_at:
                return "验证码等待时间已过，请发送 /riz resend 获取新验证码，或 /riz cancel 取消。"
            return await self._verify(openid, session, text)
        if session.stage == "confirm":
            if action == "confirm" or text == "确认":
                return self._commit(openid, session)
            return "拉档已完成，请回复“确认”绑定刚才显示的游戏账号，或 /riz cancel 放弃。"
        return "请发送 /riz cancel 取消后重试。"

    async def _send_sms(self, openid: str, session: LoginSession) -> str:
        if self._active_operations >= MAX_OPERATIONS:
            return "登录服务正忙，请稍后重新发送手机号或 /riz resend。"
        if not self._reserve_sms(openid, session.phone):
            return "短信请求过于频繁。重发至少间隔 60 秒，每手机号每小时最多 3 次；请稍后重试。"
        try:
            confirmed = await self._operate(session, lambda: self._client.send_code(session.phone, session.device_id))
        except asyncio.CancelledError:
            return "本次登录已取消或过期，未改变绑定。"
        except Exception as exc:
            return self._safe_error(exc, stage="sms")
        if not self._current(openid, session):
            return "登录已过期，请重新发送 /riz login。"
        session.stage = "sms"
        session.sms_expires_at = self._now() + SMS_SECONDS
        if not confirmed:
            return ("短信请求已提交，但发送结果暂时无法确认。\n"
                    "如果你已收到短信，请在 2 分钟内直接回复数字验证码，可以继续登录。\n"
                    "未收到请至少等待 60 秒后再 /riz resend；/riz cancel 可取消。")
        return "验证码已发送。请在 2 分钟内只回复数字验证码；重发用 /riz resend，取消用 /riz cancel。"

    async def _verify(self, openid: str, session: LoginSession, code: str) -> str:
        if self._active_operations >= MAX_OPERATIONS:
            return "登录服务正忙，请稍后再提交验证码。"
        session.attempts += 1
        try:
            snapshot = await self._operate(
                session, lambda: self._client.verify_and_fetch(session.phone, code, session.device_id)
            )
        except asyncio.CancelledError:
            return "本次登录已取消或过期，未改变绑定。"
        except Exception as exc:
            if session.attempts >= MAX_ATTEMPTS:
                if self._sessions.get(openid) is session:
                    self.cancel(openid)
                return "已达到本次会话的验证尝试上限，登录已取消，原有绑定不变。"
            return self._safe_error(exc, stage="verify")
        if not self._current(openid, session):
            return "登录已过期，未保存本次存档或改变绑定。"
        session.phone = ""
        session.snapshot = snapshot if session.remember_token else replace(snapshot, credential=None)
        session.stage = "confirm"
        return (f"已拉取游戏账号：{snapshot.username}\n可查分谱面：{snapshot.record_count} 张。\n"
                "请核对是否为你的账号。回复“确认”完成绑定，或 /riz cancel 放弃。"
                + ("\n确认后将加密保存令牌，以便 /riz update 更新。" if session.remember_token
                   else "\n本次不保存令牌；如有旧的已存令牌，确认后也会删除。")
                + ("\n确认后将替换你原来的绑定。" if session.expected_alias else ""))

    async def _update(self, openid: str) -> str:
        relogin = "未找到可用的已存令牌。请在私聊发送 /riz login，选择“同意并保存”并完成确认；以后可 /riz update 直接更新。不要在群里发送手机号或验证码。"
        if not self._vault:
            return relogin
        if len(self._sessions) >= MAX_SESSIONS or self._active_operations >= MAX_OPERATIONS:
            return "更新服务正忙，请稍后重试。"
        try:
            state = self._bindings.state_for(openid)
            credential = self._vault.get(openid, state.alias, state.revision) if state.alias else None
        except RuntimeError:
            return "加密凭据暂时无法读取，请联系管理员检查本机密钥和存储；原有成绩不变。"
        if credential is None:
            return relogin
        now = self._now()
        self._update_history = {key: stamp for key, stamp in self._update_history.items() if now - stamp < 60}
        self._global_updates = [stamp for stamp in self._global_updates if now - stamp < 60]
        rate_key = hmac.new(self._rate_secret, openid.encode(), hashlib.sha256).hexdigest()
        if rate_key in self._update_history or len(self._global_updates) >= 20:
            return "更新请求过于频繁，请至少间隔 60 秒后重试。"
        self._update_history[rate_key] = now
        self._global_updates.append(now)
        session = LoginSession(now + SESSION_SECONDS, state.alias, state.revision,
                               remember_token=True, stage="updating")
        self._sessions[openid] = session
        session.timer = asyncio.get_running_loop().call_later(SESSION_SECONDS, self._expire, openid, session)
        try:
            snapshot = await self._operate(session, lambda: self._client.fetch_with_token(credential))
            if not self._current(openid, session):
                return "本次更新已取消或过期，原有成绩不变。"
            session.snapshot = snapshot
            return self._commit(openid, session)
        except asyncio.CancelledError:
            return "本次更新已取消或过期，原有成绩不变。"
        except GameLoginError as exc:
            if exc.args == ("expired",):
                try:
                    self._vault.delete(openid, alias=state.alias, revision=state.revision)
                except RuntimeError:
                    LOGGER.warning("Unable to remove an expired encrypted game credential")
            return self._safe_error(exc, stage="update")
        except Exception as exc:
            return self._safe_error(exc, stage="update")
        finally:
            if self._sessions.get(openid) is session:
                self.cancel(openid)

    @staticmethod
    def _safe_error(error: Exception, *, stage: str = "unknown") -> str:
        category = error.args[0] if isinstance(error, GameLoginError) and error.args and isinstance(error.args[0], str) else "network"
        messages = {
            "limited": "游戏端限制了请求频率，请稍后再试。",
            "sms": "游戏端未接受短信请求，请稍后再试；如有安全验证，请先在官方游戏中完成。",
            "code": "验证码登录未通过，可能已失效或被游戏端限制。请核对验证码，必要时 /riz resend 重发。",
            "save": "登录后的存档格式无法读取，未保存本次数据，原有绑定不变。",
            "empty": "这个游戏账号还没有可查分的成绩。请先在游戏中游玩并同步存档，再重新登录。",
            "upstream": "游戏服务暂未接受请求，请稍后重试；不会显示登录详情。",
            "busy": "登录服务正忙，请稍后再试。",
            "expired": "游戏登录令牌已失效，旧成绩仍保留。请在私聊发送 /riz login 并选择“同意并保存”，重新登录一次；不要在群里发送登录详情。",
            "account": "游戏端返回的账号与原绑定不一致，本次更新已拒绝，原有成绩不变。请回到私聊重新登录核对账号。",
        }
        LOGGER.warning("RizLine self-service request did not complete (stage=%s, category=%s)",
                       stage if stage in {"sms", "verify", "update"} else "unknown",
                       category if category in messages else "network")
        return messages.get(category, "连接游戏服务失败或超时，请稍后重试；原有绑定不变。")

    def _commit(self, openid: str, session: LoginSession) -> str:
        snapshot = session.snapshot
        if not snapshot or not self._current(openid, session):
            if self._sessions.get(openid) is session:
                self.cancel(openid)
            return "登录已过期，请重新发送 /riz login。"
        temporary: Path | None = None
        target: Path | None = None
        created_target = False
        committed = False
        staged_revision = ""
        alias = ""
        try:
            if session.remember_token and (not self._vault or not snapshot.credential):
                raise RuntimeError("Encrypted credentials unavailable")
            self._save_dir.mkdir(parents=True, exist_ok=True)
            alias = "qq_" + secrets.token_hex(15)
            target = self._save_dir / f"{alias}.json"
            descriptor, filename = tempfile.mkstemp(prefix=".qq-login-", suffix=".tmp", dir=self._save_dir)
            temporary = Path(filename)
            with os.fdopen(descriptor, "wb") as output:
                output.write(snapshot.payload)
                output.flush()
                os.fsync(output.fileno())
            os.link(temporary, target)
            created_target = True
            def prepare(revision: str) -> None:
                nonlocal staged_revision
                staged_revision = revision
                if session.remember_token:
                    self._vault.put(openid, alias, revision, snapshot.credential)

            committed = self._bindings.bind_verified(
                openid, alias, expected_alias=session.expected_alias,
                expected_revision=session.expected_revision, prepare=prepare,
            )
            if not committed:
                return "你的绑定在登录或更新期间发生了变化，本次没有覆盖；请回到私聊重新发送 /riz login。"
            if self._vault:
                try:
                    self._bindings.while_current(openid, alias, staged_revision,
                                                 lambda: self._vault.retain_only(openid, alias))
                except RuntimeError:
                    LOGGER.warning("Unable to clean obsolete encrypted game credentials")
            if session.stage == "updating":
                return f"已更新 {snapshot.username} 的成绩存档（{snapshot.record_count} 张谱面）！\n发送 /riz b40 查看新的 B40 图片。"
            return ("已绑定你的 RizLine 成绩存档！\n现在可发送 /riz profile、/riz top 或 /riz b40。\n"
                    + ("登录令牌已加密保存；以后 /riz update 可直接更新，失效时需重新登录；/riz unbind 解绑并删除凭据。"
                       if session.remember_token else
                       "本次登录令牌未保存；更新成绩请再次 /riz login，解绑用 /riz unbind。"))
        except (OSError, ValueError, RuntimeError):
            LOGGER.warning("Unable to commit a self-service score snapshot")
            return "存档或绑定未能完成保存，请稍后重试；不会显示登录详情。"
        finally:
            if self._sessions.get(openid) is session:
                self.cancel(openid)
            if not committed and staged_revision and self._vault:
                try:
                    self._vault.delete(openid, alias=alias, revision=staged_revision)
                except RuntimeError:
                    LOGGER.warning("Unable to remove a staged encrypted game credential")
            for disposable in (temporary, target if created_target and not committed else None):
                if disposable:
                    try:
                        disposable.unlink(missing_ok=True)
                    except OSError:
                        LOGGER.warning("Unable to remove a temporary self-service score snapshot")
