#!/usr/bin/env python3
"""Check URLs from a text file and notify Telegram on outage/recovery."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
import json
import logging
import math
import os
from pathlib import Path
import socket
import time
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
            time.sleep(retry_delay)
    return False, detail


def send_telegram(token, chat_id, message, timeout):
    request = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=urlencode({"chat_id": chat_id, "text": message[:4000]}).encode(),
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        return isinstance(payload, dict) and payload.get("ok") is True
    except (URLError, OSError, ValueError):
        # Do not log the exception: the request URL contains the bot token.
        return False


def notify_result(url, healthy, detail, alerted, notify):
    """Only commit notification state after successful delivery."""
    if healthy and url not in alerted:
        return
    if not healthy and url in alerted:
        return
    title = "✅ 域名恢复" if healthy else "🚨 域名异常"
    message = (
        f"{title}\n{url}\n{detail[:500]}\n"
        f"检测节点：{socket.gethostname()}\n"
        f"时间：{datetime.now().astimezone().isoformat(timespec='seconds')}"
    )
    if not notify(message):
        LOG.error("Telegram 通知失败，下轮重试：%s", url)
        return
    if healthy:
        alerted.remove(url)
    else:
        alerted.add(url)


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
    except (OSError, ValueError) as exc:
        LOG.error("读取配置失败：%s", exc)
        return 2
    LOG.info("加载 %d 个目标；HTTP 200–%d 算可达", len(targets), args.max_status)
    alerted = set()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        while True:
            results = list(pool.map(
                lambda url: check_url(url, args.timeout, args.attempts,
                                      args.retry_delay, args.max_status),
                targets,
            ))
            for url, (healthy, detail) in zip(targets, results):
                LOG.info("%s %s %s", "OK" if healthy else "FAIL", url, detail)
                notify_result(url, healthy, detail, alerted, notify)
            if args.once:
                return 1 if any(not healthy for healthy, _ in results) else 0
            time.sleep(args.interval)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        pass
