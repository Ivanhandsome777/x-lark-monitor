import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

import x_lark_bot
from x_lark_bot import Config, State, XClient, deliver_pending, lark_signature, normalize_many
from web_app import basic_auth_valid, dashboard_data, default_config, load_config, public_config, validate_complete, validate_update


class MonitorTests(unittest.TestCase):
    def test_query(self):
        config = Config("token", ("openai", "x"), "hook", "", "stream", 60, False, False, False, "x.db")
        self.assertEqual(XClient.query_for(config), "(from:openai OR from:x) -is:reply -is:retweet")

    def test_normalize(self):
        payload = {
            "data": [{"id": "123", "text": "hello", "author_id": "1"}],
            "includes": {"users": [{"id": "1", "name": "OpenAI", "username": "OpenAI"}]},
        }
        post = normalize_many(payload)[0]
        self.assertEqual(post["url"], "https://x.com/OpenAI/status/123")
        self.assertEqual(post["text"], "hello")

    def test_normalize_stream_single_post_object(self):
        payload = {
            "data": {"id": "456", "text": "streamed", "author_id": "2"},
            "includes": {"users": [{"id": "2", "name": "Binance", "username": "binance"}]},
            "matching_rules": [{"id": "1", "tag": "monitor"}],
        }
        posts = normalize_many(payload)
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["id"], "456")
        self.assertEqual(posts[0]["username"], "binance")

    def test_state_deduplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            state = State(os.path.join(directory, "state.db"))
            post = {"id": "1", "text": "x"}
            self.assertTrue(state.save_post(post))
            self.assertFalse(state.save_post(post))

    def test_delivery_retry_skips_webhook_that_already_succeeded(self):
        class RecordingLark:
            def __init__(self, should_fail=False):
                self.should_fail = should_fail
                self.calls = []

            def send(self, post):
                self.calls.append(post["id"])
                if self.should_fail:
                    raise RuntimeError("temporary Lark failure")

        with tempfile.TemporaryDirectory() as directory:
            state = State(os.path.join(directory, "state.db"))
            state.save_post({"id": "101", "text": "hello", "username": "openai"})
            first = RecordingLark()
            second = RecordingLark(should_fail=True)
            with self.assertRaises(RuntimeError):
                deliver_pending(state, {"one": first, "two": second})
            self.assertEqual(first.calls, ["101"])
            self.assertEqual(second.calls, ["101"])

            second.should_fail = False
            deliver_pending(state, {"one": first, "two": second})
            self.assertEqual(first.calls, ["101"])
            self.assertEqual(second.calls, ["101", "101"])
            self.assertEqual(list(state.pending()), [])

    def test_new_webhook_does_not_receive_already_delivered_history(self):
        class RecordingLark:
            def __init__(self):
                self.calls = []

            def send(self, post):
                self.calls.append(post["id"])

        with tempfile.TemporaryDirectory() as directory:
            state = State(os.path.join(directory, "state.db"))
            state.save_post({"id": "100", "text": "old", "username": "openai"})
            state.mark_delivered("100")
            new_target = RecordingLark()
            deliver_pending(state, {"new-group": new_target})
            self.assertEqual(new_target.calls, [])

    def test_config_normalizes_usernames(self):
        env = {
            "X_BEARER_TOKEN": " token ",
            "X_USERNAMES": "@OpenAI, openai, X",
            "LARK_WEBHOOK_URL": "hook",
            "X_PROXY_URL": "http://127.0.0.1:7890",
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual(config.usernames, ("openai", "x"))
        self.assertEqual(config.proxy_url, "http://127.0.0.1:7890")

    def test_config_parses_multiple_enabled_webhooks(self):
        env = {
            "X_BEARER_TOKEN": "token",
            "X_USERNAMES": "openai",
            "LARK_WEBHOOKS_JSON": json.dumps(
                [
                    {"id": "one", "name": "群一", "url": "https://example.com/one", "enabled": True},
                    {"id": "two", "name": "群二", "url": "https://example.com/two", "enabled": False},
                    {"id": "three", "name": "群三", "url": "https://example.com/three", "enabled": True},
                ]
            ),
        }
        with patch.dict(os.environ, env, clear=True):
            config = Config.from_env()
        self.assertEqual([target.id for target in config.lark_webhooks], ["one", "three"])

    def test_signature_is_stable(self):
        self.assertEqual(lark_signature("secret", "123"), lark_signature("secret", "123"))

    def test_web_config_masks_secrets(self):
        config = default_config()
        config.update({"bearer_token": "abcdef", "lark_webhook_url": "https://hook/123456"})
        visible = public_config(config)
        self.assertEqual(visible["bearer_token_hint"], "••••cdef")
        self.assertNotIn("bearer_token", visible)
        self.assertNotIn("lark_webhook_url", visible)

    def test_web_config_masks_every_webhook(self):
        config = default_config()
        config["webhooks"] = [
            {
                "id": "group-one",
                "name": "行情群",
                "url": "https://open.larksuite.com/open-apis/bot/v2/hook/super-secret-token",
                "signing_secret": "signing-secret-value",
                "enabled": True,
            }
        ]
        encoded = json.dumps(public_config(config), ensure_ascii=False)
        self.assertNotIn("super-secret-token", encoded)
        self.assertNotIn("signing-secret-value", encoded)
        self.assertIn("••••oken", encoded)

    def test_web_update_manages_multiple_webhooks_and_keeps_masked_values(self):
        previous = default_config()
        previous["webhooks"] = [
            {"id": "one", "name": "旧名称", "url": "https://example.com/one", "signing_secret": "secret", "enabled": True}
        ]
        updated = validate_update(
            {
                "webhooks": [
                    {"id": "one", "name": "新名称", "url": "", "signing_secret": "", "enabled": False},
                    {"id": "two", "name": "第二群", "url": "https://example.com/two", "signing_secret": "new-secret", "enabled": True},
                ]
            },
            previous,
        )
        self.assertEqual(updated["webhooks"][0]["url"], "https://example.com/one")
        self.assertEqual(updated["webhooks"][0]["signing_secret"], "secret")
        self.assertFalse(updated["webhooks"][0]["enabled"])
        self.assertEqual(updated["lark_webhook_url"], "https://example.com/two")

    def test_load_config_migrates_legacy_webhook(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(
                json.dumps(
                    {
                        "lark_webhook_url": "https://example.com/legacy",
                        "lark_signing_secret": "legacy-secret",
                    }
                ),
                encoding="utf-8",
            )
            with patch("web_app.CONFIG_PATH", path):
                migrated = load_config()
        self.assertEqual(len(migrated["webhooks"]), 1)
        self.assertEqual(migrated["webhooks"][0]["name"], "默认 Lark 群")
        self.assertEqual(migrated["webhooks"][0]["url"], "https://example.com/legacy")

    def test_web_update_normalizes_accounts(self):
        config = validate_update({"usernames": ["@OpenAI", "openai", "X"]}, default_config())
        self.assertEqual(config["usernames"], ["openai", "x"])

    def test_web_complete_validation(self):
        with self.assertRaises(ValueError):
            validate_complete(default_config())

    def test_dashboard_metrics_and_feed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "state.db")
            state = State(path)
            state.save_post({"id": "1", "text": "hello", "username": "openai", "name": "OpenAI", "url": "https://x.com/OpenAI/status/1"})
            state.mark_delivered("1")
            with patch("web_app.STATE_DB", path):
                dashboard = dashboard_data()
            self.assertEqual(dashboard["metrics"]["total_posts"], 1)
            self.assertEqual(dashboard["metrics"]["estimated_total_usd"], 0.005)
            self.assertEqual(dashboard["posts"][0]["username"], "openai")

    def test_stream_rule_sync_network_error_is_retried(self):
        class BrokenX:
            attempts = 0
            def sync_rules(self, _config):
                self.attempts += 1
                raise urllib.error.URLError("temporary TLS failure")

        client = BrokenX()
        config = Config("token", ("openai",), "hook", "", "stream", 60, False, False, False, "x.db")
        with patch.object(x_lark_bot, "STOP", False), patch.object(
            x_lark_bot.time, "sleep", side_effect=lambda _delay: setattr(x_lark_bot, "STOP", True)
        ):
            x_lark_bot.run_stream(config, object(), client, object())
        self.assertEqual(client.attempts, 1)

    def test_basic_auth(self):
        import base64

        header = "Basic " + base64.b64encode(b"admin:correct horse").decode()
        self.assertTrue(basic_auth_valid(header, "admin", "correct horse"))
        self.assertFalse(basic_auth_valid(header, "admin", "wrong"))
        self.assertFalse(basic_auth_valid("", "admin", "correct horse"))


if __name__ == "__main__":
    unittest.main()
