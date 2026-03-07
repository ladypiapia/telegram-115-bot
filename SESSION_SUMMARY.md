# 会话整理

## 目标

将原有 `Telegram-115bot` 重构为一个完全独立的新项目 `telegram-115-bot`，只保留最小能力：

- 发送磁力链接到 Telegram 后离线下载到 115
- 115 离线完成后自动推送到 aria2
- aria2 下载完成后通过 Telethon 发送回 Telegram
- `/av 番号` 搜索磁力并自动下载
- 发送 `video/document` 到 bot 后上传到 115

## 新项目目录

独立项目最终放在：

- `/Users/shangshui/Downloads/withot/projects/vibe_coding/telegram-115-bot`

与旧项目目录完全分离，不复用旧项目模块。

## 已实现的项目结构

- `src/main.py`
- `src/bot/handlers.py`
- `src/services/open115.py`
- `src/services/aria2_rpc.py`
- `src/services/telegram_user.py`
- `src/services/av_search.py`
- `src/services/task_flow.py`
- `src/config.py`
- `config/config.yaml`
- `config/config.yaml.example`
- `Dockerfile`
- `docker-compose.yaml`
- `requirements.txt`
- `README.md`

## 关键实现说明

### 1. 115 功能

已实现最小 115 OpenAPI 客户端，支持：

- token 读取与刷新
- PKCE 二维码授权
- 路径查询
- 递归创建目录
- 添加离线任务
- 轮询离线任务状态
- 列出离线完成后的文件
- 获取文件下载直链
- 上传本地文件到 115

### 2. Telegram / Telethon

- Bot 负责命令、按钮、交互
- Telethon 负责：
  - 发送大文件回 Telegram
  - 拉取用户发送给 bot 的文件，再上传到 115

### 3. aria2

- 115 离线完成后，枚举文件并推送到 aria2
- aria2 下载完成后，逐个回传到 Telegram
- 回传成功后自动删除本地下载文件

### 4. `/av`

- 只使用 `sukebei.nyaa.si`
- 搜索到多个结果时，按顺序尝试
- 首个能成功加入 115 离线的磁力即采用

## 部署和排障记录

### 1. Docker 拉基础镜像失败

在本地和服务器都遇到过：

- Docker Desktop / Docker daemon 代理配置问题
- registry mirror 超时
- BuildKit 拉取 `auth.docker.io` token 超时

最终确认：

- `dockerd` 代理需要单独配置，不能只配 `docker-compose.yaml`
- 服务器上 `docker pull python:3.12-slim` 成功后，关闭 BuildKit 可继续构建：

```bash
DOCKER_BUILDKIT=0 COMPOSE_DOCKER_CLI_BUILD=0 docker compose build --no-cache
docker compose up -d
```

### 2. Python 依赖问题

构建时 `beautifulsoup4>=4.13.5` 无法解析，已调整为：

```txt
beautifulsoup4>=4.12.3,<5
```

### 3. 容器内代理和 `config.yaml` 代理的区别

- `docker-compose.yaml` 里的代理：
  - 作用于容器运行环境变量
- `config.yaml` 里的代理：
  - 是应用内部配置
  - 本项目中会直接影响 Telethon 连接代理

因此对当前项目而言，`config.yaml` 的代理配置更关键。

### 4. Telethon 报 `127.0.0.1:7890 connection refused`

根因：

- 容器内的 `127.0.0.1` 指向容器自己
- 宿主机上的 mihomo 跑在宿主机，不在容器内

建议：

- Linux 服务器优先使用 `network_mode: host`
- 这样容器里的 `127.0.0.1:7890` 才等于宿主机上的 mihomo

### 5. 115 离线任务轮询报 405

日志表现：

- `GET https://proapi.115.com/open/offline/get_task_list?page=30`
- 返回 `405 Not Allowed`

原因：

- 轮询时遍历了过多分页

已修复：

- 第一页直接使用首包数据
- 仅从第 2 页开始继续翻页
- 轮询最多检查前 3 页
- 某页返回 `405` 时优雅停止，不再让整个任务失败

修复文件：

- `/Users/shangshui/Downloads/withot/projects/vibe_coding/telegram-115-bot/src/services/open115.py`

## 当前建议的服务器操作

代码同步后执行：

```bash
cd /root/projects/telegram-115-bot
docker compose down
DOCKER_BUILDKIT=0 COMPOSE_DOCKER_CLI_BUILD=0 docker compose build --no-cache
docker compose up -d
docker logs -f telegram-115-bot
```

## 当前已知结论

- Telethon 连接 Telegram 已经打通过
- 代理链路已基本可用
- 当前重点是确保服务器上的项目代码包含最新的 `open115.py` 修复
- `UserWarning: Using async sessions support is an experimental feature` 只是 Telethon 警告，不是主故障
