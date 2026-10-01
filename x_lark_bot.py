#!/usr/bin/env python3
"""Real-time X posts to a Lark custom-bot webhook, with durable deduplication."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import random
import signal
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable


API_BASE = "https://api.x.com/2"
RULE_TAG_PREFIX = "x-lark-monitor:v1:"
USER_AGENT = "x-lark-monitor/1.0"
STOP = False


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Config:
    bearer_token: str
    usernames: tuple[str, ...]
    lark_webhook_url: str
    lark_signing_secret: str
    mode: str
    poll_interval: int
    include_replies: bool
    include_retweets: bool
    push_existing: bool
    state_db: str
    proxy_url: str = ""

    @classmethod
    def from_env(cls) -> "Config":
        usernames = tuple(
            dict.fromkeys(
                item.strip().lstrip("@").lower()
                for item in os.getenv("X_USERNAMES", "").split(",")
                if item.strip()
            )
        )
        mode = os.getenv("X_MODE", "stream").strip().lower()
        cfg = cls(
            bearer_token=os.getenv("X_BEARER_TOKEN", "").strip(),
            usernames=usernames,
            lark_webhook_url=os.getenv("LARK_WEBHOOK_URL", "").strip(),
            lark_signing_secret=os.getenv("LARK_SIGNING_SECRET", "").strip(),
            mode=mode,
            poll_interval=max(15, int(os.getenv("POLL_INTERVAL_SECONDS", "60"))),
            include_replies=env_bool("INCLUDE_REPLIES"),
            include_retweets=env_bool("INCLUDE_RETWEETS"),
            push_existing=env_bool("PUSH_EXISTING"),
            state_db=os.getenv("STATE_DB", "data/monitor.db").strip(),
            proxy_url=os.getenv("X_PROXY_URL", "").strip(),
        )
        errors = []
        if not cfg.bearer_token:
            errors.append("X_BEARER_TOKEN")
        if not cfg.usernames:
            errors.append("X_USERNAMES")
        if not cfg.lark_webhook_url:
            errors.append("LARK_WEBHOOK_URL")
        if cfg.mode not in {"stream", "poll"}:
            raise ValueError("X_MODE must be stream or poll")
        if errors:
            raise ValueError("Missing required settings: " + ", ".join(errors))
        return cfg


class State:
    def __init__(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path))
        os.makedirs(directory, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS posts (
                id TEXT PRIMARY KEY,
                payload TEXT NOT NULL,
                delivered INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )"""
        )
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self.db.commit()

    def save_post(self, post: dict[str, Any]) -> bool:
        cursor = self.db.execute(
            "INSERT OR IGNORE INTO posts(id, payload) VALUES (?, ?)",
            (post["id"], json.dumps(post, ensure_ascii=False)),
        )
        self.db.commit()
        return cursor.rowcount == 1

    def pending(self) -> Iterable[dict[str, Any]]:
        rows = self.db.execute(
            "SELECT payload FROM posts WHERE delivered = 0 ORDER BY id ASC"
        ).fetchall()
        return (json.loads(row[0]) for row in rows)

    def mark_delivered(self, post_id: str) -> None:
        self.db.execute("UPDATE posts SET delivered = 1 WHERE id = ?", (post_id,))
        self.db.commit()

    def get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM metadata WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
        self.db.commit()


