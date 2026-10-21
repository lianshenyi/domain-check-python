import io
import os
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
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



class ProgressTests(unittest.TestCase):
    def test_fast_result_is_reported_before_slow_target_finishes(self):
        from threading import Event
        reported = Event()

        def check(url, *args):
            if url == "slow" and not reported.wait(2):
                raise AssertionError("fast target was hidden behind slow target")
            return True, "HTTP 200"

        def log(message, *args):
            if "fast" in args:
                reported.set()

        with patch.object(dc, "load_dotenv"):
            with patch.object(dc, "load_targets", return_value=["slow", "fast"]):
                with patch.object(dc, "check_url", side_effect=check):
                    with patch.object(dc.LOG, "info", side_effect=log):
                        result = dc.main(["--once", "--dry-run", "--workers", "2"])
        self.assertEqual(result, 0)
        self.assertTrue(reported.is_set())


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


class PublicDnsTests(unittest.TestCase):
    def lookup(self, behavior):
        resolver = MagicMock()
        if isinstance(behavior, Exception):
            resolver.resolve.side_effect = behavior
        else:
            resolver.resolve.return_value = behavior
        with patch("domain_check.dns.resolver.Resolver", return_value=resolver):
            result = dc._public_dns_query("example.com", "8.8.8.8", 1.5)
        return result, resolver

    def test_a_record_is_ok(self):
        record = MagicMock()
        record.rdtype = dc.dns.rdatatype.A
        record.address = "93.184.216.34"
        (status, detail), resolver = self.lookup([record])
        self.assertEqual((status, detail), (dc.DNS_OK, "93.184.216.34"))
        self.assertEqual(resolver.nameservers, ["8.8.8.8"])
        self.assertEqual(resolver.timeout, 1.5)
        self.assertEqual(resolver.lifetime, 1.5)

    def test_nxdomain_and_nodata_are_missing(self):
        for error, reason in ((dc.dns.resolver.NXDOMAIN(), "NXDOMAIN"),
                              (dc.dns.resolver.NoAnswer(), "NODATA")):
            with self.subTest(reason=reason):
                (status, detail), _ = self.lookup(error)
                self.assertEqual((status, detail), (dc.DNS_MISSING, reason))

    def test_resolver_failures_are_unavailable(self):
        for error in (dc.dns.exception.Timeout(), dc.dns.resolver.NoNameservers(),
                      OSError("network down")):
            with self.subTest(error=type(error).__name__):
                (status, _), _ = self.lookup(error)
                self.assertEqual(status, dc.DNS_UNAVAILABLE)

    def test_any_resolver_with_a_record_wins(self):
        def query(hostname, resolver, timeout):
            if resolver == "192.0.2.1":
                return dc.DNS_UNAVAILABLE, "查询超时"
            return dc.DNS_OK, "93.184.216.34"

        with patch.object(dc, "_public_dns_query", side_effect=query):
            self.assertEqual(
                dc.public_dns_lookup("example.com", ["192.0.2.1", "8.8.8.8"], 1.5),
                (dc.DNS_OK, "93.184.216.34"))

    def test_missing_wins_when_no_resolver_has_a_record(self):
        def query(hostname, resolver, timeout):
            if resolver == "192.0.2.1":
                return dc.DNS_UNAVAILABLE, "查询超时"
            return dc.DNS_MISSING, "NXDOMAIN"

        with patch.object(dc, "_public_dns_query", side_effect=query):
            self.assertEqual(
                dc.public_dns_lookup("example.com", ["192.0.2.1", "8.8.8.8"], 1.5),
                (dc.DNS_MISSING, "NXDOMAIN"))

    def test_all_resolvers_unavailable(self):
        with patch.object(dc, "_public_dns_query",
                          return_value=(dc.DNS_UNAVAILABLE, "查询超时")):
            status, _ = dc.public_dns_lookup(
                "example.com", ["192.0.2.1", "192.0.2.2"], 1.5)
        self.assertEqual(status, dc.DNS_UNAVAILABLE)

    def test_ip_literal_is_ok_without_query(self):
        with patch.object(dc, "_public_dns_query") as query:
            self.assertEqual(dc.public_dns_lookup("192.0.2.1", ["8.8.8.8"], 1),
                             (dc.DNS_OK, "192.0.2.1"))
        query.assert_not_called()


