import os
import re
import json
import logging
from logging.handlers import RotatingFileHandler
import asyncio
import time
import sys
from collections import deque
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qsl
import tempfile
import secrets
import signal
import socket
import sqlite3
from telethon import TelegramClient, errors, events, functions, types, utils
from telethon.sessions import MemorySession, StringSession
from typing import Awaitable, Callable, Optional
from dataclasses import dataclass
from enum import Enum

# ─────────────────────────── Logging ───────────────────────────
logger = logging.getLogger(__name__)


def configure_logging() -> None:
    handlers = [logging.StreamHandler()]
    try:
        handlers.append(RotatingFileHandler("bot.log", maxBytes=5 * 1024 * 1024, backupCount=3,
                                            encoding="utf-8", errors="backslashreplace"))
    except OSError as exc:
        file_error = type(exc).__name__
    else:
        file_error = None
    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(message)s', level=logging.INFO, handlers=handlers)
    logging.getLogger('telethon').setLevel(logging.WARNING)
    logging.getLogger('asyncio').setLevel(logging.WARNING)
    if file_error:
        logger.warning("无法打开 bot.log（%s），日志仅写入标准错误", file_error)


if __name__ == "__main__":
    configure_logging()

# ─────────────────────────── Config ────────────────────────────
try:
    API_ID = int(os.getenv("API_ID") or 0)
except ValueError:
    raise SystemExit("配置错误：API_ID 必须是正整数") from None
API_HASH = os.getenv("API_HASH", "")
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
USER_SESSION_STRING = os.getenv("USER_SESSION_STRING", "").strip()
USER_SESSION_FILE = os.getenv("USER_SESSION_FILE", "").strip()
USER_MONITOR_ENABLED = os.getenv("USER_MONITOR_ENABLED", "1").lower() not in ("0", "false", "no", "off")
TDL_PATH = "/usr/local/bin/tdl"
FORWARD_LOG_FILE = os.getenv("FORWARD_LOG_FILE") or "forward_history.json"
FORWARD_LOG_MAX_DAYS = 7  # 保留最近7天
MONITOR_MAP_FILE = os.getenv("MONITOR_MAP_FILE", "").strip() or "monitor_message_map.sqlite3"
MAX_CHANNEL_ID = 997_852_516_352  # MTProto channel/supergroup range.
MANUAL_ALBUM_CACHE_SIZE = 500
USER_MONITOR_RETRY_SECONDS = 30
MONITOR_CLEANUP_INTERVAL = 7 * 86400
try:
    MONITOR_MAP_RETENTION_DAYS = int(os.getenv("MONITOR_MAP_RETENTION_DAYS") or 7)
    if not 0 <= MONITOR_MAP_RETENTION_DAYS <= 36500:
        raise ValueError
except ValueError:
    raise SystemExit("MONITOR_MAP_RETENTION_DAYS 必须是 0 到 36500 之间的整数（0 表示不清理）") from None
TELEGRAM_LINK_PATTERN = re.compile(
    r"https://t\.me/(?:c/[1-9][0-9]*|(?!c/)[A-Za-z0-9_]+)/[1-9][0-9]*(?=/(?:[?#\s]|$)|(?![A-Za-z0-9_/%-]))"
)
EXCLUDE_KEYWORDS = ["addlist"]
AUTO_DELETE_BOT_MESSAGES = os.getenv("AUTO_DELETE_BOT_MESSAGES", "1").lower() not in ("0", "false", "no", "off")


def delete_delay(name: str, default: int) -> int:
    value = os.getenv(name, '').strip()
    if not value:
        return default
    if not re.fullmatch(r'[0-9]{1,10}', value) or int(value) > 2_147_483_647:
        raise SystemExit(f"配置错误：{name} 必须是 0 到 2147483647 之间的秒数（0 表示不删除）")
    return int(value)


AUTO_DELETE_STATUS_SECONDS = delete_delay("AUTO_DELETE_STATUS_SECONDS", 300)
AUTO_DELETE_COMMAND_SECONDS = delete_delay("AUTO_DELETE_COMMAND_SECONDS", 120)
AUTO_DELETE_ERROR_SECONDS = delete_delay("AUTO_DELETE_ERROR_SECONDS", 600)


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


@dataclass(frozen=True)
class RejectedCopy:
    # InputPeer access hashes and random IDs must stay with the account that made the request.
    client: TelegramClient
    request: functions.messages.ForwardMessagesRequest


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
    retries: int = 0
    edit_dirty: bool = False
    # Only set while waiting after an explicit RPC rejection, never during an in-flight send.
    rejected_copy: Optional[RejectedCopy] = None

    @property
    def edit_key(self):
        return (self.source_chat_id, self.message_ids[0], self.target_chat_id)


async def edit_status(message, text: str) -> None:
    if message is None:
        return
    try:
        await message.edit(text)
    except Exception as exc:
        logger.debug("状态提示未更新：%s", type(exc).__name__)


async def send_status(event, text: str):
    try:
        return await event.respond(text)
    except Exception as exc:
        logger.warning("无法发送状态提示（%s），不影响任务执行", type(exc).__name__)
        return None


