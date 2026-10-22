#!/usr/bin/env python3
"""Check URLs from a text file and notify Telegram on outage/recovery."""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime
import ipaddress
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

import dns.exception
import dns.rdatatype
import dns.resolver
from dotenv import load_dotenv

LOG = logging.getLogger("domain-check")

# 公共 DNS 判定结果：ok=有 A 记录；missing=权威无记录（否决）；unavailable=DNS 自身故障（降级）
DNS_OK = "ok"
DNS_MISSING = "missing"
DNS_UNAVAILABLE = "unavailable"
DEFAULT_PUBLIC_RESOLVERS = "8.8.8.8,1.1.1.1"


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


def _public_dns_query(hostname, resolver_address, timeout):
    """向单个公共递归 DNS 查询 A 记录。"""
    try:
        resolver = dns.resolver.Resolver(configure=False)
        resolver.nameservers = [resolver_address]
        resolver.timeout = timeout
        resolver.lifetime = timeout
        answer = resolver.resolve(hostname, "A", search=False)
    except dns.resolver.NXDOMAIN:
        return DNS_MISSING, "NXDOMAIN"
    except dns.resolver.NoAnswer:
        return DNS_MISSING, "NODATA"
    except dns.exception.Timeout:
        return DNS_UNAVAILABLE, "查询超时"
    except (dns.resolver.NoNameservers, dns.exception.DNSException,
            OSError, ValueError, TypeError, UnicodeError) as exc:
        return DNS_UNAVAILABLE, str(exc) or "查询失败"

    records = [item.address for item in answer if item.rdtype == dns.rdatatype.A]
    if not records:
        return DNS_MISSING, "NODATA"
    return DNS_OK, ",".join(records)


def public_dns_lookup(hostname, resolvers, timeout):
    """用公共递归 DNS 查询 A 记录，结果只用于否决，不参与实际连接。"""
    if not resolvers:
        return DNS_UNAVAILABLE, "未配置公共 DNS"
    # IP 字面量无需查询，直接视为可解析，避免把它当域名查成 NXDOMAIN。
    try:
        ipaddress.ip_address(hostname)
    except (ValueError, TypeError):
        pass
    else:
        return DNS_OK, hostname

    results = []
    # 多个公共解析器并行查询：任一返回 A 记录即可解析，避免单个 DNS
    # 黑洞或抖动拖慢判定；整体等待时间由各自的 timeout 封顶。
    with ThreadPoolExecutor(max_workers=len(resolvers)) as pool:
        futures = [pool.submit(_public_dns_query, hostname, resolver, timeout)
                   for resolver in resolvers]
        for future in as_completed(futures):
            try:
                results.append(future.result())
            except (dns.exception.DNSException, OSError, ValueError,
                    TypeError, UnicodeError, RuntimeError) as exc:
                results.append((DNS_UNAVAILABLE, str(exc) or "查询失败"))

    for status, detail in results:
        if status == DNS_OK:
            return status, detail
    for status, detail in results:
        if status == DNS_MISSING:
            return status, detail
    return DNS_UNAVAILABLE, "所有公共 DNS 均不可用"


