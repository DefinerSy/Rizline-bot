#!/usr/bin/env python3
"""Local-only browser flow for importing a user's RizLine save and QQ binding.

The server is deliberately limited to a loopback address.  It invokes an
administrator-provided checkout of ``RizlineGameSaveData`` as a separate
process, discards that process's sensitive output, then imports only its
decrypted ``gameData.json`` into the bot and creates a one-time QQ binding
code.  It is not a public website and must never be reverse-proxied.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http import HTTPStatus
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from rizline_bindings import PLAYER_ALIAS_RE, RizlineBindingStore
from tools.import_rizline_save import import_save


PHONE_RE = re.compile(r"^[0-9+]{6,24}$")
SMS_CODE_RE = re.compile(r"^[0-9]{4,12}$")
SESSION_COOKIE = "rizline_local_login"
MAX_SESSIONS = 32
SMS_PROMPT = "请输入验证码"
RUNNER_FAILURE_MARKERS = {
    "TLS": "RIZLINE_LOCAL_FAILURE_TLS",
    "NETWORK": "RIZLINE_LOCAL_FAILURE_NETWORK",
    "DEPENDENCY_REQUESTS": "RIZLINE_LOCAL_FAILURE_DEPENDENCY_REQUESTS",
    "DEPENDENCY_URLLIB3": "RIZLINE_LOCAL_FAILURE_DEPENDENCY_URLLIB3",
    "DEPENDENCY_CRYPTO": "RIZLINE_LOCAL_FAILURE_DEPENDENCY_CRYPTO",
    "DEPENDENCY_DECRYPTOR": "RIZLINE_LOCAL_FAILURE_DEPENDENCY_DECRYPTOR",
    "DEPENDENCY_OTHER": "RIZLINE_LOCAL_FAILURE_DEPENDENCY_OTHER",
    "INPUT": "RIZLINE_LOCAL_FAILURE_INPUT",
    "SCRIPT": "RIZLINE_LOCAL_FAILURE_SCRIPT",
    "UPSTREAM": "RIZLINE_LOCAL_FAILURE_UPSTREAM",
    "UNEXPECTED": "RIZLINE_LOCAL_FAILURE_UNEXPECTED",
}


@dataclass
class LoginSession:
    csrf: str
    state: str = "idle"
    message: str = "等待登录。"
    alias: str = ""
    replace: bool = False
    process: subprocess.Popen[str] | None = None
    binding_code: str | None = None
    sms_prompt_seen: bool = False
    source_before: tuple[int, int, int, int] | None = None
    runner_failure: str | None = None
    updated_at: float = 0.0


class LocalLoginManager:
    """Single-use local login sessions; sensitive child output is discarded."""

    def __init__(
        self,
        *,
        tool_dir: Path | str,
        tool_python: Path | str,
        save_dir: Path | str,
        binding_db: Path | str,
    ) -> None:
        # The child process runs with ``tool_dir`` as its working directory;
        # all paths must therefore be absolute before Popen is called.
        self._tool_dir = Path(tool_dir).expanduser().resolve()
        # Do not resolve the interpreter symlink: a venv's bin/python often
        # points at the system executable, and resolving it would silently
        # bypass the venv's site-packages.
        self._tool_python = os.path.abspath(os.fspath(Path(tool_python).expanduser()))
        self._save_dir = Path(save_dir).expanduser().resolve()
        self._bindings = RizlineBindingStore(Path(binding_db).expanduser().resolve())
        self._sessions: dict[str, LoginSession] = {}
        self._lock = threading.RLock()

    def create_session(self) -> tuple[str, LoginSession]:
        with self._lock:
            self._prune_sessions()
            session_id = secrets.token_urlsafe(24)
            session = LoginSession(csrf=secrets.token_urlsafe(24), updated_at=time.monotonic())
            self._sessions[session_id] = session
            return session_id, session

    def session_for(self, session_id: str | None) -> LoginSession | None:
        if not session_id:
            return None
        with self._lock:
            session = self._sessions.get(session_id)
            if session:
                session.updated_at = time.monotonic()
            return session

    def start(self, session: LoginSession, *, phone: str, password: str, alias: str, replace: bool) -> None:
        normalized_phone = phone.strip()
        if not PHONE_RE.fullmatch(normalized_phone):
            raise ValueError("请输入有效的手机号。")
        if not PLAYER_ALIAS_RE.fullmatch(alias):
            raise ValueError("玩家别名只能包含汉字、字母、数字、下划线或连字符，且不超过 40 个字符。")
        if "\n" in password or "\r" in password:
            raise ValueError("密码格式无效。")

        script = self._tool_dir / "getUser.py"
        if not script.is_file():
            raise ValueError("本机拉档工具尚未安装；请先按页面下方说明完成安装。")
        target = self._save_dir / f"{alias}.json"
        if target.exists() and not replace:
            raise ValueError("该玩家别名已有本地存档；勾选“替换同别名的已有本地存档”后再继续。")
        if session.process and session.process.poll() is None:
            raise ValueError("当前浏览器会话已有正在进行的登录。")
        with self._lock:
            if any(
                item.process and item.process.poll() is None
                for item in self._sessions.values()
                if item is not session
            ):
                raise ValueError("已有另一项本机登录正在进行，请完成后再试。")

            try:
                self._secure_tool_config()
                # Snapshot before the child can start: a previous export is
                # not evidence that this login produced a fresh one.
                source_before = self._source_fingerprint(self._tool_dir / "gameData.json")
                process = subprocess.Popen(
                    [self._tool_python, str(PROJECT_ROOT / "tools" / "rizline_login_runner.py"), str(script)],
                    cwd=self._tool_dir,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=0,
                    # ``getUser.py`` creates both config.json and gameData.json.
                    # Set a private creation mask before it can write either file.
                    umask=0o077,
                    env={**os.environ, "PYTHONUNBUFFERED": "1"},
                )
                assert process.stdin is not None
                process.stdin.write(f"{normalized_phone}\n{password}\n")
                process.stdin.flush()
            except OSError as exc:
                raise ValueError("无法启动本机拉档工具。") from exc

            session.alias = alias
            session.replace = replace
            session.process = process
            session.binding_code = None
            session.sms_prompt_seen = False
            session.source_before = source_before
            session.runner_failure = None
            session.state = "logging_in"
            session.message = "正在本机登录并拉取存档…"
            session.updated_at = time.monotonic()
            threading.Thread(target=self._watch_process, args=(session,), daemon=True).start()

    def _secure_tool_config(self) -> None:
        """Keep the upstream token file private even before login completes."""
        config = self._tool_dir / "config.json"
        try:
            if not config.exists():
                config.write_text("{}\n", encoding="utf-8")
            os.chmod(config, 0o600)
        except OSError as exc:
            raise ValueError("无法准备本机拉档工具的私有配置文件。") from exc

    def submit_sms_code(self, session: LoginSession, code: str) -> None:
        normalized_code = code.strip()
        if not SMS_CODE_RE.fullmatch(normalized_code):
            raise ValueError("短信验证码格式无效。")
        process = session.process
        if session.state != "waiting_for_sms" or not process or process.poll() is not None:
            raise ValueError("当前没有等待输入的短信验证码。")
        try:
            assert process.stdin is not None
            process.stdin.write(f"{normalized_code}\n")
            process.stdin.flush()
        except (AssertionError, OSError) as exc:
            raise ValueError("无法提交短信验证码。") from exc
        session.state = "logging_in"
        session.message = "正在完成本机登录并拉取存档…"
        session.updated_at = time.monotonic()

    def status(self, session: LoginSession) -> dict[str, str | None]:
        return {
            "state": session.state,
            "message": session.message,
            "binding_code": session.binding_code if session.state == "complete" else None,
        }

    def stop(self) -> None:
        with self._lock:
            for session in self._sessions.values():
                process = session.process
                if process and process.poll() is None:
                    process.terminate()

    def _watch_process(self, session: LoginSession) -> None:
        process = session.process
        if not process or not process.stdout:
            return
        # The upstream script prints headers, token information and the full
        # save.  Never keep that output in a buffer, log it, or return it to
        # the browser.  The prefix counter recognizes only its fixed SMS
        # prompt, character by character.
        sms_prompt_prefix = 0
        runner_failure_prefixes = {kind: 0 for kind in RUNNER_FAILURE_MARKERS}
        try:
            while True:
                character = process.stdout.read(1)
                if not character:
                    break
                if character == SMS_PROMPT[sms_prompt_prefix]:
                    sms_prompt_prefix += 1
                else:
                    sms_prompt_prefix = 1 if character == SMS_PROMPT[0] else 0
                if sms_prompt_prefix == len(SMS_PROMPT) and not session.sms_prompt_seen:
                    session.sms_prompt_seen = True
                    session.state = "waiting_for_sms"
                    session.message = "验证码已发送；请在此页输入短信验证码。"
                    session.updated_at = time.monotonic()
                    sms_prompt_prefix = 0
                for kind, marker in RUNNER_FAILURE_MARKERS.items():
                    position = runner_failure_prefixes[kind]
                    if character == marker[position]:
                        position += 1
                    else:
                        position = 1 if character == marker[0] else 0
                    if position == len(marker):
                        session.runner_failure = kind
                        position = 0
                    runner_failure_prefixes[kind] = position
        finally:
            return_code = process.wait()
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()
            self._complete_process(session, return_code)

    def _complete_process(self, session: LoginSession, return_code: int) -> None:
        source = self._tool_dir / "gameData.json"
        config = self._tool_dir / "config.json"
        try:
            # An old export must never be mistaken for the result of this
            # login attempt.  Conversely, the upstream script fetches an
            # optional shop payload *after* writing gameData.json; an error in
            # that optional step should not discard a fresh, valid save.
            if not source.is_file() or self._source_fingerprint(source) == session.source_before:
                if return_code == 0:
                    raise ValueError("no_export")
                if session.runner_failure:
                    raise ValueError(f"runner_{session.runner_failure}")
                raise ValueError("tool_failed")
            for sensitive_path in (source, config):
                if sensitive_path.is_file():
                    os.chmod(sensitive_path, 0o600)
            import_save(source, self._save_dir, session.alias, replace=session.replace)
            session.binding_code = self._bindings.issue_code(session.alias)
            session.state = "complete"
            session.message = "存档已导入。请在 QQ C2C 私聊中使用下方一次性绑定码。"
        except ValueError as exc:
            session.state = "failed"
            if str(exc) == "no_export":
                session.message = (
                    "拉档工具已结束，但没有生成新存档。可能是登录或验证码未完成，"
                    "或者游戏接口/解密规则已更新；请重新登录一次。"
                )
            elif str(exc) == "runner_TLS":
                session.message = "无法与游戏服务器建立证书验证通过的 HTTPS 连接；请检查本机网络和系统证书。"
            elif str(exc) == "runner_NETWORK":
                session.message = "无法连接游戏服务器；请检查本机网络后重试。"
            elif str(exc) in {
                "runner_DEPENDENCY_REQUESTS",
                "runner_DEPENDENCY_URLLIB3",
                "runner_DEPENDENCY_CRYPTO",
            }:
                session.message = "本机拉档工具的 Python 依赖不完整；请重新安装其依赖后重试。"
            elif str(exc) == "runner_DEPENDENCY_DECRYPTOR":
                session.message = "本机拉档工具找不到本地存档解密模块；请检查上游工具文件是否完整。"
            elif str(exc) == "runner_DEPENDENCY_OTHER":
                session.message = "本机拉档工具缺少运行模块；其现有依赖自检正常，可能是上游工具运行环境不兼容。"
            elif str(exc) == "runner_INPUT":
                session.message = "本机拉档工具未完成登录输入流程；请刷新页面并重新提交。"
            elif str(exc) in {"runner_SCRIPT", "runner_UPSTREAM"}:
                session.message = "本机拉档工具与当前游戏接口或存档格式不兼容；需要更新其上游实现。"
            elif str(exc) == "tool_failed":
                session.message = "本机拉档工具异常结束，且没有生成新存档。请重新登录一次。"
            else:
                session.message = (
                    "已拉取到文件，但它不是可导入的 RizLine 成绩存档；"
                    "可能是拉档工具与当前游戏数据格式不匹配。"
                )
        except (OSError, RuntimeError):
            session.state = "failed"
            session.message = "存档已拉取，但无法写入机器人绑定数据。请检查本机数据目录权限后重试。"
        finally:
            session.updated_at = time.monotonic()

    @staticmethod
    def _source_fingerprint(path: Path) -> tuple[int, int, int, int] | None:
        """Return non-sensitive metadata used only to reject stale exports."""
        try:
            metadata = path.stat()
        except FileNotFoundError:
            return None
        except OSError:
            return None
        return (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)

    def _prune_sessions(self) -> None:
        now = time.monotonic()
        stale = [
            session_id
            for session_id, session in self._sessions.items()
            if len(self._sessions) >= MAX_SESSIONS
            and session.state not in {"logging_in", "waiting_for_sms"}
            and now - session.updated_at > 3600
        ]
        for session_id in stale:
            del self._sessions[session_id]


class LocalLoginHandler(BaseHTTPRequestHandler):
    manager: LocalLoginManager

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/":
            session_id, session, fresh = self._session()
            self._send_html(_page_html(session.csrf), cookie=session_id if fresh else None)
            return
        if parsed.path == "/api/status":
            session_id, session, fresh = self._session()
            self._send_json(self.manager.status(session), cookie=session_id if fresh else None)
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        _session_id, session, fresh = self._session()
        form = self._form_data()
        if form is None:
            return
        csrf = form.get("csrf", "")
        if not secrets.compare_digest(csrf, session.csrf):
            self._send_json({"error": "页面会话已失效，请刷新后重试。"}, status=HTTPStatus.FORBIDDEN)
            return

        try:
            if parsed.path == "/api/start":
                self.manager.start(
                    session,
                    phone=form.get("phone", ""),
                    password=form.get("password", ""),
                    alias=form.get("alias", ""),
                    replace=form.get("replace") == "on",
                )
                payload: dict[str, str | None] = self.manager.status(session)
            elif parsed.path == "/api/sms":
                self.manager.submit_sms_code(session, form.get("sms_code", ""))
                payload = self.manager.status(session)
            else:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
        except ValueError as exc:
            self._send_json({"error": str(exc)}, status=HTTPStatus.BAD_REQUEST)
            return

        self._send_json(payload, cookie=_session_id if fresh else None)

    def _session(self) -> tuple[str, LoginSession, bool]:
        cookie_header = self.headers.get("Cookie", "")
        cookie = SimpleCookie()
        cookie.load(cookie_header)
        existing = cookie.get(SESSION_COOKIE)
        session_id = existing.value if existing else None
        session = self.manager.session_for(session_id)
        if session:
            assert session_id is not None
            return session_id, session, False
        session_id, session = self.manager.create_session()
        return session_id, session, True

    def _form_data(self) -> dict[str, str] | None:
        try:
            content_length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            content_length = 0
        if not 0 < content_length <= 8192:
            self._send_json({"error": "请求内容无效。"}, status=HTTPStatus.BAD_REQUEST)
            return None
        body = self.rfile.read(content_length).decode("utf-8", "replace")
        parsed = parse_qs(body, keep_blank_values=True)
        return {key: values[-1] for key, values in parsed.items() if values}

    def _send_html(self, content: str, *, cookie: str | None = None) -> None:
        data = content.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", self._cookie_header(cookie))
        self.end_headers()
        self.wfile.write(data)

    def _send_json(
        self,
        payload: dict[str, str | None],
        *,
        status: HTTPStatus = HTTPStatus.OK,
        cookie: str | None = None,
    ) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        if cookie:
            self.send_header("Set-Cookie", self._cookie_header(cookie))
        self.end_headers()
        self.wfile.write(data)

    @staticmethod
    def _cookie_header(session_id: str) -> str:
        return f"{SESSION_COOKIE}={session_id}; HttpOnly; SameSite=Strict; Path=/"

    def log_message(self, format: str, *args: Any) -> None:
        # Default http.server logging can expose request paths in process logs.
        return


def _page_html(csrf: str) -> str:
    escaped_csrf = html.escape(csrf, quote=True)
    return f"""<!doctype html>
