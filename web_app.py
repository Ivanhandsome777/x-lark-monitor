#!/usr/bin/env python3
"""Local web control panel for the X -> Lark monitor."""

from __future__ import annotations

import json
import logging
import os
import secrets
import signal
import sqlite3
import subprocess
import sys
import threading
import time
import base64
import hashlib
from collections import deque
from datetime import date, timedelta
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from x_lark_bot import LarkClient


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
CONFIG_PATH = Path(os.getenv("CONFIG_PATH", str(ROOT / "data" / "config.json")))
STATE_DB = os.getenv("STATE_DB", str(ROOT / "data" / "monitor.db"))
IS_RENDER = os.getenv("RENDER", "").lower() in {"1", "true", "yes"} or bool(
    os.getenv("RENDER_SERVICE_ID")
)
HOST = os.getenv("WEB_HOST", "0.0.0.0" if IS_RENDER else "127.0.0.1")
PORT = int(os.getenv("PORT", os.getenv("WEB_PORT", "8787")))
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "")
REQUIRE_AUTH = os.getenv("REQUIRE_AUTH", "true" if IS_RENDER else "false").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CSRF_TOKEN = secrets.token_urlsafe(24)


def default_config() -> dict[str, Any]:
    return {
        "bearer_token": "",
        "usernames": [],
        "lark_webhook_url": "",
        "lark_signing_secret": "",
        "webhooks": [],
        "mode": "stream",
        "poll_interval": 60,
        "include_replies": False,
        "include_retweets": False,
        "push_existing": False,
        "proxy_url": "",
    }


def load_config() -> dict[str, Any]:
    config = default_config()
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            config.update({key: saved[key] for key in config if key in saved})
        except (OSError, ValueError, TypeError) as exc:
            logging.warning("Could not load config: %s", exc)
    raw_webhooks = config.get("webhooks")
    if not isinstance(raw_webhooks, list):
        logging.warning("Ignoring invalid saved Webhook list")
        raw_webhooks = []
    normalized_webhooks = []
    for index, item in enumerate(raw_webhooks):
        if not isinstance(item, dict):
            continue
        url = str(item.get("url", "")).strip()
        if not url:
            continue
        normalized_webhooks.append(
            {
                "id": str(item.get("id", "")).strip()
                or hashlib.sha256(url.encode()).hexdigest()[:12],
                "name": str(item.get("name", "")).strip() or f"Lark 群 {index + 1}",
                "url": url,
                "signing_secret": str(item.get("signing_secret", "")).strip(),
                "enabled": bool(item.get("enabled", True)),
            }
        )
    config["webhooks"] = normalized_webhooks
    if not config["webhooks"] and config["lark_webhook_url"]:
        config["webhooks"] = [
            {
                "id": "legacy-" + hashlib.sha256(config["lark_webhook_url"].encode()).hexdigest()[:10],
                "name": "默认 Lark 群",
                "url": config["lark_webhook_url"],
                "signing_secret": config["lark_signing_secret"],
                "enabled": True,
            }
        ]
    return config


def public_config(config: dict[str, Any]) -> dict[str, Any]:
    return {
        "usernames": config["usernames"],
        "mode": config["mode"],
        "poll_interval": config["poll_interval"],
        "include_replies": config["include_replies"],
        "include_retweets": config["include_retweets"],
        "push_existing": config["push_existing"],
        "has_bearer_token": bool(config["bearer_token"]),
        "bearer_token_hint": secret_hint(config["bearer_token"]),
        "has_lark_webhook": bool(config["lark_webhook_url"]),
        "lark_webhook_hint": secret_hint(config["lark_webhook_url"]),
        "has_signing_secret": bool(config["lark_signing_secret"]),
        "signing_secret_hint": secret_hint(config["lark_signing_secret"]),
        "webhooks": [
            {
                "id": item.get("id", ""),
                "name": item.get("name", "Lark 群"),
                "enabled": item.get("enabled", True),
                "has_url": bool(item.get("url")),
                "url_hint": secret_hint(item.get("url", "")),
                "has_signing_secret": bool(item.get("signing_secret")),
                "signing_secret_hint": secret_hint(item.get("signing_secret", "")),
            }
            for item in config.get("webhooks", [])
            if isinstance(item, dict)
        ],
        "has_proxy": bool(config["proxy_url"]),
        "proxy_hint": secret_hint(config["proxy_url"]),
    }


