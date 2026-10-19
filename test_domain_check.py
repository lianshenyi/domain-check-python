import io
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch
from urllib.error import HTTPError, URLError

import domain_check as dc



class DotenvTests(unittest.TestCase):
    def run_with_env(self, content, environment):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "domain_check.py"
            if content is not None:
                script.with_name(".env").write_text(content, encoding="utf-8")
            with patch.dict(os.environ, environment, clear=True):
                with patch.object(dc, "__file__", str(script)):
                    with patch.object(dc, "send_telegram", return_value=True) as send:
                        result = dc.main(["--test-notification"])
                        return result, send.call_args

    def test_load_script_directory_dotenv(self):
        result, call = self.run_with_env(
            "TG_BOT_TOKEN='file-token'\nTG_CHAT_ID=-123 # group\n", {})
        self.assertEqual(result, 0)
        self.assertEqual(call.args[:2], ("file-token", "-123"))

    def test_environment_takes_priority(self):
        result, call = self.run_with_env(
            "TG_BOT_TOKEN=file-token\nTG_CHAT_ID=file-chat\n",
            {"TG_BOT_TOKEN": "export-token", "TG_CHAT_ID": "export-chat"})
        self.assertEqual(result, 0)
        self.assertEqual(call.args[:2], ("export-token", "export-chat"))

    def test_missing_dotenv_uses_environment(self):
        result, call = self.run_with_env(
            None, {"TG_BOT_TOKEN": "export-token", "TG_CHAT_ID": "export-chat"})
        self.assertEqual(result, 0)
        self.assertEqual(call.args[:2], ("export-token", "export-chat"))


class ConfigTests(unittest.TestCase):
    def load(self, text):
        with patch.object(Path, "read_text", return_value=text):
            return dc.load_targets(Path("config"))

    def test_dedup_comments_and_bare_domain(self):
        self.assertEqual(self.load("# hello\nexample.com\nhttps://example.com\n\n"),
                         ["https://example.com"])

    def test_invalid_inputs(self):
        for value in ("", "ftp://example.com", "https://", "https://a:bad",
                      "https://user:pass@example.com", "https://a b"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.load(value)

    def test_invalid_numeric_args(self):
        for option, value in (("--timeout", "0"), ("--workers", "-1"),
                              ("--interval", "nan"), ("--attempts", "0"),
                              ("--timeout", "inf")):
            with self.subTest(option=option), patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    dc.parse_args([option, value])


class ProbeTests(unittest.TestCase):
    @patch("domain_check.time.sleep")
    @patch("domain_check.urlopen")
    def test_retry_and_recover(self, open_url, sleep):
        response = MagicMock()
        response.__enter__.return_value.status = 200
        open_url.side_effect = [URLError("DNS failed"), response]
        self.assertEqual(dc.check_url("https://example.com", 1, 3, 1, 499),
                         (True, "HTTP 200"))
        self.assertEqual(open_url.call_count, 2)
        sleep.assert_called_once_with(1)

    def test_http_status_policy(self):
        for status, maximum, expected in ((403, 499, True), (404, 499, True),
                                          (403, 399, False), (503, 499, False)):
            error = HTTPError("https://example.com", status, "error", {}, None)
            with self.subTest(status=status, maximum=maximum):
                with patch("domain_check.urlopen", side_effect=error):
                    healthy, detail = dc.check_url("https://example.com", 1, 1, 1, maximum)
                self.assertEqual(healthy, expected)
                self.assertEqual(detail, f"HTTP {status}")

    @patch("domain_check.time.sleep")
    @patch("domain_check.urlopen", side_effect=URLError("unreachable"))
    def test_exhaust_retries(self, open_url, sleep):
        self.assertFalse(dc.check_url("https://example.com", 1, 3, 1, 499)[0])
        self.assertEqual(open_url.call_count, 3)


class NotificationTests(unittest.TestCase):
    def test_outage_dedup_recovery_and_next_outage(self):
        alerted = set()
        notify = MagicMock(return_value=True)
        for healthy in (True, False, False, True, True, False):
            dc.notify_result("https://example.com", healthy, "detail", alerted, notify)
        self.assertEqual(notify.call_count, 3)
        self.assertIn("异常", notify.call_args_list[0].args[0])
        self.assertIn("恢复", notify.call_args_list[1].args[0])

    def test_failed_delivery_retried_for_outage_and_recovery(self):
        alerted = set()
        notify = MagicMock(side_effect=[False, True, False, True])
        for healthy, expected in ((False, set()), (False, {"url"}),
                                  (True, {"url"}), (True, set())):
            dc.notify_result("url", healthy, "detail", alerted, notify)
            self.assertEqual(alerted, expected)
        self.assertEqual(notify.call_count, 4)

    @patch("domain_check.urlopen")
    def test_telegram_post(self, open_url):
        response = MagicMock()
        response.__enter__.return_value = io.StringIO('{"ok": true}')
        open_url.return_value = response
        self.assertTrue(dc.send_telegram("secret", "123", "hello", 1))
        request = open_url.call_args.args[0]
        self.assertEqual(request.get_method(), "POST")
        self.assertIn(b"chat_id=123", request.data)

    def test_telegram_failures(self):
        for payload in ('{"ok": false}', "not json", "[]"):
            response = MagicMock()
            response.__enter__.return_value = io.StringIO(payload)
            with patch("domain_check.urlopen", return_value=response):
                self.assertFalse(dc.send_telegram("secret", "123", "hello", 1))
        with patch("domain_check.urlopen", side_effect=URLError("failed")):
            self.assertFalse(dc.send_telegram("secret", "123", "hello", 1))


if __name__ == "__main__":
    unittest.main()
