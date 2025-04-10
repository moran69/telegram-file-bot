import os
import json
import shlex
import logging
import aiohttp
import asyncio
import requests
import psutil
import humanize
import time
import subprocess
from telethon import TelegramClient, events
from telethon.sessions import MemorySession
from telethon.tl.types import MessageMediaDocument
from tenacity import retry, stop_after_attempt, wait_exponential
from typing import Optional, Dict, Any

# 日志配置
logging.basicConfig(
    format='%(asctime)s - %(levelname)s - %(message)s',
    level=logging.INFO,
    handlers=[
        logging.FileHandler("bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)
logging.getLogger('telethon').setLevel(logging.WARNING)
logging.getLogger('asyncio').setLevel(logging.WARNING)

# 基础配置
API_ID = 16612890
API_HASH = 'c0fc7dab1acc44f2a2da55cba248d656'
BOT_TOKEN = '7965940462:AAFCSi5PlG5xl9cqQDk6AFuZP4AT-K9OZQM'

# 管理员配置
ADMIN_IDS = [1227176277]  # 替换为你的 Telegram ID

# 文件限制配置
MAX_FILE_SIZE = 4 * 1024 * 1024 * 1024  # 4GB
ALLOWED_TYPES = {
    'video/mp4', 'video/x-matroska', 'video/quicktime',
    'application/zip', 'application/x-rar-compressed',
    'application/x-7z-compressed', 'application/pdf',
    'image/jpeg', 'image/png', 'image/gif',
    'audio/mpeg', 'audio/mp4', 'audio/ogg'
}

# 下载目录配置
DOWNLOAD_PATH = "/google/tg2google/downloads"
os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# TDL 配置
TDL_PATH = "/usr/local/bin/tdl"  # TDL 可执行文件路径，如果在 PATH 中可以直接使用 "tdl"

class DownloadHistory:
    """下载历史记录类"""
    def __init__(self, history_file="download_history.json"):
        self.history_file = history_file
        self.history = self._load_history()
        
    def _load_history(self) -> list:
        """从文件加载历史记录"""
        try:
            if os.path.exists(self.history_file):
                with open(self.history_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            return []
        except Exception as e:
            logger.error(f"Error loading history: {e}")
            return []
            
    def _save_history(self):
        """保存历史记录到文件"""
        try:
            with open(self.history_file, 'w', encoding='utf-8') as f:
                json.dump(self.history, f, ensure_ascii=False, indent=2)
            logger.info(f"成功保存历史记录，记录条数: {len(self.history)}")
        except Exception as e:
            logger.error(f"保存历史记录失败: {e}")
            try:
                current_dir = os.path.dirname(os.path.abspath(self.history_file))
                logger.error(f"历史文件所在目录: {current_dir}")
                logger.error(f"目录是否可写: {os.access(current_dir, os.W_OK)}")
            except Exception as nested_e:
                logger.error(f"检查目录权限时出错: {nested_e}")
            
    def add_download(self, file_id: int, filename: str, filepath: str, size: int, success: bool):
        """添加下载记录"""
        record = {
            'id': len(self.history) + 1,
            'file_id': file_id,
            'filename': filename,
            'filepath': filepath,
            'size': size,
            'size_human': humanize.naturalsize(size),
            'success': success,
            'timestamp': time.time(),
            'date': time.strftime('%Y-%m-%d %H:%M:%S')
        }
        self.history.append(record)
        self._save_history()
        return record['id']
        
    def get_all(self) -> list:
        """获取所有历史记录"""
        return self.history
        
    def get_by_id(self, record_id: int) -> dict:
        """通过ID获取记录"""
        for record in self.history:
            if record['id'] == record_id:
                return record
        return None
        
    def remove_by_id(self, record_id: int) -> bool:
        """通过ID删除记录"""
        for i, record in enumerate(self.history):
            if record['id'] == record_id:
                del self.history[i]
                self._save_history()
                return True
        return False

class FileStats:
    """文件处理统计类"""
    def __init__(self):
        self.processed_count = 0
        self.total_size = 0
        self.start_time = time.time()
        self.successful_downloads = 0
        self.failed_downloads = 0

    def add_processed_file(self, size: int, success: bool):
        """添加处理记录"""
        self.processed_count += 1
        self.total_size += size
        if success:
            self.successful_downloads += 1
        else:
            self.failed_downloads += 1

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            'processed_count': self.processed_count,
            'total_size': humanize.naturalsize(self.total_size),
            'success_rate': f"{(self.successful_downloads/self.processed_count*100):.1f}%" if self.processed_count > 0 else "0%",
            'uptime': humanize.naturaldelta(time.time() - self.start_time)
        }

class TDLDownloader:
    """TDL 下载器类"""
    def __init__(self, tdl_path=TDL_PATH):
        self.tdl_path = tdl_path
        
    def verify_tdl(self):
        if not os.path.exists(self.tdl_path):
            raise FileNotFoundError(f"TDL可执行文件未找到: {self.tdl_path}")
        if not os.access(self.tdl_path, os.X_OK):
            raise PermissionError(f"TDL文件无执行权限: {self.tdl_path}")
        
    async def download_file(self, message_id: int, chat_id: int, file_path: str, progress_callback=None, message_link: str = None):
        """使用 TDL 下载文件"""
        output_dir = os.path.dirname(file_path)
        os.makedirs(output_dir, exist_ok=True)
        
        filename = os.path.basename(file_path)
        cmd = [
            self.tdl_path,
            "dl",
            "-u", message_link,
            "-d", output_dir,
            "--template", filename,
            "--continue",
            "--rewrite-ext",
            "--group",
            "--skip-same",
            "-t", "8",
            "-l", "4"
        ]
        
        logger.info(f"Running TDL command: {' '.join(cmd)}")
        
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            
            total_size = 0
            current_size = 0
            last_update_time = time.time()
            stdout_lines = []
            stderr_lines = []
            
            while True:
                stdout_line = await process.stdout.readline()
                if stdout_line:
                    stdout_lines.append(stdout_line)
                    line_str = stdout_line.decode('utf-8', errors='ignore').strip()
                    logger.debug(f"TDL stdout: {line_str}")
                    
                    if "%" in line_str and progress_callback:
                        try:
                            if "[" in line_str and "]" in line_str:
                                progress_part = line_str.split("[")[1].split("]")[0].strip()
                                if "%" in progress_part:
                                    percentage = float(progress_part.replace("%", "").strip())
                                    
                                    if "(" in line_str and ")" in line_str:
                                        size_part = line_str.split("(")[1].split(")")[0].strip()
                                        if "/" in size_part:
                                            curr_size_str, total_size_str = size_part.split("/")
                                            
                                            def parse_size(size_str):
                                                size_str = size_str.strip()
                                                if "GB" in size_str:
                                                    return float(size_str.replace("GB", "").strip()) * 1024 * 1024 * 1024
                                                elif "MB" in size_str:
                                                    return float(size_str.replace("MB", "").strip()) * 1024 * 1024
                                                elif "KB" in size_str:
                                                    return float(size_str.replace("KB", "").strip()) * 1024
                                                else:
                                                    return float(size_str)
                                            
                                            try:
                                                current_size = int(parse_size(curr_size_str))
                                                total_size = int(parse_size(total_size_str))
                                            except:
                                                if total_size > 0:
                                                    current_size = int(total_size * percentage / 100)
                                    
                                    now = time.time()
                                    if total_size > 0 and (now - last_update_time > 1.5 or percentage >= 100):
                                        await progress_callback(current_size, total_size)
                                        last_update_time = now
                        
                        except Exception as e:
                            logger.error(f"Error parsing progress: {e}, line: {line_str}")
                
                stderr_line = await process.stderr.readline()
                if stderr_line:
                    stderr_lines.append(stderr_line)
                    line_str = stderr_line.decode('utf-8', errors='ignore').strip()
                    logger.error(f"TDL stderr: {line_str}")
                
                if not stdout_line and not stderr_line:
                    if process.returncode is not None:
                        break
                    await asyncio.sleep(0.1)
            
            await process.wait()
            
            stdout = b''.join(stdout_lines).decode('utf-8', errors='ignore')
            stderr = b''.join(stderr_lines).decode('utf-8', errors='ignore')
            
            if process.returncode == 0:
                logger.info("TDL下载报告成功，检查文件存在性")
    
                if os.path.exists(file_path):
                    logger.info(f"文件在预期位置: {file_path}")
                    return file_path
                
                expected_dir = os.path.dirname(file_path)
                filename_base = os.path.basename(file_path).split('.')[0]
                possible_files = [f for f in os.listdir(expected_dir) if f.startswith(filename_base)]
                
                if possible_files:
                    actual_file = os.path.join(expected_dir, possible_files[0])
                    logger.info(f"找到可能的文件: {actual_file}")
                    return actual_file
                
                try:
                    all_files = os.listdir(expected_dir)
                    if all_files:
                        latest_file = max(all_files, key=lambda f: os.path.getmtime(os.path.join(expected_dir, f)))
                        latest_path = os.path.join(expected_dir, latest_file)
                        logger.info(f"找到最近修改的文件: {latest_path}")
                        return latest_path
                except Exception as e:
                    logger.error(f"检查最近文件时出错: {e}")
                
                logger.error("TDL报告成功但找不到任何可能的文件")
                return None
                
        except Exception as e:
            logger.error(f"Error running TDL command: {str(e)}")
            return None

class ProgressCallback:
    """下载进度回调类"""
    def __init__(self, message, total_size: int):
        self.message = message
        self.last_update_time = time.time()
        self.last_processed = 0
        self.start_time = time.time()
        self.total = total_size
        self.last_edit_time = 0
        self.update_interval = 3
        self.current_size = 0
        self.downloaded_bytes = 0
        self.last_bytes = 0
        self.last_speed_check = time.time()

    async def callback(self, current: int, total: int):
        try:
            now = time.time()
            self.current_size = current
            self.downloaded_bytes = current
            
            if now - self.last_edit_time < self.update_interval and current != total:
                return
            
            # 计算即时速度
            time_diff = now - self.last_speed_check
            if time_diff > 0:
                speed = (current - self.last_bytes) / time_diff
                self.last_bytes = current
                self.last_speed_check = now
            else:
                speed = 0
            
            progress = (current / total * 100) if total else 0
                
            if speed > 0:
                eta_seconds = (total - current) / speed
                eta_text = f"{int(eta_seconds)}秒" if eta_seconds < 60 else f"{int(eta_seconds/60)}分钟"
            else:
                eta_text = "计算中..."

            await self.message.edit(
                f"📥 下载进度...\n"
                f"⏳ 进度: {progress:.1f}%\n"
                f"📊 已下载: {humanize.naturalsize(current)}/{humanize.naturalsize(total)}\n"
                f"🚀 速度: {humanize.naturalsize(speed)}/s\n"
                f"⌛ 预计剩余: {eta_text}"
            )
            
            self.last_update_time = now
            self.last_processed = current
            self.last_edit_time = now
                
        except Exception as e:
            logger.error(f"Progress update error: {e}")
            
    def get_total_time(self) -> float:
        """获取总用时（秒）"""
        return time.time() - self.start_time
        
    def get_average_speed(self) -> float:
        """获取平均速度（字节/秒）"""
        total_time = self.get_total_time()
        if total_time > 0:
            return self.current_size / total_time
        return 0
        
    def get_stats_text(self) -> str:
        """获取统计信息文本"""
        total_time = self.get_total_time()
        avg_speed = self.get_average_speed()
        
        if total_time < 60:
            time_text = f"{total_time:.1f}秒"
        elif total_time < 3600:
            time_text = f"{total_time/60:.1f}分钟"
        else:
            time_text = f"{total_time/3600:.1f}小时"
            
        return (
            f"⏱️ 本次下载总用时: {time_text}\n"
            f"⚡ 平均速度: {humanize.naturalsize(avg_speed)}/s"
        )

class TelegramBot:
    """Telegram 机器人主类"""
    def __init__(self):
        self.client = TelegramClient(MemorySession(), API_ID, API_HASH)
        self.stats = FileStats()
        self.history = DownloadHistory()
        self.start_time = time.time()
        self.tdl_downloader = TDLDownloader()

    async def check_file(self, event) -> tuple[bool, Optional[str]]:
        """检查文件是否符合要求"""
        if not event.message.media:
            return False, "不是文件"
            
        file_size = event.message.file.size
        if file_size > MAX_FILE_SIZE:
            return False, f"文件太大了！最大支持 {humanize.naturalsize(MAX_FILE_SIZE)}"
            
        mime_type = event.message.file.mime_type
        if mime_type not in ALLOWED_TYPES:
            return False, f"不支持的文件类型：{mime_type}"
            
        return True, None

    async def get_system_stats(self) -> Dict[str, Any]:
        """获取系统状态"""
        process = psutil.Process()
        memory_info = process.memory_info()
        
        return {
            'memory_used': humanize.naturalsize(memory_info.rss),
            'cpu_percent': process.cpu_percent(),
            'disk_usage': humanize.naturalsize(psutil.disk_usage(DOWNLOAD_PATH).used),
            'uptime': humanize.naturaldelta(time.time() - self.start_time)
        }

    async def handle_admin_commands(self, event) -> bool:
        """处理管理员命令"""
        if event.sender_id not in ADMIN_IDS:
            return False
            
        command_text = event.message.text.lower()
        command_parts = command_text.split()
        command = command_parts[0]
        
        if command == '/stats':
            sys_stats = await self.get_system_stats()
            file_stats = self.stats.get_stats()
            
            await event.respond(
                f"📊 机器人状态\n\n"
                f"系统信息：\n"
                f"💾 内存使用：{sys_stats['memory_used']}\n"
                f"💻 CPU 使用：{sys_stats['cpu_percent']}%\n"
                f"💿 磁盘使用：{sys_stats['disk_usage']}\n"
                f"⏱ 运行时间：{sys_stats['uptime']}\n\n"
                f"文件处理：\n"
                f"📁 总处理文件：{file_stats['processed_count']}\n"
                f"📦 总处理大小：{file_stats['total_size']}\n"
                f"✅ 成功率：{file_stats['success_rate']}"
            )
            return True
        elif command == '/restart':
            await event.respond("🔄 正在重启机器人...")
            await self.restart_bot()
            return True
        elif command == '/clean':
            cleaned = await self.clean_downloads()
            await event.respond(f"🧹 清理完成，删除了 {cleaned} 个临时文件")
            return True
        elif command == '/history':
            history = self.history.get_all()
            if not history:
                await event.respond("📝 暂无下载历史记录")
                return True
                
            history_text = "📝 下载历史记录：\n\n"
            for record in history[-10:]:
                history_text += (
                    f"ID: {record['id']}\n"
                    f"📁 文件名: {record['filename']}\n"
                    f"📦 大小: {record['size_human']}\n"
                    f"📅 日期: {record['date']}\n"
                    f"📂 路径: {record['filepath']}\n\n"
                )
                
            history_text += f"共 {len(history)} 条记录，显示最近 {min(10, len(history))} 条"
            await event.respond(history_text)
            return True
        elif command == '/delete':
            if len(command_parts) < 2:
                await event.respond("❌ 用法: /delete <文件ID>")
                return True
                
            try:
                file_id = int(command_parts[1])
                record = self.history.get_by_id(file_id)
                
                if not record:
                    await event.respond(f"❌ 找不到ID为 {file_id} 的文件记录")
                    return True
                    
                if not os.path.exists(record['filepath']):
                    await event.respond(f"❌ 文件不存在: {record['filepath']}")
                    self.history.remove_by_id(file_id)
                    return True
                    
                try:
                    os.remove(record['filepath'])
                    self.history.remove_by_id(file_id)
                    await event.respond(
                        f"✅ 已删除文件:\n"
                        f"📁 文件名: {record['filename']}\n"
                        f"📂 路径: {record['filepath']}"
                    )
                except Exception as e:
                    await event.respond(f"❌ 删除文件失败: {str(e)}")
                    
            except ValueError:
                await event.respond("❌ 文件ID必须是数字")
                
            return True
            
        return False

    async def clean_downloads(self) -> int:
        """清理下载目录"""
        count = 0
        for file in os.listdir(DOWNLOAD_PATH):
            try:
                os.remove(os.path.join(DOWNLOAD_PATH, file))
                count += 1
            except Exception as e:
                logger.error(f"Error cleaning file {file}: {e}")
        return count

    async def restart_bot(self):
        """重启机器人"""
        import sys
        import subprocess
        
        script_path = os.path.abspath(sys.argv[0])
        
        logger.info(f"Restarting bot via subprocess... Script path: {script_path}")
        
        try:
            logger.info("Stopping current client...")
            await self.stop()
            
            current_dir = os.path.dirname(script_path)
            logger.info(f"Working directory: {current_dir}")
            
            restart_cmd = f"cd {current_dir} && python3 {script_path} > google_bot.log 2>&1 &"
            logger.info(f"Restart command: {restart_cmd}")
            
            result = subprocess.run(
                restart_cmd,
                shell=True,
                capture_output=True,
                text=True
            )
            
            logger.info(f"Subprocess return code: {result.returncode}")
            if result.stdout:
                logger.info(f"Subprocess stdout: {result.stdout}")
            if result.stderr:
                logger.error(f"Subprocess stderr: {result.stderr}")
                
            logger.info("Waiting 3 seconds before exit...")
            await asyncio.sleep(3)
            
            logger.info("Exiting current process...")
            os._exit(0)
            
        except Exception as e:
            logger.error(f"Error during restart: {e}")
            for admin_id in ADMIN_IDS:
                try:
                    await self.client.send_message(admin_id, f"🔄 重启失败: {str(e)}")
                except:
                    pass

    async def handle_file(self, event, processing_msg):
        """处理文件下载"""
        download_path = None
        success = False
        file_size = 0
        file_name = ""
        start_time = time.time()  # 添加开始时间记录
        
        try:
            logger.info(f"开始处理文件消息: message_id={event.message.id}, chat_id={event.chat_id}")
            
            can_process, error_msg = await self.check_file(event)
            if not can_process:
                logger.warning(f"文件检查失败: {error_msg}")
                await processing_msg.edit(f"❌ {error_msg}")
                return

            if isinstance(event.message.media, MessageMediaDocument):
                orig_name = event.message.file.name if event.message.file.name else f"file_{event.message.id}"
                file_type = orig_name.split('.')[-1] if '.' in orig_name else ''
                logger.info(f"原始文件名: {orig_name}, 文件类型: {file_type}")
                
                if hasattr(event.message.media.document, 'attributes'):
                    for attr in event.message.media.document.attributes:
                        if hasattr(attr, 'title') and attr.title:
                            file_name = f"{attr.title}.{file_type}"
                            logger.info(f"从属性获取文件名: {file_name}")
                            break
                    else:
                        file_name = orig_name
                        logger.info(f"使用原始文件名: {file_name}")
                else:
                    file_name = orig_name
                    logger.info(f"使用原始文件名: {file_name}")
            else:
                file_name = f"file_{event.message.id}"
                logger.info(f"生成默认文件名: {file_name}")
            
            file_path = os.path.join(DOWNLOAD_PATH, file_name)
            logger.info(f"文件保存路径: {file_path}")
            
            progress = ProgressCallback(processing_msg, event.message.file.size)
            
            message_link = None
            
            if event.message.forward:
                logger.info("检测到转发消息")
                from_id = event.message.forward.from_id
                logger.info(f"转发来源ID: {from_id}")
                
                if hasattr(from_id, 'channel_id'):
                    channel_id = from_id.channel_id
                    forward_msg_id = event.message.forward.channel_post
                    logger.info(f"频道消息: channel_id={channel_id}, message_id={forward_msg_id}")
                    
                    try:
                        channel_entity = await event.client.get_entity(channel_id)
                        logger.info(f"获取频道实体: {channel_entity}")
                        if hasattr(channel_entity, 'username') and channel_entity.username:
                            message_link = f"https://t.me/{channel_entity.username}/{forward_msg_id}"
                            logger.info(f"构建公开频道链接: {message_link}")
                        else:
                            original_message = await event.client.get_messages(channel_id, ids=forward_msg_id)
                            if original_message and hasattr(original_message, 'id'):
                                original_chat = await event.client.get_entity(original_message.chat_id)
                                if hasattr(original_chat, 'username') and original_chat.username:
                                    message_link = f"https://t.me/{original_chat.username}/{original_message.id}"
                                    logger.info(f"构建私人频道链接: {message_link}")
                                else:
                                    message_link = original_message.link
                                    logger.info(f"使用原始消息链接: {message_link}")
                            else:
                                message_link = event.message.forward.link
                                logger.info(f"使用转发消息链接: {message_link}")
                    except Exception as e:
                        logger.error(f"获取频道实体失败: {e}")
                        message_link = event.message.forward.link
                        logger.info(f"使用转发消息链接: {message_link}")
                elif hasattr(from_id, 'user_id'):
                    user_id = from_id.user_id
                    forward_msg_id = event.message.forward.date
                    logger.info(f"用户消息: user_id={user_id}, message_id={forward_msg_id}")
                    message_link = event.message.forward.link
                    logger.info(f"使用转发消息链接: {message_link}")
            
            if not message_link:
                logger.info("使用当前消息构建链接")
                chat = await event.get_chat()
                logger.info(f"当前聊天: {chat}")
                
                if hasattr(chat, 'username') and chat.username:
                    message_link = f"https://t.me/{chat.username}/{event.message.id}"
                    logger.info(f"构建公开聊天链接: {message_link}")
                else:
                    chat_id = event.chat_id
                    logger.info(f"私人聊天ID: {chat_id}")
                    
                    if chat_id < 0:
                        try:
                            original_message = await event.client.get_messages(chat_id, ids=event.message.id)
                            if original_message and hasattr(original_message, 'id'):
                                original_chat = await event.client.get_entity(original_message.chat_id)
                                if hasattr(original_chat, 'username') and original_chat.username:
                                    message_link = f"https://t.me/{original_chat.username}/{original_message.id}"
                                    logger.info(f"构建私人频道链接: {message_link}")
                                else:
                                    message_link = original_message.link
                                    logger.info(f"使用原始消息链接: {message_link}")
                            else:
                                message_link = event.message.link
                                logger.info(f"使用当前消息链接: {message_link}")
                        except:
                            message_link = event.message.link
                            logger.info(f"使用当前消息链接: {message_link}")
                    else:
                        message_link = event.message.link
                        logger.info(f"使用当前消息链接: {message_link}")
            
            logger.info(f"开始下载文件，链接: {message_link}")
            await processing_msg.edit(f"🔄 准备使用 TDL 下载文件...\n链接: {message_link}")
            
            download_path = await self.tdl_downloader.download_file(
                event.message.id,
                event.chat_id,
                file_path,
                progress.callback,
                message_link
            )
            
            if not download_path:
                logger.error("TDL 下载失败")
                await processing_msg.edit("❌ TDL 下载失败，请重试")
                return
            
            file_size = os.path.getsize(download_path)
            end_time = time.time()
            download_time = end_time - start_time
            speed_mbps = (file_size / (1024 * 1024)) / download_time  # 计算MB/s
            
            logger.info(f"文件下载完成，大小: {humanize.naturalsize(file_size)}")
            
            record_id = self.history.add_download(
                event.message.id,
                os.path.basename(download_path),
                download_path,
                file_size,
                True
            )
            logger.info(f"添加到下载历史，记录ID: {record_id}")
            
            time_text = f"{download_time:.1f}秒" if download_time < 60 else f"{download_time/60:.1f}分钟"
            
            await processing_msg.edit(
                f"✅ TDL 下载完成！\n"
                f"📁 文件名: {os.path.basename(download_path)}\n"
                f"📦 文件大小: {humanize.naturalsize(file_size)}\n"
                f"📂 保存位置: {download_path}\n"
                f"🔢 历史记录ID: {record_id}\n\n"
                f"⏱️ 本次下载总用时: {time_text}\n"
                f"⚡ 平均速度: {speed_mbps:.1f} MB/s"
            )
            success = True
            logger.info(f"文件处理完成，总用时: {time_text}, 平均速度: {speed_mbps:.1f} MB/s")
                
        except Exception as e:
            logger.error(f"处理文件时出错: {str(e)}", exc_info=True)
            await processing_msg.edit(f"❌ 处理出错: {str(e)}")
        finally:
            if download_path and os.path.exists(download_path):
                file_size = os.path.getsize(download_path)
                self.stats.add_processed_file(file_size, success)
                logger.info(f"更新统计信息: size={file_size}, success={success}")
                
                if not success and file_name and download_path:
                    self.history.add_download(
                        event.message.id,
                        file_name,
                        download_path,
                        file_size,
                        False
                    )
                    logger.info("添加失败记录到历史")
                    
        if success:
            try:
                final_confirm = await event.respond(
                    f"✅ 确认下载已完成！\n"
                    f"📁 文件: {os.path.basename(download_path)}\n"
                    f"如果没有看到详细信息，请使用 /history 命令查看下载历史"
                )
                logger.info("已发送最终确认消息")
            except Exception as e:
                logger.error(f"发送最终确认消息失败: {e}")

    async def start(self):
        """启动机器人"""
        await self.client.start(bot_token=BOT_TOKEN)
        
        start_message = (
            '🤖 机器人已启动！\n\n'
            '👋 你好！我是文件下载助手（TDL版）\n\n'
            '📥 发送任何文件给我，我会通过TDL帮你下载到服务器\n\n'
            '✨ 支持任何类型的文件\n\n'
            '📌 管理员命令:\n'
            '/stats - 查看统计信息\n'
            '/restart - 重启机器人\n'
            '/clean - 清理临时文件\n'
            '/history - 查看下载历史\n'
            '/delete  - 删除指定ID的文件'
        )
        
        for admin_id in ADMIN_IDS:
            try:
                await self.client.send_message(admin_id, start_message)
                logger.info(f"Sent startup message to admin {admin_id}")
            except Exception as e:
                logger.error(f"Failed to send startup message to admin {admin_id}: {e}")
        
        @self.client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            await event.respond(
                '👋 你好！我是文件下载助手（TDL版）\n\n'
                '📥 发送任何文件给我，我会通过TDL帮你下载到服务器\n\n'
                '✨ 支持任何类型的文件\n\n'
                '📌 管理员命令:\n'
                '/stats - 查看统计信息\n'
                '/restart - 重启机器人\n'
                '/clean - 清理临时文件\n'
                '/history - 查看下载历史\n'
                '/delete  - 删除指定ID的文件'
            )

        @self.client.on(events.NewMessage)
        async def message_handler(event):
            try:
                if event.message.text and event.message.text.startswith('/'):
                    if await self.handle_admin_commands(event):
                        return

                if event.message.media:
                    processing_msg = await event.respond("⏳ 正在处理文件...")
                    await self.handle_file(event, processing_msg)
            except Exception as e:
                logger.error(f"Message handler error: {e}")

        logger.info("Bot started with TDL integration...")
        await self.client.run_until_disconnected()

    async def stop(self):
        """停止机器人"""
        try:
            await self.client.disconnect()
        except Exception as e:
            logger.error(f"Error during shutdown: {e}")

async def main():
    """主函数"""
    bot = TelegramBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Bot stopped by user")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
    finally:
        await bot.stop()

if __name__ == "__main__":
    if os.path.exists('bot_session.session'):
        try:
            os.remove('bot_session.session')
        except:
            pass
    
    asyncio.run(main())