class PublicDnsTargetTests(unittest.TestCase):
    def test_missing_a_record_vetoes_http_success(self):
        with patch.object(dc, "public_dns_lookup",
                          return_value=(dc.DNS_MISSING, "NXDOMAIN")):
            with patch.object(dc, "check_url", return_value=(True, "HTTP 200")):
                healthy, detail = dc.check_target(
                    "https://atmcsgo001.com", 1, 1, 1, 499, ["8.8.8.8"], 1.0)
        self.assertFalse(healthy)
        self.assertIn("公共 DNS 无 A 记录", detail)
        self.assertIn("NXDOMAIN", detail)

    def test_dns_unavailable_degrades_to_http(self):
        with patch.object(dc, "public_dns_lookup",
                          return_value=(dc.DNS_UNAVAILABLE, "查询超时")):
            with patch.object(dc, "check_url", return_value=(True, "HTTP 200")):
                healthy, detail = dc.check_target(
                    "https://example.com", 1, 1, 1, 499, ["8.8.8.8"], 1.0)
        self.assertTrue(healthy)
        self.assertEqual(detail, "HTTP 200；公共 DNS 不可用，已降级")

    def test_dns_unavailable_does_not_hide_http_failure(self):
        with patch.object(dc, "public_dns_lookup",
                          return_value=(dc.DNS_UNAVAILABLE, "查询超时")):
            with patch.object(dc, "check_url", return_value=(False, "TLS error")):
                healthy, detail = dc.check_target(
                    "https://example.com", 1, 1, 1, 499, ["8.8.8.8"], 1.0)
        self.assertFalse(healthy)
        self.assertIn("TLS error", detail)
        self.assertIn("已降级", detail)

    def test_disabled_public_dns_skips_lookup(self):
        with patch.object(dc, "public_dns_lookup") as lookup:
            with patch.object(dc, "check_url", return_value=(True, "HTTP 200")):
                healthy, detail = dc.check_target(
                    "https://example.com", 1, 1, 1, 499, [], 1.0)
        self.assertTrue(healthy)
        self.assertEqual(detail, "HTTP 200")
        lookup.assert_not_called()

    def test_dns_and_http_run_concurrently(self):
        dns_started = Event()
        release_dns = Event()
        http_started = Event()

        def fake_dns(*args):
            dns_started.set()
            if not release_dns.wait(2):
                raise AssertionError("HTTP did not start while DNS was running")
            return dc.DNS_UNAVAILABLE, "查询超时"

        def fake_http(*args):
            if not dns_started.wait(2):
                raise AssertionError("DNS did not start before HTTP")
            http_started.set()
            release_dns.set()
            return True, "HTTP 200"

        with ThreadPoolExecutor(max_workers=1) as dns_pool:
            with patch.object(dc, "public_dns_lookup", side_effect=fake_dns):
                with patch.object(dc, "check_url", side_effect=fake_http):
                    healthy, detail = dc.check_target(
                        "https://example.com", 1, 1, 1, 499, ["1.1.1.1"], 1.0,
                        dns_pool)
        self.assertTrue(dns_started.is_set())
        self.assertTrue(http_started.is_set())
        self.assertTrue(healthy)
        self.assertIn("已降级", detail)


class PublicConfigTests(unittest.TestCase):
    def test_resolver_arguments_and_disable_switch(self):
        args = dc.parse_args([
            "--public-resolvers", "8.8.8.8, 1.1.1.1",
            "--recovery-confirm-rounds", "5",
            "--public-dns-timeout", "1.5",
        ])
        self.assertEqual(args.public_resolvers, ["8.8.8.8", "1.1.1.1"])
        self.assertEqual(args.recovery_confirm_rounds, 5)
        self.assertEqual(args.public_dns_timeout, 1.5)
        self.assertEqual(dc.parse_args(["--public-resolvers", ""]).public_resolvers, [])

    def test_invalid_public_arguments(self):
        for option, value in (("--public-resolvers", "not-an-ip"),
                              ("--recovery-confirm-rounds", "0"),
                              ("--public-dns-timeout", "0"),
                              ("--public-dns-timeout", "nan")):
            with self.subTest(option=option, value=value), \
                    patch("sys.stderr", io.StringIO()):
                with self.assertRaises(SystemExit):
                    dc.parse_args([option, value])

    def test_environment_defaults(self):
        environment = {"PUBLIC_RESOLVERS": "", "RECOVERY_CONFIRM_ROUNDS": "5",
                       "PUBLIC_DNS_TIMEOUT": "1.5"}
        with patch.dict(os.environ, environment, clear=True):
            args = dc.parse_args([])
        self.assertEqual(args.public_resolvers, [])
        self.assertEqual(args.recovery_confirm_rounds, 5)
        self.assertEqual(args.public_dns_timeout, 1.5)

    def test_dotenv_settings_reach_the_parser(self):
        with tempfile.TemporaryDirectory() as directory:
            script = Path(directory) / "domain_check.py"
            script.with_name(".env").write_text(
                "PUBLIC_RESOLVERS=\nRECOVERY_CONFIRM_ROUNDS=5\nPUBLIC_DNS_TIMEOUT=1.5\n",
                encoding="utf-8")
            with patch.dict(os.environ, {}, clear=True):
                with patch.object(dc, "__file__", str(script)):
                    with patch.object(dc, "load_targets",
                                      return_value=["https://example.com"]):
                        with patch.object(dc, "check_target",
                                          return_value=(True, "HTTP 200")) as check:
                            with patch.object(dc, "notify_round",
                                              return_value=False) as notify:
                                result = dc.main(["--once", "--dry-run"])
        self.assertEqual(result, 0)
        selected = check.call_args.args
        self.assertEqual(selected[5], [])
        self.assertEqual(selected[6], 1.5)
        self.assertEqual(notify.call_args.kwargs["recovery_rounds"], 5)


class NotificationTests(unittest.TestCase):
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
