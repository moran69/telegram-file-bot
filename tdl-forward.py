import os
import re
import json
import logging
import asyncio
import time
import sys
import subprocess
import tempfile
import secrets
import sqlite3
from telethon import TelegramClient, errors, events, functions, types, utils
from telethon.sessions import MemorySession, StringSession
from typing import Awaitable, Callable, Optional
from dataclasses import dataclass
from enum import Enum

# ─────────────────────────── Logging ───────────────────────────
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

# ─────────────────────────── Config ────────────────────────────
try:
    API_ID = int(os.getenv("API_ID") or 0)
except ValueError:
    raise SystemExit("配置错误：API_ID 必须是正整数") from None
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "")
USER_SESSION_STRING = os.getenv("USER_SESSION_STRING", "").strip()
USER_SESSION_FILE = os.getenv("USER_SESSION_FILE", "").strip()
USER_MONITOR_ENABLED = os.getenv("USER_MONITOR_ENABLED", "1").lower() not in ("0", "false", "no", "off")
TDL_PATH = "/usr/local/bin/tdl"
FORWARD_LOG_FILE = "forward_history.json"
FORWARD_LOG_MAX_DAYS = 7  # 保留最近7天
MONITOR_MAP_FILE = os.getenv("MONITOR_MAP_FILE", "").strip() or "monitor_message_map.sqlite3"
MONITOR_CLEANUP_INTERVAL = 7 * 86400
try:
    MONITOR_MAP_RETENTION_DAYS = int(os.getenv("MONITOR_MAP_RETENTION_DAYS") or 7)
    if not 0 <= MONITOR_MAP_RETENTION_DAYS <= 36500:
        raise ValueError
except ValueError:
    raise SystemExit("MONITOR_MAP_RETENTION_DAYS 必须是 0 到 36500 之间的整数（0 表示不清理）") from None
TELEGRAM_LINK_PATTERN = re.compile(r"https://t\.me/(?:c/)?[A-Za-z0-9_]+/\d+")
EXCLUDE_KEYWORDS = ["addlist"]
AUTO_DELETE_BOT_MESSAGES = os.getenv("AUTO_DELETE_BOT_MESSAGES", "1").lower() not in ("0", "false", "no", "off")
AUTO_DELETE_STATUS_SECONDS = int(os.getenv("AUTO_DELETE_STATUS_SECONDS", "300"))
AUTO_DELETE_COMMAND_SECONDS = int(os.getenv("AUTO_DELETE_COMMAND_SECONDS", "120"))
AUTO_DELETE_ERROR_SECONDS = int(os.getenv("AUTO_DELETE_ERROR_SECONDS", "600"))


def monitor_visual_media(media) -> Optional[types.InputMediaPhoto | types.InputMediaDocument]:
    """可原地同步的图片/视频；网页预览、贴纸、视频留言等仍只同步文字。"""
    if isinstance(media, types.MessageMediaPhoto) and isinstance(media.photo, types.Photo):
        result = utils.get_input_media(media)
    elif isinstance(media, types.MessageMediaDocument) and isinstance(media.document, types.Document):
        doc = media.document
        if any(isinstance(a, types.DocumentAttributeSticker) or
               (isinstance(a, types.DocumentAttributeVideo) and a.round_message) for a in doc.attributes):
            return None
        if not (doc.mime_type.startswith(("video/", "image/")) or
                any(isinstance(a, types.DocumentAttributeVideo) for a in doc.attributes)):
            return None
        result = utils.get_input_media(media)
    else:
        return None
    # Telethon 1.28.5 的 get_input_media 保留文件引用和 TTL，但不复制剧透标记。
    result.spoiler = media.spoiler
    return result