<html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>本机 RizLine 存档导入</title>
<style>
body{{max-width:620px;margin:32px auto;padding:0 18px;background:#181c25;color:#eef3f8;font:16px system-ui,sans-serif}}
section{{background:#252b38;border-radius:14px;padding:22px;margin:16px 0}} label{{display:block;margin:12px 0 6px}}
input{{box-sizing:border-box;width:100%;padding:10px;border-radius:8px;border:1px solid #667287;background:#151924;color:white}}
button{{margin-top:18px;padding:10px 16px;border:0;border-radius:8px;background:#4bd4e2;color:#102127;font-weight:700;cursor:pointer}}
small{{color:#afbac9}} #status{{white-space:pre-wrap;line-height:1.6}} .danger{{color:#ffc979}} .code{{font:600 19px monospace;color:#5ce1ed;word-break:break-all}}
</style>
<h1>RizLine 本机登录与 QQ 绑定</h1>
<p class="danger">仅供此机器的管理员使用。页面只监听 127.0.0.1，不得映射到公网或反向代理。</p>
<section><form id="login"><input type="hidden" name="csrf" value="{escaped_csrf}">
<label>本地玩家别名</label><input name="alias" required maxlength="40" placeholder="例如 alice">
<label>手机号</label><input name="phone" inputmode="numeric" autocomplete="username" required>
<label>密码</label><input name="password" type="password" autocomplete="current-password"><small>留空时会发送短信验证码。</small>
<label><input name="replace" type="checkbox" style="width:auto"> 替换同别名的已有本地存档</label>
<button>本机登录并拉取存档</button></form></section>
<section id="sms" hidden><form id="smsForm"><input type="hidden" name="csrf" value="{escaped_csrf}"><label>短信验证码</label><input name="sms_code" inputmode="numeric" autocomplete="one-time-code" required><button>提交验证码</button></form></section>
<section><strong>状态</strong><div id="status">等待登录。</div><div id="code" class="code"></div></section>
<section><small>登录信息只传给本机拉档工具，不写入本页日志。拉取完成后只导入 <code>gameData.json</code>；此页会显示一次性 QQ 绑定码，请私下发送给对应用户，并让其 C2C 私聊机器人执行 <code>/riz bind &lt;码&gt;</code>。</small></section>
<script>
const statusEl=document.querySelector('#status'),sms=document.querySelector('#sms'),code=document.querySelector('#code');
async function post(path,form){{const r=await fetch(path,{{method:'POST',body:new URLSearchParams(new FormData(form))}});const d=await r.json();if(d.error)throw Error(d.error);show(d)}}
function show(d){{statusEl.textContent=d.message||'';sms.hidden=d.state!=='waiting_for_sms';code.textContent=d.binding_code||'';}}
document.querySelector('#login').onsubmit=async e=>{{e.preventDefault();try{{await post('/api/start',e.target)}}catch(x){{statusEl.textContent=x.message}}}};
document.querySelector('#smsForm').onsubmit=async e=>{{e.preventDefault();try{{await post('/api/sms',e.target)}}catch(x){{statusEl.textContent=x.message}}}};
setInterval(async()=>{{try{{const r=await fetch('/api/status',{{cache:'no-store'}});show(await r.json())}}catch(x){{}}}},1200);
</script></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a loopback-only RizLine local login page")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--tool-dir", type=Path, default=Path("vendor/RizlineGameSaveData"))
    parser.add_argument("--tool-python", type=Path)
    parser.add_argument("--save-dir", type=Path, default=Path("data/rizline_saves"))
    parser.add_argument("--binding-db", type=Path, default=Path("data/rizline_bindings.json"))
    arguments = parser.parse_args(argv)

    if arguments.host != "127.0.0.1":
        parser.error("为避免暴露登录页面，--host 只能是 127.0.0.1")
    if not 1 <= arguments.port <= 65535:
        parser.error("端口必须在 1 到 65535 之间")
    tool_python = arguments.tool_python or arguments.tool_dir / ".venv" / "bin" / "python"
    if not tool_python.is_file():
        tool_python = Path(sys.executable)

    LocalLoginHandler.manager = LocalLoginManager(
        tool_dir=arguments.tool_dir,
        tool_python=tool_python,
        save_dir=arguments.save_dir,
        binding_db=arguments.binding_db,
    )
    server = ThreadingHTTPServer((arguments.host, arguments.port), LocalLoginHandler)
    print(f"本机页面已启动：http://{arguments.host}:{arguments.port}/")
    print("仅本机或 SSH 隧道访问；按 Ctrl+C 停止。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        LocalLoginHandler.manager.stop()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())
