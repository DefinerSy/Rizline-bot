"""A minimal official QQ bot for group @ messages and C2C messages.

Credentials are deliberately read from environment variables so they never need
to appear in source control.  See README.md before running this file.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import re
import socket
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit, urlunsplit
from urllib.request import urlopen

import botpy
from botpy.message import C2CMessage, GroupMessage
from dotenv import load_dotenv
from rizline_bindings import RizlineBindingStore
from rizline_cos import CosImageStorageSettings, CosImageUploadError, CosImageUploader
from rizline import RizlineCommandResult, RizlineScoreService
from rizline_qq_login import LOGIN_ACTIONS, QQLoginService, RizlineSmsClient
from rizline_token_vault import TokenVault, TokenVaultError


LOGGER = logging.getLogger("qq_official_bot")
QQ_DOH_ENDPOINT = "https://dns.google/resolve"
QQ_DNS_SUFFIX = ".qq.com"
LEADING_QQ_MENTION = re.compile(r"^\s*<@!?\d+>\s*")
ENVIRONMENT_FILES = (".env", "env.env")


def normalize_public_image_base_url(value: str) -> str:
    """Validate an administrator-supplied static URL used by QQ to fetch PNGs."""
    cleaned = value.strip()
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("RIZLINE_IMAGE_PUBLIC_BASE_URL must be an absolute http(s) URL.")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("RIZLINE_IMAGE_PUBLIC_BASE_URL must not contain credentials, a query, or a fragment.")
    path = parsed.path.rstrip("/") + "/"
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


@dataclass(frozen=True)
class Settings:
    app_id: str
    app_secret: str
    is_sandbox: bool = False
    dns_fallback: str = "auto"
    rizline_save_dir: Path = Path("data/rizline_saves")
    rizline_default_player: str = "default"
    rizline_song_catalog_path: Path | None = Path("data/rizline_song_catalog.json")
    rizline_image_output_dir: Path | None = Path("data/rizline_exports")
    rizline_artwork_dir: Path | None = None
    rizline_binding_db: Path | None = Path("data/rizline_bindings.json")
    rizline_image_public_base_url: str = ""
    rizline_cos_settings: CosImageStorageSettings | None = None
    log_level: str = "INFO"
    rizline_qq_login_enabled: bool = True
    rizline_login_channel_id: str = "1"
    rizline_token_db: Path = Path("data/rizline_tokens.sqlite3")
    rizline_token_key_file: Path = Path(".secrets/rizline-token.key")

    @classmethod
    def from_environment(cls) -> "Settings":
        # ``.env`` remains the standard credentials file.  ``env.env`` is
        # also accepted for separately stored COS credentials; neither file
        # overrides an environment variable supplied by the service manager.
        for environment_file in ENVIRONMENT_FILES:
            load_dotenv(environment_file, override=False)
        app_id = os.getenv("QQ_APP_ID", "").strip()
        app_secret = os.getenv("QQ_APP_SECRET", "").strip()
        sandbox_value = os.getenv("QQ_IS_SANDBOX", "false").strip().lower()
        dns_fallback = os.getenv("QQ_DNS_FALLBACK", "auto").strip().lower()
        rizline_save_dir = Path(
            os.getenv("RIZLINE_SAVE_DIR", "data/rizline_saves").strip() or "data/rizline_saves"
        )
        rizline_default_player = os.getenv("RIZLINE_DEFAULT_PLAYER", "default").strip() or "default"
        catalog_value = os.getenv("RIZLINE_SONG_CATALOG_PATH", "data/rizline_song_catalog.json").strip()
        image_output_value = os.getenv("RIZLINE_IMAGE_OUTPUT_DIR", "data/rizline_exports").strip()
        artwork_value = os.getenv("RIZLINE_ARTWORK_DIR", "").strip()
        binding_db_value = os.getenv("RIZLINE_BINDING_DB", "data/rizline_bindings.json").strip()
        try:
            image_public_base_url = normalize_public_image_base_url(
                os.getenv("RIZLINE_IMAGE_PUBLIC_BASE_URL", "")
            )
            rizline_cos_settings = CosImageStorageSettings.from_environment()
        except ValueError as exc:
            raise RuntimeError(str(exc)) from exc
        rizline_song_catalog_path = Path(catalog_value) if catalog_value else None
        rizline_image_output_dir = Path(image_output_value) if image_output_value else None
        rizline_artwork_dir = Path(artwork_value) if artwork_value else None
        rizline_binding_db = Path(binding_db_value) if binding_db_value else None
        log_level = os.getenv("LOG_LEVEL", "INFO").upper()
        login_enabled = os.getenv("RIZLINE_QQ_LOGIN_ENABLED", "true").strip().lower()
        login_channel = os.getenv("RIZLINE_LOGIN_CHANNEL_ID", "1").strip()
        if login_enabled not in {"true", "false", "1", "0", "yes", "no"}:
            raise RuntimeError("RIZLINE_QQ_LOGIN_ENABLED must be true or false.")
        if login_channel not in {str(number) for number in range(1, 12)}:
            raise RuntimeError("RIZLINE_LOGIN_CHANNEL_ID must be between 1 and 11.")

        missing = [
            name
            for name, value in (("QQ_APP_ID", app_id), ("QQ_APP_SECRET", app_secret))
            if not value or value.startswith("replace-with-")
        ]
        if missing:
            raise RuntimeError(
                "Missing " + ", ".join(missing) + ". Copy .env.example to .env and fill it in."
            )
        if sandbox_value not in {"true", "false", "1", "0", "yes", "no"}:
            raise RuntimeError("QQ_IS_SANDBOX must be true or false.")
        if dns_fallback not in {"auto", "on", "off"}:
            raise RuntimeError("QQ_DNS_FALLBACK must be auto, on, or off.")
        if image_public_base_url and rizline_image_output_dir is None:
            raise RuntimeError(
                "RIZLINE_IMAGE_OUTPUT_DIR must be set when RIZLINE_IMAGE_PUBLIC_BASE_URL is configured."
            )
        if image_public_base_url and rizline_cos_settings:
            raise RuntimeError(
                "Configure either RIZLINE_IMAGE_PUBLIC_BASE_URL or Tencent COS image delivery, not both."
            )
        return cls(
            app_id=app_id,
            app_secret=app_secret,
            is_sandbox=sandbox_value in {"true", "1", "yes"},
            dns_fallback=dns_fallback,
            rizline_save_dir=rizline_save_dir,
            rizline_default_player=rizline_default_player,
            rizline_song_catalog_path=rizline_song_catalog_path,
            rizline_image_output_dir=rizline_image_output_dir,
            rizline_artwork_dir=rizline_artwork_dir,
            rizline_binding_db=rizline_binding_db,
            rizline_image_public_base_url=image_public_base_url,
            rizline_cos_settings=rizline_cos_settings,
            log_level=log_level,
            rizline_qq_login_enabled=login_enabled in {"true", "1", "yes"},
            rizline_login_channel_id=login_channel,
            rizline_token_db=Path(os.getenv("RIZLINE_TOKEN_DB", "data/rizline_tokens.sqlite3").strip() or "data/rizline_tokens.sqlite3"),
            rizline_token_key_file=Path(os.getenv("RIZLINE_TOKEN_KEY_FILE", ".secrets/rizline-token.key").strip() or ".secrets/rizline-token.key"),
        )


class ReplyService:
    """Route score queries and opt-in private SMS login without echoing input."""

    def __init__(
        self,
        rizline: RizlineScoreService | None = None,
        *,
        bindings: RizlineBindingStore | None = None,
        login: QQLoginService | None = None,
    ) -> None:
        self._rizline = rizline or RizlineScoreService("data/rizline_saves")
        self._bindings = bindings
        self._login = login

    async def reply_for(self, text: str, *, source: str, user_openid: str | None = None) -> str:
        """Return text for callers that do not support media replies."""
        return (await self.response_for(text, source=source, user_openid=user_openid)).content

    async def response_for(
        self,
        text: str,
        *,
        source: str,
        user_openid: str | None = None,
    ) -> RizlineCommandResult:
        """Return a text reply and, for B40, an optional rendered PNG path."""
        cleaned = LEADING_QQ_MENTION.sub("", text.strip()) if source == "group" else text.strip()
        command = cleaned.lower()

        if self._login:
            login_reply = await self._login.handle(cleaned, source=source, openid=user_openid)
            if login_reply is not None:
                return RizlineCommandResult(login_reply)
        else:
            tokens = command.split()
            if len(tokens) >= 2 and tokens[0] in {"/riz", "riz"} and tokens[1] in LOGIN_ACTIONS:
                return RizlineCommandResult("自助登录暂未启用；已绑定玩家仍可正常查分。")

        if command in {"/help", "帮助", "help"}:
            return RizlineCommandResult("可用命令：/help、/ping、/riz help。自助绑定请私聊发送 /riz login；请勿在群聊发送登录详情。")
        if command == "/ping":
            return RizlineCommandResult("pong")
        if command.startswith("/riz") or command.startswith("riz ") or command == "查分":
            return self._rizline_response(cleaned, source=source, user_openid=user_openid)
        if not cleaned:
            return RizlineCommandResult("你好！请发送文字，或输入 /help 查看命令。")

        return RizlineCommandResult("请发送 /help 查看命令。自助登录只在私聊中通过 /riz login 发起；不会回显你的消息内容。")

    def _rizline_response(
        self,
        command: str,
        *,
        source: str,
        user_openid: str | None,
    ) -> RizlineCommandResult:
        tokens = command.split()
        action = tokens[1].casefold() if len(tokens) > 1 else ""
        if action in {"bind", "绑定"}:
            if self._login and source == "c2c" and user_openid:
                self._login.cancel(user_openid)
            return self._bind_response(tokens, source=source, user_openid=user_openid)
        if action in {"unbind", "解绑"}:
            if self._login and source == "c2c" and user_openid:
                self._login.cancel(user_openid)
            return self._unbind_response(tokens, source=source, user_openid=user_openid)
        if action in {"binding", "bound", "绑定状态"}:
            return self._binding_status_response(tokens, user_openid=user_openid)

        bound_alias: str | None = None
        if self._bindings:
            if len(tokens) <= 1 or action in {"help", "帮助"}:
                return self._rizline.response_for(command)
            if not user_openid:
                return RizlineCommandResult("无法识别你的 QQ 身份，暂时无法查询已绑定的 RizLine 存档。")
            try:
                bound_alias = self._bindings.alias_for(user_openid)
            except RuntimeError:
                LOGGER.exception("Unable to read RizLine QQ bindings")
                return RizlineCommandResult("RizLine 绑定服务暂时不可用，请稍后再试。")
            if not bound_alias:
                return RizlineCommandResult(
                    "请先在与机器人的 C2C 私聊中发送 /riz login 自助登录绑定。也可使用原有的 /riz bind <绑定码>。"
                )
        return self._rizline.response_for(
            command,
            default_player_override=bound_alias,
            restrict_player_to=bound_alias,
        )

    def _bind_response(
        self,
        tokens: list[str],
        *,
        source: str,
        user_openid: str | None,
    ) -> RizlineCommandResult:
        if not self._bindings:
            return RizlineCommandResult("管理员尚未启用 RizLine QQ 绑定。")
        if source != "c2c":
            return RizlineCommandResult("为保护绑定码，请在与机器人的 C2C 私聊中完成绑定。")
        if not user_openid:
            return RizlineCommandResult("无法识别你的 QQ 身份，暂时无法绑定。")
        if len(tokens) != 3:
            return RizlineCommandResult("用法：/riz bind <管理员私下发给你的绑定码>")
        try:
            result = self._bindings.redeem(tokens[2], user_openid)
        except RuntimeError:
            LOGGER.warning("Unable to redeem RizLine QQ binding")
            return RizlineCommandResult("绑定操作未完成，请稍后重试。已保存的登录凭据可能已清除，必要时请 /riz login 重新登录。")
        if result.status != "bound" or not result.alias:
            return RizlineCommandResult("绑定码无效或已过期。请向管理员索取新的绑定码。")
        return RizlineCommandResult(
            "已绑定你的本机 RizLine 成绩存档。现在可直接发送 /riz profile、/riz top 或 /riz b40。"
        )

    def _unbind_response(
        self,
        tokens: list[str],
        *,
        source: str,
        user_openid: str | None,
    ) -> RizlineCommandResult:
        if source != "c2c":
            return RizlineCommandResult("请在与机器人的 C2C 私聊中解绑。")
        if len(tokens) != 2:
            return RizlineCommandResult("用法：/riz unbind")
        if not self._bindings or not user_openid:
            return RizlineCommandResult("你当前没有可解绑的 RizLine 存档。")
        try:
            removed = self._bindings.unbind(user_openid)
        except RuntimeError:
            LOGGER.warning("Unable to remove RizLine QQ binding")
            return RizlineCommandResult("解绑操作未完成，请稍后重试。已保存的登录凭据可能已清除，必要时请 /riz login 重新登录。")
        return RizlineCommandResult(("已解除 RizLine 存档绑定。" if removed else "你当前没有 RizLine 存档绑定。")
                                    + "本机保存的游戏登录凭据已删除；已有成绩文件仍保留。")

    def _binding_status_response(self, tokens: list[str], *, user_openid: str | None) -> RizlineCommandResult:
        if len(tokens) != 2:
            return RizlineCommandResult("用法：/riz binding")
        if not self._bindings or not user_openid:
            return RizlineCommandResult("你当前没有 RizLine 存档绑定。")
        try:
            alias = self._bindings.alias_for(user_openid)
        except RuntimeError:
            LOGGER.exception("Unable to read RizLine QQ bindings")
            return RizlineCommandResult("RizLine 绑定服务暂时不可用，请稍后再试。")
        return RizlineCommandResult("你当前没有 RizLine 存档绑定。" if not alias else "已绑定 RizLine 成绩存档。")


class QQOfficialBot(botpy.Client):
    def __init__(
        self,
        *,
        intents: botpy.Intents,
        replies: ReplyService,
        is_sandbox: bool = False,
        image_output_dir: Path | None = None,
        image_public_base_url: str = "",
        cos_image_uploader: CosImageUploader | None = None,
    ) -> None:
        super().__init__(intents=intents, is_sandbox=is_sandbox)
        self._replies = replies
        self._image_output_dir = image_output_dir.resolve() if image_output_dir else None
        self._image_public_base_url = image_public_base_url
        self._cos_image_uploader = cos_image_uploader

    async def on_ready(self) -> None:
        robot_name = getattr(getattr(self, "robot", None), "name", "QQ bot")
        LOGGER.info("%s is connected and ready", robot_name)

    async def on_error(self, event_method: str, *args, **kwargs) -> None:
        LOGGER.error("A QQ event could not be handled; event details suppressed")

    async def on_group_at_message_create(self, message: GroupMessage) -> None:
        """Reply when the bot is @mentioned in an enabled group."""
        await self._reply(message, source="group")

    async def on_c2c_message_create(self, message: C2CMessage) -> None:
        """Reply to an enabled official C2C/private-message event."""
        await self._reply(message, source="c2c")

    async def _reply(self, message: GroupMessage | C2CMessage, *, source: str) -> None:
        try:
            LOGGER.info("Received a %s message", source)
            response = await self._replies.response_for(
                message.content or "",
                source=source,
                user_openid=self._sender_openid(message, source=source),
            )
            # BotPy attaches the correct msg_id and recipient for a passive reply.
            await message.reply(content=response.content, msg_seq=1)
            if response.image_path:
                await self._reply_with_image(message, source=source, image_path=response.image_path)
            LOGGER.info("Replied to a %s message", source)
        except Exception:
            # Do not let one malformed event terminate the long-lived gateway client.
            LOGGER.error("Unable to reply to a %s message; response details suppressed", source)

    @staticmethod
    def _sender_openid(message: GroupMessage | C2CMessage, *, source: str) -> str | None:
        """Read only the sender identifier required for a local binding lookup."""
        author = getattr(message, "author", None)
        names = ("member_openid", "user_openid") if source == "group" else ("user_openid",)
        for name in names:
            value = getattr(author, name, None)
            if isinstance(value, str) and value:
                return value
        return None

    def _public_image_url(self, image_path: Path) -> str | None:
        if not self._image_output_dir or not self._image_public_base_url:
            return None
        try:
            relative_path = image_path.resolve(strict=True).relative_to(self._image_output_dir)
        except (OSError, ValueError):
            LOGGER.warning("Refusing to send a score image outside the configured output directory")
            return None
        encoded_path = "/".join(quote(part) for part in relative_path.parts)
        return f"{self._image_public_base_url}{encoded_path}"

    def _image_url_for_delivery(self, image_path: Path) -> str | None:
        """Get a public static URL or a short-lived private COS signed URL."""
        if self._cos_image_uploader:
            try:
                return self._cos_image_uploader.upload_and_get_url(image_path)
            except CosImageUploadError:
                LOGGER.exception("Unable to upload generated RizLine B40 image to Tencent COS")
                return None
        return self._public_image_url(image_path)

    async def _reply_with_image(
        self, message: GroupMessage | C2CMessage, *, source: str, image_path: Path
    ) -> None:
        public_url = self._image_url_for_delivery(image_path)
        if not public_url:
            detail = (
                "腾讯 COS 上传失败；请检查 COS 密钥、存储桶和地域后重试。"
                if self._cos_image_uploader
                else "尚未配置 RIZLINE_IMAGE_PUBLIC_BASE_URL 或腾讯 COS 图片发送。"
            )
            await message.reply(
                content=f"成绩图已在机器人本机生成；{detail}",
                msg_seq=2,
            )
            return

        try:
            if source == "group":
                media = await message._api.post_group_file(
                    group_openid=message.group_openid,
                    file_type=1,
                    url=public_url,
                    srv_send_msg=False,
                )
            else:
                media = await message._api.post_c2c_file(
                    openid=message.author.user_openid,
                    file_type=1,
                    url=public_url,
                    srv_send_msg=False,
                )
            await message.reply(msg_type=7, media=media, msg_seq=2)
        except Exception:
            LOGGER.exception("Unable to send generated RizLine B40 image to a %s message", source)
            try:
                await message.reply(
                    content="成绩图已生成，但 QQ 图片发送失败；请检查图片发送配置后重试。",
                    msg_seq=3,
                )
            except Exception:
                LOGGER.exception("Unable to send the B40 image failure notice")


def ensure_event_loop() -> None:
    """Support BotPy on Python 3.14+, which no longer creates a loop implicitly."""
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())


class QQDnsFallback:
    """Resolve QQ endpoints safely when a local DNS server returns private IPs.

    TLS still validates the original QQ hostname, so a DNS answer alone cannot
    impersonate the QQ API.  The fallback is deliberately limited to QQ
    domains and only activates in ``auto`` mode when normal DNS has no global
    address.
    """

    def __init__(self, mode: str) -> None:
        self._mode = mode
        self._original_getaddrinfo = socket.getaddrinfo
        self._cache: dict[str, tuple[float, list[str]]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def _is_qq_host(host: object) -> bool:
        return isinstance(host, str) and host.rstrip(".").lower().endswith(QQ_DNS_SUFFIX)

    @staticmethod
    def _has_global_address(records: list[tuple]) -> bool:
        for record in records:
            try:
                if ipaddress.ip_address(record[4][0]).is_global:
                    return True
            except (IndexError, ValueError):
                continue
        return False

    def _resolve_public_ipv4(self, host: str) -> list[str]:
        normalized_host = host.rstrip(".").lower()
        now = time.monotonic()
        with self._lock:
            cached = self._cache.get(normalized_host)
            if cached and cached[0] > now:
                return cached[1]

            query = urlencode({"name": normalized_host, "type": "A"})
            try:
                with urlopen(f"{QQ_DOH_ENDPOINT}?{query}", timeout=5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except Exception as exc:
                LOGGER.warning("Public DNS fallback failed for %s: %s", normalized_host, exc)
                return []

            ttl = 60
            addresses: list[str] = []
            for answer in payload.get("Answer", []):
                if answer.get("type") != 1:
                    continue
                candidate = answer.get("data", "")
                try:
                    if ipaddress.ip_address(candidate).is_global:
                        addresses.append(candidate)
                        ttl = min(ttl, max(1, int(answer.get("TTL", ttl))))
                except ValueError:
                    continue

            if addresses:
                self._cache[normalized_host] = (now + ttl, addresses)
            return addresses

    def install(self) -> None:
        if self._mode == "off":
            return

        def getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
            if not self._is_qq_host(host) or family == socket.AF_INET6:
                return self._original_getaddrinfo(host, port, family, type, proto, flags)

            try:
                normal_records = self._original_getaddrinfo(host, port, family, type, proto, flags)
            except socket.gaierror:
                normal_records = []

            should_fallback = self._mode == "on" or not self._has_global_address(normal_records)
            if should_fallback:
                addresses = self._resolve_public_ipv4(host)
                if addresses:
                    LOGGER.warning("Using public DNS fallback for %s", host)
                    resolved_records: list[tuple] = []
                    for address in addresses:
                        resolved_records.extend(
                            self._original_getaddrinfo(address, port, family, type, proto, flags)
                        )
                    return resolved_records

            if normal_records:
                return normal_records
            return self._original_getaddrinfo(host, port, family, type, proto, flags)

        socket.getaddrinfo = getaddrinfo


class CredentialSafeSdkFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno < logging.INFO:
            return False
        if record.levelno >= logging.WARNING or record.exc_info:
            record.msg = "QQ SDK warning/error; response details suppressed"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
            record.stack_info = None
        return True


def protect_sdk_logs() -> None:
    sdk_logger = logging.getLogger("botpy")
    sdk_logger.setLevel(logging.INFO)
    if not any(isinstance(item, CredentialSafeSdkFilter) for item in sdk_logger.filters):
        sdk_logger.addFilter(CredentialSafeSdkFilter())


def main() -> None:
    settings = Settings.from_environment()
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    # public_messages enables BotPy's group @ and C2C callbacks.  The matching
    # event permissions must also be enabled in the QQ Open Platform console.
    intents = botpy.Intents(public_messages=True)
    ensure_event_loop()
    QQDnsFallback(settings.dns_fallback).install()
    bindings = RizlineBindingStore(settings.rizline_binding_db) if settings.rizline_binding_db else None
    login = None
    vault = None
    if bindings:
        # Revocation must also work when new SMS logins are administratively disabled.
        def unavailable_revoker(openid: str) -> None:
            raise TokenVaultError()

        bindings.credential_revoker = unavailable_revoker
        try:
            for private_path in (settings.rizline_token_db, settings.rizline_token_key_file):
                for unsafe_dir in (settings.rizline_save_dir, settings.rizline_image_output_dir, settings.rizline_artwork_dir):
                    if unsafe_dir and private_path.resolve().is_relative_to(unsafe_dir.resolve()):
                        raise TokenVaultError()
            vault = TokenVault(settings.rizline_token_db, settings.rizline_token_key_file)
            bindings.credential_revoker = vault.delete
            LOGGER.info("RizLine encrypted credential storage enabled")
        except (OSError, ValueError, RuntimeError):
            LOGGER.warning("RizLine encrypted storage unavailable; login and binding changes disabled")
    if bindings and vault and settings.rizline_qq_login_enabled:
        try:
            sms_client = RizlineSmsClient(
                Path(__file__).resolve().parent / "vendor/RizlineGameSaveData/gameDataAes2Json.py",
                channel_id=settings.rizline_login_channel_id,
            )
            login = QQLoginService(bindings, settings.rizline_save_dir, sms_client, vault=vault)
            LOGGER.info("RizLine C2C self-service SMS login enabled")
        except (OSError, ImportError, ValueError, AttributeError, RuntimeError):
            LOGGER.warning("RizLine C2C login unavailable; check local decoder and dependencies")
    client = QQOfficialBot(
        intents=intents,
        replies=ReplyService(
            RizlineScoreService(
                settings.rizline_save_dir,
                settings.rizline_default_player,
                song_catalog_path=settings.rizline_song_catalog_path,
                image_output_dir=settings.rizline_image_output_dir,
                artwork_dir=settings.rizline_artwork_dir,
            ),
            bindings=bindings,
            login=login,
        ),
        is_sandbox=settings.is_sandbox,
        image_output_dir=settings.rizline_image_output_dir,
        image_public_base_url=settings.rizline_image_public_base_url,
        cos_image_uploader=(CosImageUploader(settings.rizline_cos_settings) if settings.rizline_cos_settings else None),
    )
    protect_sdk_logs()
    logging.getLogger("botpy").info("RizLine C2C self-service login status: %s", "enabled" if login else "disabled")
    client.run(appid=settings.app_id, secret=settings.app_secret)


if __name__ == "__main__":
    main()