def secret_hint(value: str) -> str:
    return "••••" + value[-4:] if value else ""


def validate_update(payload: dict[str, Any], previous: dict[str, Any]) -> dict[str, Any]:
    config = dict(previous)
    for field in ("bearer_token", "lark_webhook_url", "lark_signing_secret", "proxy_url"):
        if field in payload and payload[field] is not None:
            value = str(payload[field]).strip()
            if value:
                config[field] = value
    if "webhooks" in payload:
        raw_webhooks = payload["webhooks"]
        if not isinstance(raw_webhooks, list):
            raise ValueError("Webhook 列表格式不正确")
        previous_by_id = {
            str(item.get("id")): item
            for item in previous.get("webhooks", [])
            if isinstance(item, dict) and item.get("id")
        }
        webhooks = []
        seen_ids = set()
        for index, raw in enumerate(raw_webhooks):
            if not isinstance(raw, dict):
                raise ValueError("Webhook 配置格式不正确")
            webhook_id = str(raw.get("id", "")).strip() or secrets.token_hex(8)
            if webhook_id in seen_ids:
                raise ValueError("Webhook ID 重复")
            seen_ids.add(webhook_id)
            old = previous_by_id.get(webhook_id, {})
            name = str(raw.get("name", "")).strip() or f"Lark 群 {index + 1}"
            url = str(raw.get("url", "")).strip() or str(old.get("url", "")).strip()
            signing_secret = str(raw.get("signing_secret", "")).strip() or str(
                old.get("signing_secret", "")
            ).strip()
            if not url:
                raise ValueError(f"请填写 Webhook“{name}”的地址")
            if not url.startswith("https://"):
                raise ValueError(f"Webhook“{name}”必须使用 https:// 地址")
            webhooks.append(
                {
                    "id": webhook_id,
                    "name": name[:80],
                    "url": url,
                    "signing_secret": signing_secret,
                    "enabled": bool(raw.get("enabled", True)),
                }
            )
        config["webhooks"] = webhooks
        if webhooks:
            first = next((item for item in webhooks if item["enabled"]), webhooks[0])
            config["lark_webhook_url"] = first["url"]
            config["lark_signing_secret"] = first["signing_secret"]
    raw_names = payload.get("usernames", config["usernames"])
    if not isinstance(raw_names, list):
        raise ValueError("监控账号格式不正确")
    names = []
    for item in raw_names:
        name = str(item).strip().lstrip("@").lower()
        if name and name not in names:
            if not all(char.isalnum() or char == "_" for char in name):
                raise ValueError(f"账号名 @{name} 包含无效字符")
            names.append(name)
    config["usernames"] = names
    mode = str(payload.get("mode", config["mode"])).lower()
    if mode not in {"stream", "poll"}:
        raise ValueError("运行模式必须是 stream 或 poll")
    config["mode"] = mode
    try:
        config["poll_interval"] = max(15, min(3600, int(payload.get("poll_interval", 60))))
    except (TypeError, ValueError):
        raise ValueError("轮询间隔必须是数字")
    for field in ("include_replies", "include_retweets", "push_existing"):
        config[field] = bool(payload.get(field, False))
    return config


def validate_complete(config: dict[str, Any]) -> None:
    missing = []
    if not config["bearer_token"]:
        missing.append("X API Bearer Token")
    if not any(
        isinstance(item, dict) and item.get("enabled") and item.get("url")
        for item in config.get("webhooks", [])
    ):
        missing.append("至少一个已启用的 Lark Webhook")
    if not config["usernames"]:
        missing.append("至少一个监控账号")
    if missing:
        raise ValueError("请先填写：" + "、".join(missing))


def save_config(config: dict[str, Any]) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = CONFIG_PATH.with_suffix(".tmp")
    temporary.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(temporary, 0o600)
    temporary.replace(CONFIG_PATH)


