# 域名连通性检测

Python 3.11+，使用 python-dotenv 自动加载脚本同目录的 .env，已有环境变量优先。读取同目录现有 `config`，每行一个 HTTP(S) URL 或域名，支持空行、整行 # 注释，自动去重。修改配置后重启生效。

检测先通过公共递归 DNS 确认域名在公网有 A 记录，再做 HTTP 检查；恢复通知需要连续多轮成功确认，避免本机 DNS 缓存或单次网络抖动造成假恢复。

## 使用

```sh
uv venv
uv pip install -r requirements.txt
source .venv/bin/activate

# 先检查一轮：不需要 TG 配置，不发送消息
python3 domain_check.py --once --dry-run

# 从 @BotFather 获取 token；先向机器人发送 /start，或把它加入目标群
cp .env.example .env
# 编辑 .env，填写 TG_BOT_TOKEN 和 TG_CHAT_ID

# 发送测试消息，确认机器人权限和目标 chat_id
python3 domain_check.py --test-notification

# 常驻检测
python3 -u domain_check.py
```

也可以用 `uv run --python 3.11 --with-requirements requirements.txt domain_check.py` 运行。

.env 内容示例：

```dotenv
TG_BOT_TOKEN=你的机器人token
TG_CHAT_ID=你的用户或群chat_id
ALERT_COOLDOWN_HOURS=3
PUBLIC_RESOLVERS=8.8.8.8,1.1.1.1
RECOVERY_CONFIRM_ROUNDS=3
PUBLIC_DNS_TIMEOUT=3.0
```

无需 export；支持引号和注释。已有环境变量优先；.env 不存在时继续使用环境变量。修改后重启生效。.env 已加入 .gitignore。不要将 token 写进 config 或提交到 Git。

