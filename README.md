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
  配置 `MONITOR_CHAT_IDS` 后，源频道新帖会自动复制；编辑正文、说明或替换图片/视频时，会原地更新目标频道中的对应消息。

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

目标频道 `FORWARD_TO_CHAT_ID` 和 `/forwardto` 可填写频道裸 ID 或 Telegram 标记后的频道 ID（如 `1234567890` 或 `-1001234567890`）。普通群组的负 ID 不能作为频道目标。

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

在频道帖子中使用 `/chatid`，回复会给出可直接复制的 `/monitor add <裸频道ID>`。在私聊或群聊中只显示当前聊天 ID，不会建议将它作为监听源。

`/monitor set` 会先校验整个列表：用中英文逗号分隔，重复 ID 会合并，空项或无效 ID 会导致整个命令被拒绝，原列表不变。`/monitor` 命令只修改当前运行中的内存配置；容器重启后仍以 `.env` 中的 `MONITOR_CHAT_IDS` 为准。

---

## 快速部署

**从旧版升级：** 如果原先将 `./forward_history.json` 单文件挂载到容器，请先按下文「数据持久化说明」迁移历史，再重建服务。首次安装不需要迁移。

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

手动转发消息时，只用 Telegram 提供的原频道及原帖 ID 生成链接；来源不明时，只认正文中的明确消息链接，不会用接收聊天的消息 ID 猜原帖。直接提交带 `comment` 或 `thread` 参数的链接不会转发父帖；若提示无法确认来源，请发送要转发的消息本身或其原帖链接。

手动提交相册时，同一聊天中同一组只创建一个转发任务和入队提示。首个成员缺少来源信息，不会阻止后续可识别成员入队。相册去重只保存在内存中（最近 500 组），不是跨重启的投递记录。

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

未配置用户 session 或启动授权失败时，使用 Bot 监听。首次连接遇到短暂网络错误或限流也会先回退，随后退避重试；未授权或失效的 session 不会持续重试。已启动的用户连接在 Telethon 最终断开后也会回退到 Bot，并通知管理员；短暂的库内重连不会来回切换。程序会尝试恢复用户连接，确认登录后才切回。若 session 已失效，请更换并重启。

要让回退生效，Bot 须能接收源频道更新，并在目标频道有发帖及编辑相应帖子的权限。切换不会扫描历史，不能保证补齐断线期间错过的帖子。

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
- TDL 进度支持回车刷新和换行输出。任务只保留每个输出流最近 16 条、每条最多 2048 字节的诊断内容，避免批量转发时内存和错误历史被进度输出撑满。
- 正在显示进度的状态消息不会提前删除。手动转发和范围转发的提示即使发送失败、被删除或无法更新，任务仍按 TDL 的实际结果记录；不要仅因提示消失而重复投稿。
- `/tdlstop` 停止当前手动 TDL 任务（包括范围导出和随后的转发），等待子进程退出，并清除命令生效时已排队或等待重试的任务。已发送的消息不会撤回；执行中的频道自动同步不强行中断。停止期间新提交的任务会在队列恢复后执行。
- 普通命令回复默认 120 秒后删除。
- 失败或错误状态默认 600 秒后删除。
- `/start` 和 `/help` 的帮助内容不会自动删除。
- 设置 `AUTO_DELETE_BOT_MESSAGES=0` 可关闭自动删除。完整停服会取消尚未执行的自动删除，不会在下次启动时补删提示。

### 自动监听源频道

配置 `MONITOR_CHAT_IDS` 后，程序使用正在监听的 Telethon 账号自动复制源频道新帖，并记录 Telegram 返回的目标消息 ID。自动复制不展开帖内的其他链接；手动提交链接和范围转发仍使用 TDL。