class MonitorManager:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.process: subprocess.Popen[str] | None = None
        self.logs: deque[dict[str, Any]] = deque(maxlen=300)
        self.started_at: float | None = None
        self.last_exit_code: int | None = None

    def add_log(self, message: str, level: str = "info") -> None:
        with self.lock:
            self.logs.append(
                {"time": time.strftime("%H:%M:%S"), "level": level, "message": message.rstrip()}
            )

    def is_running(self) -> bool:
        with self.lock:
            return self.process is not None and self.process.poll() is None

    def status(self) -> dict[str, Any]:
        with self.lock:
            running = self.process is not None and self.process.poll() is None
            if self.process is not None and not running:
                self.last_exit_code = self.process.poll()
            return {
                "running": running,
                "pid": self.process.pid if running and self.process else None,
                "started_at": self.started_at,
                "uptime_seconds": int(time.time() - self.started_at) if running and self.started_at else 0,
                "last_exit_code": self.last_exit_code,
                "logs": list(self.logs),
            }

    def start(self, config: dict[str, Any]) -> None:
        validate_complete(config)
        with self.lock:
            if self.process is not None and self.process.poll() is None:
                return
            env = os.environ.copy()
            env.update(
                {
                    "X_BEARER_TOKEN": config["bearer_token"],
                    "X_USERNAMES": ",".join(config["usernames"]),
                    "LARK_WEBHOOK_URL": config["lark_webhook_url"],
                    "LARK_SIGNING_SECRET": config["lark_signing_secret"],
                    "LARK_WEBHOOKS_JSON": json.dumps(config["webhooks"], ensure_ascii=False),
                    "X_MODE": config["mode"],
                    "POLL_INTERVAL_SECONDS": str(config["poll_interval"]),
                    "INCLUDE_REPLIES": str(config["include_replies"]).lower(),
                    "INCLUDE_RETWEETS": str(config["include_retweets"]).lower(),
                    "PUSH_EXISTING": str(config["push_existing"]).lower(),
                    "X_PROXY_URL": config["proxy_url"],
                    "STATE_DB": STATE_DB,
                    "PYTHONUNBUFFERED": "1",
                }
            )
            self.add_log("正在启动监控服务…")
            self.process = subprocess.Popen(
                [sys.executable, str(ROOT / "x_lark_bot.py")],
                cwd=str(ROOT),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
            )
            self.started_at = time.time()
            self.last_exit_code = None
            threading.Thread(target=self._read_logs, args=(self.process,), daemon=True).start()

    def _read_logs(self, process: subprocess.Popen[str]) -> None:
        if process.stdout:
            for line in process.stdout:
                lowered = line.lower()
                level = "error" if " error " in lowered else "warning" if " warning " in lowered else "info"
                self.add_log(line, level)
        code = process.wait()
        with self.lock:
            self.last_exit_code = code
        self.add_log(f"监控服务已停止（退出码 {code}）", "warning" if code else "info")

    def stop(self) -> None:
        with self.lock:
            process = self.process
        if process is None or process.poll() is not None:
            return
        self.add_log("正在停止监控服务…")
        process.terminate()
        try:
            process.wait(timeout=8)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)

    def restart(self, config: dict[str, Any]) -> None:
        self.stop()
        self.start(config)


MANAGER = MonitorManager()


def basic_auth_valid(header: str, username: str, password: str) -> bool:
    if not header.startswith("Basic ") or not password:
        return False
    try:
        decoded = base64.b64decode(header[6:], validate=True).decode("utf-8")
        supplied_user, supplied_password = decoded.split(":", 1)
    except (ValueError, UnicodeDecodeError):
        return False
    return secrets.compare_digest(supplied_user, username) and secrets.compare_digest(
        supplied_password, password
    )


