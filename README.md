# 域名连通性检测

Python 3.11+，使用 python-dotenv 自动加载脚本同目录的 .env，已有环境变量优先。读取同目录现有 `config`，每行一个 HTTP(S) URL 或域名，支持空行、整行 # 注释，自动去重。修改配置后重启生效。

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
```

无需 export；支持引号和注释。已有环境变量优先；.env 不存在时继续使用环境变量。修改后重启生效。.env 已加入 .gitignore。不要将 token 写进 config 或提交到 Git。

Telegram 通过官方 [sendMessage API](https://core.telegram.org/bots/api#sendmessage) 发送通知。chat_id 可在向机器人发送消息后，通过官方 [getUpdates API](https://core.telegram.org/bots/api#getupdates) 返回的 `message.chat.id` 获取；群 ID 通常为负数。已设置 webhook 的机器人应从现有 webhook 消息获取 chat_id。

## 判定与通知

- 默认检查 HTTP 连通性：200–499 算可达（403/404 说明服务器有响应）；5xx、DNS、连接、TLS 证书错误算失败。跟随重定向，以最终响应为准。
- 检查网站健康状态只接受 200–399：加 `--max-status 399`；只接受 200：加 `--max-status 200`。
- 每个 URL 失败最多尝试 3 次，尝试间隔 1 秒，请求超时默认 10 秒；采用 GET，只获取响应头，不读取正文。
- 默认 8 个并发目标；每轮全部检查、通知完成后等待 60 秒，开始下一轮。
- 一个目标一轮内尝试全部失败后发异常通知；持续故障不重复发送，恢复时发一次恢复通知。
- TG 发送失败会记录日志，并在下一轮当前状态仍需通知时重试。未成功送达的短暂故障若已恢复，不补发过期异常。
- 去重状态保存在内存，重启后仍异常的目标会重新通知。网络超时但 TG 已实际收到时，重试可能产生重复消息。
- 检测反映运行机器的访问结果；遵循系统代理环境变量。DNS 解析耗时受操作系统影响，timeout 不是整轮总时限。

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

服务文件已提供；当前开发环境是 macOS，实际服务启用及重启行为需在 Linux 服务器验证。
