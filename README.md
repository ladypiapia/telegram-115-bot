# telegram-115-bot

独立版 Telegram 115 下载机器人，只保留这些功能：

- 发送磁力链接到 Telegram，选择目录后离线下载到 115
- 115 离线完成后自动推送到 aria2
- aria2 下载完成后通过 Telethon 发回 Telegram
- `/av` 后再发送番号，从新站点搜索前 10 条资源并选择下载
- 发送 `video/document` 到 bot，选择目录后上传到 115

## 目录

```text
.
├── config
│   ├── config.yaml
│   ├── config.yaml.example
│   ├── 115_tokens.json
│   └── user_session.session
├── src
│   ├── bot
│   ├── services
│   ├── config.py
│   └── main.py
├── downloads
├── tmp
├── Dockerfile
├── docker-compose.yaml
└── requirements.txt
```

## 命令

- `/start`
- `/auth`
- `/av`
- `/q`

直接发送 `magnet:` 链接会进入离线下载流程。

直接发送 `video/document` 会进入上传到 115 的流程。

## 部署

1. 确认 `config/config.yaml` 已填好。
2. 将 Telethon 会话文件放到 `config/user_session.session`。
3. 如果你已经有 115 token，也可以放到 `config/115_tokens.json`。
4. 确保 aria2 的下载目录和 bot 进程看到的是同一份路径。
5. 启动：

```bash
python -m src.main
```

如需使用其他配置文件，再临时指定：

```bash
BOT_CONFIG=/path/to/other-config.yaml python -m src.main
```

或者使用 Docker：

```bash
docker compose up -d --build
```

## 后台运行与异常通知

不建议直接开一个终端执行 `python -m src.main`。生产环境应使用 `systemd` 托管：

- 后台常驻
- 开机自启
- 异常退出自动重启
- 异常退出时通过 Telegram 推送告警

仓库已提供示例文件：

- `deploy/systemd/telegram-115-bot.service.example`
- `scripts/send_service_alert.py`

推荐步骤：

1. 先确认项目虚拟环境和 `config/config.yaml` 都已就绪。
2. 将示例 service 复制到系统目录：

```bash
cp deploy/systemd/telegram-115-bot.service.example /etc/systemd/system/telegram-115-bot.service
```

3. 按你的实际路径修改 `WorkingDirectory`、`BOT_CONFIG`、`ExecStart`、`ExecStopPost`。
4. 重新加载并启动：

```bash
systemctl daemon-reload
systemctl enable --now telegram-115-bot
```

5. 查看状态和日志：

```bash
systemctl status telegram-115-bot
journalctl -u telegram-115-bot -f
```

说明：

- `Restart=always` 会在进程退出后自动拉起。
- `ExecStopPost` 会在服务停止后触发告警脚本。
- 告警脚本会读取 `BOT_CONFIG`，使用当前 bot token 和 `allowed_user` 给你发消息。
- 告警脚本默认只在非正常停止时发送通知；手动正常停服不会骚扰你。

## 说明

- 默认代理写成 `http://127.0.0.1:7890`，适配你服务器上的 mihomo。
- 如果 bot 是私聊使用，Telethon 回传的文件会发送到当前账号的 `Saved Messages`。
- 本项目不做任务持久化，重启后不会恢复旧任务。

## AV 搜索脚本

可以单独抓取番号搜索结果和对应磁力链接：

```bash
python scripts/fetch_av_search_results.py MIMK-145
```