class TaskQueue:
    def __init__(self, bot_instance):
        self.bot = bot_instance
        self.queue: asyncio.Queue[ForwardTask] = asyncio.Queue()
        self.current_task: Optional[ForwardTask] = None
        self.worker_task: Optional[asyncio.Task] = None
        self.running = False
        self._control = asyncio.Lock()
        self._resume = asyncio.Event()
        self._resume.set()
        self.task_counter = 0
        self._retry_timers: dict[asyncio.TimerHandle, ForwardTask] = {}
        self._edits: dict[tuple[int, int, int], ForwardTask] = {}

    async def start(self):
        async with self._control:
            if self.worker_task is not None and not self.worker_task.done():
                return
            self.running = True
            self.worker_task = asyncio.create_task(self._worker())
            logger.info("任务队列已启动")

    async def stop(self):
        async with self._control:
            self.running = False
            if self.worker_task and not self.worker_task.done():
                self.worker_task.cancel()
                await asyncio.gather(self.worker_task, return_exceptions=True)
            await self.clear_pending_tasks(notify=False)
            logger.info("任务队列已停止")

    async def _get_status(self, task: ForwardTask):
        if task.status_msg_id is not None:
            try:
                client = task.status_client or task.event.client
                return await client.get_messages(task.chat_id, ids=task.status_msg_id)
            except Exception as exc:
                logger.debug("无法读取状态提示：%s", type(exc).__name__)
        return None

    def retry_later(self, task: ForwardTask, delay: float) -> None:
        # 不让单帖的 FloodWait 阻塞其他已排队的任务。
        def requeue():
            self._retry_timers.pop(timer, None)
            self.queue.put_nowait(task)

        task.retries += 1
        task.status = TaskStatus.PENDING
        timer = asyncio.get_running_loop().call_later(delay, requeue)
        self._retry_timers[timer] = task

    def _take_pending(self) -> list[ForwardTask]:
        pending = list(self._retry_timers.values())
        for timer in self._retry_timers:
            timer.cancel()
        self._retry_timers.clear()
        while not self.queue.empty():
            pending.append(self.queue.get_nowait())
            self.queue.task_done()
        if self.current_task:
            self.current_task.edit_dirty = False
        for task in pending:
            if task.mode == "monitor_edit":
                self._edits.pop(task.edit_key, None)
        return pending

    async def clear_pending_tasks(self, *, notify: bool = True) -> int:
        return await self._finish_pending(self._take_pending(), notify)

    async def _finish_pending(self, pending, notify: bool) -> int:
        for task in pending:
            task.status = TaskStatus.FAILED
            task.error_msg = "已停止并清空等待队列"
            if task.rejected_copy is not None:
                try:
                    await self.bot.release_rejected_request(task, task.rejected_copy.request)
                except (sqlite3.Error, OSError):
                    # Keep the reservation; never replay or clear unrelated unknown outcomes.
                    task.error_msg += "；未能释放发送记录，需人工核对"
                    logger.exception("停止任务时释放发送记录失败：%s -> %s", task.link, task.target_chat_id)
                else:
                    task.rejected_copy = None
            if notify:
                msg = await self._get_status(task)
                await edit_status(msg, "⏹️ " + task.error_msg)
                self.bot.schedule_status_delete(msg, failed=True)
        return len(pending)

    async def stop_tdl_tasks(self) -> tuple[bool, int]:
        async with self._control:
            # No await until the worker, its current task and all previously queued work are fixed.
            self._resume.clear()
            worker, current = self.worker_task, self.current_task
            pending = self._take_pending()
            manual = current is not None and current.mode in ("clone", "export_range") and current.status == TaskStatus.PROCESSING
            cancel_worker = worker is not None and not worker.done() and (current is None or manual)
            try:
                if cancel_worker:
                    worker.cancel()
                    await asyncio.gather(worker, return_exceptions=True)  # Includes child-process cleanup.
                if manual:
                    current.status, current.error_msg = TaskStatus.FAILED, "管理员已停止 TDL 任务"
                    msg = await self._get_status(current)
                    await edit_status(msg, "⏹️ 当前 TDL 任务已被管理员停止")
                    self.bot.schedule_status_delete(msg, failed=True)
                cleared = await self._finish_pending(pending, True)
                return manual, cleared
            finally:
                self._resume.set()
                if cancel_worker and self.running:
                    self.worker_task = asyncio.create_task(self._worker())

    async def add_task(self, event, link: str, status_msg_id: Optional[int], chat_id: int,
                       grouped_id: Optional[int] = None, mode: str = "clone",
                       message_ids: Optional[list[int]] = None, source_chat_id: Optional[int] = None,
                       status_client: Optional[TelegramClient] = None, *, target_chat_id: int) -> Optional[int]:
        if self.bot._stopping:
            return None  # The queue may already be drained and its worker gone.
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
        if mode == "monitor_edit":
            existing = self._edits.get(task.edit_key)
            if existing is not None:
                existing.event = event
                if existing.status == TaskStatus.PROCESSING:
                    existing.edit_dirty = True
                return self.queue.qsize()
            self._edits[task.edit_key] = task
        self.queue.put_nowait(task)
        position = self.queue.qsize()
        logger.info(f"任务已加入队列，位置: {position}, mode: {mode}")
        return position

    async def _worker(self):
        while self.running:
            try:
                await self._resume.wait()
                task = await self.queue.get()
                self.current_task = task
                task.status = TaskStatus.PROCESSING
                try:
                    await self._process_task(task)
                except Exception as exc:
                    task.status, task.error_msg = TaskStatus.FAILED, str(exc)
                    logger.exception("队列处理异常")
                finally:
                    self.queue.task_done()
                    self.current_task = None
            except asyncio.CancelledError:
                break

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

    async def _process_task(self, task: ForwardTask):
        if task.mode == "monitor_edit":
            task.status = TaskStatus.PROCESSING
            task.edit_dirty = False
            try:
                await self.bot.process_monitor_task(task)
            except BaseException:
                self._edits.pop(task.edit_key, None)
                raise
            if task.status != TaskStatus.PENDING:  # A delayed retry already reads the latest source.
                if task.edit_dirty:
                    task.status, task.retries = TaskStatus.PENDING, 0
                    self.queue.put_nowait(task)
                else:
                    self._edits.pop(task.edit_key, None)
            return
        if task.mode == "monitor_copy":
            await self.bot.process_monitor_task(task)
            return

        msg = await self._get_status(task)
        target = task.target_chat_id
        details = [f"源：{task.link}", f"目标：{target}"]
        title = "正在转发"

        last_update = None

        async def progress(line):
            nonlocal last_update
            now = time.monotonic()
            if msg is None or (last_update is not None and now - last_update < 2):
                return
            last_update = now
            text = "\n".join([title, *details, "", "进度输出：", self._clean_progress_line(line)])
            await edit_status(msg, text[:3900])

        try:
            if target is None:
                raise ValueError("未配置目标频道 ID")
            if task.mode == "clone":
                await edit_status(msg, "\n".join([title, *details]))
                ok, output = await self.bot.tdl.forward_clone(task.link, target, progress)
            elif task.mode == "export_range":
                first, last = task.message_ids
                details.append(f"范围：{first}-{last}")
                with tempfile.TemporaryDirectory() as directory:
                    export_file = os.path.join(directory, "export.json")
                    title = "正在导出消息范围"
                    await edit_status(msg, "\n".join([title, *details]))
                    ok, output = await self.bot.tdl.export_range(task.source_chat_id, first, last, export_file, progress)
                    if ok:
                        title = "正在转发导出文件"
                        await edit_status(msg, "\n".join([title, *details]))
                        ok, output = await self.bot.tdl.forward_from_export(export_file, target, progress)
            else:
                raise ValueError(f"未知任务类型：{task.mode}")
        except Exception as exc:
            logger.exception("TDL 任务执行异常")
            ok, output = False, str(exc)

        # Finalize delivery before touching Telegram notifications. A deleted status is not a cancellation.
        task.status = TaskStatus.COMPLETED if ok else TaskStatus.FAILED
        task.error_msg = "" if ok else output
        self.bot.forward_log.add(task.link, target, ok, task.error_msg)
        await edit_status(msg, "\n".join(["✅ 任务成功" if ok else "❌ 任务失败", *details, self._format_last_output(output)]))
        self.bot.schedule_status_delete(msg, failed=not ok)

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
    '  设置或修改目标频道，可使用频道裸 ID 或 Telegram 标记后的频道 ID\n'
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
    '  停止当前手动 TDL 任务，清空当时的等待队列和定时重试；不强停在途自动同步\n\n'
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
def parse_admin_ids(raw: str, name: str = 'ADMIN_IDS') -> list[int]:
    result = {}
    for item in raw.split(','):
        item = item.strip()
        if not item:
            continue
        if not re.fullmatch(r'[0-9]{1,19}', item) or not 0 < int(item) < 2**63:
            logger.warning("忽略 %s 中的无效用户 ID（须为正整数）", name)
            continue
        result[int(item)] = None  # Keep notification order without notifying twice.
    return list(result)


def parse_chat_id(text: str) -> Optional[int]:
    value = text.strip()
    if not value.startswith('-'):
        return parse_monitor_chat_id(value)
    if not re.fullmatch(r'-[0-9]{13}', value):
        return None
    marked = int(value)
    bare, peer_type = utils.resolve_id(marked)
    return marked if peer_type is types.PeerChannel and 1 <= bare <= MAX_CHANNEL_ID else None


def targets_submessage(query: str) -> bool:
    return any(key.lower() in {'comment', 'thread'} for key, _ in parse_qsl(query, keep_blank_values=True))


