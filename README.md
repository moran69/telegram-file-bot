


# 📥 Telegram 文件下载机器人 (Docker + TDL)

![Docker](https://img.shields.io/badge/Docker-支持-blue?logo=docker)
![Python](https://img.shields.io/badge/Python-3.9+-blue?logo=python)
![Telegram](https://img.shields.io/badge/Telegram-Bot-0088cc?logo=telegram)

通过 Telegram Bot 下载文件到服务器，使用 [TDL](https://github.com/iyear/tdl) 加速大文件传输，支持 Docker 容器化部署。

## 🌟 功能特性
- ✅ **大文件下载** - 支持最大 4GB 文件（可配置）
- ⚡ **TDL 加速** - 使用宿主机的 `tdl` 工具高效下载
- 📊 **下载统计** - 记录文件大小、下载速度等指标
- 🔒 **安全存储** - 文件持久化保存到宿主机目录
- 🛡️ **管理员控制** - 支持查看历史、清理文件等管理命令
- 📦 **Docker 集成** - 一键部署，环境隔离

## 🚀 快速部署

### 前提条件
- 已安装 [Docker](https://docs.docker.com/engine/install/) 和 [Docker Compose](https://docs.docker.com/compose/install/)
- 宿主机已安装 `tdl` 并配置执行权限：
  ```bash
  chmod +x /usr/local/bin/tdl
  ```
- 配tdl目录权限
  ```bash
  chmod -R 755 ~/.tdl
  ```
- Telegram API 凭证（从 [@BotFather](https://t.me/BotFather) 获取）

### 步骤 1：克隆仓库
```bash
git clone https://github.com/yourusername/telegram-file-bot.git
cd telegram-file-bot
```

### 步骤 2：配置环境
复制环境模板文件并填写真实值：
```bash
cp .env.template .env
nano .env  # 编辑配置
```

`.env` 文件示例：
```ini
# Telegram API 配置
API_ID=12345678
API_HASH=abcdef1234567890
BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11

# 管理员 ID（你的 Telegram User ID）
ADMIN_IDS=1227176277

# 可选：最大文件大小（字节，默认 4GB）
MAX_FILE_SIZE=4294967296
```

### 步骤 3：启动服务
```bash
docker-compose up -d --build
```

### 步骤 4：验证运行状态
```bash
docker logs -f telegram-file-bot
```

## 🛠️ 使用指南

### 基础命令
- 发送任意文件给机器人自动下载
- 回复 `/start` 查看帮助菜单

### 管理员命令
| 命令 | 功能 | 示例 |
|------|------|------|
| `/stats` | 查看系统统计 | `/stats` |
| `/history` | 显示下载历史 | `/history` |
| `/delete <ID>` | 删除指定文件 | `/delete 3` |
| `/clean` | 清理临时文件 | `/clean` |
| `/restart` | 重启机器人 | `/restart` |

# 效果演示
![image](https://github.com/user-attachments/assets/45d06ca3-c73d-47ec-8a76-ff6c37d63cc3)



## 📂 文件目录结构
```text
telegram-file-bot/
├── Dockerfile             # Docker 构建配置
├── docker-compose.yml     # 容器编排配置
├── requirements.txt       # Python 依赖
├── bot.py                 # 主程序代码
├── downloads/             # 文件下载目录（自动创建）
└── download_history.json  # 下载历史记录（自动创建）
```

## ⚙️ 配置调优

### 修改下载限制
编辑 `docker-compose.yml`：
```yaml
environment:
  - MAX_FILE_SIZE=8589934592  # 8GB
```

### 更改下载存储路径
修改 `docker-compose.yml` 中的 volumes：
```yaml
volumes:
  - /custom/download/path:/google/tg2google/downloads
```

## 🚨 故障排查

### 常见问题
1. **TDL 无法执行**  
   - 确保宿主机 `tdl` 已安装且路径正确
   - 检查权限：`chmod +x /usr/local/bin/tdl`   `chmod -R 755 ~/.tdl`

2. **文件下载失败**  
   - 检查磁盘空间：`df -h`
   - 查看容器日志：`docker logs telegram-file-bot`

3. **环境变量未生效**  
   - 重启容器：`docker-compose restart`

## 📜 许可证
本项目采用 [MIT License](LICENSE)



# 补充
如果遇到 tdl 命令无法执行的问题，可以检查：
- 容器内 tdl 的权限

`docker-compose exec telegram-bot ls -l /usr/local/bin/tdl`
- 确认挂载成功：

`docker-compose exec telegram-bot which tdl`

- 寻求claude deepseek cursor等支持


---

## ☕ 请作者喝杯咖啡

> 如果这个项目对你有帮助，欢迎请我喝杯咖啡，让开源走得更远 🌈

<div align="center">

每一份支持，都是深夜敲键盘的动力。
**你的 coffee，我的 code。** 💝

<br>

[![打赏](https://img.shields.io/badge/💝_打赏支持-点击进入-FF6B6B?style=for-the-badge&logo=alipay&logoColor=white)](https://7998888.xyz/donate/)
[![金额](https://img.shields.io/badge/💰_一杯茶_起步-¥6-EE5A6F?style=for-the-badge&logo=buy-me-a-coffee&logoColor=white)](https://7998888.xyz/donate/)

</div>

---

**支持的打赏方式：** 支付宝当面付（扫码即付，安全加密）

<div align="center">

<sub> Generated with ❤️ by <a href="https://github.com/moran69">@moran69</a> · 既然都看到这了，不点一下上面的粉色按钮吗？</sub>

</div>
