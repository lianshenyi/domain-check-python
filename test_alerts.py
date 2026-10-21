import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import domain_check as dc


class AlertTests(unittest.TestCase):
    def test_aggregate_and_report_counts(self):
        state = {}
        send = MagicMock(return_value=True)
        results = {"bad-a": (False, "DNS"), "bad-b": (False, "TLS"),
                   "good": (True, "HTTP 200")}
        dc.notify_round(results, state, send, now=10000, cooldown=10800)
        send.assert_called_once()
        message = send.call_args.args[0]
        for text in ("总计 3", "可用 1", "异常 2", "bad-a", "bad-b"):
            self.assertIn(text, message)
        self.assertEqual(set(state), {"bad-a", "bad-b"})

    def test_cooldown_boundary_and_independent_targets(self):
        state = {}
        send = MagicMock(return_value=True)
        dc.notify_round({"a": (False, "DNS")}, state, send, now=10000, cooldown=10800)
        dc.notify_round({"a": (False, "DNS")}, state, send, now=20799, cooldown=10800)
        self.assertEqual(send.call_count, 1)
        dc.notify_round({"a": (False, "DNS"), "b": (False, "TLS")},
                        state, send, now=20799, cooldown=10800)
        self.assertEqual(send.call_count, 2)
        self.assertNotIn("\na\n", send.call_args.args[0])
        dc.notify_round({"a": (False, "DNS")}, state, send, now=20800, cooldown=10800)
        self.assertEqual(send.call_count, 3)

    def test_recovery_bypasses_cooldown_and_preserves_last_alert(self):
        state = {}
        send = MagicMock(return_value=True)
        dc.notify_round({"a": (False, "DNS")}, state, send, now=10000, cooldown=10800)
        dc.notify_round({"a": (True, "HTTP 200")}, state, send, now=10001, cooldown=10800)
        self.assertEqual(send.call_count, 2)
        self.assertIn("恢复", send.call_args.args[0])
        self.assertFalse(state["a"].unrecovered)
        self.assertEqual(state["a"].last_alert, 10000)
        dc.notify_round({"a": (True, "HTTP 200")}, state, send, now=10002, cooldown=10800)
        dc.notify_round({"a": (False, "TLS")}, state, send, now=10003, cooldown=10800)
        self.assertEqual(send.call_count, 2)

    def test_mixed_recovery_and_outage_in_one_message(self):
        state = {"a": dc.AlertState(10000, True)}
        send = MagicMock(return_value=True)
        dc.notify_round({"a": (True, "HTTP 200"), "b": (False, "TLS")},
                        state, send, now=10001, cooldown=10800)
        send.assert_called_once()
        self.assertIn("恢复", send.call_args.args[0])
        self.assertIn("异常", send.call_args.args[0])
        self.assertFalse(state["a"].unrecovered)
        self.assertTrue(state["b"].unrecovered)

    def test_failed_send_does_not_commit_and_retries(self):
        state = {}
        send = MagicMock(side_effect=[False, True, False, True])
        dc.notify_round({"a": (False, "DNS")}, state, send, now=10000, cooldown=10800)
        self.assertEqual(state, {})
        dc.notify_round({"a": (False, "DNS")}, state, send, now=10001, cooldown=10800)
        dc.notify_round({"a": (True, "HTTP 200")}, state, send, now=10002, cooldown=10800)
        self.assertTrue(state["a"].unrecovered)
        dc.notify_round({"a": (True, "HTTP 200")}, state, send, now=10003, cooldown=10800)
        self.assertFalse(state["a"].unrecovered)
        self.assertEqual(send.call_count, 4)

    def test_persist_restart_keeps_cooldown_and_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "alerts.json"
            dc.save_alert_state(path, {"a": dc.AlertState(10000, True)})
            state = dc.load_alert_state(path)
            send = MagicMock(return_value=True)
            dc.notify_round({"a": (False, "DNS")}, state, send, now=10001, cooldown=10800)
            send.assert_not_called()
            dc.notify_round({"a": (True, "HTTP 200")}, state, send, now=10002, cooldown=10800)
            send.assert_called_once()
            dc.save_alert_state(path, state)
            self.assertFalse(dc.load_alert_state(path)["a"].unrecovered)

    def test_invalid_state_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            self.assertEqual(dc.load_alert_state(path), {})
            for payload in ('oops', '[]', '{"a":{"last_alert":true,"unrecovered":true}}',
                            '{"a":{"last_alert":-1,"unrecovered":true}}'):
                path.write_text(payload)
                with self.subTest(payload=payload), self.assertRaises(ValueError):
                    dc.load_alert_state(path)

    def test_cooldown_configuration(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(dc.cooldown_seconds(), 10800)
        with patch.dict(os.environ, {"ALERT_COOLDOWN_HOURS": "0.5"}):
            self.assertEqual(dc.cooldown_seconds(), 1800)
        for value in ("0", "-1", "nan", "inf", "bad", "1e308"):
            with patch.dict(os.environ, {"ALERT_COOLDOWN_HOURS": value}):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    dc.cooldown_seconds()

    def test_large_details_preserve_all_24_urls(self):
        results = {f"https://example{i}.com/check": (False, "error" * 1000)
                   for i in range(24)}
        send = MagicMock(return_value=True)
        dc.notify_round(results, {}, send, now=10000, cooldown=10800)
        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertLessEqual(len(message), 4000)
        for url in results:
            self.assertIn(url, message)

    def test_dry_run_does_not_write_state_or_send(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"STATE_DIRECTORY": directory}):
                with patch.object(dc, "load_dotenv"):
                    with patch.object(dc, "load_targets", return_value=["a", "b"]):
                        with patch.object(dc, "check_url", return_value=(False, "DNS")):
                            with patch.object(dc, "send_telegram") as send:
                                self.assertEqual(dc.main(["--once", "--dry-run"]), 1)
            send.assert_not_called()
            self.assertFalse((Path(directory) / "alerts.json").exists())


