"""Privacy-preserving RizLine score queries from locally exported save files.

The bot never logs in to a player's game account.  An administrator places an
already exported and decrypted save JSON file at ``<save_dir>/<player>.json``;
this module reads only the score-related fields needed for a reply.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from html import unescape
from pathlib import Path
from typing import Any
from urllib.parse import unquote

from rizline_b40 import B40ImageRenderer, B40Report, RizlinePlayerCard, RizlineSongCatalog, build_b40


LOGGER = logging.getLogger("qq_official_bot.rizline")
PLAYER_ALIAS_RE = re.compile(r"^[\w-]{1,40}$", re.UNICODE)
TRACK_SUFFIX_RE = re.compile(r"\.\d+$")
MAX_SAVE_BYTES = 10 * 1024 * 1024
MAX_TOP_ROWS = 20


@dataclass(frozen=True)
class ScoreRecord:
    track_id: str
    title: str
    difficulty: str
    score: int
    complete_rate: float
    full_combo: bool
    cleared: bool
    rks: float | None


@dataclass(frozen=True)
class PlayerProfile:
    alias: str
    username: str
    total_rks: float | None
    records: tuple[ScoreRecord, ...]
    card: RizlinePlayerCard = field(default_factory=RizlinePlayerCard)

    @property
    def ranked_records(self) -> list[ScoreRecord]:
        return sorted(
            self.records,
            key=lambda record: (
                record.rks is not None,
                record.rks if record.rks is not None else -1.0,
                record.score,
            ),
            reverse=True,
        )


@dataclass(frozen=True)
class CachedProfile:
    modified_ns: int
    size: int
    profile: PlayerProfile


@dataclass(frozen=True)
class RizlineCommandResult:
    """A safe text reply with an optional locally rendered B40 image."""

    content: str
    image_path: Path | None = None


class RizlineScoreService:
    """Serve read-only score queries for local RizLine save exports."""

    def __init__(
        self,
        save_dir: Path | str,
        default_player: str = "default",
        *,
        song_catalog_path: Path | str | None = None,
        image_output_dir: Path | str | None = None,
        artwork_dir: Path | str | None = None,
    ) -> None:
        self._save_dir = Path(save_dir)
        self._default_player = default_player.strip() or "default"
        self._cache: dict[str, CachedProfile] = {}
        self._song_catalog = RizlineSongCatalog(song_catalog_path)
        self._b40_renderer = B40ImageRenderer(image_output_dir, artwork_dir=artwork_dir)
        self._image_output_enabled = image_output_dir is not None

    def reply_for(self, message: str) -> str:
        """Handle a `/riz ...` command and return a backward-compatible text reply."""
        return self.response_for(message).content

    def response_for(
        self,
        message: str,
        *,
        default_player_override: str | None = None,
        restrict_player_to: str | None = None,
    ) -> RizlineCommandResult:
        """Handle a `/riz ...` command and include a rendered image when available."""
        tokens = message.strip().split()
        if not tokens or tokens[0].casefold() not in {"/riz", "riz", "查分"}:
            return RizlineCommandResult(self.help_text())

        args = tokens[1:]
        if not args or args[0].casefold() in {"help", "帮助"}:
            return RizlineCommandResult(self.help_text())

        if args[0].casefold() in {"profile", "top", "b40", "song"}:
            player = default_player_override or self._default_player
            action = args[0].casefold()
            action_args = args[1:]
            # Also accept the more natural `/riz top alice 10` form alongside
            # `/riz alice top 10`.  For `song`, text after the action remains
            # the default player's search phrase to avoid ambiguity.
            if action in {"profile", "b40"} and len(action_args) == 1:
                player = action_args[0]
                action_args = []
            elif (
                action == "top"
                and action_args
                and PLAYER_ALIAS_RE.fullmatch(action_args[0])
                and not action_args[0].isdigit()
            ):
                player = action_args[0]
                action_args = action_args[1:]
        elif len(args) >= 2 and args[1].casefold() in {"profile", "top", "b40", "song"}:
            player = args[0]
            action = args[1].casefold()
            action_args = args[2:]
        else:
            return RizlineCommandResult(self.help_text())

        if restrict_player_to is not None and player != restrict_player_to:
            return RizlineCommandResult(
                "你已绑定自己的 RizLine 存档；不能查询其他玩家。"
                "如需更换，请在私聊发送 /riz login 自助登录，或使用新的绑定码。"
            )

        profile, error = self._load_player(player)
        if error:
            return RizlineCommandResult(error)
        assert profile is not None

        if action == "profile":
            if action_args:
                return RizlineCommandResult("`profile` 后不需要额外参数。\n\n" + self.help_text())
            return RizlineCommandResult(self._profile_text(profile))
        if action == "top":
            return RizlineCommandResult(self._top_text(profile, action_args))
        if action == "b40":
            if action_args:
                return RizlineCommandResult("`b40` 后不需要额外参数。\n\n" + self.help_text())
            return self._b40_result(profile)
        return RizlineCommandResult(self._song_text(profile, action_args))

    def help_text(self) -> str:
        return (
            "RizLine 查分（读取你已绑定的本机成绩快照）\n"
            "• /riz login（仅私聊：同意 → 手机号 → 验证码 → 确认）\n"
            "• /riz resend / /riz cancel（私聊重发短信或取消）\n"
            "• /riz update（群聊 @ 或私聊：使用已存令牌更新自己的成绩）\n"
            "• /riz profile [玩家别名]\n"
            "• /riz top [玩家别名] [1-20]\n"
            "• /riz b40 [玩家别名]\n"
            "• /riz song <关键词>\n"
            "• /riz <玩家别名> song <关键词>\n"
            "• /riz bind <绑定码>（仅 C2C 私聊）\n"
            "• /riz unbind（仅 C2C 私聊）\n"
            "私聊 /riz login 短信登录，选择“同意并保存”可加密保存令牌；以后 /riz update 更新，令牌失效需重新登录。"
            "只想保存成绩可选“仅本次登录”。/riz unbind 解绑并删除凭据；请勿在群里发送登录详情。"
        )

    def _load_player(self, player: str) -> tuple[PlayerProfile | None, str | None]:
        if not PLAYER_ALIAS_RE.fullmatch(player):
            return None, "玩家别名只能包含汉字、字母、数字、下划线或连字符，且不超过 40 个字符。"

        path = self._save_dir / f"{player}.json"
        try:
            stat = path.stat()
        except FileNotFoundError:
            return None, f"未配置玩家“{player}”的本地 RizLine 存档。"
        except OSError:
            LOGGER.exception("Unable to inspect RizLine save for player %s", player)
            return None, "读取 RizLine 存档时发生错误，请稍后再试。"

        if stat.st_size > MAX_SAVE_BYTES:
            return None, "RizLine 存档文件过大，已拒绝读取。"

        cached = self._cache.get(player)
        if cached and cached.modified_ns == stat.st_mtime_ns and cached.size == stat.st_size:
            return cached.profile, None

        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            profile = self._parse_profile(player, raw)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            LOGGER.exception("Unable to parse RizLine save for player %s", player)
            return None, f"玩家“{player}”的 RizLine 存档格式无效。"

        self._cache[player] = CachedProfile(
            modified_ns=stat.st_mtime_ns,
            size=stat.st_size,
            profile=profile,
        )
        return profile, None

    @classmethod
    def _parse_profile(cls, player: str, raw: Any) -> PlayerProfile:
        if not isinstance(raw, dict):
            raise ValueError("save must be an object")
        # Accept either the raw rn_login document or a simple {"data": ...} wrapper.
        data = raw.get("data") if isinstance(raw.get("data"), dict) and "myBest" not in raw else raw
        if not isinstance(data, dict):
            raise ValueError("save data must be an object")

        rks_by_key: dict[str, float] = {}
        levels = data.get("levelsRks")
        if isinstance(levels, list):
            for level in levels:
                if not isinstance(level, dict):
                    continue
                track_id = cls._text(level.get("trackId"))
                difficulty = cls._text(level.get("difficultyClassName")).upper()
                rks = cls._number(level.get("rks"))
                if track_id and difficulty and rks is not None:
                    for key in cls._record_key_variants(track_id, difficulty):
                        rks_by_key[key] = rks

        records: list[ScoreRecord] = []
        best_list = data.get("myBest")
        if isinstance(best_list, list):
            for best in best_list:
                if not isinstance(best, dict):
                    continue
                track_id = cls._text(best.get("trackAssetId"))
                difficulty = cls._text(best.get("difficultyClassName")).upper()
                if not track_id or not difficulty:
                    continue
                rks = next(
                    (rks_by_key[key] for key in cls._record_key_variants(track_id, difficulty) if key in rks_by_key),
                    None,
                )
                records.append(
                    ScoreRecord(
                        track_id=track_id,
                        title=cls._title_from_track(track_id),
                        difficulty=difficulty,
                        score=int(cls._number(best.get("score")) or 0),
                        complete_rate=cls._number(best.get("completeRate")) or 0.0,
                        full_combo=cls._truthy(best.get("isFullCombo")),
                        cleared=cls._truthy(best.get("isClear")),
                        rks=rks,
                    )
                )

        if not records:
            raise ValueError("myBest is empty or missing")

        return PlayerProfile(
            alias=player,
            username=cls._text(data.get("username")) or player,
            total_rks=cls._number(data.get("totalRks")),
            records=tuple(records),
            card=cls._parse_player_card(data.get("rizcard")),
        )

    @classmethod
    def _parse_player_card(cls, raw_card: Any) -> RizlinePlayerCard:
        """Extract display-only Rizcard fields without retaining the full save."""
        if not isinstance(raw_card, dict):
            return RizlinePlayerCard()
        position = raw_card.get("avatarPos")
        position = position if isinstance(position, dict) else {}
        return RizlinePlayerCard(
            avatar_id=cls._asset_id(raw_card.get("avatarId")),
            background_id=cls._asset_id(raw_card.get("backgroundId")),
            layout_id=cls._asset_id(raw_card.get("layoutId")),
            bio_id1=cls._asset_id(raw_card.get("bioId1")),
            bio_id2=cls._asset_id(raw_card.get("bioId2")),
            avatar_x=cls._unit_number(position.get("x"), default=0.5),
            avatar_y=cls._unit_number(position.get("y"), default=0.5),
        )

    @staticmethod
    def _text(value: Any) -> str:
        return str(value).strip() if value is not None else ""

    @classmethod
    def _asset_id(cls, value: Any) -> str:
        candidate = cls._text(value)
        if len(candidate) > 256 or any(character in candidate for character in "\r\n\x00"):
            return ""
        return candidate

    @classmethod
    def _unit_number(cls, value: Any, *, default: float) -> float:
        number = cls._number(value)
        if number is None:
            return default
        return max(0.0, min(number, 1.0))

    @staticmethod
    def _number(value: Any) -> float | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return float(str(value).replace(",", "").rstrip("+"))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _truthy(value: Any) -> bool:
        return value is True or str(value).strip().lower() in {"1", "true", "yes"}

    @staticmethod
    def _record_key_variants(track_id: str, difficulty: str) -> tuple[str, ...]:
        normalized = track_id.strip()
        without_prefix = normalized.removeprefix("track.")
        return (
            f"{normalized}|{difficulty}",
            f"{without_prefix}|{difficulty}",
        )

    @staticmethod
    def _title_from_track(track_id: str) -> str:
        asset = unquote(unescape(track_id)).removeprefix("track.")
        asset = TRACK_SUFFIX_RE.sub("", asset)
        title = asset.split(".", maxsplit=1)[0].strip()
        return title or track_id

    @staticmethod
    def _record_line(rank: int, record: ScoreRecord) -> str:
        flags = "FC" if record.full_combo else "Clear" if record.cleared else ""
        rks_text = f"RKS {record.rks:.4f}" if record.rks is not None else "RKS -"
        return (
            f"{rank}. {record.title} [{record.difficulty}]\n"
            f"   {record.score:,} · {record.complete_rate:.2f}% · {rks_text}"
            f"{f' · {flags}' if flags else ''}"
        )

    def _profile_text(self, profile: PlayerProfile) -> str:
        full_combo_count = sum(record.full_combo for record in profile.records)
        clear_count = sum(record.cleared for record in profile.records)
        total_rks = f"{profile.total_rks:.4f}" if profile.total_rks is not None else "-"
        return (
            f"RizLine｜{profile.username}（{profile.alias}）\n"
            f"总 RKS：{total_rks}\n"
            f"已记录谱面：{len(profile.records)} · FC：{full_combo_count} · 通关：{clear_count}"
        )

    def _top_text(self, profile: PlayerProfile, args: list[str]) -> str:
        if len(args) > 1:
            return "`top` 最多接受一个数量参数。\n\n" + self.help_text()
        count = 10
        if args:
            try:
                count = int(args[0])
            except ValueError:
                return "数量应为 1 到 20 的整数。"
        if not 1 <= count <= MAX_TOP_ROWS:
            return f"数量应为 1 到 {MAX_TOP_ROWS}。"

        ranked = profile.ranked_records[:count]
        if not ranked:
            return "这份存档没有可显示的成绩。"
        lines = [f"RizLine Top {len(ranked)}｜{profile.username}（{profile.alias}）"]
        lines.extend(self._record_line(index, record) for index, record in enumerate(ranked, start=1))
        return "\n".join(lines)

    def _b40_result(self, profile: PlayerProfile) -> RizlineCommandResult:
        report = build_b40(profile.records, self._song_catalog)
        if not report.selected:
            return RizlineCommandResult("这份存档缺少 levelsRks，暂时无法生成 RKS Top 40 摘要。")

        image_path: Path | None = None
        image_note = ""
        if self._image_output_enabled:
            try:
                image_path = self._b40_renderer.render(
                    profile.username,
                    profile.alias,
                    report,
                    total_rks=profile.total_rks,
                    player_card=profile.card,
                )
                image_note = "\n已生成 B40 成绩图。"
            except (OSError, ValueError):
                LOGGER.exception("Unable to render RizLine B40 image for player %s", profile.alias)
                image_note = "\n成绩图生成失败，已返回文字结果。"
        else:
            image_note = "\n图片导出未启用；管理员需配置 RIZLINE_IMAGE_OUTPUT_DIR。"

        return RizlineCommandResult(self._b40_text(profile, report) + image_note, image_path)

    @staticmethod
    def _b40_text(profile: PlayerProfile, report: B40Report) -> str:
        selected_count = len(report.selected)
        lines = [
            f"RizLine B40｜{profile.username}（{profile.alias}）",
            f"计算：{report.mode_label}",
            f"B40 RATING：{report.rating:.4f}",
            f"纳入：AH5 {len(report.special5)}/5 · B35 {len(report.ordinary35)}/35 · 共 {selected_count}/40",
        ]
        if report.exact_ah5_b35:
            if len(report.special5) < 5:
                lines.append("说明：可识别的 AH 候选不足 5 条，结果保留空位并仍按 40 条固定分母计算。")
            else:
                possible_count = sum(entry.ah_status == 2 for entry in report.special5)
                if possible_count:
                    lines.append(f"说明：AH5 中有 {possible_count} 条为 ?AH 推定，可能与游戏内显示有轻微差异。")
        else:
            lines.append(
                f"说明：{report.missing_metadata_count} 条谱面缺少本地定数数据，已退回 RKS Top 40，不把它误称为 AH5+B35。"
            )
        return "\n".join(lines)

    def _song_text(self, profile: PlayerProfile, args: list[str]) -> str:
        keyword = " ".join(args).strip().casefold()
        if not keyword:
            return "用法：/riz song <关键词>，或 /riz <玩家别名> song <关键词>。"
        matches = [
            record
            for record in profile.ranked_records
            if keyword in record.title.casefold() or keyword in record.track_id.casefold()
        ][:5]
        if not matches:
            return f"在“{profile.alias}”的存档中没有找到“{' '.join(args)}”。"
        lines = [f"RizLine 单曲查分｜{profile.username}（{profile.alias}）"]
        lines.extend(self._record_line(index, record) for index, record in enumerate(matches, start=1))
        return "\n".join(lines)