- **相册缺少整组事件**：先暂存新帖事件中实际收到的相册成员，收到相册事件即合并入队；从首个成员起等 1 秒仍未收到整组事件，也会复制已收到的成员。晚到的成员单独补齐，可能不是同一组；不会根据相邻消息 ID 猜测缺项。
- **原地更新**：收到编辑事件后，会在已映射的目标消息上同步正文、说明（含格式）以及替换后的照片或视频，包括以文件发送的普通图片和视频。只改说明时不替换媒体；相册只编辑对应成员，目标消息 ID 和链接不变。其他附件只同步文字。若 Telegram 不允许该次替换，记录失败，不会删除目标帖或另发一条。
- **编辑先于回执**：进程在内存保留最近 4096 个尚无映射的编辑键。对应成员的精确回执落库后，再读取最新正文和媒体原地更新；仅收到编辑绝不创建新帖。重复的新帖事件带有编辑标记时也会核对已映射的目标。
- **手动修正已转发的旧帖**：管理员可发送 `/resync <源频道裸ID> <源消息ID>`，例如 `/resync 1234567890 42`。只操作当前监听源到当前目标的已知映射，成功入队不等于已执行；最终编辑失败会记录日志并通知首位管理员（如已配置）。
- **没有映射不猜测目标**：升级前的转发、手动转发和旧目标频道的记录不会被猜测匹配。程序不扫描历史编辑，目标消息已删除或无法编辑时也不会重发。
- **发送结果未知**：首次复制前先把每条源消息的请求 ID 写入 SQLite。回执不完整、连接中断或服务中途退出时，只保存可确认的目标 ID；其余项保留在 `monitor_copy_intents` 供人工核对，重复事件不会再次发送。不要为了重试而直接删除这些记录。旧版已确认的映射可继续使用。
- **切换监听账号时的排队任务**：尚未发送的复制和待同步的编辑，由出队时的监听账号重新读取源帖、定位目标。Telegram 已明确返回限流的复制请求只允许原发送账号重试；若它已断开，则取消这次重试并报告失败。没有收到回执的请求仍须人工核对，不会因为换号重发。
- **复制限流**：Telegram 明确返回 FloodWait 后，按指定秒数延后，最多重试两次。同一发送请求复用已持久化的 `random_id`（相册逐条复用），等待不占用队列。发送前解析频道时的暂时网络错误可安全重试；已经发出请求后断线或缺回执则不会盲目重发。定时器在内存中：正常停服或 `/tdlstop` 会取消等待；进程突然退出时，尚无回执的记录须人工核对。
- **重复编辑**：同一源帖、同一目标的排队编辑合并处理；执行中再次编辑最多追加一次同步。相册的不同成员与不同目标互不合并。
- **临时编辑失败**：遇到临时连接故障、限流或媒体引用失效时最多重试两次，每次重新读取源帖的最新正文和媒体。等待期间不占用转发队列；目标已删除、权限不足等错误不重发。等待中的编辑重试保存在内存，停止服务或 `/tdlstop` 会取消。
- **持久化**：Compose 的 `monitor-data` 卷保存 SQLite 映射；保留它，重建容器后才能继续更新原帖。不要用 `docker compose down -v` 删除该卷。非 Compose 部署可用 `MONITOR_MAP_FILE` 指定持久化路径。映射独立于 `forward_history.json`。
- **每 7 天清理一次**：默认只清理确认复制已满 7 天的映射，不删除 Telegram 消息或媒体。清理使用后台线程的独立 SQLite 连接，并记录删除条数和耗时。每周批量执行，不是每条映射一满 7 天就删除。启动时检查是否到期，停机错过的清理会在下次运行时补做。旧库无时间戳的映射从升级时开始计时。
- **清理的影响**：清掉映射后，原帖再编辑将被跳过；已清理序号及更早的旧帖不会因新帖事件重放而再次复制。未确认的发送记录保留供人工核对。即使序号早于已清理的帖子，重复事件也不会将它改报为成功或触发重发。SQLite 可复用释放的空间，卷文件不一定立即变小。需要长期同步编辑时，可在 `.env` 设置 `MONITOR_MAP_RETENTION_DAYS=0` 关闭清理，或设为更长的保留天数；不会恢复已清除的映射。

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
/resync <源频道裸ID> <源消息ID>  原地补同步已映射的帖子
/queue                   查看队列状态
/flog                    查看最近转发记录
/flog clear              清空转发记录
/export_range <来源> <起始ID> <结束ID>  批量导出并转发消息范围
```

管理命令必须直接发送。转发给 Bot 的消息或监听频道的正文即使以 `/restart`、`/flog clear` 等命令开头，也不会执行管理操作。群聊可使用 `/queue@机器人用户名`；发给其他机器人的命令不会当作投稿。`/chatid` 仍可在频道中直接查询。

`/flog` 按最新优先展示最近十条记录。过长的错误只截短显示，保留末尾诊断，不影响历史文件里的记录。旧记录过期后，新编号从保留记录的最大编号继续。

`/export_range` 的消息 ID 必须在 1 至 2147483647 之间，起点不能大于终点。若用两个原帖链接指定范围，须属于同一频道；评论、话题链接和多余参数不会入队。`/tdlstop`、`/restart` 和 `/flog clear` 均不接受额外参数，避免误停或误清。

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

Linux 容器收到 `SIGTERM` 后会停止接收任务并清理子进程、定时器及数据库连接；Compose 为此预留 30 秒。已发送的消息不会撤回，结果未知的复制不会因停服而重发。超时强杀或断电不能保证完成清理。

### 重启服务

```bash
docker-compose restart
```

### 查看日志

```bash
docker-compose logs -f
```

应用运行日志 `bot.log` 每 5 MiB 轮换一次，最多保留 3 个备份。文件无法打开时仍输出到标准错误，可用上面的命令查看。此限制不改变 Docker 自身的日志保留策略。

### 重新构建并启动

```bash
docker-compose build --no-cache && docker-compose up -d
```

---

## 数据持久化说明

当前配置会将以下内容挂载到容器中：

```yaml
volumes:
  - ./history:/data/history
  - monitor-data:/data/monitor
  - type: bind
    source: /usr/local/bin/tdl
    target: /usr/local/bin/tdl
    read_only: true
    bind:
      create_host_path: false
  - ~/.tdl:/root/.tdl