def check_target(url, timeout, attempts, retry_delay, max_status,
                 public_resolvers, public_dns_timeout, dns_executor=None):
    """公共 DNS 与 HTTP 并发检测；公共 DNS 无 A 记录时否决 HTTP 结果。

    公共 DNS 查询放在独立线程池，避免与 HTTP 串行叠加；DNS 自身故障时
    只降级为 HTTP 判定，绝不让 DNS 抖动造成目标误报。
    """
    hostname = urlsplit(url).hostname
    dns_future = None
    dns_result = None
    if public_resolvers and hostname:
        if dns_executor is None:
            try:
                dns_result = public_dns_lookup(
                    hostname, public_resolvers, public_dns_timeout)
            except (dns.exception.DNSException, OSError, ValueError,
                    TypeError, UnicodeError, RuntimeError) as exc:
                dns_result = (DNS_UNAVAILABLE, str(exc) or "查询失败")
        else:
            try:
                dns_future = dns_executor.submit(
                    public_dns_lookup, hostname, public_resolvers, public_dns_timeout)
            except RuntimeError as exc:
                dns_result = (DNS_UNAVAILABLE, str(exc) or "DNS 线程池不可用")

    healthy, detail = check_url(url, timeout, attempts, retry_delay, max_status)

    if dns_future is not None:
        try:
            dns_result = dns_future.result()
        except (dns.exception.DNSException, OSError, ValueError,
                TypeError, UnicodeError, RuntimeError) as exc:
            dns_result = (DNS_UNAVAILABLE, str(exc) or "查询失败")

    if dns_result is not None:
        status, dns_detail = dns_result
        if status == DNS_MISSING:
            # 公网用户拿不到 A 记录，即使本机解析器残留缓存也不能算恢复。
            return False, f"公共 DNS 无 A 记录（{dns_detail}）"
        if status == DNS_UNAVAILABLE:
            detail = f"{detail}；公共 DNS 不可用，已降级"
    return healthy, detail


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
    # 连续成功轮数；只有连续达到阈值才发送“恢复通知”，跨进程持久化。
    streak: int = 0


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
        # 旧状态文件没有 streak 字段，按 0 兼容读取。
        streak = record.get("streak", 0)
        if type(streak) is not int or streak < 0:
            raise ValueError("连续成功次数格式错误")
        states[url] = AlertState(stamp, record["unrecovered"], streak)
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


def notify_round(results, states, notify, *, now, cooldown, recovery_rounds=1):
    # 默认 1 保持旧调用语义；主程序始终传入 --recovery-confirm-rounds（默认 3）。
    outages = []
    recoveries = []
    changed = False
    for url, (healthy, detail) in sorted(results.items()):
        prior = states.get(url)
        if healthy:
            if prior is not None and prior.unrecovered:
                streak = prior.streak + 1
                if streak >= recovery_rounds:
                    recoveries.append((url, detail))
                else:
                    # 恢复通知未达标，但该目标本轮仍按“可用”统计。
                    states[url] = AlertState(prior.last_alert, True, streak)
                    changed = True
            elif prior is not None and prior.streak:
                states[url] = AlertState(prior.last_alert, prior.unrecovered, 0)
                changed = True
        else:
            if prior is not None and prior.streak:
                # 中间任何一轮失败都清零，之后重新累计。
                states[url] = AlertState(prior.last_alert, prior.unrecovered, 0)
                changed = True
            if prior is None or now - prior.last_alert >= cooldown:
                outages.append((url, detail))
    if not outages and not recoveries:
        # streak 变化也需要落盘，否则重启后会丢失累计轮数。
        return changed

    available = sum(healthy for healthy, _ in results.values())
    header = (f"📊 域名检测汇总\n总计 {len(results)} / 可用 {available}"
              f" / 异常 {len(results) - available}")
    footer = (f"\n检测节点：{socket.gethostname()}\n"
              f"时间：{datetime.now().astimezone().isoformat(timespec='seconds')}")
    # 异常清单始终列出本轮全部失败目标：🆕 为本轮需要通知的（新告警或冷却
    # 期满），⏳ 为仍在冷却期的已知异常。恢复通知保持原有格式。
    outage_urls = {url for url, _ in outages}
    failures = [(url, detail) for url, (healthy, detail) in sorted(results.items())
                if not healthy]
    # Preserve all target URLs; shorten reasons before dropping notification.
    for limit in (160, 80, 0):
        sections = [header]
        if failures:
            lines = [f"🚨 异常清单（{len(failures)}）"]
            for url, detail in failures:
                marker = "🆕" if url in outage_urls else "⏳"
                if limit:
                    reason = detail.replace("\n", " ")[:limit]
                    lines.append(f"{marker} {url} — {reason}")
                else:
                    lines.append(f"{marker} {url}")
            if outages:
                lines.append(f"本轮新告警 {len(outages)} 个")
            sections.append("\n".join(lines))
        if recoveries:
            lines = [f"✅ 恢复通知（{len(recoveries)}）"]
            for url, detail in recoveries:
                lines.append(url)
                if limit:
                    lines.append(detail.replace("\n", " ")[:limit])
            sections.append("\n".join(lines))
        message = "\n\n".join(sections) + footer
        if len(message) <= 4000:
            break
    else:
        LOG.error("汇总消息超过 Telegram 长度限制，未发送；请减少单个实例的目标数")
        return changed

    if not notify(message):
        LOG.error("Telegram 汇总发送失败，下轮重试")
        return changed
    for url, _ in outages:
        states[url] = AlertState(now, True, 0)
    for url, _ in recoveries:
        states[url] = AlertState(states[url].last_alert, False, 0)
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


