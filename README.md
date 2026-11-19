# Telegram 转发机器人（Docker + TDL）

一个基于 Telegram Bot 的文件转发工具，可将消息或文件转发到指定私人频道，并通过 [TDL](https://github.com/iyear/tdl) 提升大文件下载与传输效率。

支持 Docker 容器化部署，适合需要稳定转存 Telegram 文件、归档资源或管理转发历史的场景。

> 说明：本项目支持处理禁止复制、禁止转发的文件。请确保仅在合法、合规且已获得授权的范围内使用。

---

## 功能特性

- **TDL 加速传输**  
  调用宿主机已安装的 `tdl` 工具，高效下载 Telegram 文件。

- **自动转发文件**  
  向机器人发送文件、消息链接或转发消息后，可自动加入队列并转发到指定频道。

- **用户账号监听源频道**  
  可使用 Telethon 用户账号 session 监听源频道的新消息和编辑，bot 只负责接收命令和发送状态消息。

- **自动监听频道转发**  
  配置 `MONITOR_CHAT_IDS` 后，源频道新帖会自动复制；编辑正文或媒体说明时，原地更新目标频道中的对应消息。

- **转发历史记录**  
  自动保存转发记录，便于后续查询与管理。

- **文件持久化存储**  
  通过 Docker Volume 将数据保存到宿主机，避免容器重建导致数据丢失。

- **管理员控制**  
  支持管理员查看历史、清理文件等管理操作。

- **Docker 部署**  
  支持 Docker 与 Docker Compose，一键构建并运行。

---

## 环境要求

部署前请确保宿主机已安装并配置以下组件：

- [Docker](https://docs.docker.com/engine/install/)
- [Docker Compose](https://docs.docker.com/compose/install/)
- [TDL](https://docs.iyear.me/tdl/zh/getting-started/installation/)
- Telegram API 凭证
- Telegram Bot Token
- 管理员 Telegram 用户 ID
- 接收文件的频道 ID
- 需要自动监听时：源频道 ID
- 需要用户账号监听时：Telethon 用户账号 session

---

## 安装 TDL

执行以下命令安装 TDL：

```bash
curl -sSL https://docs.iyear.me/tdl/install.sh | sudo bash
```

安装完成后，使用二维码登录：

```bash
tdl login -T qr
```

扫码后，如账号开启了两步验证，请根据提示输入二步验证密码。

确认 `tdl` 具有执行权限：

```bash
chmod +x /usr/local/bin/tdl
```

配置 TDL 目录权限：

```bash
chmod -R go-rwx ~/.tdl
```

---

## 安装 Docker

```bash
curl -fsSL get.docker.com -o get-docker.sh && sh get-docker.sh
```

---

## 安装 Docker Compose

下载官方二进制文件：

```bash
sudo curl -L "https://github.com/docker/compose/releases/latest/download/docker-compose-$(uname -s)-$(uname -m)" \
  -o /usr/local/bin/docker-compose
```

添加执行权限：

```bash
sudo chmod +x /usr/local/bin/docker-compose
```

验证安装结果：

```bash
docker-compose --version
```

---

## 获取必要配置

### Telegram API 凭证

前往以下地址申请：

[Telegram API](https://my.telegram.org/auth)

需要获取：

- `API_ID`
- `API_HASH`

### Bot Token

通过 Telegram 中的 [@BotFather](https://t.me/BotFather) 创建机器人，并获取：

- `BOT_TOKEN`

### 用户 ID / 频道 ID

可通过 [@userinfobot](https://t.me/userinfobot) 获取账号 ID。

目标频道 `FORWARD_TO_CHAT_ID` 建议填写 tdl 可识别的裸 ID，也就是去除 `-100` 前缀后的数字。

示例：

```text
原始频道 ID：-1001234567890
FORWARD_TO_CHAT_ID 配置：1234567890
```

自动监听源频道 `MONITOR_CHAT_IDS` 只填写源频道裸 ID，不要加 `-100` 前缀。多个频道用英文逗号分隔。

示例：

```text
MONITOR_CHAT_IDS=1234567890,9876543210
```

也可以先启动 bot 后，在管理员会话里使用：

```text
/chatid
/monitor add 1234567890
/monitor set 1234567890,9876543210
```

`/monitor` 命令只修改当前运行中的内存配置；容器重启后仍以 `.env` 中的 `MONITOR_CHAT_IDS` 为准。

---

## 快速部署

### 步骤 1：克隆仓库

```bash
git clone https://github.com/Homomimo/telegram-tdl-forwarder.git
cd telegram-tdl-forwarder
```

---

### 步骤 2：配置本机 `.env`

首次部署时在项目目录执行（已有 `.env` 不要覆盖）：

```bash
cp -n .env.example .env
chmod 600 .env
nano .env
```

填写 `API_ID`、`API_HASH`、`BOT_TOKEN`、`ADMIN_IDS` 和 `FORWARD_TO_CHAT_ID`。需要自动监听时再填写 `MONITOR_CHAT_IDS`。仅生成用户 session 时只需 `API_ID` 和 `API_HASH`。Compose 会将这些变量传给相应服务，不用修改 YAML；不要公开 `.env` 或已展开的 Compose 配置。

**默认只接受授权用户投稿。** 管理员可以手动提交链接和转发消息；要授权其他投稿者，在 `SUBMITTER_IDS` 中填写其 Telegram 用户 ID（逗号分隔）。投稿者没有管理权限。两份名单都为空时不接受手动投稿，已配置的源频道自动监听不受影响。`/forwardto` 的更改不会改变已入队任务的目标频道。

**从旧版升级：** 先把旧 Compose 中填写的真实值迁到本机 `.env`，再切换到新版配置。若曾分享或推送包含凭证的旧镜像，请轮换 Bot Token 并撤销受影响的用户会话；重建镜像不能撤回旧凭证。

---

### 步骤 3：生成 Telethon 用户账号 session（可选但推荐）

如果只手动给 bot 发送链接或转发消息，可以跳过本步骤。

如果需要自动监听源频道的新消息和编辑，推荐生成 `USER_SESSION_STRING`，让用户账号负责监听源频道。这样源频道不需要把 bot 设为管理员。用于监听的用户账号须能读取源频道，并且有权在目标频道发帖和编辑相应帖子。

运行一次性服务：

```bash
docker compose run --rm gen-session
```

旧版 Docker Compose 命令也可以使用：

```bash
docker-compose run --rm gen-session
```

按提示输入：

- 手机号
- Telegram 验证码
- 二步验证密码，如果账号开启了 2FA

成功后会输出一行：

```text
USER_SESSION_STRING=一大串字符串
```

将生成的值填回本机 `.env`：

```dotenv
USER_SESSION_STRING=一大串字符串
```

`USER_SESSION_STRING` 等同于该 Telegram 用户账号的登录凭证，请妥善保存，不要泄露。

---

### 步骤 4：启动服务

```bash
docker-compose build --no-cache && docker-compose up -d
```

---

### 步骤 5：查看运行日志

```bash
docker-compose logs -f
```

如果日志中没有报错，并且机器人可以正常响应消息，则说明部署成功。

如果启用了用户账号监听，日志中应能看到：

```text
用户账号监听已启动
源频道的新消息及编辑将由用户账号 Telethon client 监听，bot 只处理命令和状态消息
```

如果没有配置 `USER_SESSION_STRING` 或 session 无效，程序会回退到 bot 推送监听。此时 bot 必须是源频道管理员，否则 Telegram 不会把频道消息更新推送给 bot。

---

## 使用指南

### 基础使用

1. 启动机器人服务。
2. 在 Telegram 中向机器人发送消息链接、转发频道消息，或直接发送支持的文件消息。
3. 机器人会将任务加入队列。
4. 处理完成后，消息会被转发到配置的目标频道。

### 自动删除机器人消息

默认会自动删除 bot 发送的临时消息，可通过环境变量调整：

```yaml
- AUTO_DELETE_BOT_MESSAGES=1
- AUTO_DELETE_STATUS_SECONDS=300
- AUTO_DELETE_COMMAND_SECONDS=120
- AUTO_DELETE_ERROR_SECONDS=600
```

说明：

- 状态消息只会在任务成功、失败或被 `/tdlstop` 停止后开始倒计时删除。
- 正在显示进度的状态消息不会提前删除。
- 普通命令回复默认 120 秒后删除。
- 失败或错误状态默认 600 秒后删除。
- `/start` 和 `/help` 的帮助内容不会自动删除。
- 设置 `AUTO_DELETE_BOT_MESSAGES=0` 可关闭自动删除。

### 自动监听源频道

配置 `MONITOR_CHAT_IDS` 后，程序使用正在监听的 Telethon 账号自动复制源频道新帖，并记录 Telegram 返回的目标消息 ID。自动复制不展开帖内的其他链接；手动提交链接和范围转发仍使用 TDL。

- **原地更新**：收到编辑事件后，只更新当前目标频道中已经建立映射的那条消息的正文或媒体说明（含格式），保留目标消息 ID 和链接。相册只更新被编辑的成员；不会替换媒体文件、删除目标帖或另发一条。
- **没有映射只记日志**：升级前的转发、手动转发和旧目标频道的记录不会被猜测匹配。程序不扫描历史编辑，目标消息已删除或无法编辑时也不会重发。
- **发送结果未知**：首次复制前先把每条源消息的请求 ID 写入 SQLite。回执不完整、连接中断或服务中途退出时，只保存可确认的目标 ID；其余项保留在 `monitor_copy_intents` 供人工核对，重复事件不会再次发送。不要为了重试而直接删除这些记录。旧版已确认的映射可继续使用。
- **临时编辑失败**：连接问题或限流最多重试两次，每次重新读取最新正文。等待期间不占用转发队列；目标已删除、权限不足等错误不重发。等待中的编辑重试保存在内存，停止服务或 `/tdlstop` 会取消。
- **持久化**：Compose 的 `monitor-data` 卷保存 SQLite 映射；保留它，重建容器后才能继续更新原帖。不要用 `docker compose down -v` 删除该卷。非 Compose 部署可用 `MONITOR_MAP_FILE` 指定持久化路径。映射独立于 `forward_history.json`。

推荐配置：

```dotenv
MONITOR_CHAT_IDS=1234567890
USER_MONITOR_ENABLED=1
USER_SESSION_STRING=一大串字符串
```

权限要求：

- 用户账号监听：用户账号须加入源频道，并拥有目标频道的发帖和编辑相应帖子的权限。
- Bot 回退监听：Bot 须为源频道管理员，并拥有目标频道的发帖和编辑相应帖子的权限。
- 自动复制走 Telegram 服务端，遵守源频道的转发限制；若无法复制，不会回退到无法记录映射的 TDL 转发。
- 手动 TDL 转发仍要求 TDL 登录账号能读取源频道并向目标频道发帖。

### 查看帮助

向机器人发送：

```text
/start
```

即可查看帮助菜单。

### 常用 bot 命令

```text
/forwardto <频道ID>       设置目标频道
/showto                  查看当前目标频道
/chatid                  查看当前聊天/频道 ID，用于配置监听源频道
/monitor                 查看当前监听源频道列表
/monitor add <裸ID>       添加监听源频道
/monitor del <裸ID>       删除监听源频道
/monitor set <ID1,ID2>    覆盖监听源频道列表
/monitor clear           清空监听源频道列表
/queue                   查看队列状态
/flog                    查看最近转发记录
/flog clear              清空转发记录
/export_range <来源> <起始ID> <结束ID>  批量导出并转发消息范围
```

---

## 常用命令

### 启动服务

```bash
docker-compose up -d
```

### 停止服务

```bash
docker-compose down
```

### 重启服务

```bash
docker-compose restart
```

### 查看日志

```bash
docker-compose logs -f
```

### 重新构建并启动

```bash
docker-compose build --no-cache && docker-compose up -d
```

---

## 数据持久化说明

当前配置会将以下内容挂载到容器中：

```yaml
volumes:
  - ./forward_history.json:/app/forward_history.json
  - monitor-data:/data/monitor
  - /usr/local/bin/tdl:/usr/local/bin/tdl
  - ~/.tdl:/root/.tdl
```

说明：

- `forward_history.json`：保存转发历史记录。
- `monitor-data`：保存源消息与目标消息 ID 的映射，不随转发历史清理。
- `/usr/local/bin/tdl`：挂载宿主机的 TDL 可执行文件。
- `~/.tdl`：挂载宿主机的 TDL 登录会话与配置目录。

---

## 注意事项

- 请确保宿主机已完成 `tdl login` 登录。
- 如需自动监听源频道，建议先通过 `docker compose run --rm gen-session` 生成 `USER_SESSION_STRING`。
- 使用用户账号监听时，生成 session 的用户账号必须已加入源频道。
- `USER_SESSION_STRING` 是登录凭证，请像密码一样保护。
- 请确保容器内可以访问 `/usr/local/bin/tdl`。
- 请确保 `~/.tdl` 权限正确，否则可能导致 TDL 无法读取登录状态。
- `FORWARD_TO_CHAT_ID` 建议使用去除 `-100` 前缀后的裸 ID。
- `MONITOR_CHAT_IDS` 只填写源频道裸 ID，不要加 `-100` 前缀，多个频道用英文逗号分隔。
- 不要泄露 `API_HASH`、`BOT_TOKEN` 等敏感信息。
- 如需长期运行，建议将 `restart` 改为：

```yaml
restart: unless-stopped
```

---

## 许可证

本项目基于 [MIT License](LICENSE) 开源。


## 离线回归测试

安装 `requirements.txt` 后，在仓库根目录运行 `python -m unittest discover -s tests -v`；测试不需要真实 Telegram 凭证。