class XClient:
    def __init__(self, token: str, proxy_url: str = "") -> None:
        self.token = token
        handlers = []
        if proxy_url:
            handlers.append(urllib.request.ProxyHandler({"http": proxy_url, "https": proxy_url}))
        self.opener = urllib.request.build_opener(*handlers)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
        timeout: int = 30,
    ) -> urllib.response.addinfourl:
        url = API_BASE + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "User-Agent": USER_AGENT,
            },
        )
        return self.opener.open(request, timeout=timeout)

    def json_request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        with self._request(method, path, **kwargs) as response:
            return json.loads(response.read())

    @staticmethod
    def query_for(config: Config) -> str:
        authors = " OR ".join(f"from:{name}" for name in config.usernames)
        query = f"({authors})"
        if not config.include_replies:
            query += " -is:reply"
        if not config.include_retweets:
            query += " -is:retweet"
        return query

    def sync_rules(self, config: Config) -> None:
        query = self.query_for(config)
        fingerprint = hashlib.sha256(query.encode()).hexdigest()[:12]
        desired_tag = RULE_TAG_PREFIX + fingerprint
        current = self.json_request("GET", "/tweets/search/stream/rules").get("data", [])
        managed = [rule for rule in current if rule.get("tag", "").startswith(RULE_TAG_PREFIX)]
        desired_exists = any(
            rule.get("tag") == desired_tag and rule.get("value") == query for rule in managed
        )
        stale = [
            rule["id"]
            for rule in managed
            if rule.get("tag") != desired_tag or rule.get("value") != query
        ]
        if stale:
            self.json_request(
                "POST", "/tweets/search/stream/rules", body={"delete": {"ids": stale}}
            )
            logging.info("Removed %d stale managed stream rule(s)", len(stale))
        if not desired_exists:
            self.json_request(
                "POST",
                "/tweets/search/stream/rules",
                body={"add": [{"value": query, "tag": desired_tag}]},
            )
            logging.info("Added X stream rule: %s", query)

    def stream(self) -> Iterable[dict[str, Any]]:
        query = {
            "tweet.fields": "id,text,author_id,created_at,lang,attachments",
            "expansions": "author_id,attachments.media_keys",
            "user.fields": "id,name,username,profile_image_url",
            "media.fields": "media_key,type,url,preview_image_url",
        }
        response = self._request("GET", "/tweets/search/stream", query=query, timeout=90)
        try:
            for raw_line in response:
                if STOP:
                    return
                line = raw_line.strip()
                if line:
                    yield json.loads(line)
        finally:
            response.close()

    def recent(self, config: Config, since_id: str | None) -> list[dict[str, Any]]:
        query = {
            "query": self.query_for(config),
            "max_results": "10",
            "tweet.fields": "id,text,author_id,created_at,lang,attachments",
            "expansions": "author_id,attachments.media_keys",
            "user.fields": "id,name,username,profile_image_url",
            "media.fields": "media_key,type,url,preview_image_url",
        }
        if since_id:
            query["since_id"] = since_id
        payload = self.json_request("GET", "/tweets/search/recent", query=query)
        return normalize_many(payload)


def normalize_many(payload: dict[str, Any]) -> list[dict[str, Any]]:
    users = {user["id"]: user for user in payload.get("includes", {}).get("users", [])}
    media = {
        item["media_key"]: item for item in payload.get("includes", {}).get("media", [])
    }
    posts = []
    for tweet in payload.get("data", []):
        author = users.get(tweet.get("author_id"), {})
        keys = tweet.get("attachments", {}).get("media_keys", [])
        images = [
            media[key].get("url") or media[key].get("preview_image_url")
            for key in keys
            if key in media
        ]
        username = author.get("username", "unknown")
        posts.append(
            {
                "id": tweet["id"],
                "text": tweet.get("text", ""),
                "created_at": tweet.get("created_at"),
                "username": username,
                "name": author.get("name", username),
                "url": f"https://x.com/{username}/status/{tweet['id']}",
                "images": [url for url in images if url],
            }
        )
    return posts