class TaskStatus(Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    UNCONFIRMED = "unconfirmed"


class CopyOutcomeUnknown(Exception):
    """已保留发送意图，不能证明 Telegram 未发送。"""


@dataclass
class ForwardTask:
    event: events.NewMessage.Event | events.Album.Event
    link: str
    status_msg_id: Optional[int]
    chat_id: int
    target_chat_id: int
    status_client: Optional[TelegramClient] = None
    status: TaskStatus = TaskStatus.PENDING
    error_msg: str = ""
    created_at: float = 0
    grouped_id: Optional[int] = None
    mode: str = "clone"  # clone、export_range、monitor_copy、monitor_edit
    message_ids: Optional[list[int]] = None
    source_chat_id: Optional[int] = None
    edit_retries: int = 0


class TaskQueue:
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.queue: asyncio.Queue[ForwardTask] = asyncio.Queue()
        self.current_task: Optional[ForwardTask] = None
        self.worker_task: Optional[asyncio.Task] = None
        self.running = False
        self.task_counter = 0
        self._retry_timers: set[asyncio.TimerHandle] = set()

    async def start(self):
        self.running = True
        self.worker_task = asyncio.create_task(self._worker())
        logger.info("任务队列已启动")

    async def stop(self):
        self.running = False
        self.cancel_delayed_edits()
        if self.worker_task:
            self.worker_task.cancel()
            try:
                await self.worker_task
            except asyncio.CancelledError:
                pass
        logger.info("任务队列已停止")

    async def _edit_task_status(self, task: ForwardTask, text: str):
        if task.status_msg_id is None:
            return None
        try:
            status_client = task.status_client or task.event.client
            status_msg = await status_client.get_messages(task.chat_id, ids=task.status_msg_id)
            if status_msg:
                await status_msg.edit(text)
            return status_msg
        except Exception as e:
            logger.debug(f"更新任务状态失败: {e}")
        return None

    def retry_edit_later(self, task: ForwardTask, delay: float) -> None:
        # 不让单帖的 FloodWait 阻塞其他已排队的任务。
        def requeue():
            self._retry_timers.discard(timer)
            self.queue.put_nowait(task)

        task.edit_retries += 1
        task.status = TaskStatus.PENDING
        timer = asyncio.get_running_loop().call_later(delay, requeue)
        self._retry_timers.add(timer)

    def cancel_delayed_edits(self) -> int:
        count = len(self._retry_timers)
        for timer in self._retry_timers:
            timer.cancel()
        self._retry_timers.clear()
        return count

    async def clear_pending_tasks(self) -> int:
        cleared = self.cancel_delayed_edits()
        while True:
            try:
                task = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            task.status = TaskStatus.FAILED
            task.error_msg = "管理员已停止并清空等待队列"
            status_msg = await self._edit_task_status(task, "⏹️ 已被管理员停止，等待队列已清空")
            self.bot.schedule_status_delete(status_msg, failed=True)
            self.queue.task_done()
            cleared += 1
        return cleared

    async def mark_current_task_stopped(self) -> bool:
        task = self.current_task
        if not task or task.mode in ("monitor_copy", "monitor_edit"):
            return False
        task.status = TaskStatus.FAILED
        task.error_msg = "管理员已停止 TDL 进程"
        status_msg = await self._edit_task_status(task, "⏹️ 已被管理员停止，当前 TDL 进程已终止")
        self.bot.schedule_status_delete(status_msg, failed=True)
        return True

    async def add_task(self, event, link: str, status_msg_id: Optional[int], chat_id: int,
                       grouped_id: Optional[int] = None, mode: str = "clone",
                       message_ids: Optional[list[int]] = None, source_chat_id: Optional[int] = None,
                       status_client: Optional[TelegramClient] = None, *, target_chat_id: int) -> int:
        self.task_counter += 1
        task = ForwardTask(
            event=event,
            link=link,
            status_msg_id=status_msg_id,
            chat_id=chat_id,
            status_client=status_client or event.client,
            created_at=time.time(),
            grouped_id=grouped_id,
            mode=mode,
            message_ids=message_ids,
            source_chat_id=source_chat_id,
            target_chat_id=target_chat_id,
        )
        await self.queue.put(task)
        position = self.queue.qsize()
        logger.info(f"任务已加入队列，位置: {position}, mode: {mode}")
        return position

    async def _worker(self):
        while self.running:
            try:
                task = await self.queue.get()
                self.current_task = task
                task.status = TaskStatus.PROCESSING
                await self._process_task(task)
                self.queue.task_done()
                self.current_task = None
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"队列处理异常: {e}", exc_info=True)
                if self.current_task:
                    self.current_task.status = TaskStatus.FAILED
                    self.current_task.error_msg = str(e)
                self.current_task = None

    def _clean_progress_line(self, line: str, max_len: int = 800) -> str:
        clean_line = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line).strip()
        if len(clean_line) > max_len:
            clean_line = clean_line[-max_len:]
        return clean_line

    def _format_last_output(self, output: str, max_len: int = 1000) -> str:
        lines = [self._clean_progress_line(line, 300) for line in output.splitlines() if line.strip()]
        if not lines:
            return ""
        text = "\n".join(lines[-3:])
        if len(text) > max_len:
            text = text[-max_len:]
        return text

    async def _edit_progress(self, status_msg, header: str, details: list[str], line: str) -> None:
        now = time.time()
        last_update = getattr(status_msg, '_last_progress_update', 0)
        if now - last_update < 2:
            return

        setattr(status_msg, '_last_progress_update', now)
        clean_line = self._clean_progress_line(line)

        text = "\n".join([header, *details, "", "进度输出：", clean_line])
        if len(text) > 3900:
            text = text[:3900]

        try:
            await status_msg.edit(text)
        except Exception as e:
            logger.debug(f"更新进度消息失败: {e}")

    async def _process_task(self, task: ForwardTask):
        if task.mode in ("monitor_copy", "monitor_edit"):
            await self.bot.process_monitor_task(task)
            return

        try:
            status_client = task.status_client or task.event.client
            status_msg = await status_client.get_messages(task.chat_id, ids=task.status_msg_id)
            if not status_msg:
                logger.error("无法获取状态消息")
                return

            target_id = task.target_chat_id
            if target_id is None:
                await status_msg.edit("❌ 未配置目标频道ID")
                task.status = TaskStatus.FAILED
                task.error_msg = "未配置目标频道ID"
                return

            if task.mode == "clone":
                await status_msg.edit(
                    f"🔄 正在转发（队列中）...\n"
                    f"FROM: {task.link}\n"
                    f"TO:   {target_id}"
                )
                progress_details = [
                    f"FROM: {task.link}",
                    f"TO:   {target_id}",
                ]

                async def update_clone_progress(line: str) -> None:
                    await self._edit_progress(status_msg, "🔄 正在转发...", progress_details, line)

                ok, output = await self.bot.tdl.forward_clone(task.link, target_id, update_clone_progress)
                self.bot.forward_log.add(task.link, target_id, ok, "" if ok else output)

                last_output = self._format_last_output(output)
                last_output_text = f"\n\n最后输出：\n{last_output}" if last_output else ""
                if ok:
                    await status_msg.edit(
                        f"✅ 转发成功\n"
                        f"FROM: {task.link}\n"
                        f"TO:   {target_id}"
                        f"{last_output_text}"
                    )
                    task.status = TaskStatus.COMPLETED
                else:
                    await status_msg.edit(
                        f"❌ 转发失败\n"
                        f"FROM: {task.link}\n"
                        f"错误: {output}"
                    )
                    task.status = TaskStatus.FAILED
                    task.error_msg = output

            elif task.mode == "export_range":
                await status_msg.edit(
                    f"📥 正在导出消息范围...\n"
                    f"源: {task.link}\n"
                    f"范围: {task.message_ids[0]}-{task.message_ids[1]}"
                )
                with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
                    export_file = tmp.name
                try:
                    export_details = [
                        f"源: {task.link}",
                        f"范围: {task.message_ids[0]}-{task.message_ids[1]}",
                    ]

                    async def update_export_progress(line: str) -> None:
                        await self._edit_progress(status_msg, "📥 正在导出消息范围...", export_details, line)

                    ok, output = await self.bot.tdl.export_range(
                        task.source_chat_id,
                        task.message_ids[0],
                        task.message_ids[1],
                        export_file,
                        update_export_progress,
                    )
                    if not ok:
                        last_output = self._format_last_output(output)
                        last_output_text = f"\n\n最后输出：\n{last_output}" if last_output else ""
                        await status_msg.edit(
                            f"❌ 导出失败\n"
                            f"源: {task.link}\n"
                            f"范围: {task.message_ids[0]}-{task.message_ids[1]}"
                            f"{last_output_text}"
                        )
                        task.status = TaskStatus.FAILED
                        task.error_msg = output
                        return

                    await status_msg.edit(
                        f"📤 正在转发导出文件...\n"
                        f"文件: {os.path.basename(export_file)}"
                    )
                    forward_details = [
                        f"文件: {os.path.basename(export_file)}",
                        f"TO: {target_id}",
                    ]

                    async def update_forward_progress(line: str) -> None:
                        await self._edit_progress(status_msg, "📤 正在转发导出文件...", forward_details, line)

                    ok, output = await self.bot.tdl.forward_from_export(export_file, target_id, update_forward_progress)
                    self.bot.forward_log.add(task.link, target_id, ok, "" if ok else output)

                    last_output = self._format_last_output(output)
                    last_output_text = f"\n\n最后输出：\n{last_output}" if last_output else ""
                    if ok:
                        await status_msg.edit(
                            f"✅ 导出+转发成功 (范围)\n"
                            f"源: {task.link}\n"
                            f"范围: {task.message_ids[0]}-{task.message_ids[1]}\n"
                            f"TO: {target_id}"
                            f"{last_output_text}"
                        )
                        task.status = TaskStatus.COMPLETED
                    else:
                        await status_msg.edit(
                            f"❌ 导出+转发失败 (范围)\n"
                            f"源: {task.link}\n"
                            f"范围: {task.message_ids[0]}-{task.message_ids[1]}\n"
                            f"错误: {output}"
                        )
                        task.status = TaskStatus.FAILED
                        task.error_msg = output
                finally:
                    if os.path.exists(export_file):
                        os.remove(export_file)

        except Exception as e:
            logger.error(f"任务处理异常: {e}", exc_info=True)
            task.status = TaskStatus.FAILED
            task.error_msg = str(e)
            try:
                status_client = task.status_client or task.event.client
                status_msg = await status_client.get_messages(task.chat_id, ids=task.status_msg_id)
                if status_msg:
                    await status_msg.edit(f"❌ 处理异常: {e}")
            except Exception:
                pass
        finally:
            if task.status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
                try:
                    status_client = task.status_client or task.event.client
                    status_msg = await status_client.get_messages(task.chat_id, ids=task.status_msg_id)
                    self.bot.schedule_status_delete(status_msg, failed=task.status == TaskStatus.FAILED)
                except Exception as e:
                    logger.debug(f"安排状态消息自动删除失败: {e}")

    def get_queue_status(self) -> dict:
        return {
            "queue_size": self.queue.qsize() + len(self._retry_timers),
            "current_task": {
                "link": self.current_task.link[:50] if self.current_task else None,
                "status": self.current_task.status.value if self.current_task else None
            } if self.current_task else None,
            "running": self.running
        }

# 管理员帮助说明
START_MESSAGE_ADMIN = (
    '🤖 消息转发助手管理面板\n\n'
    '【基础用法】\n'
    '1. 直接转发频道/群消息给机器人\n'
    '2. 发送公开或私有频道消息链接：\n'
    '   https://t.me/channel_name/123\n'
    '   https://t.me/c/123456789/123\n'
    '3. 机器人会按队列克隆转发到当前目标频道\n'
    '4. 配置 MONITOR_CHAT_IDS 后，自动复制源频道新帖并原地同步正文和说明的编辑\n\n'
    '【管理员命令】\n'
    '/forwardto <频道ID>\n'
    '  设置或修改目标频道，使用 tdl 可识别的裸 ID，不需要加 -100 前缀\n'
    '  示例：/forwardto 1234567890\n\n'
    '/showto\n'
    '  查看当前目标频道 ID\n\n'
    '/chatid\n'
    '  查看当前聊天/频道 ID，用于配置监听源频道\n\n'
    '/monitor\n'
    '  查看当前自动监听的源频道 ID 列表\n'
    '/monitor add <频道ID>\n'
    '  添加自动监听源频道\n'
    '/monitor del <频道ID>\n'
    '  删除自动监听源频道\n'
    '/monitor set <频道ID1,频道ID2>\n'
    '  覆盖自动监听源频道列表\n'
    '/monitor clear\n'
    '  清空自动监听源频道列表\n\n'
    '/tdlstop\n'
    '  停止当前正在运行的 tdl 进程，并清空等待队列\n\n'
    '/queue\n'
    '  查看当前等待中的转发任务数量\n\n'
    '/flog\n'
    '  查看最近 10 条转发记录和失败原因\n\n'
    '/flog clear\n'
    '  清空本地转发记录文件\n\n'
    '/export_range <来源> <起始消息ID> <结束消息ID>\n'
    '  从指定 Chat 导出一段消息并转发到目标频道\n'
    '  来源支持裸 Chat ID、t.me/c 频道链接或消息链接\n'
    '  示例：/export_range 3482276715 100 200\n'
    '  示例：/export_range https://t.me/c/3482276715 100 200\n'
    '  示例：/export_range https://t.me/c/3482276715/100 https://t.me/c/3482276715/200\n\n'
    '/restart\n'
    '  重启机器人进程\n\n'
    '/help\n'
    '  显示本帮助\n\n'
    '【注意事项】\n'
    '- 目标频道必须先用 /forwardto 设置，或通过 FORWARD_TO_CHAT_ID 环境变量配置\n'
    '- 自动监听源频道通过 MONITOR_CHAT_IDS 环境变量配置，只填写裸 ID，不要加 -100 前缀，多个频道用英文逗号分隔\n'
    '- 用户账号监听通过 USER_SESSION_STRING 或 USER_SESSION_FILE 启用；该用户账号需要加入源频道\n'
    '- /monitor 子命令只修改当前运行中的内存配置；容器重启后仍以环境变量为准\n'
    '- 机器人和 tdl 登录账号需要有源频道读取权限、目标频道发帖权限\n'
    '- export_range 适合批量补发，消息 ID 范围不宜一次过大'
)


# ─────────────────────────── Helpers ───────────────────────────
def parse_admin_ids(raw: str) -> list[int]:
    result = []
    for item in raw.split(','):
        item = item.strip()
        if not item:
            continue
        try:
            result.append(int(item))
        except ValueError:
            logger.warning(f"忽略无效管理员ID: {item}")
    return result


def parse_chat_id(text: str) -> Optional[int]:
    try:
        return int(text.strip())
    except (ValueError, AttributeError):
        return None