def parse_resolvers(value):
    text = value.strip()
    if not text:
        # 空字符串表示关闭公共 DNS 判定，退回纯 HTTP 行为。
        return []
    resolvers = [item.strip() for item in text.split(",") if item.strip()]
    for resolver in resolvers:
        try:
            ipaddress.ip_address(resolver)
        except ValueError as exc:
            raise argparse.ArgumentTypeError(
                f"不是有效的公共 DNS 地址：{resolver}") from exc
    return resolvers


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
    parser.add_argument(
        "--public-resolvers", type=parse_resolvers,
        default=os.environ.get("PUBLIC_RESOLVERS", DEFAULT_PUBLIC_RESOLVERS),
        help="逗号分隔的公共 DNS 列表；空字符串关闭公共 DNS 判定")
    parser.add_argument(
        "--recovery-confirm-rounds", type=positive_int,
        default=os.environ.get("RECOVERY_CONFIRM_ROUNDS", "3"),
        help="发送恢复通知所需的连续成功轮数，默认 3")
    parser.add_argument(
        "--public-dns-timeout", type=positive_float,
        default=os.environ.get("PUBLIC_DNS_TIMEOUT", "3.0"),
        help="单次公共 DNS 查询超时秒数，默认 3.0")
    parser.add_argument("--once", action="store_true", help="仅检测一轮")
    parser.add_argument("--dry-run", action="store_true", help="只打印，不发送 Telegram")
    parser.add_argument("--test-notification", action="store_true",
                        help="仅发送一条测试消息")
    return parser.parse_args(argv)


def main(argv=None):
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # 先加载 .env，让 PUBLIC_RESOLVERS 等配置成为 argparse 默认值；已有环境变量优先。
    load_dotenv(Path(__file__).resolve().with_name(".env"), override=False)
    args = parse_args(argv)
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
    LOG.info("加载 %d 个目标；HTTP 200–%d 算可达；恢复通知需连续 %d 轮成功",
             len(targets), args.max_status, args.recovery_confirm_rounds)
    if args.public_resolvers:
        LOG.info("公共 DNS 判定：%s（超时 %.1f 秒，仅用于否决）",
                 ",".join(args.public_resolvers), args.public_dns_timeout)
    else:
        LOG.info("公共 DNS 判定已关闭，仅按 HTTP 结果判定")
    # DNS 使用独立线程池，避免目标线程在同一个池里等待子任务造成死锁，
    # 同时保证 DNS 与 HTTP 真正并发，不串行累加超时。
    with ThreadPoolExecutor(max_workers=args.workers) as pool, \
            ThreadPoolExecutor(max_workers=args.workers) as dns_pool:
        while True:
            started = time.monotonic()
            futures = {
                pool.submit(check_target, url, args.timeout, args.attempts,
                            args.retry_delay, args.max_status, args.public_resolvers,
                            args.public_dns_timeout, dns_pool): url
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
            changed = notify_round(results, states, notify, now=time.time(),
                                   cooldown=cooldown,
                                   recovery_rounds=args.recovery_confirm_rounds)
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
