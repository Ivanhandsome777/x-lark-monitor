import os
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

import x_lark_bot
from x_lark_bot import Config, State, XClient, lark_signature, normalize_many
from web_app import basic_auth_valid, dashboard_data, default_config, public_config, validate_complete, validate_update


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

    def test_signature_is_stable(self):
        self.assertEqual(lark_signature("secret", "123"), lark_signature("secret", "123"))

    def test_web_config_masks_secrets(self):
        config = default_config()
        config.update({"bearer_token": "abcdef", "lark_webhook_url": "https://hook/123456"})
        visible = public_config(config)
        self.assertEqual(visible["bearer_token_hint"], "••••cdef")
        self.assertNotIn("bearer_token", visible)
        self.assertNotIn("lark_webhook_url", visible)

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