Telegram 通过官方 [sendMessage API](https://core.telegram.org/bots/api#sendmessage) 发送通知。chat_id 可在向机器人发送消息后，通过官方 [getUpdates API](https://core.telegram.org/bots/api#getupdates) 返回的 `message.chat.id` 获取；群 ID 通常为负数。已设置 webhook 的机器人应从现有 webhook 消息获取 chat_id。

## 判定与通知

- 默认检查 HTTP 连通性：200–499 算可达（403/404 说明服务器有响应）；5xx、DNS、连接、TLS 证书错误算失败。跟随重定向，以最终响应为准。
- 检查网站健康状态只接受 200–399：加 `--max-status 399`；只接受 200：加 `--max-status 200`。
- 公共 DNS 交叉判定：HTTP 检查前用公共递归 DNS 查询目标域名的 A 记录。查不到（NXDOMAIN / NODATA）直接判失败，即使本机解析器仍有旧缓存；公共 DNS 自身超时或不可达时降级为仅按 HTTP 判定，detail 中注明“公共 DNS 不可用，已降级”，不会因此误报失败。
- 公共 DNS 结果只用于否决：解析出的 IP 不用于连接，HTTP 请求仍走系统默认解析，保持原有可达性语义。多个公共解析器并行查询，任一返回 A 记录即视为可解析；全部解析器都超时或故障时才降级。公共 DNS 查询与 HTTP 查询并发执行，不串行叠加超时；默认解析器为 `8.8.8.8,1.1.1.1`，不使用存在残留缓存的内网递归器。
- 恢复通知需要连续 N 轮成功（默认 3 轮）才发送；中途任何一轮失败都会清零并重新累计。未达到 N 轮期间该目标仍按“可用”统计，只是暂缓恢复通知。
- 每个 URL 失败最多尝试 3 次，尝试间隔 1 秒，请求超时默认 10 秒；采用 GET，只获取响应头，不读取正文。
- 默认 8 个并发目标；每轮全部检查、通知完成后等待 60 秒，开始下一轮。
- 每轮完成后汇总异常与恢复，包含总计、可用和异常数；同一 URL 的异常通知默认冷却 3 小时。
- TG 发送失败会记录日志，并在下一轮当前状态仍需通知时重试。未成功送达的短暂故障若已恢复，不补发过期异常。
- 通知状态保存在磁盘，重启后继续遵守冷却和恢复确认轮数；网络超时但 TG 已实际收到时，重试可能产生重复消息。
- 检测反映运行机器到 HTTP 服务的访问结果，但域名在公网是否存在以公共 DNS 为准；遵循系统代理环境变量。DNS 解析耗时受操作系统影响，timeout 不是整轮总时限。

### 公共 DNS 与恢复确认参数

| 命令行参数 | .env | 默认 | 说明 |
|---|---|---|---|
| `--public-resolvers` | `PUBLIC_RESOLVERS` | `8.8.8.8,1.1.1.1` | 逗号分隔的公共 DNS；空字符串关闭公共 DNS 判定，退回纯 HTTP 行为 |
| `--recovery-confirm-rounds` | `RECOVERY_CONFIRM_ROUNDS` | `3` | 发送恢复通知所需的连续成功轮数，必须 ≥ 1 |
| `--public-dns-timeout` | `PUBLIC_DNS_TIMEOUT` | `3.0` | 单次公共 DNS 查询超时（秒），正浮点 |

命令行参数优先于环境变量，已有环境变量优先于 .env。例如排查时可完全不使用公共 DNS：

```sh
python3 domain_check.py --once --dry-run --public-resolvers ""
```

```sh
python3 domain_check.py --interval 60 --timeout 10 --attempts 3 --workers 8
python3 -B -m unittest -v
```

单轮模式 `--once` 退出码：全部可达为 0，有目标失败为 1；配置错误或测试通知失败为 2。单轮检测的通知发送失败记录在日志中；常驻模式会在下一轮重试。常驻运行可交由现有 systemd、Docker 或进程管理器托管。

## Linux：开机自启、崩溃自动重启

适用于使用 systemd 的 Linux。将项目上传到 `/opt/domain-check`；若使用其他目录，同步修改 `domain-check.service` 中的 WorkingDirectory 和 ExecStart。

先安装 uv，并确认 `/usr/bin/python3` 是 Python 3.11+。在 Linux 上创建虚拟环境，不要从 macOS 复制 .venv：

```sh
cd /opt/domain-check
uv venv --python /usr/bin/python3 .venv
uv pip install --python .venv/bin/python -r requirements.txt

# 尚未创建 .env 时复制示例，然后填写真实 token 和 chat_id
cp -n .env.example .env
```

创建专用服务账号（只需一次），授予读取权限，并验证配置：

```sh
id domain-check || sudo useradd --system --user-group --no-create-home --shell /usr/sbin/nologin domain-check
sudo chown -R root:domain-check /opt/domain-check
sudo chmod 750 /opt/domain-check
sudo chmod 640 /opt/domain-check/.env
sudo -u domain-check /opt/domain-check/.venv/bin/python /opt/domain-check/domain_check.py --once --dry-run
```

确保 config、脚本及虚拟环境可被 domain-check 用户读取/执行。如果之前通过 nohup 启动过，先停止旧进程，避免重复检测。

安装并启动服务：

```sh
sudo install -m 644 domain-check.service /etc/systemd/system/domain-check.service
sudo systemd-analyze verify /etc/systemd/system/domain-check.service
sudo systemctl daemon-reload
sudo systemctl enable --now domain-check.service

# 查看状态和实时日志
sudo systemctl status domain-check.service
sudo journalctl -u domain-check.service -f
```

服务直接运行虚拟环境中的 Python，自动读取项目 .env，不依赖终端 export 或启动时联网安装包。异常退出后等待 10 秒自动重启；检测到目标不可达时进程仍继续工作并按原逻辑告警。手动 stop 不会触发自动重启。配置依据 [systemd.service 官方文档](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)。

```sh
# 修改 .env、config 或代码后重启
sudo systemctl restart domain-check.service

# 停止，并取消开机自启
sudo systemctl disable --now domain-check.service
```

服务文件包含 StateDirectory=domain-check，用于为服务账号提供可写的通知记录目录。更新服务文件后需 daemon-reload 并重启。

## 检测耗时与进度

结果按完成顺序实时打印，并显示 `[已完成/总数]`、失败重试和整轮耗时。慢目标不会阻止已经完成的目标输出。

默认失败目标最多尝试 3 次，每次请求超时 10 秒，重试间隔 1 秒；TLS、多个解析地址、重定向和 DNS 可能使总耗时进一步增加，10 秒不是单个目标的总时限。`--dry-run` 只关闭 TG 发送，不减少检测或重试。

需要快速排查时：

```sh
python3 domain_check.py --once --dry-run --attempts 1 --timeout 3 --workers 24
```

快速模式可能将偶发的网络延迟判为失败；常驻监控建议保留重试。

## 汇总通知与冷却时间

.env 中设置：

```dotenv
ALERT_COOLDOWN_HOURS=3
```

单位为小时，默认 3，支持 0.5 等小数，必须大于 0。修改后重启服务生效。

每轮完成后最多发送一条汇总，包含“总计 / 可用 / 异常”数量，以及本次需要提醒的异常和恢复列表。控制台仍按完成顺序实时打印检测结果。汇总中的异常总数包含仍处于冷却期的目标；列表仅包含本次符合通知条件的目标。

- 冷却按 URL 独立计算，以异常消息成功发送为准。3 小时内不重复报该 URL 的异常；持续异常满 3 小时后再次汇总提醒。新的异常 URL 不受其他 URL 冷却限制。
- 已成功告警的目标需要连续 N 轮成功（`RECOVERY_CONFIRM_ROUNDS`，默认 3）才发送恢复通知，不受冷却限制；中途失败会清零并重新累计。达标前该目标仍计入“可用”，只是不发送恢复通知。恢复后若再次异常，仍沿用上次异常告警的冷却时间；冷却内未重新告警的短暂故障不会再单独发恢复通知。
- TG 发送失败不更新通知记录，下轮按最新状态重试。全部正常且没有待通知的恢复时保持静默；恢复确认轮数累计变化仍会落盘。
- 通知记录原子写入磁盘，重启后保留冷却、待恢复状态和连续成功轮数。systemd 使用 StateDirectory 提供的 `/var/lib/domain-check/alerts.json`；手动运行默认使用项目 `.state/alerts.json`，两者记录独立。可通过 STATE_DIRECTORY 指定手动运行的状态目录。
- dry-run 会读取记录并模拟汇总，但不发送 TG、不写入通知记录。首次升级无法继承旧进程的内存记录，生效后可能先收到一条当前异常汇总。
- 汇总过长时优先缩短错误原因以保留所有 URL；仍无法容纳一条消息时记录错误且不更新冷却。请求超时但 TG 实际已收到、或送达后进程立即崩溃时，仍可能重复通知。

### 状态文件格式

`alerts.json` 以 URL 为键，每个目标记录最近一次异常通知时间、是否待恢复以及连续成功轮数：

```json
{
  "https://example.com": {
    "last_alert": 1700000000.0,
    "unrecovered": true,
    "streak": 1
  }
}
```

旧版本状态文件只有 `last_alert` 和 `unrecovered`，缺少 `streak` 时按 0 兼容读取。因此升级后，旧文件中 `unrecovered=true` 的目标仍需重新连续成功 N 轮才会发送恢复通知，不会立即误发。`last_alert`、`unrecovered` 或 `streak` 类型非法时仍会报错并拒绝启动。

生效命令（git-jks 上的项目路径）：

```sh
cd /git/domain-check-python
# 编辑 .env，将 ALERT_COOLDOWN_HOURS 设为需要的小时数；不填默认 3
sed 's|/opt/domain-check|/git/domain-check-python|g' domain-check.service > /etc/systemd/system/domain-check.service
systemctl daemon-reload
systemctl restart domain-check
journalctl -u domain-check -f
```

首次安装用户请按上文完成服务账号和虚拟环境准备后再启用服务。