```

说明：

- `./history/forward_history.json`：`/flog` 只显示近 7 天的记录，即使停机重启或一直没有新转发也是如此；下次保存时才从文件移除过期记录。目录挂载允许临时文件写完后原子替换；损坏的非空文件保留为同目录 `.corrupt-*` 备份，无法备份时不覆盖原件。非 Compose 部署默认仍在当前目录读写 `forward_history.json`，可通过 `FORWARD_LOG_FILE` 更改。
- `monitor-data`：保存源消息与目标消息 ID 的映射，已确认映射按独立的每周清理规则保留。
- `/usr/local/bin/tdl`：只读挂载宿主机的 TDL 可执行文件。部署前用 `test -f /usr/local/bin/tdl && test -x /usr/local/bin/tdl` 确认它是可执行文件；路径不存在时 Compose 会报错，不会自动创建同名目录。
- `~/.tdl`：挂载宿主机的 TDL 登录会话与配置目录。

---

### 旧版转发历史迁移（只需一次）

在原 Compose 项目目录执行。先停服务避免复制时发生写入；如果新位置已有历史，请先人工核对，不要覆盖。旧文件保留作备份，勿删除 `monitor-data` 卷。

```bash
docker compose stop telegram-bot
mkdir -p history
if [ -f forward_history.json ] && [ ! -e history/forward_history.json ]; then
  cp -p forward_history.json history/forward_history.json
fi
docker compose up -d --build telegram-bot
```

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
- 默认重启策略为 `on-failure:3`，避免配置错误时反复启动。若日志提示 `BOT_TOKEN` 无效或过期，修正 `.env` 后运行 `docker compose up -d --force-recreate telegram-bot`；单纯 `restart` 不会重新载入环境变量。

---

## 许可证

本项目基于 [MIT License](LICENSE) 开源。


## 离线回归测试

安装 `requirements.txt` 后，在仓库根目录运行 `python -m unittest discover -s tests -v`；测试不需要真实 Telegram 凭证。
