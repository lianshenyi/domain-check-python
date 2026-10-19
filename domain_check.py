#!/usr/bin/env python3
"""Check URLs from a text file and notify Telegram on outage/recovery."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import socket
import time
import tempfile
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from dotenv import load_dotenv

LOG = logging.getLogger("domain-check")


def load_targets(path):
    targets = []
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        url = raw.strip()
        if not url or url.startswith("#"):
            continue
        if "://" not in url:
            url = "https://" + url
        try:
            parts = urlsplit(url)
            valid = (parts.scheme in ("http", "https") and parts.hostname
                     and not parts.username and not parts.password
                     and not any(c.isspace() for c in url))
            _ = parts.port
            url.encode("ascii")
        except (ValueError, UnicodeError):
            valid = False
        if not valid:
            raise ValueError(f"config 第 {number} 行不是有效的 HTTP(S) URL")
        if url not in targets:
            targets.append(url)
    if not targets:
        raise ValueError("config 中没有检测目标")
    return targets


def check_url(url, timeout, attempts, retry_delay, max_status):
    detail = ""
    for attempt in range(attempts):
        try:
            request = Request(url, headers={"User-Agent": "domain-check/1.0"})
            with urlopen(request, timeout=timeout) as response:
                code = response.status
            detail = f"HTTP {code}"
            if 200 <= code <= max_status:
                return True, detail
        except HTTPError as exc:
            code = exc.code
            exc.close()
            detail = f"HTTP {code}"
            if 200 <= code <= max_status:
                return True, detail
        except (URLError, OSError, ValueError) as exc:
            detail = str(exc)
        if attempt + 1 < attempts:
            LOG.warning("%s 第 %d/%d 次失败：%s；%.1f 秒后重试",
                        url, attempt + 1, attempts, detail, retry_delay)
            time.sleep(retry_delay)
    return False, detail


def send_telegram(token, chat_id, message, timeout):
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urlencode({"chat_id": chat_id, "text": message}).encode(),
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        return isinstance(payload, dict) and payload.get("ok") is True
    except (URLError, OSError, ValueError):
        # Do not log the exception: the request URL contains the bot token.
        return False


@dataclass(frozen=True)
class AlertState:
    last_alert: float
    unrecovered: bool


def cooldown_seconds():
    try:
        seconds = float(os.environ.get("ALERT_COOLDOWN_HOURS", "3")) * 3600
    except ValueError as exc:
        raise ValueError("ALERT_COOLDOWN_HOURS 必须是大于 0 的有限小时数") from exc
    if not math.isfinite(seconds) or seconds <= 0:
        raise ValueError("ALERT_COOLDOWN_HOURS 必须是大于 0 的有限小时数")
    return seconds


def load_alert_state(path):
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    if not isinstance(payload, dict):
        raise ValueError("通知记录格式错误")
    states = {}
    for url, record in payload.items():
        if not isinstance(record, dict):
            raise ValueError("通知记录格式错误")
        stamp = record.get("last_alert")
        if type(stamp) not in (int, float):
            raise ValueError("通知时间格式错误")
        try:
            stamp = float(stamp)
        except OverflowError as exc:
            raise ValueError("通知时间超出范围") from exc
        if not math.isfinite(stamp) or stamp < 0 or type(record.get("unrecovered")) is not bool:
            raise ValueError("通知记录格式错误")
        states[url] = AlertState(stamp, record["unrecovered"])
    return states


def save_alert_state(path, states):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
                mode="w", encoding="utf-8", dir=path.parent,
                prefix=path.name + ".", delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({url: asdict(state) for url, state in states.items()}, handle)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def notify_round(results, states, notify, *, now, cooldown):
    outages = []
    recoveries = []
    for url, (healthy, detail) in sorted(results.items()):
        prior = states.get(url)
        if healthy:
            if prior is not None and prior.unrecovered:
                recoveries.append((url, detail))
        elif prior is None or now - prior.last_alert >= cooldown:
            outages.append((url, detail))
    if not outages and not recoveries:
        return False

    available = sum(healthy for healthy, _ in results.values())
    header = (f"📊 域名检测汇总\n总计 {len(results)} / 可用 {available}"
              f" / 异常 {len(results) - available}")
    footer = (f"\n检测节点：{socket.gethostname()}\n"
              f"时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    # Preserve all target URLs; shorten reasons before dropping notification.
    for limit in (160, 80, 0):
        sections = [header]
        for title, entries in (("🚨 异常提醒", outages), ("✅ 恢复通知", recoveries)):
            if entries:
                lines = [f"{title}（{len(entries)}）"]
                for url, detail in entries:
                    lines.append(url)
                    if limit:
                        lines.append(detail.replace("\n", " ")[:limit])
                sections.append("\n".join(lines))
        message = "\n\n".join(sections) + footer
        if len(message) <= 4000:
            break
    else:
        LOG.error("汇总消息超过 Telegram 长度限制，未发送；请减少单个实例的目标数")
        return False

    if not notify(message):
        LOG.error("Telegram 汇总发送失败，下轮重试")
        return False
    for url, _ in outages:
        states[url] = AlertState(now, True)
    for url, _ in recoveries:
        states[url] = AlertState(states[url].last_alert, False)
    return True


def positive_float(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("必须是大于 0 的有限数值")
    return number


def positive_int(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return number


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path(__file__).with_name("config"))
    parser.add_argument("--interval", type=positive_float, default=60,
                        help="每轮结束后等待秒数，默认 60")
    parser.add_argument("--timeout", type=positive_float, default=10)
    parser.add_argument("--attempts", type=positive_int, default=3)
    parser.add_argument("--retry-delay", type=positive_float, default=1)
    parser.add_argument("--workers", type=positive_int, default=8)
    parser.add_argument("--max-status", type=int, choices=range(200, 500),
                        metavar="200..499", default=499)
    parser.add_argument("--once", action="store_true", help="仅检测一轮")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发送 Telegram")
    parser.add_argument("--test-notification", action="store_true",
                        help="仅发送一条测试消息")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)
    token = os.environ.get("TG_BOT_TOKEN", "").strip()
    chat_id = os.environ.get("TG_CHAT_ID", "").strip()
    if not args.dry_run and (not token or not chat_id):
        LOG.error("请在 .env 或环境变量中设置 TG_BOT_TOKEN 和 TG_CHAT_ID")
        return 2

    def notify(message):
        if args.dry_run:
            LOG.info("[dry-run] %s", message)
            return True
        return send_telegram(token, chat_id, message, args.timeout)

    if args.test_notification:
        return 0 if notify("✅ domain-check 测试通知") else 2
    try:
        targets = load_targets(args.config)
        cooldown = cooldown_seconds()
        state_path = Path(os.environ.get(
            "STATE_DIRECTORY", str(Path(__file__).resolve().parent / ".state"))) / "alerts.json"
        states = load_alert_state(state_path)
        if not args.dry_run:
            save_alert_state(state_path, states)
    except (OSError, ValueError) as exc:
        LOG.error("读取配置失败：%s", exc)
        return 2
    LOG.info("加载 %d 个目标；HTTP 200–%d 算可达", len(targets), args.max_status)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            started = time.monotonic()
            futures = {
                pool.submit(check_url, url, args.timeout, args.attempts,
                            args.retry_delay, args.max_status): url
                for url in targets
            }
            failed = 0
            results = {}
            for completed, future in enumerate(as_completed(futures), 1):
                url = futures[future]
                healthy, detail = future.result()
                failed += not healthy
                LOG.info("[%d/%d] %s %s %s（本轮已用 %.1f 秒）",
                         completed, len(targets), "OK" if healthy else "FAIL",
                         url, detail, time.monotonic() - started)
                results[url] = (healthy, detail)
            LOG.info("本轮完成：%d 个目标，%d 个失败，耗时 %.1f 秒",
                     len(targets), failed, time.monotonic() - started)
            changed = notify_round(results, states, notify, now=time.time(), cooldown=cooldown)
            if changed and not args.dry_run:
                try:
                    save_alert_state(state_path, states)
                except OSError as exc:
                    LOG.error("保存通知记录失败：%s", exc)
                    return 2
            if args.once:
                return 1 if failed else 0
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