def lark_signature(secret: str, timestamp: str) -> str:
    string_to_sign = f"{timestamp}\n{secret}".encode()
    digest = hmac.new(string_to_sign, digestmod=hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


class LarkClient:
    def __init__(self, webhook_url: str, signing_secret: str = "") -> None:
        self.webhook_url = webhook_url
        self.signing_secret = signing_secret

    def send(self, post: dict[str, Any]) -> None:
        timestamp = str(int(time.time()))
        text = post["text"].strip() or "（无文字内容）"
        if len(text) > 2800:
            text = text[:2797] + "..."
        created = post.get("created_at") or datetime.now(timezone.utc).isoformat()
        content = f"**@{post['username']}** · {created}\n\n{text}"
        if post.get("images"):
            content += "\n\n" + "\n".join(f"[查看媒体]({url})" for url in post["images"])
        payload: dict[str, Any] = {
            "msg_type": "interactive",
            "card": {
                "header": {
                    "template": "blue",
                    "title": {"tag": "plain_text", "content": f"{post['name']} 发布了新帖"},
                },
                "elements": [
                    {"tag": "markdown", "content": content},
                    {
                        "tag": "action",
                        "actions": [
                            {
                                "tag": "button",
                                "text": {"tag": "plain_text", "content": "在 X 中查看"},
                                "type": "primary",
                                "url": post["url"],
                            }
                        ],
                    },
                ],
            },
        }
        if self.signing_secret:
            payload.update(
                {"timestamp": timestamp, "sign": lark_signature(self.signing_secret, timestamp)}
            )
        request = urllib.request.Request(
            self.webhook_url,
            data=json.dumps(payload, ensure_ascii=False).encode(),
            method="POST",
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        )
        with urllib.request.urlopen(request, timeout=15) as response:
            result = json.loads(response.read())
        code = result.get("code", result.get("StatusCode", 0))
        if code != 0:
            raise RuntimeError(f"Lark rejected message: {result}")


def deliver_pending(state: State, lark: LarkClient) -> None:
    for post in state.pending():
        lark.send(post)
        state.mark_delivered(post["id"])
        logging.info("Delivered @%s post %s", post["username"], post["id"])


def handle_payload(payload: dict[str, Any], state: State, lark: LarkClient) -> None:
    for post in normalize_many(payload):
        if state.save_post(post):
            logging.info("Found new @%s post %s", post["username"], post["id"])
    deliver_pending(state, lark)


def run_stream(config: Config, state: State, x: XClient, lark: LarkClient) -> None:
    delay = 1.0
    rules_synced = False
    while not STOP:
        try:
            if not rules_synced:
                logging.info("Syncing X filtered-stream rules")
                x.sync_rules(config)
                rules_synced = True
            deliver_pending(state, lark)
            logging.info("Connected to X filtered stream for: %s", ", ".join(config.usernames))
            for payload in x.stream():
                handle_payload(payload, state, lark)
                delay = 1.0
        except (OSError, ValueError, RuntimeError, urllib.error.HTTPError) as exc:
            if STOP:
                break
            logging.warning("Stream interrupted: %s; reconnecting in %.1fs", exc, delay)
            time.sleep(delay + random.random())
            delay = min(delay * 2, 120)


def run_poll(config: Config, state: State, x: XClient, lark: LarkClient) -> None:
    metadata_key = "poll_since_id"
    while not STOP:
        try:
            since_id = state.get(metadata_key)
            posts = x.recent(config, since_id)
            if posts:
                newest_id = max((post["id"] for post in posts), key=int)
                initial_silent = since_id is None and not config.push_existing
                if initial_silent:
                    for post in posts:
                        if state.save_post(post):
                            state.mark_delivered(post["id"])
                    logging.info("Initialized cursor at %s without sending history", newest_id)
                else:
                    for post in sorted(posts, key=lambda item: int(item["id"])):
                        if state.save_post(post):
                            logging.info("Found new @%s post %s", post["username"], post["id"])
                    deliver_pending(state, lark)
                state.set(metadata_key, newest_id)
            elif since_id is None:
                logging.info("No matching posts found during initial poll")
        except (OSError, ValueError, RuntimeError, urllib.error.HTTPError) as exc:
            logging.warning("Poll failed: %s", exc)
        for _ in range(config.poll_interval):
            if STOP:
                return
            time.sleep(1)


def stop_handler(_signum: int, _frame: Any) -> None:
    global STOP
    STOP = True


def main() -> int:
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(message)s",
    )
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    if "--test-lark" in sys.argv:
        webhook = os.getenv("LARK_WEBHOOK_URL", "").strip()
        if not webhook:
            logging.error("Configuration error: missing LARK_WEBHOOK_URL")
            return 2
        lark = LarkClient(webhook, os.getenv("LARK_SIGNING_SECRET", "").strip())
        lark.send(
            {
                "id": "test",
                "text": "连接成功，X → Lark 实时推送机器人已就绪。",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "username": "x_lark_bot",
                "name": "推送测试",
                "url": "https://www.larksuite.com/",
                "images": [],
            }
        )
        logging.info("Lark test message delivered successfully")
        return 0
    try:
        config = Config.from_env()
    except (ValueError, TypeError) as exc:
        logging.error("Configuration error: %s", exc)
        return 2
    state = State(config.state_db)
    x = XClient(config.bearer_token, config.proxy_url)
    lark = LarkClient(config.lark_webhook_url, config.lark_signing_secret)
    logging.info("Starting in %s mode", config.mode)
    if config.mode == "stream":
        run_stream(config, state, x, lark)
    else:
        run_poll(config, state, x, lark)
    logging.info("Stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
