



# 使用官方 Python 镜像
FROM python:3.9-slim

# 设置工作目录
WORKDIR /app

# 安装系统依赖（仅 ffmpeg，不安装 tdl）
RUN apt-get update && apt-get install -y ffmpeg && rm -rf /var/lib/apt/lists/*

# 复制 Python 依赖列表并安装
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建下载目录
RUN mkdir -p /google/tg2google/downloads

# 设置环境变量
ENV PYTHONUNBUFFERED=1

# 启动命令
CMD ["python3", "-u", "tdl-tg2gd.py"]