def dashboard_data(limit: int = 100, username: str = "") -> dict[str, Any]:
    """Read the durable monitor database without creating it when it is absent."""
    empty_daily = [
        {"date": (date.today() - timedelta(days=offset)).isoformat(), "count": 0}
        for offset in range(13, -1, -1)
    ]
    empty = {
        "posts": [],
        "metrics": {
            "total_posts": 0,
            "today_posts": 0,
            "month_posts": 0,
            "delivered_posts": 0,
            "pending_posts": 0,
            "estimated_total_usd": 0.0,
            "estimated_month_usd": 0.0,
            "daily": empty_daily,
            "accounts": [],
        },
    }
    db_path = Path(STATE_DB)
    if not db_path.exists():
        return empty
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3)
        db.row_factory = sqlite3.Row
        tables = {
            row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "posts" not in tables:
            db.close()
            return empty
        params: list[Any] = []
        where = ""
        if username:
            where = "WHERE lower(json_extract(payload, '$.username')) = ?"
            params.append(username.lower())
        rows = db.execute(
            f"SELECT payload, delivered, created_at FROM posts {where} "
            "ORDER BY created_at DESC, id DESC LIMIT ?",
            (*params, max(1, min(limit, 250))),
        ).fetchall()
        posts = []
        for row in rows:
            try:
                post = json.loads(row["payload"])
            except (ValueError, TypeError):
                continue
            post["delivered"] = bool(row["delivered"])
            post["collected_at"] = row["created_at"]
            posts.append(post)

        summary = db.execute(
            """SELECT
                count(*) AS total,
                sum(CASE WHEN date(created_at) = date('now') THEN 1 ELSE 0 END) AS today,
                sum(CASE WHEN strftime('%Y-%m', created_at) = strftime('%Y-%m', 'now') THEN 1 ELSE 0 END) AS month,
                sum(CASE WHEN delivered = 1 THEN 1 ELSE 0 END) AS delivered
               FROM posts"""
        ).fetchone()
        daily_rows = db.execute(
            """WITH RECURSIVE days(day) AS (
                 SELECT date('now', '-13 days')
                 UNION ALL SELECT date(day, '+1 day') FROM days WHERE day < date('now')
               )
               SELECT days.day, count(posts.id) AS count
               FROM days LEFT JOIN posts ON date(posts.created_at) = days.day
               GROUP BY days.day ORDER BY days.day"""
        ).fetchall()
        account_rows = db.execute(
            """SELECT lower(json_extract(payload, '$.username')) AS username,
                      count(*) AS count, max(created_at) AS last_collected_at
               FROM posts GROUP BY username ORDER BY count DESC, username ASC"""
        ).fetchall()
        db.close()
        total = int(summary["total"] or 0)
        month = int(summary["month"] or 0)
        delivered = int(summary["delivered"] or 0)
        return {
            "posts": posts,
            "metrics": {
                "total_posts": total,
                "today_posts": int(summary["today"] or 0),
                "month_posts": month,
                "delivered_posts": delivered,
                "pending_posts": total - delivered,
                "estimated_total_usd": round(total * 0.005, 3),
                "estimated_month_usd": round(month * 0.005, 3),
                "daily": [{"date": row["day"], "count": row["count"]} for row in daily_rows],
                "accounts": [dict(row) for row in account_rows if row["username"]],
            },
        }
    except (sqlite3.Error, OSError, ValueError) as exc:
        logging.warning("Could not read dashboard data: %s", exc)
        return empty


class Handler(BaseHTTPRequestHandler):
    server_version = "XLarkPanel/1.0"

    def log_message(self, fmt: str, *args: Any) -> None:
        logging.debug(fmt, *args)

    def require_authentication(self) -> bool:
        if not REQUIRE_AUTH and not ADMIN_PASSWORD:
            return True
        if basic_auth_valid(self.headers.get("Authorization", ""), ADMIN_USERNAME, ADMIN_PASSWORD):
            return True
        self.send_response(HTTPStatus.UNAUTHORIZED)
        self.send_header("WWW-Authenticate", 'Basic realm="X Lark Monitor", charset="UTF-8"')
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return False

    def send_json(self, payload: dict[str, Any], status: int = 200) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(data)

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 64 * 1024:
            raise ValueError("请求内容过大")
        return json.loads(self.rfile.read(length) or b"{}")

    def check_csrf(self) -> bool:
        if secrets.compare_digest(self.headers.get("X-CSRF-Token", ""), CSRF_TOKEN):
            return True
        self.send_json({"ok": False, "error": "页面会话已失效，请刷新后重试"}, HTTPStatus.FORBIDDEN)
        return False

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/health":
            self.send_json(
                {
                    "ok": True,
                    "service": "x-lark-monitor",
                    "monitor_running": MANAGER.is_running(),
                }
            )
            return
        if not self.require_authentication():
            return
        if path == "/api/config":
            self.send_json({"ok": True, "config": public_config(load_config()), "csrf_token": CSRF_TOKEN})
            return
        if path == "/api/status":
            self.send_json({"ok": True, "status": MANAGER.status()})
            return
        if path == "/api/dashboard":
            query = parse_qs(urlparse(self.path).query)
            try:
                limit = int(query.get("limit", ["100"])[0])
            except ValueError:
                limit = 100
            username = query.get("username", [""])[0]
            self.send_json({"ok": True, **dashboard_data(limit, username)})
            return
        if path in {"/", "/index.html", "/settings"}:
            self.send_file(WEB_ROOT / "index.html", "text/html; charset=utf-8")
            return
        if path == "/styles.css":
            self.send_file(WEB_ROOT / "styles.css", "text/css; charset=utf-8")
            return
        if path == "/app.js":
            self.send_file(WEB_ROOT / "app.js", "text/javascript; charset=utf-8")
            return
        self.send_error(HTTPStatus.NOT_FOUND)

    def send_file(self, path: Path, content_type: str) -> None:
        try:
            data = path.read_bytes()
        except OSError:
            self.send_error(HTTPStatus.NOT_FOUND)
            return
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        if not self.require_authentication():
            return
        if not self.check_csrf():
            return
        path = urlparse(self.path).path
        try:
            if path == "/api/config":
                config = validate_update(self.read_json(), load_config())
                validate_complete(config)
                save_config(config)
                if MANAGER.is_running():
                    MANAGER.restart(config)
                self.send_json({"ok": True, "config": public_config(config)})
                return
            if path == "/api/start":
                config = load_config()
                MANAGER.start(config)
                self.send_json({"ok": True, "status": MANAGER.status()})
                return
            if path == "/api/stop":
                MANAGER.stop()
                self.send_json({"ok": True, "status": MANAGER.status()})
                return
            if path == "/api/test-lark":
                config = load_config()
                payload = self.read_json()
                webhook_id = str(payload.get("webhook_id", "")).strip()
                saved = next(
                    (item for item in config.get("webhooks", []) if item.get("id") == webhook_id),
                    {},
                )
                webhook = str(payload.get("url", "")).strip() or str(saved.get("url", "")).strip()
                signing_secret = str(payload.get("signing_secret", "")).strip() or str(
                    saved.get("signing_secret", "")
                ).strip()
                if not webhook:
                    raise ValueError("请先填写 Lark Webhook")
                LarkClient(webhook, signing_secret).send(
                    {
                        "id": "test",
                        "text": "连接成功，X → Lark 实时推送机器人已就绪。",
                        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
                        "username": "x_lark_bot",
                        "name": "推送测试",
                        "url": "https://www.larksuite.com/",
                        "images": [],
                    }
                )
                MANAGER.add_log("Lark 测试消息发送成功")
                self.send_json({"ok": True})
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except (ValueError, OSError, RuntimeError) as exc:
            self.send_json({"ok": False, "error": str(exc)}, HTTPStatus.BAD_REQUEST)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if REQUIRE_AUTH and not ADMIN_PASSWORD:
        logging.error("ADMIN_PASSWORD is required when REQUIRE_AUTH=true")
        return 2
    server = ThreadingHTTPServer((HOST, PORT), Handler)

    def shutdown(_signum: int, _frame: Any) -> None:
        MANAGER.stop()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    logging.info("Control panel: http://%s:%s", HOST, PORT)
    if os.getenv("AUTO_START", "true").lower() in {"1", "true", "yes"}:
        try:
            config = load_config()
            validate_complete(config)
            MANAGER.start(config)
        except ValueError:
            pass
    server.serve_forever()
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