def parse_private_channel_link(text: str) -> tuple[Optional[int], Optional[int]]:
    """Return the bare channel and optional post. A comment/thread is not its parent post."""
    path, _, query = text.partition('?')
    if targets_submessage(query):
        return None, None
    match = re.fullmatch(r"https?://t\.me/c/([1-9][0-9]{0,18})(?:/([0-9]{1,10}))?/?", path)
    if not match:
        return None, None
    return int(match[1]), int(match[2]) if match[2] is not None else None


def parse_export_range_args(parts: list[str]) -> tuple[Optional[int], Optional[int], Optional[int], Optional[str]]:
    """Accept exactly a chat + two IDs, or a starting post + one ID/post in the same chat."""
    def invalid(reason):
        return None, None, None, reason

    def message_id(value):
        return int(value) if re.fullmatch(r'[0-9]{1,10}', value) else None

    args = parts[1:]
    if not args:
        return invalid('missing')
    source = args[0]
    if source.startswith(('http://', 'https://')):
        chat_id, start = parse_private_channel_link(source)
        if chat_id is None:
            return invalid('invalid_source_link')
    else:
        chat_id = int(source) if re.fullmatch(r'-?[0-9]{1,19}', source) else None
        start = None
    if not chat_id or not -(2**63) <= chat_id < 2**63:
        return invalid('invalid_range_id')

    if start is None:
        if len(args) != 3:
            return invalid('missing_range' if len(args) < 3 else 'extra_args')
        start, end = message_id(args[1]), message_id(args[2])
    else:
        if len(args) != 2:
            return invalid('missing_end_id' if len(args) < 2 else 'extra_args')
        if args[1].startswith(('http://', 'https://')):
            end_chat, end = parse_private_channel_link(args[1])
            if end is None:
                return invalid('invalid_end_id')
            if end_chat != chat_id:
                return invalid('chat_mismatch')
        else:
            end = message_id(args[1])
    if not all(mid is not None and 1 <= mid <= 2**31 - 1 for mid in (start, end)):
        return invalid('invalid_range_id')
    if start > end:
        return invalid('reversed_range')
    return chat_id, start, end, None


def parse_monitor_chat_id(text: str) -> Optional[int]:
    value = text.strip()
    if not re.fullmatch(r'[0-9]{1,12}', value):
        return None
    chat_id = int(value)
    return chat_id if 1 <= chat_id <= MAX_CHANNEL_ID else None


def parse_chat_ids(raw: str) -> list[int]:
    result = []
    for item in raw.split(','):
        chat_id = parse_monitor_chat_id(item)
        if chat_id is None:
            if item.strip():
                logger.warning(f"忽略无效监听源频道裸ID: {item}")
            continue
        result.append(chat_id)
    return list(dict.fromkeys(result))


ADMIN_IDS: list[int] = parse_admin_ids(os.getenv("ADMIN_IDS", ""))
SUBMITTER_IDS = set(parse_admin_ids(os.getenv("SUBMITTER_IDS", ""), "SUBMITTER_IDS"))


def can_submit(sender_id: Optional[int]) -> bool:
    return bool(sender_id and sender_id > 0 and (sender_id in ADMIN_IDS or sender_id in SUBMITTER_IDS))

TARGET_CHAT_ID: Optional[int] = parse_chat_id(os.getenv("FORWARD_TO_CHAT_ID", ""))
if os.getenv("FORWARD_TO_CHAT_ID", "").strip() and TARGET_CHAT_ID is None:
    logger.warning("FORWARD_TO_CHAT_ID 不是有效频道 ID；设置有效目标前不会转发")
MONITOR_CHAT_IDS: list[int] = parse_chat_ids(os.getenv("MONITOR_CHAT_IDS", ""))


# ─────────────────────────── ForwardLog ────────────────────────
class ForwardLog:
    def __init__(self, log_file: str = FORWARD_LOG_FILE):
        self.log_file = Path(log_file)
        self.writable = True
        self.logs: list[dict] = self._load()
        self._last_id = max((int(r['id']) for r in self.logs if re.fullmatch(r'[0-9]{1,20}', str(r['id']))), default=0)

    @staticmethod
    def _valid_row(row) -> bool:
        if not isinstance(row, dict):
            return False
        timestamp = row.get('timestamp')
        return (type(timestamp) in (int, float) and 0 <= timestamp < float('inf')
                and type(row.get('success')) is bool and type(row.get('to')) is int
                and type(row.get('id')) in (int, str)
                and isinstance(row.get('from'), str) and isinstance(row.get('date'), str)
                and isinstance(row.get('error', '' if row['success'] else None), str))

    def _load(self) -> list[dict]:
        try:
            text = self.log_file.read_text(encoding="utf-8-sig")
            if not text.strip():
                return []
            rows = json.loads(text)
            if not isinstance(rows, list) or not all(self._valid_row(row) for row in rows):
                raise ValueError("转发历史格式不正确")
            return rows
        except FileNotFoundError:
            return []
        except (UnicodeError, ValueError):
            backup = self.log_file.with_name(f"{self.log_file.name}.corrupt-{secrets.token_hex(8)}")
            try:
                self.log_file.rename(backup)
                logger.warning("转发历史已损坏，原件已保留在 %s", backup)
            except OSError:
                self.writable = False
                logger.exception("无法备份损坏的转发历史；为保护原件，已停用历史写入")
        except OSError:
            self.writable = False
            logger.exception("无法读取转发历史；为保护原件，已停用历史写入")
        return []

    def _prune(self) -> None:
        if FORWARD_LOG_MAX_DAYS > 0:
            cutoff = time.time() - FORWARD_LOG_MAX_DAYS * 86400
            self.logs = [row for row in self.logs if row['timestamp'] > cutoff]

    def _save(self) -> bool:
        self._prune()
        if not self.writable:
            return False
        tmp = None
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.log_file.parent,
                                             prefix=".forward-history-", delete=False) as f:
                tmp = Path(f.name)
                json.dump(self.logs, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, self.log_file)
            return True
        except (OSError, ValueError, TypeError):
            logger.exception("保存转发历史失败，原文件未被截断")
            return False
        finally:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    logger.warning("无法清除转发历史临时文件：%s", tmp)

    def add(self, from_link: str, to_chat_id: int, success: bool, error_msg: str = "") -> str:
        self._last_id += 1
        record_id = str(self._last_id)
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
        self._prune()
        return self.logs

    def get_recent(self, count: int = 10) -> list[dict]:
        return self.get_all()[-count:] if count > 0 else []

    def clear(self) -> bool:
        previous, self.logs = self.logs, []
        if self._save():
            self._last_id = 0
            return True
        self.logs = previous
        return False


