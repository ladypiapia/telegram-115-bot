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

## 说明

- 默认代理写成 `http://127.0.0.1:7890`，适配你服务器上的 mihomo。
- 如果 bot 是私聊使用，Telethon 回传的文件会发送到当前账号的 `Saved Messages`。
- 本项目不做任务持久化，重启后不会恢复旧任务。

## AV 搜索脚本

可以单独抓取番号搜索结果和对应磁力链接：

```bash
python scripts/fetch_av_search_results.py MIMK-145
```