def strip_url_query(text: str) -> str:
    return text.strip().split('?', 1)[0].rstrip('/')


def parse_private_channel_link(text: str) -> tuple[Optional[int], Optional[int]]:
    """解析 t.me/c/<chat_id>[/message_id] 链接。

    返回: (chat_id, message_id)。频道链接没有 message_id 时返回 (chat_id, None)。
    公开用户名链接无法得到 tdl 裸 Chat ID，因此不在这里解析。
    """
    link = strip_url_query(text)
    match = re.match(r"https?://t\.me/c/(-?\d+)(?:/(\d+))?/?$", link)
    if not match:
        return None, None

    chat_id = int(match.group(1))
    message_id = int(match.group(2)) if match.group(2) else None
    return chat_id, message_id


def parse_export_range_args(parts: list[str]) -> tuple[Optional[int], Optional[int], Optional[int], Optional[str]]:
    """解析 /export_range 参数。

    支持:
    - /export_range <Chat ID> <起始ID> <结束ID>
    - /export_range <频道链接> <起始ID> <结束ID>
    - /export_range <起始消息链接> <结束消息链接>
    - /export_range <消息链接> <结束ID>
    """
    args = parts[1:]
    if len(args) < 2:
        return None, None, None, "missing"

    first_chat_id, first_msg_id = parse_private_channel_link(args[0])

    if first_chat_id is not None:
        if first_msg_id is not None:
            if len(args) < 2:
                return None, None, None, "missing_end_id"

            second_chat_id, second_msg_id = parse_private_channel_link(args[1])
            if second_chat_id is not None:
                if second_chat_id != first_chat_id:
                    return None, None, None, "chat_mismatch"
                if second_msg_id is None:
                    return None, None, None, "missing_end_id"
                return first_chat_id, first_msg_id, second_msg_id, None

            end_id = parse_chat_id(args[1])
            if end_id is None:
                return None, None, None, "invalid_end_id"
            return first_chat_id, first_msg_id, end_id, None

        if len(args) < 3:
            return None, None, None, "missing_range"
        start_id = parse_chat_id(args[1])
        end_id = parse_chat_id(args[2])
        if start_id is None or end_id is None:
            return None, None, None, "invalid_range_id"
        return first_chat_id, start_id, end_id, None

    if args[0].startswith(("http://t.me/", "https://t.me/")):
        return None, None, None, "public_link_not_supported"

    if len(args) < 3:
        return None, None, None, "missing_range"

    chat_id = parse_chat_id(args[0])
    start_id = parse_chat_id(args[1])
    end_id = parse_chat_id(args[2])
    if chat_id is None or start_id is None or end_id is None:
        return None, None, None, "invalid_range_id"
    return chat_id, start_id, end_id, None


def parse_monitor_chat_id(text: str) -> Optional[int]:
    chat_id = parse_chat_id(text)
    if chat_id is None or chat_id <= 0:
        return None
    raw = str(chat_id)
    if raw.startswith('100') and len(raw) > 10:
        return None
    return chat_id


def parse_chat_ids(raw: str) -> list[int]:
    result = []
    for item in raw.split(','):
        chat_id = parse_monitor_chat_id(item)
        if chat_id is None:
            if item.strip():
                logger.warning(f"忽略无效监听源频道裸ID: {item}")
            continue
        result.append(chat_id)
    return result


ADMIN_IDS: list[int] = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
SUBMITTER_IDS = set(parse_admin_ids(os.getenv("SUBMITTER_IDS", "")))


def can_submit(sender_id: Optional[int]) -> bool:
    return bool(sender_id and sender_id > 0 and (sender_id in ADMIN_IDS or sender_id in SUBMITTER_IDS))

TARGET_CHAT_ID: Optional[int] = parse_chat_id(os.getenv("FORWARD_TO_CHAT_ID", ""))
MONITOR_CHAT_IDS: list[int] = parse_chat_ids(os.getenv("MONITOR_CHAT_IDS", ""))


# ─────────────────────────── ForwardLog ────────────────────────
class ForwardLog:
    def __init__(self, log_file: str = FORWARD_LOG_FILE):
        self.log_file = log_file
        self.logs: list[dict] = self._load()

    def _load(self) -> list[dict]:
        if not os.path.exists(self.log_file):
            return []
        try:
            with open(self.log_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"加载转发日志失败: {e}")
            return []

    def _save(self) -> None:
        if FORWARD_LOG_MAX_DAYS > 0:
            cutoff = time.time() - (FORWARD_LOG_MAX_DAYS * 86400)
            self.logs = [r for r in self.logs if r.get('timestamp', 0) > cutoff]

        try:
            with open(self.log_file, 'w', encoding='utf-8') as f:
                json.dump(self.logs, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存转发日志失败: {e}")

    def add(self, from_link: str, to_chat_id: int, success: bool, error_msg: str = "") -> str:
        record_id = f"{len(self.logs) + 1}"
        record = {
            'id': record_id,
            'from': from_link,
            'to': to_chat_id,
            'success': success,
            'error': error_msg,
            'timestamp': time.time(),
            'date': time.strftime('%Y-%m-%d %H:%M:%S'),
        }
        self.logs.append(record)
        self._save()
        return record_id

    def get_all(self) -> list[dict]:
        return self.logs

    def get_recent(self, count: int = 10) -> list[dict]:
        return self.logs[-count:] if self.logs else []

    def clear(self) -> None:
        self.logs = []
        self._save()


class MonitorMessageMap:
    """自动复制的精确消息关联，独立于会过期、可清理的转发历史。"""

    def __init__(self, path: str):
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self.db = sqlite3.connect(path)
        try:
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                self.db.execute("""
                    CREATE TABLE IF NOT EXISTS monitor_messages (
                        source_chat_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        target_chat_id INTEGER NOT NULL,
                        target_message_id INTEGER NOT NULL,
                        confirmed_at INTEGER NOT NULL,
                        PRIMARY KEY (source_chat_id, source_message_id, target_chat_id)
                    )
                """)
                columns = {row[1] for row in self.db.execute("PRAGMA table_info(monitor_messages)")}
                if "confirmed_at" not in columns:
                    # 无法推断旧回执时间，给旧映射一个完整保留期。DDL 和回填一同提交。
                    self.db.execute("ALTER TABLE monitor_messages ADD COLUMN confirmed_at INTEGER NOT NULL DEFAULT 0")
                    self.db.execute("UPDATE monitor_messages SET confirmed_at=?", (int(time.time()),))
                self.db.execute("CREATE INDEX IF NOT EXISTS monitor_confirmed_at ON monitor_messages(confirmed_at)")
                self.db.execute("CREATE TABLE IF NOT EXISTS monitor_cleanup "
                                "(id INTEGER PRIMARY KEY CHECK(id=1), last_run INTEGER NOT NULL)")
                self.db.execute("""
                    CREATE TABLE IF NOT EXISTS monitor_pruned_through (
                        source_chat_id INTEGER NOT NULL, target_chat_id INTEGER NOT NULL,
                        last_id INTEGER NOT NULL, PRIMARY KEY (source_chat_id, target_chat_id)
                    )
                """)
                # 此表只存尚未确认的发送；不参与定时清理。
                self.db.execute("""
                    CREATE TABLE IF NOT EXISTS monitor_copy_intents (
                        source_chat_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        target_chat_id INTEGER NOT NULL,
                        random_id INTEGER NOT NULL UNIQUE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (source_chat_id, source_message_id, target_chat_id)
                    )
                """)
        except Exception:
            self.db.close()
            raise

    def get(self, source_chat_id: int, source_message_id: int, target_chat_id: int) -> Optional[int]:
        row = self.db.execute(
            "SELECT target_message_id FROM monitor_messages "
            "WHERE source_chat_id = ? AND source_message_id = ? AND target_chat_id = ?",
            (source_chat_id, source_message_id, target_chat_id),
        ).fetchone()
        return row[0] if row else None

    def needs_copy(self, source_chat_id: int, source_message_id: int, target_chat_id: int) -> bool:
        if self.get(source_chat_id, source_message_id, target_chat_id) is not None:
            return False
        # 已过期帖不能因事件重放而成为“新帖”。每对频道只保留一个序号，不保留逐帖记录。
        return self.db.execute(
            "SELECT 1 FROM monitor_pruned_through WHERE source_chat_id=? AND target_chat_id=? AND last_id>=?",
            (source_chat_id, target_chat_id, source_message_id),
        ).fetchone() is None

    def cleanup_if_due(self, retention_days: int) -> int:
        """原子提交清理水位和最后成功时间，返回下次检查前的秒数。"""
        if retention_days <= 0:
            return MONITOR_CLEANUP_INTERVAL
        now = int(time.time())
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            last = self.db.execute("SELECT last_run FROM monitor_cleanup WHERE id=1").fetchone()
            if last and 0 <= now - last[0] < MONITOR_CLEANUP_INTERVAL:
                return MONITOR_CLEANUP_INTERVAL - (now - last[0])
            cutoff = now - retention_days * 86400
            self.db.execute("""
                INSERT INTO monitor_pruned_through (source_chat_id, target_chat_id, last_id)
                SELECT source_chat_id, target_chat_id, MAX(source_message_id) FROM monitor_messages
                WHERE confirmed_at <= ? GROUP BY source_chat_id, target_chat_id
                ON CONFLICT(source_chat_id, target_chat_id) DO UPDATE SET last_id=MAX(last_id, excluded.last_id)
            """, (cutoff,))
            removed = self.db.execute("DELETE FROM monitor_messages WHERE confirmed_at <= ?", (cutoff,)).rowcount
            self.db.execute("INSERT OR REPLACE INTO monitor_cleanup (id, last_run) VALUES (1, ?)", (now,))
        logger.info("monitor-data 清理完成：移除 %s 条已确认映射，保留期 %s 天", removed, retention_days)
        return MONITOR_CLEANUP_INTERVAL

    def reserve(self, source_chat_id: int, target_chat_id: int, ids: list[int]) -> dict[int, int]:
        """RPC 前提交意图；已有映射或未确认的意图都不能重新发送。"""
        reserved = {}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for mid in ids:
                key = (source_chat_id, mid, target_chat_id)
                if not self.needs_copy(*key):
                    continue
                nonce = secrets.randbits(63) or 1
                cursor = self.db.execute(
                    "INSERT INTO monitor_copy_intents (source_chat_id,source_message_id,target_chat_id,random_id) "
                    "VALUES (?,?,?,?) ON CONFLICT(source_chat_id,source_message_id,target_chat_id) DO NOTHING",
                    (*key, nonce),
                )
                if cursor.rowcount == 1:
                    reserved[mid] = nonce
                else:
                    logger.warning("复制结果待核对，不重复发送：source=%s/%s target=%s", *key)
        return reserved

    def release_rejected(self, source_id: int, target_id: int, reserved: dict[int, int]) -> None:
        # 只用于 Telegram 明确拒绝本次请求的情形，不用于网络中断或缺回执。
        with self.db:
            self.db.executemany(
                "DELETE FROM monitor_copy_intents WHERE source_chat_id=? AND source_message_id=? "
                "AND target_chat_id=? AND random_id=?",
                [(source_id, mid, target_id, nonce) for mid, nonce in reserved.items()],
            )

    def remember(self, source_chat_id: int, target_chat_id: int, pairs: dict[int, int]) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO monitor_messages (source_chat_id,source_message_id,target_chat_id,target_message_id,confirmed_at) "
                "VALUES (?, ?, ?, ?, ?)",
                [(source_chat_id, src, target_chat_id, dst, int(time.time())) for src, dst in pairs.items()],
            )
            self.db.executemany(
                "DELETE FROM monitor_copy_intents WHERE source_chat_id=? AND source_message_id=? AND target_chat_id=?",
                [(source_chat_id, mid, target_chat_id) for mid in pairs],
            )

    def close(self) -> None:
        self.db.close()