class RecoveryStreakTests(unittest.TestCase):
    def test_recovery_requires_consecutive_successes(self):
        state = {"a": dc.AlertState(10000, True)}
        send = MagicMock(return_value=True)
        for now in (10001, 10002):
            changed = dc.notify_round({"a": (True, "HTTP 200")}, state, send,
                                      now=now, cooldown=10800, recovery_rounds=3)
            self.assertTrue(changed)
            send.assert_not_called()
            self.assertTrue(state["a"].unrecovered)
        self.assertEqual(state["a"].streak, 2)

        dc.notify_round({"a": (True, "HTTP 200")}, state, send,
                        now=10003, cooldown=10800, recovery_rounds=3)
        send.assert_called_once()
        self.assertIn("恢复", send.call_args.args[0])
        self.assertFalse(state["a"].unrecovered)
        self.assertEqual(state["a"].streak, 0)

        # 达标并发过一次恢复通知后，后续成功不再重复发送。
        dc.notify_round({"a": (True, "HTTP 200")}, state, send,
                        now=10004, cooldown=10800, recovery_rounds=3)
        self.assertEqual(send.call_count, 1)

    def test_failure_resets_streak(self):
        state = {"a": dc.AlertState(10000, True)}
        send = MagicMock(return_value=True)
        for now in (10001, 10002):
            dc.notify_round({"a": (True, "HTTP 200")}, state, send,
                            now=now, cooldown=10800, recovery_rounds=3)
        self.assertEqual(state["a"].streak, 2)

        # 冷却期内失败不重复发异常，但必须清零 streak。
        dc.notify_round({"a": (False, "TLS error")}, state, send,
                        now=10003, cooldown=10800, recovery_rounds=3)
        send.assert_not_called()
        self.assertTrue(state["a"].unrecovered)
        self.assertEqual(state["a"].streak, 0)

        for now in (10004, 10005):
            dc.notify_round({"a": (True, "HTTP 200")}, state, send,
                            now=now, cooldown=10800, recovery_rounds=3)
        send.assert_not_called()
        self.assertEqual(state["a"].streak, 2)
        dc.notify_round({"a": (True, "HTTP 200")}, state, send,
                        now=10006, cooldown=10800, recovery_rounds=3)
        send.assert_called_once()
        self.assertFalse(state["a"].unrecovered)

    def test_streak_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            dc.save_alert_state(path, {"a": dc.AlertState(10000, True, 2)})
            state = dc.load_alert_state(path)
            self.assertEqual(state["a"].streak, 2)
            send = MagicMock(return_value=True)
            dc.notify_round({"a": (True, "HTTP 200")}, state, send,
                            now=10001, cooldown=10800, recovery_rounds=3)
            send.assert_called_once()
            self.assertFalse(state["a"].unrecovered)

    def test_old_state_without_streak_is_compatible(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            path.write_text('{"a":{"last_alert":10000,"unrecovered":true}}',
                            encoding="utf-8")
            state = dc.load_alert_state(path)
            self.assertEqual(state["a"], dc.AlertState(10000, True, 0))
            send = MagicMock(return_value=True)
            dc.notify_round({"a": (True, "HTTP 200")}, state, send,
                            now=10001, cooldown=10800, recovery_rounds=3)
            send.assert_not_called()
            self.assertTrue(state["a"].unrecovered)
            self.assertEqual(state["a"].streak, 1)

    def test_invalid_streak_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            for value in ("true", "-1", '"2"', "1.5", "null"):
                path.write_text(
                    '{"a":{"last_alert":10000,"unrecovered":true,"streak":%s}}' % value,
                    encoding="utf-8")
                with self.subTest(value=value), self.assertRaises(ValueError):
                    dc.load_alert_state(path)

    def test_main_persists_streak_across_restarts(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "alerts.json"
            dc.save_alert_state(path, {"https://a.example": dc.AlertState(10000, True, 0)})
            environment = {"STATE_DIRECTORY": directory, "TG_BOT_TOKEN": "token",
                           "TG_CHAT_ID": "1"}
            send = MagicMock(return_value=True)
            with patch.dict(os.environ, environment, clear=True):
                with patch.object(dc, "load_dotenv"):
                    with patch.object(dc, "load_targets",
                                      return_value=["https://a.example"]):
                        with patch.object(dc, "check_target",
                                          return_value=(True, "HTTP 200")):
                            with patch.object(dc, "send_telegram", send):
                                for expected in (1, 2):
                                    self.assertEqual(dc.main([
                                        "--once", "--recovery-confirm-rounds", "3"]), 0)
                                    send.assert_not_called()
                                    saved = dc.load_alert_state(path)["https://a.example"]
                                    self.assertEqual(saved.streak, expected)
                                    self.assertTrue(saved.unrecovered)
                                self.assertEqual(dc.main([
                                    "--once", "--recovery-confirm-rounds", "3"]), 0)
            send.assert_called_once()
            saved = dc.load_alert_state(path)["https://a.example"]
            self.assertFalse(saved.unrecovered)
            self.assertEqual(saved.streak, 0)

    def test_unconfirmed_recovery_still_counts_as_available(self):
        state = {"a": dc.AlertState(10000, True)}
        send = MagicMock(return_value=True)
        results = {"a": (True, "HTTP 200"), "b": (False, "TLS error")}
        dc.notify_round(results, state, send, now=10001, cooldown=10800,
                        recovery_rounds=3)
        send.assert_called_once()
        message = send.call_args.args[0]
        self.assertIn("总计 2 / 可用 1 / 异常 1", message)
        self.assertNotIn("恢复通知", message)
        self.assertIn("b", message)
        self.assertTrue(state["a"].unrecovered)
        self.assertEqual(state["a"].streak, 1)


if __name__ == "__main__":
    unittest.main()