def format_recent_history(rows: list[dict], total: int) -> str:
    def clip(value, limit: int, *, tail=False) -> str:
        text = str(value)
        data = text.encode('utf-16-le', errors='replace')
        if len(data) <= 2 * limit:
            return text
        size = (limit - 1) * 2
        if tail:
            return '…' + data[-size:].decode('utf-16-le', errors='ignore')
        return data[:size].decode('utf-16-le', errors='ignore') + '…'

    if not rows:
        return '📝 暂无转发记录'
    # Reserve room for every row; a single long failure must not hide newer deliveries.
    rows = rows[-10:]
    lines = ['📝 转发记录（最新在前）：']
    for row in reversed(rows):
        entry = (f"{'✅' if row['success'] else '❌'} #{clip(row['id'], 24)}\n"
                 f"FROM: {clip(row['from'], 110)}\nTO:   {clip(row['to'], 24)}\n📅 {clip(row['date'], 24)}")
        if not row['success']:
            entry += '\n错误: ' + clip(row['error'], 1500 // len(rows), tail=True)
        lines.append(entry)
    lines.append(f'共 {total} 条记录')
    return '\n\n'.join(lines)


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

    def copy_state(self, source_chat_id: int, source_message_id: int, target_chat_id: int) -> TaskStatus:
        # A missing receipt is still unresolved even if a higher message ID has since expired.
        row = self.db.execute("""
            SELECT CASE
                WHEN EXISTS (SELECT 1 FROM monitor_messages WHERE source_chat_id=:src
                    AND source_message_id=:mid AND target_chat_id=:dst) THEN 'completed'
                WHEN EXISTS (SELECT 1 FROM monitor_copy_intents WHERE source_chat_id=:src
                    AND source_message_id=:mid AND target_chat_id=:dst) THEN 'unconfirmed'
                WHEN EXISTS (SELECT 1 FROM monitor_pruned_through WHERE source_chat_id=:src
                    AND target_chat_id=:dst AND last_id>=:mid) THEN 'completed'
                ELSE 'pending' END
        """, {"src": source_chat_id, "mid": source_message_id, "dst": target_chat_id}).fetchone()
        return TaskStatus(row[0])

    def batch_copy_state(self, source_id: int, target_id: int, ids: list[int]) -> TaskStatus:
        states = {self.copy_state(source_id, mid, target_id) for mid in ids}
        if TaskStatus.PENDING in states:
            return TaskStatus.PENDING  # At least one new member can be sent.
        if TaskStatus.UNCONFIRMED in states:
            logger.warning("复制结果待核对，不重发：source=%s/%s target=%s", source_id, ids, target_id)
            return TaskStatus.UNCONFIRMED
        return TaskStatus.COMPLETED

    def cleanup_if_due(self, retention_days: int) -> int:
        """原子提交清理水位和最后成功时间，返回下次检查前的秒数。"""
        if retention_days <= 0:
            return MONITOR_CLEANUP_INTERVAL
        started = time.monotonic()
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
        logger.info("monitor-data 清理完成：移除 %s 条已确认映射，保留期 %s 天，耗时 %.3f 秒",
                    removed, retention_days, time.monotonic() - started)
        return MONITOR_CLEANUP_INTERVAL

    def reserve(self, source_chat_id: int, target_chat_id: int, ids: list[int]) -> dict[int, int]:
        """RPC 前提交意图；已有映射或未确认的意图都不能重新发送。"""
        reserved = {}
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            for mid in ids:
                key = (source_chat_id, mid, target_chat_id)
                if self.copy_state(*key) != TaskStatus.PENDING:
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
        """Keep bare IDs intact; remove Telegram's numeric peer mark only for marked IDs."""
        return str(utils.resolve_id(to_chat_id)[0])

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
        # One bounded diagnostic tail per pipe. TDL progress may use CR or no newline at all.
        stdout_lines, stderr_lines = deque(maxlen=16), deque(maxlen=16)

        async def read_stream(stream, collector, prefix: str) -> None:
            tail = b""
            while True:
                chunk = await stream.read(8192)
                # Split bytes, then decode complete records so chunk boundaries don't break UTF-8.
                parts = (tail + chunk).replace(b"\r", b"\n").split(b"\n")
                tail = parts.pop()[-2048:] if chunk else b""
                for line in parts:
                    text = line[-2048:].decode("utf-8", errors="replace").strip()
                    if text:
                        collector.append(text)
                        logger.debug("TDL %s: %s", prefix, text)
                        if progress_callback:
                            await progress_callback(text)
                if not chunk:
                    return

        # Shield creation: cancellation may arrive after the OS has spawned the child but before
        # asyncio hands us its Process. In that case still take ownership and reap it.
        spawn = asyncio.create_task(asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        ))
        readers = []
        try:
            process = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            try:
                process = await spawn
            except Exception as exc:
                # No child to reap. Do not turn cancellation into a normal TDL failure:
                # /tdlstop is waiting for this worker to exit, not to fetch another task.
                logger.debug("已取消的进程未启动：%s", type(exc).__name__)
            else:
                self.active_processes.add(process)
                await self._reap(process, readers)
            raise
        self.active_processes.add(process)
        try:
            readers = [asyncio.create_task(read_stream(process.stdout, stdout_lines, "stdout")),
                       asyncio.create_task(read_stream(process.stderr, stderr_lines, "stderr"))]
            await asyncio.gather(*readers)
            code = await process.wait()
            return code, "\n".join(stdout_lines).strip(), "\n".join(stderr_lines).strip()
        finally:
            cleanup = asyncio.create_task(self._reap(process, readers))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise

    async def _reap(self, process, readers) -> None:
        for reader in readers:
            reader.cancel()
        await asyncio.gather(*readers, return_exceptions=True)

        async def drain(stream):
            while await stream.read(65536):
                pass

        # Keep draining even after cancellation, so a full OS pipe cannot deadlock wait().
        drains = asyncio.gather(drain(process.stdout), drain(process.stderr), return_exceptions=True)
        try:
            if process.returncode is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(process.wait(), timeout=5)
                except asyncio.TimeoutError:
                    try:
                        process.kill()
                    except ProcessLookupError:
                        pass
            await process.wait()
        finally:
            await drains
            if process.returncode is not None:
                self.active_processes.discard(process)

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
        self.client = TelegramClient(MemorySession(), API_ID, API_HASH, flood_sleep_threshold=0)
        self.user_client: Optional[TelegramClient] = None
        self.user_monitor_running = False
        self._user_monitor_watch: Optional[asyncio.Task] = None
        self.tdl = TDLDownloader()
        self.target_chat_id: Optional[int] = TARGET_CHAT_ID
        self.monitor_chat_ids: list[int] = MONITOR_CHAT_IDS.copy()
        self.forward_log = ForwardLog()
        self._pending_albums: set[tuple[int, int]] = set()
        self._album_processed: dict[tuple[int, int], None] = {}
        self._forward_status_ids: dict[tuple[int, int], None] = {}
        self._stopping = False
        self._delete_tasks: set[asyncio.Task] = set()
        self.restart_requested = False
        self._bot_handlers_registered = False
        self._bot_username: Optional[str] = None
        self._monitor_db_lock = asyncio.Lock()
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
        # Never strip a routing query and silently submit the parent post instead.
        query = re.match(r"/?\?([^\s#]*)", text[match.end():])
        if query and targets_submessage(query[1]):
            return None
        link = match.group(0)
        return None if any(kw in link.lower() for kw in EXCLUDE_KEYWORDS) else link

    async def resolve_link(self, event) -> Optional[str]:
        message = event.message
        fwd = message.forward
        if fwd:
            peer, mid = getattr(fwd, 'from_id', None), getattr(fwd, 'channel_post', None)
            if isinstance(peer, types.PeerChannel) and peer.channel_id > 0 and isinstance(mid, int) and mid > 0:
                # PeerChannel already contains the bare source ID. Do not use the receiving chat's ID.
                # The cached username is optional; resolving it over the network is unnecessary.
                username = getattr(getattr(fwd, 'chat', None), 'username', None)
                return f"https://t.me/{username}/{mid}" if username else f"https://t.me/c/{peer.channel_id}/{mid}"
        return self.extract_link(message.text or "")

    @staticmethod
    def normalize_source_chat_id(chat_id: int) -> int:
        return utils.resolve_id(chat_id)[0]

    @staticmethod
    def format_chat_id(event) -> str:
        peer = event.message.peer_id
        chat_id = event.chat_id
        if chat_id is None:
            return "无法获取当前聊天 ID"
        text = f"🆔 当前聊天/频道 ID：{chat_id}"
        # Only broadcast posts can be monitored. Telethon also uses PeerChannel for megagroups.
        if isinstance(peer, types.PeerChannel) and event.message.post:
            text += f"\n频道裸 ID：{peer.channel_id}\n\n可用以下命令添加监听：\n/monitor add {peer.channel_id}"
        return text

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
        if self.is_user_monitor_active():
            lines.append("\n监听客户端：用户账号 Telethon client\n")
        elif self.user_client:
            lines.append("\n监听客户端：Bot 回退监听（用户账号当前不可用）\n")
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
        if self._stopping or not AUTO_DELETE_BOT_MESSAGES or not message or delay <= 0:
            return
        job = asyncio.create_task(self._delete_message_later(message, delay))
        self._delete_tasks.add(job)
        job.add_done_callback(self._delete_tasks.discard)

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
        return self.user_monitor_running and self.user_client is not None and self.user_client.is_connected()

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
        def selected():
            return not self.is_user_monitor_active() if fallback else self.is_user_monitor_active()

        @client.on(events.Album)
        async def monitor_album(event):
            if selected():
                await self.enqueue_monitor_post(event, event.messages)

        @client.on(events.MessageEdited)
        async def monitor_edit(event):
            if selected():
                await self.enqueue_monitor_post(event, [event.message], edited=True)

    async def get_monitor_channel_peer(self, client: TelegramClient, channel_id: int):
        try:
            return await client.get_input_entity(types.PeerChannel(channel_id))
        except (ValueError, errors.ChannelPrivateError):
            # 用户 session 初次运行时可能尚未缓存频道的 access hash。
            if client is not self.client:
                async for dialog in client.iter_dialogs():
                    peer = dialog.input_entity
                    if isinstance(peer, types.InputPeerChannel) and peer.channel_id == channel_id:
                        return peer
            raise

    async def monitor_source_peer(self, client, event, source_id):
        if client is event.client:
            return await event.get_input_chat()
        return await self.get_monitor_channel_peer(client, source_id)

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

    async def release_rejected_request(self, task, request) -> None:
        async with self._monitor_db_lock:
            self.monitor_message_map().release_rejected(task.source_chat_id, task.target_chat_id,
                                                        dict(zip(request.id, request.random_id)))

    def defer_flooded_copy(self, task, exc, rejected: Optional[RejectedCopy] = None) -> bool:
        if task.retries >= 2:
            return False
        task.rejected_copy = rejected
        delay = max(1, exc.seconds)
        self.task_queue.retry_later(task, delay)
        logger.warning("频道复制限流，%s 秒后重试（%s/2）：%s", delay, task.retries, task.link)
        return True

    async def process_monitor_task(self, task: ForwardTask) -> None:
        source_id, target_id = task.source_chat_id, task.target_chat_id
        message_ids = task.message_ids
        event = task.event
        try:
            # Select the current listener at execution time, not when the update was enqueued.
            client = self.user_client if self.is_user_monitor_active() else self.client

            if task.mode == "monitor_edit":
                source_msg_id = message_ids[0]
                async with self._monitor_db_lock:
                    target_msg_id = self.monitor_message_map().get(source_id, source_msg_id, target_id)
                if target_msg_id is None:
                    logger.info("跳过频道编辑（没有转发映射）：%s, target=%s", task.link, target_id)
                    task.status = TaskStatus.COMPLETED
                    return
                # 出队后取源帖最新版本，避免较旧的编辑事件覆盖新的正文或媒体。
                source_peer = await self.monitor_source_peer(client, event, source_id)
                current = await client.get_messages(source_peer, ids=source_msg_id)
                if current is None:
                    logger.info("跳过频道编辑（源帖已不可读取）：%s", task.link)
                    task.status = TaskStatus.COMPLETED
                    return
                target_peer = await self.get_monitor_channel_peer(client, target_id)
                await self.edit_monitor_message(client, target_peer, target_msg_id, current)
                logger.info("已同步频道编辑：%s -> %s/%s", task.link, target_id, target_msg_id)
            else:
                rejected = task.rejected_copy
                if rejected is None:
                    async with self._monitor_db_lock:
                        mappings = self.monitor_message_map()
                        state = mappings.batch_copy_state(source_id, target_id, message_ids)
                        if state != TaskStatus.PENDING:
                            task.status = state
                            return
                    try:
                        source_peer = await self.monitor_source_peer(client, event, source_id)
                        target_peer = await self.get_monitor_channel_peer(client, target_id)
                    except errors.FloodWaitError as exc:
                        if self.defer_flooded_copy(task, exc):
                            return
                        raise
                    async with self._monitor_db_lock:
                        mappings = self.monitor_message_map()
                        reserved = mappings.reserve(source_id, target_id, message_ids)
                        if not reserved:
                            task.status = mappings.batch_copy_state(source_id, target_id, message_ids)
                            return
                    request = functions.messages.ForwardMessagesRequest(
                        from_peer=source_peer, id=list(reserved), random_id=list(reserved.values()),
                        to_peer=target_peer, drop_author=True,
                    )
                else:
                    client, request = rejected.client, rejected.request
                    if not client.is_connected():
                        # Prior call was explicitly rejected and no retry has gone out.
                        await self.release_rejected_request(task, request)
                        task.rejected_copy = None
                        raise ConnectionError("限流重试取消：原发送账号已断开，未换号重发")
                reserved = dict(zip(request.id, request.random_id))
                task.rejected_copy = None  # From this point cancellation does NOT release any intent.
                try:
                    sent = await client(request)
                except errors.FloodWaitError as exc:
                    if self.defer_flooded_copy(task, exc, RejectedCopy(client, request)):
                        return
                    await self.release_rejected_request(task, request)
                    raise
                except (errors.ChatWriteForbiddenError, errors.ChatForwardsRestrictedError,
                        errors.ChatAdminRequiredError, errors.ChannelPrivateError):
                    await self.release_rejected_request(task, request)
                    raise
                except Exception as exc:
                    raise CopyOutcomeUnknown(f"发送未获确认（{type(exc).__name__}），请核对源消息 {list(reserved)}") from exc
                # Telegram 回传 random_id 到目标消息 ID 的映射；不能按相册顺序或频道最后一帖猜测。
                by_random_id = {update.random_id: update.id for update in getattr(sent, 'updates', ())
                                if isinstance(update, types.UpdateMessageID)}
                pairs = {mid: by_random_id[rid] for mid, rid in reserved.items() if rid in by_random_id}
                try:
                    async with self._monitor_db_lock:
                        mappings = self.monitor_message_map()
                        mappings.remember(source_id, target_id, pairs)
                        unknown = [mid for mid in message_ids
                                   if mappings.copy_state(source_id, mid, target_id) != TaskStatus.COMPLETED]
                except sqlite3.Error as exc:
                    raise CopyOutcomeUnknown("目标 ID 未能持久化，请核对后处理") from exc
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
            # 编辑重试重新读取映射、最新正文和媒体引用，不执行复制请求。
            if task.mode == "monitor_edit" and task.retries < 2:
                delay = None
                if isinstance(exc, errors.FloodWaitError):
                    delay = max(1, exc.seconds)
                elif isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError,
                                      errors.ServerError, errors.TimedOutError,
                                      errors.FileReferenceExpiredError, errors.FileReferenceInvalidError)):
                    delay = 2 ** (task.retries + 1)
                if delay is not None:
                    self.task_queue.retry_later(task, delay)
                    logger.warning("频道编辑 %s 秒后重试（%s/2）：%s", delay, task.retries, task.link)
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
            self.user_client = TelegramClient(session, API_ID, API_HASH, flood_sleep_threshold=0)
        except Exception as exc:
            # 异常文本可能包含输入内容或路径，只记录类型。
            logger.warning("用户 session 初始化失败（%s），回退到 Bot 监听", type(exc).__name__)
            await self.notify_admins("⚠️ 用户 session 无效或无法读取，已回退到 Bot 监听。")
            return

        @self.user_client.on(events.NewMessage)
        async def user_monitor_handler(event):
            try:
                if self.is_user_monitor_active() and event.message.grouped_id is None:
                    await self.enqueue_monitor_post(event, [event.message])
            except Exception as e:
                logger.error(f"用户账号监听消息处理异常: {e}", exc_info=True)

        self.register_monitor_events(self.user_client)

        retry_after = await self._connect_user_monitor(self.user_client, initial=True)
        if self.user_monitor_running or retry_after is not None:
            self._user_monitor_watch = asyncio.create_task(self._watch_user_monitor(self.user_client, retry_after))

    async def _connect_user_monitor(self, client, *, initial: bool) -> Optional[int]:
        """Verify identity before selecting this listener; return a delay only for retryable failures."""
        retry_after = None
        try:
            await client.connect()
            me = await client.get_me()
            if me is not None:
                self.user_monitor_running = True
                logger.info("用户账号监听已启动: id=%s", me.id)
                if not initial:
                    await self.notify_admins("✅ 用户账号监听已恢复，已从 Bot 切回。")
                return None
            await self.notify_admins("⚠️ 用户 session 未授权或已失效，请更换并重启；继续使用 Bot 监听。")
        except (ConnectionError, TimeoutError, asyncio.TimeoutError, socket.gaierror, errors.ServerError,
                errors.TimedOutError, errors.FloodWaitError) as exc:
            logger.warning("用户监听连接暂时失败（%s），将重试", type(exc).__name__)
            retry_after = max(USER_MONITOR_RETRY_SECONDS, getattr(exc, 'seconds', 0))
            if initial:
                await self.notify_admins("⚠️ 用户账号监听暂时无法连接，已回退到 Bot 监听，将自动重试。")
        except Exception as exc:
            logger.error("用户账号监听连接失败（%s），继续使用 Bot 监听", type(exc).__name__)
            await self.notify_admins("⚠️ 用户账号监听失败，请检查 session 并重启；继续使用 Bot 监听。")
        await self._disconnect_user_monitor(client)
        return retry_after

    async def _disconnect_user_monitor(self, client) -> None:
        try:
            await client.disconnect()
        except Exception as exc:
            logger.warning("断开用户监听客户端时出错：%s", type(exc).__name__)

    async def _watch_user_monitor(self, client, retry_after: Optional[int] = None) -> None:
        delay = USER_MONITOR_RETRY_SECONDS
        while True:
            if self.user_monitor_running:
                try:
                    await client.disconnected
                except Exception as exc:
                    logger.warning("用户监听连接终止：%s", type(exc).__name__)
                self.user_monitor_running = False
                await self.notify_admins("⚠️ 用户账号监听已断开，已回退到 Bot 监听。")
                await self._disconnect_user_monitor(client)
                delay = USER_MONITOR_RETRY_SECONDS
            elif retry_after is None:
                return  # Invalid session or configuration; do not keep trying credentials.
            await asyncio.sleep(max(delay, retry_after or 0))
            retry_after = await self._connect_user_monitor(client, initial=False)
            delay = min(delay * 2, 300)

    # ── Core forward logic ──────────────────────────────────────

    async def do_forward(self, event) -> None:
        target_id = self.target_chat_id
        gid = getattr(event.message, 'grouped_id', None)
        key = (event.chat_id, gid) if gid is not None else None
        link = await self.resolve_link(event)
        if key is not None and (key in self._pending_albums or key in self._album_processed):
            return  # Don't create a second status while the first submitter awaits Telegram.

        if target_id is None:
            problem = "❌ 未配置目标频道ID，请管理员使用 /forwardto 设置"
        elif not link:
            problem = "❌ 无法确认原帖，请直接发送原帖的 https://t.me/... 消息链接"
        elif any(kw in link.lower() for kw in EXCLUDE_KEYWORDS):
            problem = f"❌ 链接包含排除关键词，已跳过: {link}"
        else:
            problem = None
        if problem:
            self.schedule_status_delete(await send_status(event, problem), failed=True)
            return  # A later album member may carry usable source metadata.

        if key is not None:
            self._pending_albums.add(key)
        try:
            status_msg = await send_status(event, "⏳ 正在加入转发队列...")
            status_chat_id = getattr(status_msg, 'chat_id', event.chat_id)
            if status_msg is not None:
                self.remember_forward_status(status_chat_id, status_msg.id)
            position = await self.task_queue.add_task(
                event, link, getattr(status_msg, 'id', None), status_chat_id, gid,
                status_client=getattr(status_msg, 'client', None) or self.client, target_chat_id=target_id,
            )
            if position is None:
                await edit_status(status_msg, "⏹️ 服务正在停止，任务未入队")
                return
            if key is not None:
                self._album_processed[key] = None
                if len(self._album_processed) > MANUAL_ALBUM_CACHE_SIZE:
                    self._album_processed.pop(next(iter(self._album_processed)))
        finally:
            if key is not None:
                self._pending_albums.discard(key)
        await edit_status(status_msg,
            f"⏳ 已加入转发队列（手动提交）\n"
            f"队列位置: {position}\n"
            f"FROM: {link}\n"
            f"TO:   {target_id}"
        )

    # ── Admin commands ──────────────────────────────────────────

    async def command_is_for_me(self, username: Optional[str]) -> bool:
        if not username:
            return True
        if self._bot_username is None:
            me = await self.client.get_me()
            self._bot_username = (getattr(me, 'username', None) or '').lower() or None
        return username.lower() == self._bot_username

    async def handle_admin_command(self, event, *, command: Optional[str] = None) -> bool:
        if event.sender_id not in ADMIN_IDS:
            return False

        raw = event.message.text or ""
        parts = raw.split()
        if not parts:
            return False
        command = command or parts[0].lower()

        if command == '/help':
            await event.respond(START_MESSAGE_ADMIN)
            return True

        async def respond(text, **kwargs):
            message = await event.respond(text, **kwargs)
            self.schedule_command_delete(message)
            return message

        if command in ('/restart', '/tdlstop') and len(parts) != 1:
            await respond(f"❌ {command} 不接受其他参数，未执行")
            return True

        if command == '/restart':
            await respond("🔄 正在重启机器人...")
            await self._restart()
            return True

        if command == '/forwardto':
            if len(parts) != 2:
                await respond(
                    "❌ 请指定且只指定一个目标频道 ID\n\n"
                    "用法：/forwardto <频道ID>\n"
                    "示例：/forwardto 1234567890\n\n"
                    "说明：可使用频道裸 ID 或 Telegram 标记后的频道 ID；机器人和 tdl 账号需要具备发帖权限。"
                )
                return True
            new_id = parse_chat_id(parts[1])
            if not new_id:
                await respond(
                    "❌ 频道 ID 格式无效\n\n"
                    "请输入频道裸 ID 或 Telegram 标记后的频道 ID，例如：\n"
                    "/forwardto 1234567890"
                )
                return True
            self.target_chat_id = new_id
            await respond(
                f"✅ 目标频道已更新\n"
                f"当前目标频道 ID：{self.target_chat_id}\n\n"
                "之后收到的有效消息会转发到该频道。"
            )
            return True

        if command == '/showto':
            if self.target_chat_id is None:
                await respond(
                    "ℹ️ 尚未设置目标频道\n\n"
                    "请使用以下命令设置：\n"
                    "/forwardto 1234567890"
                )
            else:
                await respond(
                    f"🎯 当前目标频道 ID：{self.target_chat_id}\n\n"
                    "所有新加入队列的任务都会转发到该频道。"
                )
            return True

        if command == '/tdlstop':
            current_stopped, cleared = await self.task_queue.stop_tdl_tasks()
            await respond(
                f"⏹️ 已执行全局停止\n当前 TDL 任务已停止：{'是' if current_stopped else '否'}\n"
                f"清空等待任务数：{cleared}"
            )
            return True

        if command == '/monitor':
            if len(parts) == 1:
                await respond(self.format_monitor_chat_ids())
                return True

            action = parts[1].lower()
            if action == 'clear':
                if len(parts) != 2:
                    await respond("❌ /monitor clear 不接受其他参数，监听列表未更改")
                    return True
                self.monitor_chat_ids = []
                await respond("✅ 已清空自动监听源频道列表")
                return True

            if action in ('add', 'del', 'delete', 'remove'):
                if len(parts) != 3:
                    await respond(
                        "❌ 请指定且只指定一个频道 ID\n\n"
                        "用法：\n"
                        "/monitor add <源频道裸ID>\n"
                        "/monitor del <源频道裸ID>"
                    )
                    return True
                chat_id = parse_monitor_chat_id(parts[2])
                if chat_id is None:
                    await respond("❌ 监听源频道 ID 格式无效，请输入裸 ID，不要加 -100 前缀，例如：/monitor add 1234567890")
                    return True

                if action == 'add':
                    added = self.add_monitor_chat_id(chat_id)
                    if added:
                        await respond(f"✅ 已添加自动监听源频道：{chat_id}\n\n{self.format_monitor_chat_ids()}")
                    else:
                        await respond(f"ℹ️ 该频道已在监听列表中：{chat_id}")
                    return True

                removed = self.remove_monitor_chat_id(chat_id)
                if removed:
                    await respond(f"✅ 已删除自动监听源频道：{chat_id}\n\n{self.format_monitor_chat_ids()}")
                else:
                    await respond(f"ℹ️ 该频道不在监听列表中：{chat_id}")
                return True

            if action == 'set':
                if len(parts) < 3:
                    await respond(
                        "❌ 缺少频道 ID 列表\n\n"
                        "用法：/monitor set <源频道裸ID1,源频道裸ID2>\n"
                        "示例：/monitor set 1234567890,9876543210"
                    )
                    return True
                items = " ".join(parts[2:]).replace('，', ',').split(',')
                ids = [parse_monitor_chat_id(item) for item in items]
                if None in ids:
                    await respond("❌ 列表中有空项或无效频道 ID，原监听列表未更改。请用逗号分隔裸频道 ID。")
                    return True
                self.monitor_chat_ids = list(dict.fromkeys(ids))
                await respond(f"✅ 已覆盖自动监听源频道列表\n\n{self.format_monitor_chat_ids()}")
                return True

            await respond(
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
                if len(parts) != 2:
                    await respond("❌ /flog clear 不接受其他参数，历史未清除")
                    return True
                ok = self.forward_log.clear()
                await respond("🗑️ 已清除所有转发记录" if ok else "⚠️ 历史文件未清除，请检查服务日志")
                return True

            rows = self.forward_log.get_all()
            text = format_recent_history(rows[-10:], len(rows))
            await respond(text, parse_mode=None, link_preview=False)
            return True

        if command == '/queue':
            status = self.task_queue.get_queue_status()
            queue_size = status["queue_size"]
            current_task = status["current_task"]

            if queue_size == 0 and not current_task:
                await respond("📋 转发队列为空，当前没有正在处理或等待中的任务。")
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
            await respond("".join(lines))
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
                    "extra_args": "❌ 范围导出参数过多，未创建任务。",
                    "reversed_range": "❌ 消息范围无效：起始消息 ID 不能大于结束消息 ID。",
                    "invalid_end_id": (
                        "❌ 结束消息 ID 格式错误\n\n"
                        "结束位置请填写数字，或填写同一频道的消息链接。\n"
                        "示例：/export_range https://t.me/c/3482276715/100 200"
                    ),
                    "invalid_range_id": (
                        "❌ 参数格式错误\n\n"
                        "Chat ID 不能为 0；起始及结束消息 ID 必须为 1 至 2147483647 的整数。\n"
                        "示例：/export_range 3482276715 100 200"
                    ),
                    "chat_mismatch": (
                        "❌ 起始消息链接和结束消息链接不属于同一个频道\n\n"
                        "请确认两个链接中的 Chat ID 一致。"
                    ),
                    "invalid_source_link": (
                        "❌ 来源必须是数字 Chat ID 或 t.me/c 频道、原帖链接，不能用公开用户名、评论或话题链接。\n\n"
                        "请改用 t.me/c/<Chat ID>/<消息ID> 链接，或手动填写 Chat ID。\n"
                        "示例：/export_range 3482276715 100 200"
                    ),
                }
                await respond(error_tips.get(parse_error, "❌ 参数解析失败，请检查 /export_range 用法。"))
                return True

            logger.info(f"处理 export_range 命令, chat_id: {chat_id}, range: {start_id}-{end_id}")

            # 用于显示的链接占位
            display_link = f"chat_id:{chat_id}"

            target_id = self.target_chat_id
            if target_id is None:
                await respond("❌ 未配置目标频道ID，请先使用 /forwardto 设置")
                return True
            status_msg = await send_status(event, "⏳ 正在加入范围导出队列...")
            if status_msg is not None:
                self.remember_forward_status(status_msg.chat_id, status_msg.id)
            gid = None
            position = await self.task_queue.add_task(
                event, display_link, getattr(status_msg, "id", None), event.chat_id, gid,
                mode="export_range",
                message_ids=[start_id, end_id],
                source_chat_id=chat_id,
                status_client=getattr(status_msg, 'client', None) or self.client, target_chat_id=target_id,
            )
            if position is None:
                await edit_status(status_msg, "⏹️ 服务正在停止，范围导出未入队")
                return True
            await edit_status(status_msg,
                f"⏳ 已加入范围导出队列\n"
                f"队列位置: {position}\n"
                f"源: {display_link}\n"
                f"范围: {start_id}-{end_id}\n"
                f"TO: {target_id}"
            )
            return True

        return False

    # ── Restart ─────────────────────────────────────────────────

    def begin_shutdown(self) -> None:
        # Close admissions even while the listener is still unwinding its connection.
        self._stopping = True

    async def _restart(self) -> None:
        self.begin_shutdown()
        self.restart_requested = True
        await self.client.disconnect()  # main closes resources before replacing this process.

    # ── Lifecycle ───────────────────────────────────────────────

    def monitor_message_map(self) -> MonitorMessageMap:
        if self._monitor_message_map is None:
            self._monitor_message_map = MonitorMessageMap(MONITOR_MAP_FILE)
        return self._monitor_message_map

    @staticmethod
    def cleanup_monitor_db(path: str, retention_days: int) -> int:
        with closing(MonitorMessageMap(path)) as mappings:
            return mappings.cleanup_if_due(retention_days)

    async def monitor_cleanup_loop(self) -> None:
        while True:
            try:
                # Wait asynchronously for maintenance, never for its SQLite writer lock on the event loop.
                async with self._monitor_db_lock:
                    job = asyncio.create_task(asyncio.to_thread(self.cleanup_monitor_db, MONITOR_MAP_FILE,
                                                                MONITOR_MAP_RETENTION_DAYS))
                    try:
                        delay = await asyncio.shield(job)
                    except asyncio.CancelledError:
                        # Cancelling to_thread does not stop SQLite; let its own connection close first.
                        await asyncio.gather(job, return_exceptions=True)
                        raise
            except (OSError, sqlite3.Error):
                logger.exception("monitor-data 清理失败，一小时后重试")
                delay = 3600
            await asyncio.sleep(delay)

    def _register_bot_handlers(self) -> None:
        @self.client.on(events.NewMessage)
        async def message_handler(event):
            if self._stopping:
                return
            try:
                text = event.message.text or ""
                logger.info(
                    f"收到消息: chat_id={event.chat_id}, msg_id={event.message.id}, "
                    f"sender_id={event.sender_id}, is_channel={event.is_channel}, "
                    f"post={getattr(event.message, 'post', None)}"
                )

                # Forward headers and broadcast posts are data, even if the caption is an admin command.
                forwarded = bool(event.message.forward)
                parts = text.split(maxsplit=1)
                match = (re.fullmatch(r"(/[A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?", parts[0])
                         if parts and not forwarded and not event.out else None)
                command = match[1].lower() if match else None
                if command == '/chatid' and await self.command_is_for_me(match[2]):
                    msg = await event.respond(self.format_chat_id(event))
                    self.schedule_command_delete(msg)
                    return

                if self.is_monitor_post(event.message):
                    if not self.is_user_monitor_active() and event.message.grouped_id is None:
                        await self.enqueue_monitor_post(event, [event.message])
                    return

                peer = event.message.peer_id
                if event.out or (isinstance(peer, types.PeerChannel) and
                                 (event.message.post or peer.channel_id == self.monitor_target_channel_id())):
                    return
                if command:
                    if not await self.command_is_for_me(match[2]):
                        return
                    if command == '/start':
                        if event.sender_id in ADMIN_IDS:
                            await event.respond(START_MESSAGE_ADMIN)
                        elif can_submit(event.sender_id):
                            await event.respond("🤖 你可以转发消息或发送 Telegram 消息链接。")
                        else:
                            await event.respond("🤖 此机器人仅接受授权用户投稿。")
                    else:
                        await self.handle_admin_command(event, command=command)
                    return  # Unknown commands are not link submissions.
                if not can_submit(event.sender_id):
                    return
                if event.message.forward or TELEGRAM_LINK_PATTERN.search(text):
                    await self.do_forward(event)

            except Exception as e:
                logger.error(f"消息处理异常: {e}", exc_info=True)

        self.register_monitor_events(self.client, fallback=True)

    async def start(self) -> None:
        self._stopping = False
        await self.client.start(bot_token=BOT_TOKEN)
        if MONITOR_MAP_RETENTION_DAYS and self._monitor_cleanup_task is None:
            self._monitor_cleanup_task = asyncio.create_task(self.monitor_cleanup_loop())
        await self.task_queue.start()

        if not self._bot_handlers_registered:
            self._register_bot_handlers()
            self._bot_handlers_registered = True
        await self.start_user_monitor()

        for admin_id in ADMIN_IDS:
            try:
                await self.client.send_message(admin_id, START_MESSAGE_ADMIN)
                logger.info(f"已向管理员 {admin_id} 发送启动消息")
            except Exception as e:
                logger.error(f"向管理员 {admin_id} 发送启动消息失败: {e}")

        if self.monitor_chat_ids:
            logger.info(f"自动监听源频道已启用: {self.monitor_chat_ids}")
            if self.is_user_monitor_active():
                logger.info("源频道的新消息及编辑将由用户账号 Telethon client 监听，bot 只处理命令和状态消息")
            else:
                logger.info("用户账号监听未运行，将回退到 bot；bot 需要源频道读取权限及目标频道发帖和编辑权限")
        else:
            logger.info("自动监听源频道未配置")

        logger.info("Bot 已启动（TDL 集成模式）")
        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        self.begin_shutdown()
        self.user_monitor_running = False
        jobs = tuple(self._delete_tasks)
        for job in jobs:
            job.cancel()
        if jobs:
            await asyncio.gather(*jobs, return_exceptions=True)
        if self._user_monitor_watch is not None:
            self._user_monitor_watch.cancel()
            await asyncio.gather(self._user_monitor_watch, return_exceptions=True)
            self._user_monitor_watch = None
        if self.user_client:
            await self._disconnect_user_monitor(self.user_client)
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
    loop = asyncio.get_running_loop()
    runner = asyncio.create_task(bot.start())
    terminating = False
    installed = False

    def request_shutdown():
        nonlocal terminating
        if not terminating:
            terminating = True
            bot.begin_shutdown()
            logger.info("收到 SIGTERM，正在停止服务")
            runner.cancel()  # Cancel startup/listening only; never interrupt the cleanup below.

    try:
        try:
            loop.add_signal_handler(signal.SIGTERM, request_shutdown)
            installed = True
        except (NotImplementedError, RuntimeError):
            # Windows and non-main-thread loops do not support asyncio signal handlers.
            pass
        await runner
    except asyncio.CancelledError:
        if not terminating:
            raise
    except KeyboardInterrupt:
        logger.info("用户手动停止")
    except (errors.AccessTokenInvalidError, errors.AccessTokenExpiredError):
        logger.error("机器人令牌无效或已过期，请检查 .env 中的 BOT_TOKEN 并重建容器；不要分享令牌。")
        raise SystemExit(2) from None
    except errors.FloodWaitError as exc:
        logger.error("启动受到 Telegram 限流，请在 %s 秒后重试", exc.seconds)
        raise SystemExit(1) from None
    except Exception:
        logger.exception("机器人异常退出")
        raise SystemExit(1) from None
    finally:
        try:
            await bot.stop()
        finally:
            if installed:
                loop.remove_signal_handler(signal.SIGTERM)
    if not terminating and bot.restart_requested:
        os.execv(sys.executable, [sys.executable, *sys.argv])


if __name__ == "__main__":
    asyncio.run(main())