# ─────────────────────────── TDLDownloader ─────────────────────
class TDLDownloader:
    def __init__(self, tdl_path: str = TDL_PATH):
        self.tdl_path = tdl_path
        self.active_processes: set[asyncio.subprocess.Process] = set()

    @staticmethod
    def normalize_chat_id(to_chat_id: int) -> str:
        """剥掉 -100 前缀，tdl 接受裸 ID"""
        raw = str(to_chat_id).lstrip('-')
        if raw.startswith('100') and len(raw) > 10:
            raw = raw[3:]
        return raw

    @staticmethod
    def parse_message_link(link: str) -> tuple[Optional[int], Optional[int]]:
        """从 t.me 链接解析 chat_id 和 message_id
        支持格式:
        - https://t.me/username/123
        - https://t.me/c/123456789/123 (私有频道，数字 ID)
        - http://t.me/... 也支持
        - 带查询参数如 ?single 也支持
        返回的 chat_id 为链接中的原始数字，直接供 tdl 使用
        """
        # 去除可能的空白字符
        link = link.strip()
        # 去除查询参数
        if '?' in link:
            link = link.split('?')[0]
        logger.debug(f"解析链接: {link}")
        patterns = [
            r"https?://t\.me/([A-Za-z0-9_]+)/(\d+)/?",        # 公开频道
            r"https?://t\.me/c/(-?\d+)/(\d+)/?",              # 私有频道
        ]
        for pattern in patterns:
            match = re.match(pattern, link)
            if match:
                chat_part = match.group(1)
                msg_id = int(match.group(2))
                if chat_part.lstrip('-').isdigit():
                    # 链接中已是 tdl 需要的裸 ID，直接使用
                    chat_id = int(chat_part)
                    logger.debug(f"解析成功: chat_id={chat_id}, msg_id={msg_id}")
                else:
                    chat_id = None  # 需要用户名解析
                return chat_id, msg_id
        logger.warning(f"链接格式不匹配: {link}")
        return None, None

    async def _run_command(
        self,
        cmd: list[str],
        progress_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> tuple[int, str, str]:
        """执行命令并实时回调 stdout/stderr 输出。"""
        logger.info(f"执行 TDL 命令: {' '.join(cmd)}")
        stdout_lines: list[str] = []
        stderr_lines: list[str] = []

        async def read_stream(stream, collector: list[str], prefix: str = "") -> None:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode('utf-8', errors='ignore').strip()
                if not text:
                    continue
                collector.append(text)
                logger.info(f"TDL {prefix}: {text}")
                if progress_callback:
                    await progress_callback(text)

        process = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.active_processes.add(process)

        try:
            await asyncio.gather(
                read_stream(process.stdout, stdout_lines, "stdout"),
                read_stream(process.stderr, stderr_lines, "stderr"),
            )
            return_code = await process.wait()
            return return_code, "\n".join(stdout_lines).strip(), "\n".join(stderr_lines).strip()
        finally:
            self.active_processes.discard(process)

    async def stop_active_processes(self) -> int:
        processes = list(self.active_processes)
        if not processes:
            return 0

        stopped = 0
        for process in processes:
            if process.returncode is not None:
                self.active_processes.discard(process)
                continue
            try:
                process.terminate()
                stopped += 1
            except ProcessLookupError:
                self.active_processes.discard(process)
            except Exception as e:
                logger.error(f"停止 TDL 进程失败: {e}")

        deadline = time.time() + 5
        for process in processes:
            if process.returncode is not None:
                self.active_processes.discard(process)
                continue
            try:
                wait_time = max(0.1, deadline - time.time())
                await asyncio.wait_for(process.wait(), timeout=wait_time)
            except asyncio.TimeoutError:
                try:
                    process.kill()
                except ProcessLookupError:
                    pass
                except Exception as e:
                    logger.error(f"强制停止 TDL 进程失败: {e}")
            finally:
                self.active_processes.discard(process)
        return stopped

    async def export_range(
        self,
        chat_id: int,
        start_id: int,
        end_id: int,
        output_file: str,
        progress_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> tuple[bool, str]:
        """导出消息 ID 范围到文件"""
        ids_str = f"{start_id},{end_id}"
        chat_arg = self.normalize_chat_id(chat_id)
        cmd = [
            self.tdl_path, "chat", "export",
            "-c", chat_arg,
            "-T", "id",
            "-i", ids_str,
            "-o", output_file,
        ]
        try:
            return_code, out, err = await self._run_command(cmd, progress_callback)
            if return_code == 0 and os.path.exists(output_file):
                return True, out or f"导出成功: {output_file}"
            return False, err or out or "导出失败"
        except Exception as e:
            logger.error(f"TDL export range 执行异常: {e}")
            return False, str(e)

    async def forward_from_export(
        self,
        export_file: str,
        to_chat_id: int,
        progress_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> tuple[bool, str]:
        """从导出文件转发"""
        to_arg = self.normalize_chat_id(to_chat_id)
        cmd = [
            self.tdl_path, "forward",
            "--from", export_file,
            "--to", to_arg,
            "--mode", "clone",
            "--desc",
        ]
        try:
            return_code, out, err = await self._run_command(cmd, progress_callback)
            if return_code == 0:
                return True, out or "转发成功"
            return False, err or out or "未知错误"
        except Exception as e:
            logger.error(f"TDL forward 执行异常: {e}")
            return False, str(e)

    async def forward_clone(
        self,
        from_link: str,
        to_chat_id: int,
        progress_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> tuple[bool, str]:
        to_arg = self.normalize_chat_id(to_chat_id)
        cmd = [
            self.tdl_path, "forward",
            "--from", from_link,
            "--to", to_arg,
            "--mode", "clone",
        ]
        try:
            return_code, out, err = await self._run_command(cmd, progress_callback)
            if return_code == 0:
                return True, out or "转发成功"
            return False, err or out or "未知错误"
        except Exception as e:
            logger.error(f"TDL forward 执行异常: {e}")
            return False, str(e)


# ─────────────────────────── TelegramBot ───────────────────────
class TelegramBot:
    def __init__(self):
        self.client = TelegramClient(MemorySession(), API_ID, API_HASH)
        self.user_client: Optional[TelegramClient] = None
        self.user_monitor_running = False
        self.tdl = TDLDownloader()
        self.target_chat_id: Optional[int] = TARGET_CHAT_ID
        self.monitor_chat_ids: list[int] = MONITOR_CHAT_IDS
        self.forward_log = ForwardLog()
        self._album_processed: set[int] = set()
        self._forward_status_ids: dict[tuple[int, int], None] = {}
        self._monitor_message_map: Optional[MonitorMessageMap] = None
        self._monitor_cleanup_task: Optional[asyncio.Task] = None
        self.task_queue = TaskQueue(self)

    # ── Link utilities ──────────────────────────────────────────

    def extract_link(self, text: str) -> Optional[str]:
        if not text:
            return None
        match = TELEGRAM_LINK_PATTERN.search(text)
        if not match:
            return None
        link = match.group(0)
        return None if any(kw in link.lower() for kw in EXCLUDE_KEYWORDS) else link

    def build_message_link(self, chat, msg_id: int) -> str:
        username = getattr(chat, 'username', None)
        if username:
            return f"https://t.me/{username}/{msg_id}"
        chat_id = str(getattr(chat, 'id', '')).lstrip('-')
        if chat_id.startswith('100') and len(chat_id) > 10:
            chat_id = chat_id[3:]
        return f"https://t.me/c/{chat_id}/{msg_id}"

    async def resolve_link(self, event) -> Optional[str]:
        # 1. 转发来源自带链接
        if event.message.forward:
            fwd = event.message.forward

            if getattr(fwd, 'link', None):
                return fwd.link

            if hasattr(fwd, 'from_id') and fwd.from_id:
                try:
                    from_id = fwd.from_id
                    chat = await event.client.get_entity(from_id)

                    if hasattr(fwd, 'channel_post') and fwd.channel_post:
                        msg_id = fwd.channel_post
                    elif hasattr(fwd, 'message') and fwd.message:
                        msg_id = fwd.message
                    else:
                        msg_id = event.message.id

                    return self.build_message_link(chat, msg_id)
                except Exception as e:
                    logger.error(f"从转发构建链接失败: {e}")

        # 2. 消息文本中直接含链接
        link = self.extract_link(event.message.text or "")
        if link:
            return link

        # 3. 消息本身的链接属性
        if getattr(event.message, 'link', None):
            return event.message.link

        # 4. 根据 chat 信息构建
        chat = await event.get_chat()
        return self.build_message_link(chat, event.message.id)

    # ── Album handling ──────────────────────────────────────────

    def is_album(self, event) -> bool:
        return getattr(event.message, 'grouped_id', None) is not None

    def add_single_param(self, link: str) -> str:
        if "?single" not in link:
            link = link + "?single"
        return link

    @staticmethod
    def normalize_source_chat_id(chat_id: int) -> int:
        return utils.resolve_id(chat_id)[0]

    async def get_event_chat_ids(self, event) -> list[int]:
        ids: list[int] = []

        def add_id(value) -> None:
            if value is None:
                return
            try:
                chat_id = int(value)
            except (TypeError, ValueError):
                return
            if chat_id not in ids:
                ids.append(chat_id)

        add_id(getattr(event, 'chat_id', None))

        peer_id = getattr(event.message, 'peer_id', None)
        channel_id = getattr(peer_id, 'channel_id', None)
        if channel_id is not None:
            add_id(channel_id)
            add_id(f"-100{channel_id}")

        chat_id = getattr(peer_id, 'chat_id', None)
        if chat_id is not None:
            add_id(chat_id)
            add_id(-chat_id)

        try:
            chat = await event.get_chat()
            add_id(getattr(chat, 'id', None))
        except Exception as e:
            logger.debug(f"获取事件 Chat 信息失败: {e}")

        return ids

    def is_monitor_post(self, message) -> bool:
        return (
            bool(message.post)
            and isinstance(message.peer_id, types.PeerChannel)
            and self.has_monitor_chat_id(message.peer_id.channel_id)
        )

    def has_monitor_chat_id(self, chat_id: int) -> bool:
        normalized_chat_id = self.normalize_source_chat_id(chat_id)
        return any(self.normalize_source_chat_id(source_id) == normalized_chat_id for source_id in self.monitor_chat_ids)

    def add_monitor_chat_id(self, chat_id: int) -> bool:
        if self.has_monitor_chat_id(chat_id):
            return False
        self.monitor_chat_ids.append(chat_id)
        return True

    def remove_monitor_chat_id(self, chat_id: int) -> bool:
        normalized_chat_id = self.normalize_source_chat_id(chat_id)
        before = len(self.monitor_chat_ids)
        self.monitor_chat_ids = [
            source_id for source_id in self.monitor_chat_ids
            if self.normalize_source_chat_id(source_id) != normalized_chat_id
        ]
        return len(self.monitor_chat_ids) != before

    def format_monitor_chat_ids(self) -> str:
        if not self.monitor_chat_ids:
            return (
                "ℹ️ 尚未配置自动监听源频道\n\n"
                "用法：\n"
                "/monitor add <源频道裸ID>\n"
                "/monitor del <源频道裸ID>\n"
                "/monitor set <源频道裸ID1,源频道裸ID2>\n"
                "/monitor clear"
            )
        lines = ["📡 当前自动监听源频道：\n"]
        for chat_id in self.monitor_chat_ids:
            lines.append(f"- {chat_id}\n")
        if self.user_monitor_running:
            lines.append("\n监听客户端：用户账号 Telethon client\n")
        elif self.user_client:
            lines.append("\n监听客户端：用户账号未启动或未授权\n")
        else:
            lines.append("\n监听客户端：bot 推送监听\n")
        return "".join(lines)

    async def notify_admins(self, text: str) -> None:
        for admin_id in ADMIN_IDS:
            try:
                await self.client.send_message(admin_id, text)
            except Exception as e:
                logger.error(f"向管理员 {admin_id} 发送通知失败: {e}")

    async def _delete_message_later(self, message, delay: int) -> None:
        if not message or delay <= 0:
            return
        await asyncio.sleep(delay)
        try:
            await message.delete()
        except Exception as e:
            logger.debug(f"自动删除消息失败: {e}")

    def schedule_delete(self, message, delay: int) -> None:
        if not AUTO_DELETE_BOT_MESSAGES or not message or delay <= 0:
            return
        asyncio.create_task(self._delete_message_later(message, delay))

    def schedule_status_delete(self, message, failed: bool = False) -> None:
        delay = AUTO_DELETE_ERROR_SECONDS if failed else AUTO_DELETE_STATUS_SECONDS
        self.schedule_delete(message, delay)

    def schedule_command_delete(self, message) -> None:
        self.schedule_delete(message, AUTO_DELETE_COMMAND_SECONDS)

    def remember_forward_status(self, chat_id: int, message_id: int) -> None:
        self._forward_status_ids[(chat_id, message_id)] = None
        if len(self._forward_status_ids) > 1024:
            self._forward_status_ids.pop(next(iter(self._forward_status_ids)))

    def is_user_monitor_active(self) -> bool:
        return self.user_client is not None and self.user_monitor_running

    def monitor_target_channel_id(self) -> Optional[int]:
        if not self.target_chat_id:
            return None
        return utils.resolve_id(self.target_chat_id)[0]

    async def enqueue_monitor_post(self, event, messages, *, edited: bool = False) -> None:
        if not messages:
            return
        message = messages[0]
        if not self.is_monitor_post(message) or (event.chat_id, message.id) in self._forward_status_ids:
            return
        source_id = message.peer_id.channel_id
        target_id = self.monitor_target_channel_id()
        if target_id is None or target_id == source_id:
            logger.warning("跳过频道同步：未配置目标频道或目标就是源频道，source=%s", source_id)
            return
        # Album 处理完整的相册；NewMessage 不单独复制相册成员。
        ids = sorted({item.id for item in messages})
        await self.task_queue.add_task(
            event, f"https://t.me/c/{source_id}/{ids[0]}", None, event.chat_id,
            mode="monitor_edit" if edited else "monitor_copy", message_ids=ids,
            source_chat_id=source_id, target_chat_id=target_id,
        )

    def register_monitor_events(self, client: TelegramClient, *, fallback: bool = False) -> None:
        @client.on(events.Album)
        async def monitor_album(event):
            if not fallback or not self.is_user_monitor_active():
                await self.enqueue_monitor_post(event, event.messages)

        @client.on(events.MessageEdited)
        async def monitor_edit(event):
            if not fallback or not self.is_user_monitor_active():
                await self.enqueue_monitor_post(event, [event.message], edited=True)

    async def get_monitor_target_peer(self, client: TelegramClient, target_id: int):
        try:
            return await client.get_input_entity(types.PeerChannel(target_id))
        except (ValueError, errors.ChannelPrivateError):
            # 用户 session 初次运行时可能尚未缓存目标频道的 access hash。
            if client is not self.client:
                async for dialog in client.iter_dialogs():
                    peer = dialog.input_entity
                    if isinstance(peer, types.InputPeerChannel) and peer.channel_id == target_id:
                        return peer
            raise

    async def report_monitor_copy(self, text: str, *, failed: bool = False) -> None:
        if ADMIN_IDS:
            try:
                status_msg = await self.client.send_message(ADMIN_IDS[0], text)
                self.schedule_status_delete(status_msg, failed=failed)
            except Exception as exc:
                logger.debug("发送频道复制状态失败：%s", exc)

    @staticmethod
    async def edit_monitor_message(client, target_peer, target_msg_id, current) -> None:
        file = monitor_visual_media(current.media)
        if file is not None:
            target = await client.get_messages(target_peer, ids=target_msg_id)
            if target is None or isinstance(target, types.MessageEmpty):
                raise ValueError(f"目标消息 {target_msg_id} 已删除或不可读取，不能原地编辑")
            previous = monitor_visual_media(target.media)
            # 同一媒体重新读取时 file_reference 可以变化；仅改说明不重新挂载媒体。
            if (type(previous) is type(file) and previous.id.id == file.id.id and
                    bool(previous.spoiler) == bool(file.spoiler)):
                file = None
        try:
            await client.edit_message(
                target_peer, target_msg_id, text=current.raw_text or "", file=file,
                formatting_entities=current.entities or [], parse_mode=None,
                link_preview=isinstance(current.media, types.MessageMediaWebPage),
            )
        except errors.MessageNotModifiedError:
            pass

    async def process_monitor_task(self, task: ForwardTask) -> None:
        source_id, target_id = task.source_chat_id, task.target_chat_id
        message_ids = task.message_ids
        try:
            mappings = self.monitor_message_map()
            client = task.event.client

            if task.mode == "monitor_edit":
                source_msg_id = message_ids[0]
                target_msg_id = mappings.get(source_id, source_msg_id, target_id)
                if target_msg_id is None:
                    logger.info("跳过频道编辑（没有转发映射）：%s, target=%s", task.link, target_id)
                    task.status = TaskStatus.COMPLETED
                    return
                # 出队后取源帖最新版本，避免较旧的编辑事件覆盖新的正文或媒体。
                source_peer = await task.event.get_input_chat()
                current = await client.get_messages(source_peer, ids=source_msg_id)
                if current is None:
                    logger.info("跳过频道编辑（源帖已不可读取）：%s", task.link)
                    task.status = TaskStatus.COMPLETED
                    return
                target_peer = await self.get_monitor_target_peer(client, target_id)
                await self.edit_monitor_message(client, target_peer, target_msg_id, current)
                logger.info("已同步频道编辑：%s -> %s/%s", task.link, target_id, target_msg_id)
            else:
                pending = [mid for mid in message_ids if mappings.needs_copy(source_id, mid, target_id)]
                if not pending:
                    task.status = TaskStatus.COMPLETED
                    return
                source_peer = await task.event.get_input_chat()
                target_peer = await self.get_monitor_target_peer(client, target_id)
                reserved = mappings.reserve(source_id, target_id, pending)
                if not reserved:
                    task.status = (TaskStatus.UNCONFIRMED if any(mappings.needs_copy(source_id, mid, target_id)
                                                               for mid in pending) else TaskStatus.COMPLETED)
                    return
                try:
                    sent = await client(functions.messages.ForwardMessagesRequest(
                        from_peer=source_peer, id=list(reserved), random_id=list(reserved.values()),
                        to_peer=target_peer, drop_author=True,
                    ))
                except (errors.ChatWriteForbiddenError, errors.ChatForwardsRestrictedError,
                        errors.ChatAdminRequiredError, errors.ChannelPrivateError):
                    mappings.release_rejected(source_id, target_id, reserved)
                    raise
                except Exception as exc:
                    raise CopyOutcomeUnknown(f"发送未获确认（{type(exc).__name__}），请核对源消息 {list(reserved)}") from exc
                # Telegram 回传 random_id 到目标消息 ID 的映射；不能按相册顺序或频道最后一帖猜测。
                by_random_id = {update.random_id: update.id for update in getattr(sent, 'updates', ())
                                if isinstance(update, types.UpdateMessageID)}
                pairs = {mid: by_random_id[rid] for mid, rid in reserved.items() if rid in by_random_id}
                try:
                    mappings.remember(source_id, target_id, pairs)
                except sqlite3.Error as exc:
                    raise CopyOutcomeUnknown("目标 ID 未能持久化，请核对后处理") from exc
                unknown = [mid for mid in pending if mappings.get(source_id, mid, target_id) is None]
                if unknown:
                    raise CopyOutcomeUnknown(f"回执不完整；以下源消息待核对，不重发：{unknown}")
                self.forward_log.add(task.link, target_id, True)
                logger.info("已复制频道帖子并记录映射：%s -> %s, ids=%s", task.link, target_id, pairs)
                await self.report_monitor_copy(f"✅ 自动复制成功：{task.link} → {target_id}")
            task.status = TaskStatus.COMPLETED
        except CopyOutcomeUnknown as exc:
            task.status = TaskStatus.UNCONFIRMED
            task.error_msg = str(exc)
            logger.error("自动复制待核对：%s -> %s: %s", task.link, target_id, exc)
            self.forward_log.add(task.link, target_id, False, str(exc))
            await self.report_monitor_copy(f"⚠️ 自动复制结果待核对：{task.link} → {target_id}\n{exc}", failed=True)
        except Exception as exc:
            # 只重试编辑；重新出队会重读映射、最新正文和媒体引用，不执行任何复制请求。
            if task.mode == "monitor_edit" and task.edit_retries < 2:
                delay = None
                if isinstance(exc, errors.FloodWaitError):
                    delay = max(1, exc.seconds)
                elif isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError,
                                      errors.ServerError, errors.TimedOutError,
                                      errors.FileReferenceExpiredError, errors.FileReferenceInvalidError)):
                    delay = 2 ** (task.edit_retries + 1)
                if delay is not None:
                    self.task_queue.retry_edit_later(task, delay)
                    logger.warning("频道编辑 %s 秒后重试（%s/2）：%s", delay, task.edit_retries, task.link)
                    return
            task.error_msg = str(exc)
            task.status = TaskStatus.FAILED
            logger.error("频道同步失败（%s）：%s -> %s: %s", task.mode, task.link, target_id, exc, exc_info=True)
            if task.mode == "monitor_copy":
                self.forward_log.add(task.link, target_id, False, str(exc))
                await self.report_monitor_copy(f"⚠️ 自动复制失败：{task.link} → {target_id}\n{exc}", failed=True)

    async def start_user_monitor(self) -> None:
        if not USER_MONITOR_ENABLED:
            return
        try:
            if USER_SESSION_STRING and not USER_SESSION_STRING.startswith("填入"):
                session = StringSession(USER_SESSION_STRING)
            elif USER_SESSION_FILE:
                session = USER_SESSION_FILE
            else:
                logger.info("用户账号监听未启用：未配置用户 session")
                return
            self.user_client = TelegramClient(session, API_ID, API_HASH)
        except Exception as exc:
            # 异常文本可能包含输入内容或路径，只记录类型。
            logger.warning("用户 session 初始化失败（%s），回退到 Bot 监听", type(exc).__name__)
            await self.notify_admins("⚠️ 用户 session 无效或无法读取，已回退到 Bot 监听。")
            return

        @self.user_client.on(events.NewMessage)
        async def user_monitor_handler(event):
            try:
                if event.message.grouped_id is None:
                    await self.enqueue_monitor_post(event, [event.message])
            except Exception as e:
                logger.error(f"用户账号监听消息处理异常: {e}", exc_info=True)

        self.register_monitor_events(self.user_client)

        try:
            await self.user_client.connect()
            if not await self.user_client.is_user_authorized():
                logger.error(
                    "用户账号监听未启动：session 未授权。请先生成 USER_SESSION_STRING，"
                    "或配置已登录的 USER_SESSION_FILE。"
                )
                await self.notify_admins(
                    "⚠️ 用户账号监听未启动：session 未授权。\n"
                    "请先生成 USER_SESSION_STRING，或配置已登录的 USER_SESSION_FILE。"
                )
                await self.user_client.disconnect()
                return

            me = await self.user_client.get_me()
            self.user_monitor_running = True
            logger.info(f"用户账号监听已启动: id={me.id}, username={getattr(me, 'username', None)}")
        except Exception as e:
            logger.error("用户账号监听启动失败（%s），回退到 Bot 监听", type(e).__name__)
            await self.notify_admins("⚠️ 用户账号监听启动失败，已回退到 Bot 监听。")
            try:
                await self.user_client.disconnect()
            except Exception:
                pass

    # ── Core forward logic ──────────────────────────────────────

    async def do_forward(self, event, status_msg) -> None:
        status_chat_id = getattr(status_msg, 'chat_id', event.chat_id)
        self.remember_forward_status(status_chat_id, status_msg.id)
        target_id = self.target_chat_id
        if target_id is None:
            await status_msg.edit("❌ 未配置目标频道ID，请管理员使用 /forwardto 设置")
            self.schedule_status_delete(status_msg, failed=True)
            return

        is_album = self.is_album(event)
        if is_album:
            gid = getattr(event.message, 'grouped_id', None)
            if gid and gid in self._album_processed:
                logger.info(f"跳过 Album 后续消息: grouped_id={gid}")
                return
            if gid:
                self._album_processed.add(gid)
                if len(self._album_processed) > 500:
                    self._album_processed.clear()

        link = await self.resolve_link(event)

        if not link:
            await status_msg.edit("❌ 无法识别消息链接，请转发消息或发送 https://t.me/... 链接")
            self.schedule_status_delete(status_msg, failed=True)
            return

        if any(kw in link.lower() for kw in EXCLUDE_KEYWORDS):
            await status_msg.edit(f"❌ 链接包含排除关键词，已跳过: {link}")
            self.schedule_status_delete(status_msg, failed=True)
            return

        if is_album:
            link = self.add_single_param(link)
            logger.info(f"Album 消息添加 ?single 参数: {link}")

        gid = getattr(event.message, 'grouped_id', None)
        status_client = getattr(status_msg, 'client', None) or self.client
        position = await self.task_queue.add_task(
            event, link, status_msg.id, status_chat_id, gid,
            status_client=status_client, target_chat_id=target_id,
        )
        await status_msg.edit(
            f"⏳ 已加入转发队列（手动提交）\n"
            f"队列位置: {position}\n"
            f"FROM: {link}\n"
            f"TO:   {target_id}"
        )

    # ── Admin commands ──────────────────────────────────────────

    async def handle_admin_command(self, event) -> bool:
        if event.sender_id not in ADMIN_IDS:
            return False

        raw = event.message.text or ""
        parts = raw.split()
        command = parts[0].lower()

        if command == '/help':
            await event.respond(START_MESSAGE_ADMIN)
            return True

        original_respond = event.respond

        async def auto_delete_respond(*args, **kwargs):
            auto_delete = kwargs.pop("_auto_delete", True)
            message = await original_respond(*args, **kwargs)
            if auto_delete:
                self.schedule_command_delete(message)
            return message

        event.respond = auto_delete_respond

        if command == '/restart':
            await event.respond("🔄 正在重启机器人...")
            await self._restart()
            return True

        if command == '/forwardto':
            if len(parts) < 2:
                await event.respond(
                    "❌ 缺少目标频道 ID\n\n"
                    "用法：/forwardto <频道ID>\n"
                    "示例：/forwardto 1234567890\n\n"
                    "说明：频道 ID 使用 tdl 可识别的裸 ID，不需要加 -100 前缀；机器人和 tdl 账号需要具备发帖权限。"
                )
                return True
            new_id = parse_chat_id(parts[1])
            if new_id is None:
                await event.respond(
                    "❌ 频道 ID 格式无效\n\n"
                    "请只输入数字，不需要加 -100 前缀，例如：\n"
                    "/forwardto 1234567890"
                )
                return True
            self.target_chat_id = new_id
            await event.respond(
                f"✅ 目标频道已更新\n"
                f"当前目标频道 ID：{self.target_chat_id}\n\n"
                "之后收到的有效消息会转发到该频道。"
            )
            return True

        if command == '/showto':
            if self.target_chat_id is None:
                await event.respond(
                    "ℹ️ 尚未设置目标频道\n\n"
                    "请使用以下命令设置：\n"
                    "/forwardto 1234567890"
                )
            else:
                await event.respond(
                    f"🎯 当前目标频道 ID：{self.target_chat_id}\n\n"
                    "所有新加入队列的任务都会转发到该频道。"
                )
            return True

        if command == '/chatid':
            event_chat_ids = await self.get_event_chat_ids(event)
            lines = ["🆔 当前聊天/频道 ID：\n"]
            if event_chat_ids:
                for chat_id in event_chat_ids:
                    lines.append(f"- {chat_id}，裸 ID: {self.normalize_source_chat_id(chat_id)}\n")
            else:
                lines.append("无法获取 chat_id\n")
            lines.append("\n可用以下命令添加监听：\n")
            if event_chat_ids:
                lines.append(f"/monitor add {self.normalize_source_chat_id(event_chat_ids[0])}")
            else:
                lines.append("/monitor add <频道ID>")
            await event.respond("".join(lines))
            return True

        if command == '/tdlstop':
            stopped = await self.tdl.stop_active_processes()
            cleared = await self.task_queue.clear_pending_tasks()
            current_stopped = await self.task_queue.mark_current_task_stopped()
            await event.respond(
                f"⏹️ 已执行全局停止\n"
                f"停止中的 TDL 进程数：{stopped}\n"
                f"当前任务标记停止：{'是' if current_stopped else '否'}\n"
                f"清空等待任务数：{cleared}"
            )
            return True

        if command == '/monitor':
            if len(parts) == 1:
                await event.respond(self.format_monitor_chat_ids())
                return True

            action = parts[1].lower()
            if action == 'clear':
                self.monitor_chat_ids = []
                await event.respond("✅ 已清空自动监听源频道列表")
                return True

            if action in ('add', 'del', 'delete', 'remove'):
                if len(parts) < 3:
                    await event.respond(
                        "❌ 缺少频道 ID\n\n"
                        "用法：\n"
                        "/monitor add <源频道裸ID>\n"
                        "/monitor del <源频道裸ID>"
                    )
                    return True
                chat_id = parse_monitor_chat_id(parts[2])
                if chat_id is None:
                    await event.respond("❌ 监听源频道 ID 格式无效，请输入裸 ID，不要加 -100 前缀，例如：/monitor add 1234567890")
                    return True

                if action == 'add':
                    added = self.add_monitor_chat_id(chat_id)
                    if added:
                        await event.respond(f"✅ 已添加自动监听源频道：{chat_id}\n\n{self.format_monitor_chat_ids()}")
                    else:
                        await event.respond(f"ℹ️ 该频道已在监听列表中：{chat_id}")
                    return True

                removed = self.remove_monitor_chat_id(chat_id)
                if removed:
                    await event.respond(f"✅ 已删除自动监听源频道：{chat_id}\n\n{self.format_monitor_chat_ids()}")
                else:
                    await event.respond(f"ℹ️ 该频道不在监听列表中：{chat_id}")
                return True

            if action == 'set':
                if len(parts) < 3:
                    await event.respond(
                        "❌ 缺少频道 ID 列表\n\n"
                        "用法：/monitor set <源频道裸ID1,源频道裸ID2>\n"
                        "示例：/monitor set 1234567890,9876543210"
                    )
                    return True
                new_ids = parse_chat_ids(" ".join(parts[2:]).replace('，', ','))
                if not new_ids:
                    await event.respond("❌ 未解析到有效频道 ID，请检查格式")
                    return True
                deduped_ids = []
                for chat_id in new_ids:
                    normalized_chat_id = self.normalize_source_chat_id(chat_id)
                    if all(self.normalize_source_chat_id(existing_id) != normalized_chat_id for existing_id in deduped_ids):
                        deduped_ids.append(chat_id)
                self.monitor_chat_ids = deduped_ids
                await event.respond(f"✅ 已覆盖自动监听源频道列表\n\n{self.format_monitor_chat_ids()}")
                return True

            await event.respond(
                "❌ 未知 /monitor 子命令\n\n"
                "可用命令：\n"
                "/monitor\n"
                "/monitor add <源频道裸ID>\n"
                "/monitor del <源频道裸ID>\n"
                "/monitor set <源频道裸ID1,源频道裸ID2>\n"
                "/monitor clear"
            )
            return True

        if command == '/flog':
            if len(parts) > 1 and parts[1].lower() == 'clear':
                self.forward_log.clear()
                await event.respond("🗑️ 已清除所有转发记录")
                return True

            logs = self.forward_log.get_recent(10)
            if not logs:
                await event.respond("📝 暂无转发记录")
                return True

            lines = ["📝 转发记录（最近10条）：\n"]
            for log in logs:
                status = "✅" if log['success'] else "❌"
                entry = (
                    f"{status} #{log['id']}\n"
                    f"FROM: {log['from']}\n"
                    f"TO:   {log['to']}\n"
                    f"📅 {log['date']}\n"
                )
                if not log['success']:
                    entry += f"错误: {log['error']}\n"
                entry += "\n"
                if sum(len(l) for l in lines) + len(entry) > 3800:
                    break
                lines.append(entry)

            lines.append(f"共 {len(self.forward_log.get_all())} 条记录")
            await event.respond("".join(lines))
            return True

        if command == '/queue':
            status = self.task_queue.get_queue_status()
            queue_size = status["queue_size"]
            current_task = status["current_task"]

            if queue_size == 0 and not current_task:
                await event.respond("📋 转发队列为空，当前没有正在处理或等待中的任务。")
                return True

            lines = [
                "📋 转发队列状态：\n",
                f"运行状态：{'运行中' if status['running'] else '已停止'}\n",
                f"等待中：{queue_size}\n",
            ]
            if current_task:
                lines.extend([
                    "\n正在处理：\n",
                    f"状态：{current_task['status']}\n",
                    f"来源：{current_task['link']}\n",
                ])
            await event.respond("".join(lines))
            return True

        if command == '/export_range':
            chat_id, start_id, end_id, parse_error = parse_export_range_args(parts)
            if parse_error:
                error_tips = {
                    "missing": (
                        "❌ 缺少范围导出参数\n\n"
                        "用法 1：/export_range <Chat ID> <起始消息ID> <结束消息ID>\n"
                        "用法 2：/export_range <频道链接> <起始消息ID> <结束消息ID>\n"
                        "用法 3：/export_range <起始消息链接> <结束消息链接>\n\n"
                        "示例：\n"
                        "/export_range 3482276715 100 200\n"
                        "/export_range https://t.me/c/3482276715 100 200\n"
                        "/export_range https://t.me/c/3482276715/100 https://t.me/c/3482276715/200"
                    ),
                    "missing_range": (
                        "❌ 缺少起始或结束消息 ID\n\n"
                        "频道链接后面还需要填写起始消息 ID 和结束消息 ID。\n"
                        "示例：/export_range https://t.me/c/3482276715 100 200"
                    ),
                    "missing_end_id": (
                        "❌ 缺少结束消息 ID\n\n"
                        "使用消息链接作为起点时，需要再提供结束消息 ID 或结束消息链接。\n"
                        "示例：/export_range https://t.me/c/3482276715/100 200"
                    ),
                    "invalid_end_id": (
                        "❌ 结束消息 ID 格式错误\n\n"
                        "结束位置请填写数字，或填写同一频道的消息链接。\n"
                        "示例：/export_range https://t.me/c/3482276715/100 200"
                    ),
                    "invalid_range_id": (
                        "❌ 参数格式错误\n\n"
                        "Chat ID、起始消息 ID、结束消息 ID 必须是数字；也可以使用 t.me/c 私有频道链接。\n"
                        "示例：/export_range 3482276715 100 200"
                    ),
                    "chat_mismatch": (
                        "❌ 起始消息链接和结束消息链接不属于同一个频道\n\n"
                        "请确认两个链接中的 Chat ID 一致。"
                    ),
                    "public_link_not_supported": (
                        "❌ 公开用户名链接无法自动提取数字 Chat ID\n\n"
                        "请改用 t.me/c/<Chat ID>/<消息ID> 链接，或手动填写 Chat ID。\n"
                        "示例：/export_range 3482276715 100 200"
                    ),
                }
                await event.respond(error_tips.get(parse_error, "❌ 参数解析失败，请检查 /export_range 用法。"))
                return True

            if start_id > end_id:
                await event.respond(
                    "❌ 消息范围无效\n\n"
                    "起始消息 ID 不能大于结束消息 ID。\n"
                    "示例：/export_range 3482276715 100 200"
                )
                return True

            logger.info(f"处理 export_range 命令, chat_id: {chat_id}, range: {start_id}-{end_id}")

            # 用于显示的链接占位
            display_link = f"chat_id:{chat_id}"

            target_id = self.target_chat_id
            if target_id is None:
                await event.respond("❌ 未配置目标频道ID，请先使用 /forwardto 设置")
                return True
            status_msg = await event.respond("⏳ 正在加入范围导出队列...", _auto_delete=False)
            self.remember_forward_status(status_msg.chat_id, status_msg.id)
            gid = None
            position = await self.task_queue.add_task(
                event, display_link, status_msg.id, event.chat_id, gid,
                mode="export_range",
                message_ids=[start_id, end_id],
                source_chat_id=chat_id,
                status_client=getattr(status_msg, 'client', None) or self.client, target_chat_id=target_id,
            )
            await status_msg.edit(
                f"⏳ 已加入范围导出队列\n"
                f"队列位置: {position}\n"
                f"源: {display_link}\n"
                f"范围: {start_id}-{end_id}\n"
                f"TO: {target_id}"
            )
            return True

        return False

    # ── Restart ─────────────────────────────────────────────────

    async def _restart(self) -> None:
        script_path = os.path.abspath(sys.argv[0])
        work_dir = os.path.dirname(script_path)
        logger.info(f"重启机器人: {script_path}")
        try:
            await self.stop()
            with open("google_bot.log", "a") as log_file:
                subprocess.Popen(
                    [sys.executable, script_path],
                    cwd=work_dir,
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                )
            await asyncio.sleep(2)
            os._exit(0)
        except Exception as e:
            logger.error(f"重启失败: {e}")
            for admin_id in ADMIN_IDS:
                try:
                    await self.client.send_message(admin_id, f"⚠️ 重启失败: {e}")
                except Exception:
                    pass

    # ── Lifecycle ───────────────────────────────────────────────

    def monitor_message_map(self) -> MonitorMessageMap:
        if self._monitor_message_map is None:
            self._monitor_message_map = MonitorMessageMap(MONITOR_MAP_FILE)
        return self._monitor_message_map

    async def monitor_cleanup_loop(self) -> None:
        while True:
            try:
                delay = self.monitor_message_map().cleanup_if_due(MONITOR_MAP_RETENTION_DAYS)
            except (OSError, sqlite3.Error):
                logger.exception("monitor-data 清理失败，一小时后重试")
                delay = 3600
            await asyncio.sleep(delay)

    async def start(self) -> None:
        await self.client.start(bot_token=BOT_TOKEN)
        if MONITOR_MAP_RETENTION_DAYS and self._monitor_cleanup_task is None:
            self._monitor_cleanup_task = asyncio.create_task(self.monitor_cleanup_loop())
        await self.task_queue.start()
        await self.start_user_monitor()

        for admin_id in ADMIN_IDS:
            try:
                await self.client.send_message(admin_id, START_MESSAGE_ADMIN)
                logger.info(f"已向管理员 {admin_id} 发送启动消息")
            except Exception as e:
                logger.error(f"向管理员 {admin_id} 发送启动消息失败: {e}")

        @self.client.on(events.NewMessage(pattern='/start'))
        async def start_handler(event):
            if event.sender_id in ADMIN_IDS:
                await event.respond(START_MESSAGE_ADMIN)
            elif can_submit(event.sender_id):
                await event.respond("🤖 你可以转发消息或发送 Telegram 消息链接。")
            else:
                await event.respond("🤖 此机器人仅接受授权用户投稿。")

        @self.client.on(events.NewMessage)
        async def message_handler(event):
            try:
                text = event.message.text or ""
                logger.info(
                    f"收到消息: chat_id={event.chat_id}, msg_id={event.message.id}, "
                    f"sender_id={event.sender_id}, is_channel={event.is_channel}, "
                    f"post={getattr(event.message, 'post', None)}"
                )

                if text.split()[0].lower() == '/chatid' if text.split() else False:
                    event_chat_ids = await self.get_event_chat_ids(event)
                    lines = ["🆔 当前聊天/频道 ID：\n"]
                    if event_chat_ids:
                        for chat_id in event_chat_ids:
                            lines.append(f"- {chat_id}，裸 ID: {self.normalize_source_chat_id(chat_id)}\n")
                    else:
                        lines.append("无法获取 chat_id\n")
                    lines.append("\n可用以下命令添加监听：\n")
                    if event_chat_ids:
                        lines.append(f"/monitor add {event_chat_ids[0]}")
                    else:
                        lines.append("/monitor add <频道ID>")
                    msg = await event.respond("".join(lines))
                    self.schedule_command_delete(msg)
                    return

                if text.startswith('/'):
                    if await self.handle_admin_command(event):
                        return

                if self.is_monitor_post(event.message):
                    if not self.is_user_monitor_active() and event.message.grouped_id is None:
                        await self.enqueue_monitor_post(event, [event.message])
                    return

                if (isinstance(event.message.peer_id, types.PeerChannel)
                        and event.message.peer_id.channel_id == self.monitor_target_channel_id()):
                    return  # 不把复制到目标频道的链接帖当作手动转发请求。

                if event.out or not can_submit(event.sender_id):
                    return
                if event.message.forward or self.extract_link(text):
                    status_msg = await event.respond("⏳ 正在加入转发队列...")
                    await self.do_forward(event, status_msg)

            except Exception as e:
                logger.error(f"消息处理异常: {e}", exc_info=True)

        self.register_monitor_events(self.client, fallback=True)

        if self.monitor_chat_ids:
            logger.info(f"自动监听源频道已启用: {self.monitor_chat_ids}")
            if self.user_monitor_running:
                logger.info("源频道的新消息及编辑将由用户账号 Telethon client 监听，bot 只处理命令和状态消息")
            else:
                logger.info("用户账号监听未运行，将回退到 bot；bot 需要源频道读取权限及目标频道发帖和编辑权限")
        else:
            logger.info("自动监听源频道未配置")

        logger.info("Bot 已启动（TDL 集成模式）")
        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        self.user_monitor_running = False
        if self.user_client:
            try:
                await self.user_client.disconnect()
            except Exception as e:
                logger.error(f"断开用户监听客户端时出错: {e}")
        await self.task_queue.stop()
        if self._monitor_cleanup_task is not None:
            self._monitor_cleanup_task.cancel()
            try:
                await self._monitor_cleanup_task
            except asyncio.CancelledError:
                pass
            self._monitor_cleanup_task = None
        if self._monitor_message_map is not None:
            self._monitor_message_map.close()
            self._monitor_message_map = None
        try:
            await self.client.disconnect()
        except Exception as e:
            logger.error(f"断开连接时出错: {e}")


# ─────────────────────────── Entry ─────────────────────────────
async def main() -> None:
    missing = [name for name, valid in (("API_ID", API_ID > 0), ("API_HASH", API_HASH.strip()),
                                        ("BOT_TOKEN", BOT_TOKEN.strip())) if not valid]
    if missing:
        raise SystemExit(f"请在 .env 或运行环境中配置：{', '.join(missing)}")
    bot = TelegramBot()
    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("用户手动停止")
    except Exception as e:
        error_msg = str(e)
        if "FloodWaitError" in error_msg or "wait of" in error_msg.lower():
            match = re.search(r'wait of (\d+) seconds', error_msg)
            if match:
                wait_time = int(match.group(1))
                logger.error(f"Telegram 限流，需要等待 {wait_time} 秒 ({wait_time//60} 分钟)")
                logger.error("请等待限流结束后手动重启: docker-compose restart")
        else:
            logger.error(f"致命错误: {e}", exc_info=True)
    finally:
        await bot.stop()


if __name__ == "__main__":
    for f in ("bot_session.session",):
        try:
            os.remove(f)
        except FileNotFoundError:
            pass

    asyncio.run(main())


