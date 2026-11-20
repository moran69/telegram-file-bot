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
import threading
from telethon import TelegramClient, errors, events, functions, types, utils
from telethon import __version__ as TELETHON_VERSION
from telethon.sessions import MemorySession, StringSession
from typing import Awaitable, Callable, Optional
from dataclasses import dataclass
from enum import Enum
from functools import partial

from admin_panel import AdminPanel
from task_journal import TaskJournal
from monitor_clone import media_key, source_posts, upload_batch, publish_request, receipt_pairs

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
    logger.info("[启动/环境] Python %s | Telethon %s | SQLite %s | 本地时区 %s；开始读取配置",
                sys.version.split()[0], TELETHON_VERSION, sqlite3.sqlite_version, time.strftime('%Z %z'))

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
MONITOR_TDL_FALLBACK = os.getenv("MONITOR_TDL_FALLBACK", "").strip().lower() in ("1", "true", "yes", "on")
FORWARD_LOG_FILE = os.getenv("FORWARD_LOG_FILE") or "forward_history.json"
FORWARD_LOG_MAX_DAYS = 7  # 保留最近7天
MONITOR_MAP_FILE = os.getenv("MONITOR_MAP_FILE", "").strip() or "monitor_message_map.sqlite3"
MONITOR_ADMISSION_DELAYS = (1, 3)
MONITOR_ALERT_WINDOW = 1.0
MAX_CHANNEL_ID = 997_852_516_352  # MTProto channel/supergroup range.
MANUAL_ALBUM_CACHE_SIZE = 500
MONITOR_ALBUM_WAIT = 1.0  # seconds from the first grouped NewMessage
MONITOR_EARLY_EDIT_LIMIT = 4096
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
AUTO_DELETE_SYNC_RECEIPT_SECONDS = delete_delay("AUTO_DELETE_SYNC_RECEIPT_SECONDS", 0)


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


class UnconfirmedReceipt(str):
    """TDL diagnostics without proof of complete delivery (still JSON-compatible)."""


class MonitorPeerMissing(Exception):
    """A channel ID is known but this session cannot yet resolve its input peer."""


@dataclass(frozen=True)
class RejectedCopy:
    # InputPeer access hashes and random IDs must stay with the account that made the request.
    client: TelegramClient
    request: functions.messages.ForwardMessagesRequest


@dataclass
class ForwardTask:
    event: events.NewMessage.Event | events.Album.Event | None
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
    job_id: Optional[int] = None
    ready_at: float = 0
    send_started: bool = False
    followup: Optional['ForwardTask'] = None
    observer: Optional[str] = None  # Account role only. Never persist a session or access hash.
    verify_edit: bool = False
    delivery_recorded: bool = False
    refused_role: Optional[str] = None

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
        self._albums: dict[tuple[int, int, int], tuple[ForwardTask, asyncio.TimerHandle]] = {}
        self._early_edits: dict[tuple[int, int, int], int] = {}
        self._retry_timers: dict[asyncio.TimerHandle, ForwardTask] = {}
        self._edits: dict[tuple[int, int, int], ForwardTask] = {}
        self.journal = TaskJournal(MONITOR_MAP_FILE)
        self._loaded = False
        self._peer_wait = {}
        self.admission_epoch = 0

    def record_copy_result(self, task, unresolved):
        if unresolved:
            task.message_ids = unresolved
            task.link = f'https://t.me/c/{task.source_chat_id}/{unresolved[0]}'
            self.journal.save(task, 'review')
        else:
            self.journal.discard([task.job_id])
        task.delivery_recorded = True

    def _restore(self):
        if self._loaded:
            return
        rows = self.journal.restore()
        ready, early, edits = [], {}, {}
        for jid, state, fields in rows:
            task = ForwardTask(event=None, job_id=jid, **fields)
            if state in ('sending', 'review'):
                if task.mode == 'monitor_copy':
                    m = self.bot.monitor_message_map()
                    unresolved = [mid for mid in task.message_ids if m.copy_state(
                        task.source_chat_id, mid, task.target_chat_id) != TaskStatus.COMPLETED]
                    if state == 'sending' or unresolved != task.message_ids:
                        self.record_copy_result(task, unresolved)
                    continue
                if state == 'review':
                    continue
                if task.mode != 'monitor_edit':
                    self.journal.save(task, 'review')
                    continue
                task.verify_edit = True  # Interrupted in-place edit: compare first, never copy.
                self.journal.save(task)
            if state == 'early':
                m = self.bot.monitor_message_map()
                if m.get(*task.edit_key) is None:
                    if m.can_await_mapping(*task.edit_key):
                        old = early.get(task.edit_key)
                        self.journal.discard([old])
                        early[task.edit_key] = jid
                    else:
                        self.journal.discard([jid])
                    continue
                self.journal.save(task)
            if task.mode == 'monitor_edit':
                if task.edit_key in edits:
                    self.journal.discard([jid])  # One latest-source read covers both admissions.
                    continue
                edits[task.edit_key] = task
            ready.append(task)  # Publish only after all fallible recovery operations.
        count, _ = self.journal.reviews()
        self._edits.update(edits)
        self._early_edits.update(early)
        delayed = 0
        for task in ready:
            if task.ready_at > time.time():
                self._schedule_retry(task, task.ready_at - time.time())
                delayed += 1
            else:
                self.queue.put_nowait(task)
        self._loaded = True
        logger.info('[启动/队列] 读取 %d 条持久化记录：立即入队 %d，定时重试 %d，等待编辑映射 %d，待核对 %d（不重发）',
                    len(rows), len(ready) - delayed, delayed, len(early), count)

    def begin_send(self, task):
        self.journal.save(task, 'sending')
        task.send_started = True

    async def start(self):
        async with self._control:
            if self.worker_task is not None and not self.worker_task.done():
                return
            self._restore()
            self.running = True
            self.worker_task = asyncio.create_task(self._worker())
            logger.info("任务队列已启动")

    async def stop(self):
        async with self._control:
            self.running = False
            if self.worker_task and not self.worker_task.done():
                self.worker_task.cancel()
                await asyncio.gather(self.worker_task, return_exceptions=True)
            pending = self._take_pending()
            for task in pending:
                if task.rejected_copy is not None:
                    # Do not reconstruct an already attempted, account-bound RPC on restart.
                    task.status, task.error_msg = TaskStatus.FAILED, '已停止等待限流重试，未再次发送'
                    try:
                        await self.bot.release_rejected_request(task, task.rejected_copy.request)
                        self.journal.discard([task.job_id])
                        task.rejected_copy = None
                    except (sqlite3.Error, OSError):
                        task.error_msg += '；无法释放发送记录，须人工核对'
                        logger.exception('无法释放限流预约；保留待核对记录')
            self._edits.clear()
            self.journal.close()
            self._loaded = False
            logger.info("任务队列已停止")

    async def _get_status(self, task: ForwardTask):
        if task.status_msg_id is not None:
            try:
                client = task.status_client or self.bot.client
                return await client.get_messages(task.chat_id, ids=task.status_msg_id)
            except Exception as exc:
                logger.debug("无法读取状态提示：%s", type(exc).__name__)
        return None

    def retry_later(self, task: ForwardTask, delay: float) -> None:
        task.retries += 1
        task.ready_at = time.time() + delay
        self.journal.save(task, 'sending' if task.send_started else 'pending')
        self._schedule_retry(task, delay)

    def _schedule_retry(self, task, delay):
        task.status = TaskStatus.PENDING
        def requeue():
            self._retry_timers.pop(timer, None)
            self.queue.put_nowait(task)

        timer = asyncio.get_running_loop().call_later(delay, requeue)
        self._retry_timers[timer] = task

    def _take_pending(self) -> list[ForwardTask]:
        pending = list(self._retry_timers.values())
        pending.extend(self._peer_wait.values())
        self._peer_wait.clear()
        for task, timer in self._albums.values():
            timer.cancel()
            pending.append(task)
        self._albums.clear()
        self._early_edits.clear()
        for timer in self._retry_timers:
            timer.cancel()
        self._retry_timers.clear()
        while not self.queue.empty():
            pending.append(self.queue.get_nowait())
            self.queue.task_done()
        for task in pending:
            if task.mode == "monitor_edit":
                self._edits.pop(task.edit_key, None)
        return pending

    async def clear_pending_tasks(self, *, notify: bool = True) -> int:
        return await self._finish_pending(self._cancel_pending(), notify)

    def _cancel_pending(self, stop_current=False):
        self.admission_epoch += 1
        self.journal.cancel_waiters(self.current_task.job_id if self.current_task else None,
                                    [t.job_id for t in self._retry_timers.values()], stop_current)
        pending = self._take_pending()
        if self.current_task and self.current_task.followup:
            self.current_task.followup = None
            self.current_task.edit_dirty = False
        return pending

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
            self._restore()
            worker, current = self.worker_task, self.current_task
            manual = current is not None and current.mode in ("clone", "export_range") and current.status == TaskStatus.PROCESSING
            pending = self._cancel_pending(stop_current=manual)
            self._resume.clear()
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

    def note_early_edit(self, task: ForwardTask) -> None:
        # Keep keys only, never the media or event. No copy RPC is triggered by an unmapped edit.
        key = task.edit_key
        self.journal.save(task, 'early')
        old = self._early_edits.pop(key, None)
        if old != task.job_id:
            self.journal.discard([old])
        self._early_edits[key] = task.job_id
        if len(self._early_edits) > MONITOR_EARLY_EDIT_LIMIT:
            self.journal.discard([self._early_edits.pop(next(iter(self._early_edits)))])

    async def enqueue_edit(self, event, source_id: int, mid: int, target_id: int) -> Optional[int]:
        return await self.add_task(event, f"https://t.me/c/{source_id}/{mid}", None, event.chat_id,
                                   mode="monitor_edit", message_ids=[mid], source_chat_id=source_id,
                                   target_chat_id=target_id)

    async def reconcile_early_edits(self, task: ForwardTask, confirmed: dict[int, int]) -> None:
        for mid in confirmed:
            key = (task.source_chat_id, mid, task.target_chat_id)
            if key in self._early_edits:
                old = self._early_edits[key]
                accepted = await self.add_task(task.event, f'https://t.me/c/{task.source_chat_id}/{mid}', None,
                                               task.chat_id, mode='monitor_edit', message_ids=[mid],
                                               source_chat_id=task.source_chat_id, target_chat_id=task.target_chat_id)
                if accepted is not None:
                    self.journal.discard([old])
                    del self._early_edits[key]

    def _collect_album(self, task: ForwardTask, member_only: bool) -> None:
        key = (task.source_chat_id, task.grouped_id, task.target_chat_id)
        pending = self._albums.get(key)
        if pending:
            first, timer = pending
            task.message_ids = sorted(set(first.message_ids).union(task.message_ids))
            task.link = f"https://t.me/c/{task.source_chat_id}/{task.message_ids[0]}"
            task.job_id = first.job_id
            self.journal.save(task)
            first.message_ids, first.link = task.message_ids, task.link
            if member_only:
                return
            timer.cancel()
            del self._albums[key]
            task = first
        else:
            self.journal.save(task)
        if not pending and member_only:
            def flush():
                del self._albums[key]
                if not self.bot._stopping:
                    self.queue.put_nowait(task)
            timer = asyncio.get_running_loop().call_later(MONITOR_ALBUM_WAIT, flush)
            self._albums[key] = (task, timer)
            return
        self.queue.put_nowait(task)

    async def add_task(self, event, link: str, status_msg_id: Optional[int], chat_id: int,
                       grouped_id: Optional[int] = None, mode: str = "clone",
                       message_ids: Optional[list[int]] = None, source_chat_id: Optional[int] = None,
                       status_client: Optional[TelegramClient] = None, *, target_chat_id: int,
                       album_member: bool = False) -> Optional[int]:
        if self.bot._stopping:
            return None  # The queue may already be drained and its worker gone.
        try:
            self._restore()
            return self._admit(event, link, status_msg_id, chat_id, grouped_id, mode, message_ids,
                               source_chat_id, status_client, target_chat_id, album_member)
        except (sqlite3.Error, OSError):
            logger.exception('未能保存入队记录：%s → %s', link, target_chat_id)
            return None

    def admission_error(self, result='任务未入队'):
        return (f'⏹️ 服务正在停止，{result}' if self.bot._stopping else
                f'⚠️ 无法保存队列记录，{result}。请检查数据卷空间和写入权限。')

    def _admit(self, event, link, status_msg_id, chat_id, grouped_id, mode, message_ids,
               source_chat_id, status_client, target_chat_id, album_member):
        observer = None
        if (mode in ('monitor_copy', 'monitor_edit') and event is not None and source_chat_id
                and getattr(event, 'chat_id', None) == utils.get_peer_id(types.PeerChannel(source_chat_id))):
            observer = 'bot' if event.client is self.bot.client else 'user'
            for jid, waiting in list(self._peer_wait.items()):
                if waiting.source_chat_id == source_chat_id:
                    waiting.event, waiting.observer = event, observer
                    self.journal.save(waiting)
                    del self._peer_wait[jid]
                    self.queue.put_nowait(waiting)
        self.task_counter += 1
        task = ForwardTask(
            event=event,
            link=link,
            status_msg_id=status_msg_id,
            chat_id=chat_id,
            status_client=status_client or getattr(event, 'client', None),
            created_at=time.time(),
            grouped_id=grouped_id,
            mode=mode,
            message_ids=message_ids,
            source_chat_id=source_chat_id,
            observer=observer,
            target_chat_id=target_chat_id,
        )
        if mode == "monitor_edit":
            existing = self._edits.get(task.edit_key)
            if existing is not None:
                if observer:
                    existing.event, existing.observer = event, observer
                if existing.status == TaskStatus.PROCESSING and not existing.edit_dirty:
                    self.journal.save(task)
                    existing.followup = task
                    existing.edit_dirty = True
                return self.queue.qsize()
        if mode == "monitor_copy" and grouped_id is not None:
            self._collect_album(task, album_member)
        else:
            self.journal.save(task)
            if mode == 'monitor_edit':
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
        try:
            await self._deliver(task)
        except Exception:
            if task.send_started and not task.delivery_recorded:
                self.journal.save(task, 'review')
            raise
        if task.mode not in ('monitor_copy', 'monitor_edit'):
            return  # Manual delivery is finalized before updating the status message.
        if task.delivery_recorded:
            return
        if task.status == TaskStatus.UNCONFIRMED:
            if task.mode == 'monitor_copy':
                self.record_copy_result(task, [mid for mid in task.message_ids if self.bot.monitor_message_map().copy_state(
                    task.source_chat_id, mid, task.target_chat_id) != TaskStatus.COMPLETED])
            else:
                self.journal.save(task, 'review')
        elif task.status != TaskStatus.PENDING and not (
                task.mode == 'monitor_edit' and self._early_edits.get(task.edit_key) == task.job_id):
            self.journal.discard([task.job_id])

    async def _deliver(self, task: ForwardTask):
        if task.mode == "monitor_edit":
            task.status = TaskStatus.PROCESSING
            task.edit_dirty = False
            try:
                await self.bot.process_monitor_task(task)
            except BaseException:
                self._edits.pop(task.edit_key, None)
                raise
            if task.status == TaskStatus.PENDING:  # The retry already reads the latest source.
                if task.followup:
                    self.journal.discard([task.followup.job_id])
            elif task.followup:
                self._edits[task.edit_key] = task.followup
                self.queue.put_nowait(task.followup)
            else:
                self._edits.pop(task.edit_key, None)
            task.followup = None
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
                self.begin_send(task)
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
                        self.begin_send(task)
                        ok, output = await self.bot.tdl.forward_from_export(export_file, target, progress)
            else:
                raise ValueError(f"未知任务类型：{task.mode}")
        except Exception as exc:
            logger.exception("TDL 任务执行异常")
            ok, output = False, UnconfirmedReceipt(str(exc)) if task.send_started else str(exc)

        # Finalize delivery before touching Telegram notifications. A deleted status is not a cancellation.
        task.status = (TaskStatus.COMPLETED if ok else TaskStatus.UNCONFIRMED
                       if isinstance(output, UnconfirmedReceipt) else TaskStatus.FAILED)
        task.error_msg = "" if ok else output
        self.bot.forward_log.add(task.link, target, ok, task.error_msg)
        if task.status == TaskStatus.UNCONFIRMED:
            self.journal.save(task, 'review')
        else:
            self.journal.discard([task.job_id])
        heading = ("✅ TDL 命令已完成" if ok else "⚠️ 投递结果待核对" if task.status == TaskStatus.UNCONFIRMED
                   else "❌ 任务失败")
        await edit_status(msg, "\n".join([heading, *details, self._format_last_output(output)]))
        self.bot.schedule_status_delete(msg, failed=not ok)

    def get_queue_status(self) -> dict:
        return {
            "queue_size": self.queue.qsize() + len(self._retry_timers) + len(self._albums) + len(self._peer_wait),
            "current_task": {
                "link": self.current_task.link[:50] if self.current_task else None,
                "status": self.current_task.status.value if self.current_task else None
            } if self.current_task else None,
            "running": self.running
        }

# 管理员启动通知和 /start 使用速查面板；/help 使用分步向导。
START_MESSAGE_ADMIN = """🤖 消息转发助手 · 管理面板

转发一条消息：把原消息或消息链接发给我。
持续同步频道：添加监听后，新帖会自动复制；原帖修改后，会更新已关联的目标帖，不另发一条。

🎯 转发到哪里？
/showto — 查看当前目标频道
/forwardto 1234567890 — 设置目标频道

📡 监听哪些频道？
/monitor — 查看监听列表
/monitor add 9876543210 — 添加频道
/monitor del 9876543210 — 移除频道
/monitor set 9876543210,8765432109 — 替换整个列表
/monitor clear — 清空监听列表

🔄 目标帖没有跟着更新？
发送源帖链接，重新同步这一条：
/resync https://t.me/c/9876543210/42
也可使用：/resync 9876543210 42
只更新已关联的目标帖，不会重复发帖。

📦 批量转发
/export_range 9876543210 100 200
将源频道中消息 ID 从 100 到 200 的帖子转发到当前目标频道。

🛠 任务与维护
/queue — 查看等待中的任务
/tdlstop — 停止当前手动转发，清空等待任务和定时重试；已开始的自动同步不强停
/flog — 查看最近 10 条转发记录及失败原因
/flog clear — 清空本地记录，不删除频道消息
/restart — 重启机器人
/help — 查看分步使用向导

💡 监听源请填裸 ID，不加 -100。通过 /monitor 修改的列表仅本次运行有效；要在重启后保留，请写入 MONITOR_CHAT_IDS。"""

HELP_MESSAGE_ADMIN = """👋 欢迎使用消息转发助手

① 先选一个接收消息的频道
发送 /showto 查看当前目标。还没设置？例如：
/forwardto 1234567890

② 试着转发一条消息
把频道原帖转发给我，或直接发送消息链接：
https://t.me/channel_name/42
我会把它加入队列，转发到你选好的目标频道。

③ 想让源频道以后自动同步？
假设源帖链接是 https://t.me/c/9876543210/42
其中 9876543210 是源频道裸 ID，42 是消息 ID。
发送：/monitor add 9876543210

以后该频道发新帖时会自动复制；正文、媒体说明或支持的图片和视频有修改时，会更新原来的目标帖。

④ 有一条已经转发，但内容没更新？
发送：/resync https://t.me/c/9876543210/42
也可以发：/resync 9876543210 42
机器人只更新有对应记录的目标帖，不会另发一份。

⑤ 想一次转发多条历史消息？
发送：/export_range 9876543210 100 200
最后两个数字是起始和结束消息 ID，不是转发数量。

📋 用到时再查
/monitor — 查看监听列表
/monitor del <裸ID> — 移除一个监听源
/monitor set <ID1,ID2> — 替换整个监听列表
/monitor clear — 清空监听列表
/queue — 查看等待任务
/tdlstop — 停止手动任务并清空排队、重试，不强停正在执行的自动同步
/flog · /flog clear — 查看最近 10 条记录 · 清空本地记录
/restart · /start — 重启机器人 · 返回管理面板

💡 重启后仍要监听？请把源频道裸 ID 写入 MONITOR_CHAT_IDS，多个 ID 用英文逗号分隔。"""


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


def parse_monitor_list(raw: str) -> Optional[list[int]]:
    ids = [parse_monitor_chat_id(item) for item in raw.replace('，', ',').split(',')]
    return None if None in ids else list(dict.fromkeys(ids))


def parse_chat_ids(raw: str) -> list[int]:
    result = []
    for item in raw.split(','):
        chat_id = parse_monitor_chat_id(item)
        if chat_id is None:
            if item.strip():
                logger.warning("忽略 MONITOR_CHAT_IDS 中的无效频道 ID；请填写以英文逗号分隔的裸 ID")
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
        logger.info('[启动/存储] 转发历史：%s；载入 %d 条；%s', self.log_file, len(self.logs),
                    '允许写入' if self.writable else '已停用写入以保护原件')
        self._last_id = max((int(r['id']) for r in self.logs if re.fullmatch(r'[0-9]{1,20}', str(r['id']))), default=0)
        self._state_lock, self._disk_lock = threading.Lock(), threading.Lock()
        self._version = self._saved = 0
        self._scheduled = self.write_failed = False

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

    def _save(self, snapshot) -> bool:
        if not self.writable:
            return False
        tmp = None
        try:
            self.log_file.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=self.log_file.parent,
                                             prefix=".forward-history-", delete=False) as f:
                tmp = Path(f.name)
                json.dump(snapshot, f, ensure_ascii=False, indent=2)
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
        with self._state_lock:
            self._last_id += 1
            record_id = str(self._last_id)
            self.logs.append(dict(id=record_id, to=to_chat_id, success=success, error=error_msg,
                                  timestamp=time.time(), date=time.strftime('%Y-%m-%d %H:%M:%S'),
                                  **{'from': from_link}))
            self._version += 1
            if self._scheduled:
                return record_id
            self._scheduled = True
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush()  # Synchronous callers have no service loop to drain on shutdown.
        else:
            loop.run_in_executor(None, self.flush)
        return record_id

    def flush(self) -> bool:
        with self._disk_lock:
            while True:
                with self._state_lock:
                    if self._saved == self._version:
                        self._scheduled = False
                        return True
                    self._prune()
                    version, snapshot = self._version, list(self.logs)
                ok = self._save(snapshot)
                with self._state_lock:
                    self.write_failed = not ok
                    if not ok:
                        self._scheduled = False
                        return False  # Retain dirty records; next add or shutdown retries.
                    self._saved = version

    def get_all(self) -> list[dict]:
        with self._state_lock:
            self._prune()
            return list(self.logs)

    def get_recent(self, count: int = 10) -> list[dict]:
        return self.get_all()[-count:] if count > 0 else []

    def clear(self) -> bool:
        with self._disk_lock:
            with self._state_lock:
                boundary = self._version
            if not self._save([]):
                return False
            with self._state_lock:
                # Keep additions accepted during the clear. A later writer takes a fresh snapshot.
                new_count = self._version - boundary
                self.logs = self.logs[-new_count:] if new_count else []
                if not new_count:
                    self._last_id = 0
                self._saved = boundary
                self.write_failed = False
            return True


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
                if 'sender_role' not in columns:
                    self.db.execute('ALTER TABLE monitor_messages ADD COLUMN sender_role TEXT')
                self.db.execute("CREATE INDEX IF NOT EXISTS monitor_confirmed_at ON monitor_messages(confirmed_at)")
                self.db.execute("CREATE TABLE IF NOT EXISTS monitor_cleanup "
                                "(id INTEGER PRIMARY KEY CHECK(id=1), last_run INTEGER NOT NULL)")
                self.db.execute("CREATE TABLE IF NOT EXISTS monitor_pruned_ids (source_chat_id INTEGER NOT NULL, "
                                "source_message_id INTEGER NOT NULL, target_chat_id INTEGER NOT NULL, "
                                "PRIMARY KEY(source_chat_id, source_message_id, target_chat_id)) WITHOUT ROWID")
                self.db.execute("""
                    CREATE TABLE IF NOT EXISTS monitor_pruned_through (
                        source_chat_id INTEGER NOT NULL, target_chat_id INTEGER NOT NULL,
                        last_id INTEGER NOT NULL, PRIMARY KEY (source_chat_id, target_chat_id)
                    )
                """)
                if 'cloned_media_key' not in columns:
                    self.db.execute('ALTER TABLE monitor_messages ADD COLUMN cloned_media_key TEXT')
                # Legacy TDL CLI completions have no target ID; preserve their deduplication records.
                self.db.execute("""
                    CREATE TABLE IF NOT EXISTS monitor_copy_intents (
                        source_chat_id INTEGER NOT NULL,
                        source_message_id INTEGER NOT NULL,
                        target_chat_id INTEGER NOT NULL,
                        random_id INTEGER NOT NULL UNIQUE,
                        created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                        transport TEXT NOT NULL DEFAULT 'telegram',
                        completed INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY (source_chat_id, source_message_id, target_chat_id)
                    )
                """)
                intent_columns = {row[1] for row in self.db.execute("PRAGMA table_info(monitor_copy_intents)")}
                for name, definition in (("transport", "TEXT NOT NULL DEFAULT 'telegram'"),
                                         ("completed", "INTEGER NOT NULL DEFAULT 0")):
                    if name not in intent_columns:
                        self.db.execute(f"ALTER TABLE monitor_copy_intents ADD COLUMN {name} {definition}")
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
                    AND source_message_id=:mid AND target_chat_id=:dst
                    AND transport='tdl' AND completed=1) THEN 'completed'
                WHEN EXISTS (SELECT 1 FROM monitor_copy_intents WHERE source_chat_id=:src
                    AND source_message_id=:mid AND target_chat_id=:dst) THEN 'unconfirmed'
                WHEN EXISTS (SELECT 1 FROM monitor_pruned_ids WHERE source_chat_id=:src
                    AND source_message_id=:mid AND target_chat_id=:dst) THEN 'completed'
                WHEN EXISTS (SELECT 1 FROM monitor_pruned_through WHERE source_chat_id=:src
                    AND target_chat_id=:dst AND last_id=:mid) THEN 'completed'
                WHEN EXISTS (SELECT 1 FROM monitor_pruned_through WHERE source_chat_id=:src
                    AND target_chat_id=:dst AND last_id>:mid) THEN 'unconfirmed'
                ELSE 'pending' END
        """, {"src": source_chat_id, "mid": source_message_id, "dst": target_chat_id}).fetchone()
        return TaskStatus(row[0])

    def can_await_mapping(self, source_id, mid, target_id):
        # An explicitly rate-limited native copy still owns a live reservation and can acquire a mapping.
        # A pruned receipt, legacy watermark or external copy can never supply one.
        return (self.copy_state(source_id, mid, target_id) == TaskStatus.PENDING or
                bool(self.db.execute('SELECT 1 FROM monitor_copy_intents WHERE source_chat_id=? AND '
                                     "source_message_id=? AND target_chat_id=? AND transport IN ('telegram','tdl_upload')",
                                     (source_id, mid, target_id)).fetchone()))

    def sender_role(self, source_id, mid, target_id):
        row = self.db.execute('SELECT sender_role FROM monitor_messages WHERE source_chat_id=? '
                              'AND source_message_id=? AND target_chat_id=?', (source_id, mid, target_id)).fetchone()
        return row[0] if row else None

    def cloned_media_key(self, source_id, mid, target_id):
        row = self.db.execute('SELECT cloned_media_key FROM monitor_messages WHERE source_chat_id=? '
                             'AND source_message_id=? AND target_chat_id=?', (source_id, mid, target_id)).fetchone()
        return row[0] if row else None

    def update_cloned_media_key(self, source_id, mid, target_id, key):
        with self.db:
            self.db.execute('UPDATE monitor_messages SET cloned_media_key=? WHERE source_chat_id=? '
                            'AND source_message_id=? AND target_chat_id=?', (key, source_id, mid, target_id))

    def batch_copy_state(self, source_id: int, target_id: int, ids: list[int]) -> TaskStatus:
        states = {self.copy_state(source_id, mid, target_id) for mid in ids}
        if TaskStatus.PENDING in states:
            return TaskStatus.PENDING  # At least one new member can be sent.
        if TaskStatus.UNCONFIRMED in states:
            logger.warning("复制结果待核对，不重发：source=%s/%s target=%s", source_id, ids, target_id)
            return TaskStatus.UNCONFIRMED
        return TaskStatus.COMPLETED

    def cleanup_if_due(self, retention_days: int) -> int:
        """原子保留确切已投递 ID，仅删除过期的目标映射。"""
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
                INSERT OR IGNORE INTO monitor_pruned_ids (source_chat_id, source_message_id, target_chat_id)
                SELECT source_chat_id, source_message_id, target_chat_id FROM monitor_messages WHERE confirmed_at <= ?
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

    def start_tdl_clone(self, source_id: int, mid: int, target_id: int, nonce: int) -> None:
        # Atomically claim the rejected RPC reservation before starting an external process.
        # A crash after this point is unresolved, never an invitation to send again.
        with self.db:
            changed = self.db.execute(
                "UPDATE monitor_copy_intents SET transport='tdl' WHERE source_chat_id=? "
                "AND source_message_id=? AND target_chat_id=? AND random_id=? "
                "AND transport='telegram' AND completed=0", (source_id, mid, target_id, nonce),
            ).rowcount
            if changed != 1:
                raise CopyOutcomeUnknown(f"TDL 发送记录已变化，未重复发送源消息 {mid}")

    def start_tdl_upload(self, source_id, target_id, reserved):
        # All members change transport in ONE transaction, immediately before publication.
        with self.db:
            for mid, nonce in reserved.items():
                changed = self.db.execute("UPDATE monitor_copy_intents SET transport='tdl_upload' "
                    "WHERE source_chat_id=? AND source_message_id=? AND target_chat_id=? AND random_id=? "
                    "AND transport='telegram' AND completed=0", (source_id, mid, target_id, nonce)).rowcount
                if changed != 1:
                    raise CopyOutcomeUnknown('TDL 回退发送记录已变化，未重复发送')

    def complete_tdl_clone(self, source_id: int, mid: int, target_id: int, nonce: int) -> None:
        with self.db:
            changed = self.db.execute(
                "UPDATE monitor_copy_intents SET completed=1 WHERE source_chat_id=? "
                "AND source_message_id=? AND target_chat_id=? AND random_id=? "
                "AND transport='tdl' AND completed=0", (source_id, mid, target_id, nonce),
            ).rowcount
            if changed != 1:
                raise CopyOutcomeUnknown(f"TDL 完成记录未能保存，请核对源消息 {mid}")

    def release_rejected(self, source_id: int, target_id: int, reserved: dict[int, int]) -> None:
        # 只用于 Telegram 明确拒绝本次请求的情形，不用于网络中断或缺回执。
        with self.db:
            self.db.executemany(
                "DELETE FROM monitor_copy_intents WHERE source_chat_id=? AND source_message_id=? "
                "AND target_chat_id=? AND random_id=? AND transport='telegram'",
                [(source_id, mid, target_id, nonce) for mid, nonce in reserved.items()],
            )

    def remember(self, source_chat_id: int, target_chat_id: int, pairs: dict[int, int], sender_role=None,
                 cloned_media_keys=None) -> None:
        with self.db:
            self.db.executemany(
                "INSERT INTO monitor_messages (source_chat_id,source_message_id,target_chat_id,target_message_id,confirmed_at,sender_role,cloned_media_key) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                [(source_chat_id, src, target_chat_id, dst, int(time.time()), sender_role,
                  (cloned_media_keys or {}).get(src)) for src, dst in pairs.items()],
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
            def bounded(record):
                return record if len(record) <= 2048 else record[:1024] + record[-1024:]

            tail = b""
            while True:
                chunk = await stream.read(8192)
                # Split bytes, then decode complete records so chunk boundaries don't break UTF-8.
                parts = (tail + chunk).replace(b"\r", b"\n").split(b"\n")
                tail = bounded(parts.pop()) if chunk else b""
                for line in parts:
                    text = bounded(line).decode("utf-8", errors="replace").strip()
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
        return await self._forward_receipt(cmd, progress_callback)

    async def forward_clone(
        self,
        from_link: str,
        to_chat_id: int,
        progress_callback: Optional[Callable[[str], Awaitable[None]]] = None,
        *, single: bool = False,
    ) -> tuple[bool, str]:
        to_arg = self.normalize_chat_id(to_chat_id)
        cmd = [
            self.tdl_path, "forward",
            "--from", from_link,
            "--to", to_arg,
            "--mode", "clone",
        ]
        if single:
            cmd.append("--single")  # Do not expand a monitored member into its whole album.
        return await self._forward_receipt(cmd, progress_callback, single=single)

    async def download_media(self, link: str, output_file: str) -> None:
        # One explicit member, isolated directory, no album expansion or resume prompts.
        path = Path(output_file)
        cmd = [self.tdl_path, 'download', '--url', link, '--dir', str(path.parent),
               '--template', path.name, '--restart']
        code, out, err = await self._run_command(cmd)
        if code or not path.is_file() or path.stat().st_size <= 0:
            raise ValueError(f'TDL 媒体下载失败：{(err or out)[-1000:]}')

    async def _forward_receipt(self, cmd, progress_callback, *, single=False):
        failed = {}
        pattern = re.compile(r'\((-?\d+)\):(\d+)\s*->.*?\(-?\d+\)\s+error\s*:\s*(.*)', re.I)

        def inspect(line):
            clean = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', line)
            match = pattern.search(clean)
            if match:
                failed[(match[1], match[2])] = match[3][:300]

        async def progress(line):
            inspect(line)
            if progress_callback:
                await progress_callback(line)

        try:
            code, out, err = await self._run_command(cmd, progress)
            for line in (out + '\n' + err).splitlines():
                inspect(line)
        except Exception as e:
            code, out, err = -1, '', str(e)
        if failed:
            ids = ', '.join(f'{src}/{mid}' for src, mid in failed)
            detail = next(iter(failed.values()))
            text = f'逐条报错 {len(failed)} 条：{ids}（{detail}）'
            return False, text if single else UnconfirmedReceipt(text + '；其余消息可能已送达，请核对，不自动重发整批。')
        if code:
            return False, UnconfirmedReceipt(f'TDL 未正常结束（{code}），不能确认投递结果：{err or out}')
        return True, out or 'TDL 已结束，未报告逐条错误。'


# ─────────────────────────── TelegramBot ───────────────────────
class TelegramBot:
    def __init__(self):
        self._startup_phase = '初始化'
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
        self._admission_alert = None
        self._admission_failures = deque(maxlen=20)
        self._admission_failure_count = 0
        self.task_queue = TaskQueue(self)
        self.admin_panel = AdminPanel(self, allowed=lambda uid: not self._stopping and uid in ADMIN_IDS,
                                      home=START_MESSAGE_ADMIN, guide=HELP_MESSAGE_ADMIN,
                                      target_id=parse_chat_id, source_id=parse_monitor_chat_id,
                                      source_list=parse_monitor_list, post_link=parse_private_channel_link,
                                      export_range=parse_export_range_args)

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
            lines.append("\n监听客户端：用户账号与 Bot 同时接收，各自使用本账号的频道权限。\n")
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
                logger.error('向管理员发送通知失败（%s）', type(e).__name__)

    def schedule_cleanup(self, action, delay: int):
        """One process-local timer, governed by the global switch and shutdown."""
        if self._stopping or not AUTO_DELETE_BOT_MESSAGES or delay <= 0:
            return None

        async def later():
            await asyncio.sleep(delay)
            try:
                await action()
            except Exception:
                logger.debug('自动清理消息失败', exc_info=True)

        job = asyncio.create_task(later())
        self._delete_tasks.add(job)
        job.add_done_callback(self._delete_tasks.discard)
        return job

    def schedule_delete(self, message, delay: int) -> None:
        if message:
            self.schedule_cleanup(lambda: message.delete(), delay)

    def schedule_panel_receipt(self, message, kind):
        delay = {'command': AUTO_DELETE_COMMAND_SECONDS, 'error': AUTO_DELETE_ERROR_SECONDS,
                 'sync': AUTO_DELETE_SYNC_RECEIPT_SECONDS}[kind]
        self.schedule_delete(message, delay)

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

    async def _admit_monitor(self, event, source, target, ids, *, mode, grouped_id=None, album_member=False):
        queue = self.task_queue
        epoch = queue.admission_epoch
        link = f'https://t.me/c/{source}/{ids[0]}'
        for delay in (0, *MONITOR_ADMISSION_DELAYS):
            if delay:
                await asyncio.sleep(delay)
            if self._stopping or epoch != queue.admission_epoch:
                return False
            position = await queue.add_task(event, link, None, event.chat_id, mode=mode, message_ids=ids,
                                            source_chat_id=source, target_chat_id=target, grouped_id=grouped_id,
                                            album_member=album_member)
            if position is not None:
                return True
        self._admission_failures.append(f'{mode}：{source}/{",".join(map(str, ids))} → {target}')
        self._admission_failure_count += 1
        if self._admission_alert is None or self._admission_alert.done():
            self._admission_alert = asyncio.create_task(self._report_admission_failures())
        return False

    async def _report_admission_failures(self):
        while self._admission_failure_count:
            await asyncio.sleep(MONITOR_ALERT_WINDOW)
            text = (f'⚠️ {self._admission_failure_count} 个监听任务未能持久化（最近 20 项）：\n'
                    + '\n'.join(self._admission_failures) + '\n请检查数据卷后核对源帖；未自动补发。')
            self._admission_failures.clear()
            self._admission_failure_count = 0
            logger.error('%s', text)
            await self.notify_admins(text[:3900])

    async def enqueue_monitor_post(self, event, messages, *, edited: bool = False, album_member: bool = False) -> None:
        if self._stopping or not messages:
            return
        message = messages[0]
        if not self.is_monitor_post(message) or (event.chat_id, message.id) in self._forward_status_ids:
            return
        source_id = message.peer_id.channel_id
        target_id = self.monitor_target_channel_id()
        if target_id is None or target_id == source_id:
            logger.warning("跳过频道同步：未配置目标频道或目标就是源频道，source=%s", source_id)
            return
        # Only observed posts of this source may be forwarded, never inferred adjacent IDs.
        members = {m.id: m for m in messages if m.peer_id == message.peer_id and m.grouped_id == message.grouped_id}
        epoch = self.task_queue.admission_epoch
        if not edited:
            ids = sorted(members)
            await self._admit_monitor(event, source_id, target_id, ids, mode='monitor_copy',
                                      grouped_id=message.grouped_id, album_member=album_member)
        # NewMessage can be an edited snapshot of an already mapped post. Copy dedup alone
        # would leave its caption stale. Always read the actual latest content in the worker.
        for mid, item in members.items():
            if epoch != self.task_queue.admission_epoch:
                return
            if edited or getattr(item, "edit_date", None) is not None:
                await self._admit_monitor(event, source_id, target_id, [mid], mode='monitor_edit')

    def register_monitor_events(self, client: TelegramClient, *, fallback: bool = False) -> None:
        def selected():
            return fallback or (client is self.user_client and self.is_user_monitor_active())

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
        except ValueError as exc:
            # 用户 session 初次运行时可能尚未缓存频道的 access hash。
            if client is not self.client:
                async for dialog in client.iter_dialogs():
                    peer = dialog.input_entity
                    if isinstance(peer, types.InputPeerChannel) and peer.channel_id == channel_id:
                        return peer
            raise MonitorPeerMissing(f'频道 {channel_id} 的实体尚未缓存') from exc

    async def monitor_route(self, task, preferred=None):
        """Resolve both peers on one connected account before any write; never transfer access hashes."""
        clients = [self.client]
        if self.is_user_monitor_active():
            clients = [self.client, self.user_client] if (preferred or task.observer) == 'bot' else [self.user_client, self.client]
        clients = [c for c in clients if ('bot' if c is self.client else 'user') != task.refused_role]
        if not clients:
            raise ValueError('没有其他在线且可用的发送账号')
        missing = None
        for client in clients:
            try:
                source = await self.monitor_source_peer(client, task.event, task.source_chat_id)
                target = await self.get_monitor_channel_peer(client, task.target_chat_id)
                return client, source, target
            except (MonitorPeerMissing, ValueError) as exc:
                missing = MonitorPeerMissing(str(exc))
            except errors.ChannelPrivateError:
                if client is clients[-1] and missing is None:
                    raise
        raise missing or MonitorPeerMissing('无法解析源或目标频道')

    async def monitor_source_peer(self, client, event, source_id):
        if event is not None and client is event.client and event.chat_id == utils.get_peer_id(types.PeerChannel(source_id)):
            peer = await event.get_input_chat()
            if peer is not None:
                return peer
        # A cache miss can return None. Never read a channel-scoped message ID without its peer.
        return await self.get_monitor_channel_peer(client, source_id)

    async def report_monitor_status(self, text: str, *, failed: bool = False) -> None:
        if ADMIN_IDS:
            try:
                status_msg = await self.client.send_message(ADMIN_IDS[0], text)
                self.schedule_status_delete(status_msg, failed=failed)
            except Exception as exc:
                logger.debug("发送频道同步状态失败：%s", exc)

    @staticmethod
    async def edit_monitor_message(client, target_peer, target_msg_id, current, before_send=None, *, verify=False,
                                   cloned=False, replacement=None) -> None:
        file = replacement if cloned else monitor_visual_media(current.media)
        if file is not None or verify:
            target = await client.get_messages(target_peer, ids=target_msg_id)
            if target is None or isinstance(target, types.MessageEmpty):
                raise ValueError(f"目标消息 {target_msg_id} 已删除或不可读取，不能原地编辑")
            previous = monitor_visual_media(target.media)
            # 同一媒体重新读取时 file_reference 可以变化；仅改说明不重新挂载媒体。
            if (file is not None and type(previous) is type(file) and previous.id.id == file.id.id and
                    bool(previous.spoiler) == bool(file.spoiler)):
                file = None
            if (verify and file is None and (current.raw_text or '') == (target.raw_text or '')
                    and (current.entities or []) == (target.entities or [])
                    and isinstance(current.media, types.MessageMediaWebPage) == isinstance(target.media, types.MessageMediaWebPage)):
                return
        if before_send:
            before_send()
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

    async def clone_restricted_posts(self, task: ForwardTask, reserved: dict[int, int],
                                     client, source_peer, target_peer) -> None:
        """Download with TDL, publish once with this session and persist exact API receipts."""
        source_id, target_id = task.source_chat_id, task.target_chat_id
        reserved = dict(reserved)
        publishing = False
        logger.warning('源频道禁止直接转发，使用 TDL 下载及带回执上传：%s', task.link)
        try:
            posts = await source_posts(client, source_peer, task.message_ids, task.grouped_id)
            ids = [p.id for p in posts]
            async with self._monitor_db_lock:
                mappings = self.monitor_message_map()
                # Previously sent/uncertain members cannot be joined into a new album safely.
                if any(mid not in reserved and mappings.copy_state(source_id, mid, target_id)
                       != TaskStatus.PENDING for mid in ids):
                    raise ValueError('相册已有已发送或待核对成员，未重复或拆散发送，请人工核对')
                task.message_ids = ids
                self.task_queue.journal.save(task, 'sending')
                reserved.update(mappings.reserve(source_id, target_id, [i for i in ids if i not in reserved]))
                if set(reserved) != set(ids):
                    raise ValueError('相册成员未能完整预留，未发送')
            with tempfile.TemporaryDirectory(prefix='monitor-tdl-') as directory:
                media = await upload_batch(self.tdl, client, source_peer, target_peer, source_id, posts, directory)
                # Re-read after slow transfers: latest caption, never attach an obsolete media snapshot.
                latest = {p.id: p for p in await source_posts(client, source_peer, ids, task.grouped_id)}
                if set(latest) != set(ids) or any(media_key(latest[p.id]) != media_key(p) or
                    latest[p.id].grouped_id != task.grouped_id for p in posts):
                    raise ValueError('下载期间源媒体或相册已变化，未发送，请重新同步')
                posts = [latest[mid] for mid in ids]
                request = publish_request(target_peer, posts, media, reserved)
                async with self._monitor_db_lock:
                    if self._stopping:
                        raise asyncio.CancelledError
                    # Set before commit: an uncertain commit must not enable a duplicate send.
                    publishing = True
                    try:
                        self.monitor_message_map().start_tdl_upload(source_id, target_id, reserved)
                    except Exception as exc:
                        raise CopyOutcomeUnknown('回退发送意图未能确认落库，未启动发布；请核对数据库状态') from exc
                try:
                    sent = await client(request)
                    pairs = receipt_pairs(sent, reserved)
                    async with self._monitor_db_lock:
                        mappings = self.monitor_message_map()
                        mappings.remember(source_id, target_id, pairs,
                            'bot' if client is self.client else 'user', {p.id: media_key(p) for p in posts})
                        unknown = [mid for mid in ids if mappings.copy_state(source_id, mid, target_id)
                                   != TaskStatus.COMPLETED]
                    self.task_queue.record_copy_result(task, unknown)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    raise CopyOutcomeUnknown(f'TDL 回退上传结果须核对，不自动重发：{exc}') from exc
                await self.task_queue.reconcile_early_edits(task, pairs)
                if unknown:
                    raise CopyOutcomeUnknown(f'回退回执不完整，以下源成员待核对：{unknown}')
        finally:
            if not publishing:
                # Downloads and uploadMedia do not publish any channel posts.
                async with self._monitor_db_lock:
                    self.monitor_message_map().release_rejected(source_id, target_id, reserved)
        task.status = TaskStatus.COMPLETED
        self.forward_log.add(task.link, target_id, True)
        links = '\n'.join(f'https://t.me/c/{target_id}/{pairs[mid]}' for mid in ids)
        await self.report_monitor_status(f'✅ TDL 回退上传成功，已建立 {len(pairs)} 条编辑映射。\n{links}')

    async def process_monitor_task(self, task: ForwardTask) -> None:
        source_id, target_id = task.source_chat_id, task.target_chat_id
        message_ids = task.message_ids
        mapped = None
        try:
            if task.mode == "monitor_edit":
                source_msg_id = message_ids[0]
                async with self._monitor_db_lock:
                    m = self.monitor_message_map()
                    mapped = m.get(*task.edit_key)
                    sender_role = m.sender_role(*task.edit_key) if mapped is not None else None
                    cloned_key = m.cloned_media_key(*task.edit_key) if mapped is not None else None
                    if mapped is None and m.can_await_mapping(*task.edit_key):
                        self.task_queue.note_early_edit(task)
                if mapped is None:
                    logger.info('没有转发映射，未执行编辑：%s → %s', task.link, target_id)
                    task.status = TaskStatus.COMPLETED
                    return
                # 出队后取源帖最新版本，避免较旧的编辑事件覆盖新的正文或媒体。
                client, source_peer, target_peer = await self.monitor_route(task, sender_role)
                current = await client.get_messages(source_peer, ids=source_msg_id)
                if current is None or isinstance(current, types.MessageEmpty):
                    raise ValueError(f"源帖 {source_msg_id} 已删除或不可读取，未修改目标消息 {mapped}")
                if cloned_key is None:
                    await self.edit_monitor_message(client, target_peer, mapped, current,
                        partial(self.task_queue.begin_send, task), verify=task.verify_edit)
                else:
                    key = media_key(current)
                    with tempfile.TemporaryDirectory(prefix='monitor-edit-') as directory:
                        replacement = None
                        if key != cloned_key and key != 'text':
                            media = await upload_batch(self.tdl, client, source_peer, target_peer, source_id, [current], directory)
                            replacement = media[source_msg_id]
                            latest = await client.get_messages(source_peer, ids=source_msg_id)
                            if latest is None or isinstance(latest, types.MessageEmpty):
                                raise ValueError('下载期间源帖已删除，未编辑目标消息')
                            if media_key(latest) != key:
                                raise errors.FileReferenceExpiredError(None)
                            current = latest
                        await self.edit_monitor_message(client, target_peer, mapped, current,
                            partial(self.task_queue.begin_send, task), verify=task.verify_edit,
                            cloned=True, replacement=replacement)
                    async with self._monitor_db_lock:
                        self.monitor_message_map().update_cloned_media_key(*task.edit_key, key)
                logger.info("已同步频道编辑：%s -> %s/%s", task.link, target_id, mapped)
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
                        client, source_peer, target_peer = await self.monitor_route(task)
                    except errors.FloodWaitError as exc:
                        if self.defer_flooded_copy(task, exc):
                            return
                        raise
                    except (ConnectionError, TimeoutError, asyncio.TimeoutError, socket.gaierror,
                            errors.ServerError, errors.TimedOutError) as exc:
                        if task.retries >= 2:
                            raise
                        delay = 2 ** (task.retries + 1)
                        self.task_queue.retry_later(task, delay)
                        logger.warning("发送前解析频道失败（%s），%s 秒后重试：%s", type(exc).__name__, delay, task.link)
                        return
                    async with self._monitor_db_lock:
                        mappings = self.monitor_message_map()
                        self.task_queue.begin_send(task)
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
                except errors.ChatForwardsRestrictedError:
                    if MONITOR_TDL_FALLBACK:
                        await self.clone_restricted_posts(task, reserved, client, request.from_peer, request.to_peer)
                        return
                    await self.release_rejected_request(task, request)
                    raise
                except (errors.ChatWriteForbiddenError,
                        errors.ChatAdminRequiredError, errors.ChannelPrivateError):
                    await self.release_rejected_request(task, request)
                    if self.is_user_monitor_active() and task.refused_role is None and rejected is None:
                        # Explicit permission denial: nothing was delivered. Re-resolve both peers on
                        # the other account. Never do this for a restricted post or an uncertain send.
                        task.refused_role = 'bot' if client is self.client else 'user'
                        task.send_started = False
                        self.task_queue.retry_later(task, 0)
                        return
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
                        mappings.remember(source_id, target_id, pairs, 'bot' if client is self.client else 'user')
                        unknown = [mid for mid in message_ids
                                   if mappings.copy_state(source_id, mid, target_id) != TaskStatus.COMPLETED]
                except sqlite3.Error as exc:
                    raise CopyOutcomeUnknown("目标 ID 未能持久化，请核对后处理") from exc
                try:
                    self.task_queue.record_copy_result(task, unknown)
                except sqlite3.Error as exc:
                    raise CopyOutcomeUnknown('回执已保存，但队列状态未能更新；将按逐条映射核对') from exc
                await self.task_queue.reconcile_early_edits(task, pairs)
                if unknown:
                    raise CopyOutcomeUnknown(f"回执不完整；以下源消息待核对，不重发：{unknown}")
                self.forward_log.add(task.link, target_id, True)
                logger.info("已复制频道帖子并记录映射：%s -> %s, ids=%s", task.link, target_id, pairs)
                await self.report_monitor_status(f"✅ 自动复制成功：{task.link} → {target_id}")
            task.status = TaskStatus.COMPLETED
        except MonitorPeerMissing as exc:
            # No send RPC was made. Preserve admission and its original target through a cold cache.
            if task.retries < 2:
                self.task_queue.retry_later(task, 2 ** (task.retries + 1))
            else:
                task.status = TaskStatus.PENDING
                self.task_queue.journal.save(task, 'wait_peer')
                self.task_queue._peer_wait[task.job_id] = task
                await self.report_monitor_status(f'⏳ 频道实体尚未就绪，任务已保留：{task.link} → {target_id}\n{exc}', failed=True)
        except CopyOutcomeUnknown as exc:
            task.status = TaskStatus.UNCONFIRMED
            task.error_msg = str(exc)
            logger.error("自动复制待核对：%s -> %s: %s", task.link, target_id, exc)
            self.forward_log.add(task.link, target_id, False, str(exc))
            await self.report_monitor_status(f"⚠️ 自动复制结果待核对：{task.link} → {target_id}\n{exc}", failed=True)
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
                    if task.send_started:
                        # Only an existing mapped post can be edited. For ambiguous responses,
                        # compare it to the fresh source before another edit, including after restart.
                        task.verify_edit = task.verify_edit or not isinstance(exc, (errors.FloodWaitError,
                            errors.FileReferenceExpiredError, errors.FileReferenceInvalidError))
                        task.send_started = False
                    self.task_queue.retry_later(task, delay)
                    logger.warning("频道编辑 %s 秒后重试（%s/2）：%s", delay, task.retries, task.link)
                    return
            task.error_msg = str(exc)
            task.status = (TaskStatus.UNCONFIRMED if task.mode == 'monitor_edit' and task.send_started and
                           isinstance(exc, (ConnectionError, TimeoutError, asyncio.TimeoutError, errors.ServerError,
                                            errors.TimedOutError)) else TaskStatus.FAILED)
            logger.error("频道同步失败（%s）：%s -> %s: %s", task.mode, task.link, target_id, exc, exc_info=True)
            if task.mode == "monitor_copy":
                self.forward_log.add(task.link, target_id, False, str(exc))
                await self.report_monitor_status(f"⚠️ 自动复制失败：{task.link} → {target_id}\n{exc}", failed=True)
            else:
                await self.report_monitor_status(f"⚠️ 频道编辑未同步：{task.link} → {target_id}\n{exc}", failed=True)

    async def start_user_monitor(self) -> None:
        if not USER_MONITOR_ENABLED:
            logger.info('[启动/用户监听] USER_MONITOR_ENABLED 已关闭，使用 Bot 监听')
            return
        try:
            if USER_SESSION_STRING and not USER_SESSION_STRING.startswith("填入"):
                logger.info('[启动/用户监听] 使用字符串会话；正在验证登录状态')
                session = StringSession(USER_SESSION_STRING)
            elif USER_SESSION_FILE:
                logger.info('[启动/用户监听] 使用文件会话；正在验证登录状态')
                session = USER_SESSION_FILE
            else:
                logger.info('[启动/用户监听] 未配置用户会话，使用 Bot 监听')
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
                if event.client is self.user_client and self.is_user_monitor_active():
                    await self.enqueue_monitor_post(event, [event.message], album_member=event.message.grouped_id is not None)
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
                logger.info("用户账号监听已启动：会话已授权")
                if not initial:
                    await self.notify_admins("✅ 用户账号监听已恢复，已从仅 Bot 监听切回双账号监听。")
                return None
            logger.warning('用户会话未授权或已失效；继续使用 Bot 监听')
            await self.notify_admins("⚠️ 用户 session 未授权或已失效，请更换并重启；继续使用 Bot 监听。")
        except (ConnectionError, TimeoutError, asyncio.TimeoutError, socket.gaierror, errors.ServerError,
                errors.TimedOutError, errors.FloodWaitError) as exc:
            retry_after = max(USER_MONITOR_RETRY_SECONDS, getattr(exc, 'seconds', 0))
            logger.warning("用户监听连接暂时失败（%s），%d 秒后重试", type(exc).__name__, retry_after)
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
                await edit_status(status_msg, self.task_queue.admission_error())
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

    async def show_admin_page(self, event, *, guide=False):
        if event.chat_id == event.sender_id and event.sender_id in ADMIN_IDS:
            await self.admin_panel.open(event.sender_id, event.respond, guide=guide)
        else:  # /start and /help in groups retain plain text; never attach management buttons.
            await event.respond(HELP_MESSAGE_ADMIN if guide else START_MESSAGE_ADMIN, link_preview=False)

    async def handle_admin_command(self, event, *, command: Optional[str] = None) -> bool:
        if event.sender_id not in ADMIN_IDS:
            return False

        raw = event.message.text or ""
        parts = raw.split()
        if not parts:
            return False
        parts[0] = command or parts[0].lower()

        if parts[0] == '/help':
            await self.show_admin_page(event, guide=True)
            return True

        async def respond(text, **kwargs):
            message = await event.respond(text, **kwargs)
            self.schedule_command_delete(message)
            return message

        return await self.run_admin_action(event, parts, respond)

    async def run_admin_action(self, event, parts, respond, *, panel_feedback=False) -> bool:
        """Shared authorization and execution for typed commands and private panel actions."""
        if self._stopping or event.sender_id not in ADMIN_IDS:
            return False
        command = parts[0]
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

        if command == '/resync':
            source_id = mid = None
            if len(parts) == 2:
                source_id, mid = parse_private_channel_link(parts[1])
            elif len(parts) == 3:
                source_id = parse_monitor_chat_id(parts[1])
                mid = int(parts[2]) if re.fullmatch(r'[0-9]{1,10}', parts[2]) else None
            if not source_id or source_id > MAX_CHANNEL_ID or mid is None or not 1 <= mid < 2**31:
                await respond("❌ 用法：/resync https://t.me/c/1234567890/42\n"
                              "或 /resync <源频道裸ID> <源消息ID>。请使用源频道原帖链接，不要使用评论或话题链接。")
                return True
            target_id = self.monitor_target_channel_id()
            if not self.has_monitor_chat_id(source_id) or not target_id or target_id == source_id:
                await respond("❌ 请检查监听源频道与当前目标频道设置")
                return True
            async with self._monitor_db_lock:
                if self._stopping:
                    mapped = None
                else:
                    mapped = self.monitor_message_map().get(source_id, mid, target_id)
            if self._stopping:
                await respond("⏹️ 服务正在停止，未加入同步队列")
            elif mapped is None:
                await respond("ℹ️ 当前目标没有转发映射，未修改或补发任何消息")
            elif await self.task_queue.enqueue_edit(event, source_id, mid, target_id) is None:
                await respond(self.task_queue.admission_error('未加入同步队列'))
            else:
                await respond(
                    f"⏳ 已加入原地同步队列：{source_id}/{mid}\n"
                    f"🔗 查看目标帖： https://t.me/c/{target_id}/{mapped}\n"
                    "此回执表示已受理，不代表同步完成。",
                    link_preview=False, **({"receipt_kind": "sync"} if panel_feedback else {}),
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
                ids = parse_monitor_list(" ".join(parts[2:]))
                if ids is None:
                    await respond("❌ 列表中有空项或无效频道 ID，原监听列表未更改。请用逗号分隔裸频道 ID。")
                    return True
                self.monitor_chat_ids = ids
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
                ok = await asyncio.to_thread(self.forward_log.clear)
                await respond("🗑️ 已清除所有转发记录" if ok else "⚠️ 历史文件未清除，请检查服务日志")
                return True

            rows = self.forward_log.get_all()
            text = format_recent_history(rows[-10:], len(rows))
            if self.forward_log.write_failed is True:
                text += '\n⚠️ 历史文件写入失败，部分记录仅在内存中。请检查磁盘空间和目录权限。'
            await respond(text, parse_mode=None, link_preview=False)
            return True

        if command == '/queue':
            status = self.task_queue.get_queue_status()
            queue_size = status["queue_size"]
            current_task = status["current_task"]
            review_count, recent = self.task_queue.journal.reviews()

            if queue_size == 0 and not current_task and not review_count:
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
            if review_count:
                lines.append(f'\n待核对：{review_count} 项（不自动重发，最近 5 项）：\n')
                for item in recent:
                    detail = f" {item['message_ids']}" if item['mode'] in ('export_range', 'monitor_copy') else ''
                    lines.append(f"{item['mode']}：{item['link']}{detail} → {item['target_chat_id']}\n")
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
                await edit_status(status_msg, self.task_queue.admission_error())
                if panel_feedback:
                    await respond(self.task_queue.admission_error())
                return True
            await edit_status(status_msg,
                f"⏳ 已加入范围导出队列\n"
                f"队列位置: {position}\n"
                f"源: {display_link}\n"
                f"范围: {start_id}-{end_id}\n"
                f"TO: {target_id}"
            )
            if panel_feedback:
                await respond(f"⏳ 范围任务已入队：{chat_id}/{start_id} 至 {end_id} → {target_id}。进度可在任务队列查看。")
            return True

        return False

    # ── Restart ─────────────────────────────────────────────────

    def begin_shutdown(self) -> None:
        # Close admissions even while the listener is still unwinding its connection.
        self.task_queue.admission_epoch += 1
        self._stopping = True
        self.admin_panel.sessions.clear()

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
        self.client.on(events.CallbackQuery)(self.admin_panel.callback)

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

                if self.is_monitor_post(event.message):
                    await self.enqueue_monitor_post(event, [event.message], album_member=event.message.grouped_id is not None)
                    return

                peer = event.message.peer_id
                if event.out or (isinstance(peer, types.PeerChannel) and
                                 (event.message.post or peer.channel_id == self.monitor_target_channel_id())):
                    return
                if await self.admin_panel.reply(event):
                    return  # A reply to our input prompt must never fall through to manual forwarding.
                # Forward headers and broadcast posts are data, even if the caption is an admin command.
                forwarded = bool(event.message.forward)
                parts = text.split(maxsplit=1)
                match = (re.fullmatch(r"(/[A-Za-z0-9_]+)(?:@([A-Za-z0-9_]+))?", parts[0])
                         if parts and not forwarded and not event.out else None)
                command = match[1].lower() if match else None
                if command:
                    if not await self.command_is_for_me(match[2]):
                        return
                    if command == '/start':
                        if event.sender_id in ADMIN_IDS:
                            await self.show_admin_page(event)
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
                elif text.strip():
                    await self.admin_panel.remind_if_waiting(event)

            except Exception as e:
                logger.error(f"消息处理异常: {e}", exc_info=True)

        self.register_monitor_events(self.client, fallback=True)

    def _log_startup_settings(self) -> None:
        sources = self.monitor_chat_ids
        preview = ', '.join(map(str, sources[:8])) or '无'
        if len(sources) > 8:
            preview += f'，另有 {len(sources) - 8} 个'
        logger.info('[启动/配置] 目标频道=%s；源频道 %d 个（%s）；管理员 %d 人；额外投稿名单 %d 人',
                    self.monitor_target_channel_id() or '未设置', len(sources), preview, len(ADMIN_IDS), len(SUBMITTER_IDS))
        logger.info('[启动/配置] MONITOR_TDL_FALLBACK：%s；状态消息自动删除：%s',
                    '开启' if MONITOR_TDL_FALLBACK else '关闭',
                    (f'普通 {AUTO_DELETE_STATUS_SECONDS}s / 错误 {AUTO_DELETE_ERROR_SECONDS}s / 命令 {AUTO_DELETE_COMMAND_SECONDS}s'
                     if AUTO_DELETE_BOT_MESSAGES else '关闭'))
        logger.info('[启动/存储] 编辑映射=%s；任务队列=%s；映射清理：%s',
                    MONITOR_MAP_FILE, self.task_queue.journal.path,
                    (f'每 {MONITOR_CLEANUP_INTERVAL // 86400} 天检查，保留 {MONITOR_MAP_RETENTION_DAYS} 天'
                     if MONITOR_MAP_RETENTION_DAYS else '关闭，永久保留'))
        path = self.tdl.tdl_path
        if os.path.isfile(path) and os.access(path, os.X_OK):
            logger.info('[启动/TDL] %s 存在且可执行；未检查 TDL 登录状态', path)
        else:
            logger.warning('[启动/TDL] %s 不存在或不可执行；请检查挂载和执行权限。需要 TDL 的任务不可用，Bot 继续启动', path)
        if not ADMIN_IDS:
            logger.warning('[启动/配置] ADMIN_IDS 为空；无人可使用管理面板或接收管理员通知')
        if not self.target_chat_id:
            logger.warning('[启动/配置] 未设置目标频道；转发前请用 /forwardto 或配置 FORWARD_TO_CHAT_ID')

    async def start(self) -> None:
        started = time.monotonic()
        self._stopping = False
        self._startup_phase = '配置'
        self._log_startup_settings()
        self._startup_phase = 'Bot 登录'
        if not self._bot_handlers_registered:
            self._register_bot_handlers()
            self._bot_handlers_registered = True
        logger.info('[启动/Bot 登录] 消息和按钮处理器已注册；正在连接 Telegram 并验证 Bot 身份')
        await self.client.start(bot_token=BOT_TOKEN)
        logger.info('[启动/Bot 登录] Bot 身份验证成功')
        if MONITOR_MAP_RETENTION_DAYS and self._monitor_cleanup_task is None:
            self._monitor_cleanup_task = asyncio.create_task(self.monitor_cleanup_loop())
        self._startup_phase = '用户监听'
        await self.start_user_monitor()
        self._startup_phase = '队列恢复'
        logger.info('[启动/队列] 正在恢复持久化任务')
        await self.task_queue.start()

        self._startup_phase = '管理员通知'
        notified = 0
        if ADMIN_IDS:
            logger.info('[启动/通知] 正在向 %d 位管理员发送启动面板', len(ADMIN_IDS))
        for admin_id in ADMIN_IDS:
            try:
                await self.admin_panel.open(admin_id, partial(self.client.send_message, admin_id))
                notified += 1
            except Exception as e:
                logger.warning('[启动/通知] 启动面板发送失败（%s）；不影响 Bot 运行', type(e).__name__)

        target = self.monitor_target_channel_id()
        if not self.monitor_chat_ids:
            sync = '未配置监听源'
        elif not target:
            sync = '等待设置目标频道'
        elif all(source == target for source in self.monitor_chat_ids):
            sync = '源频道与目标相同，跳过复制'
        else:
            sync = '等待源频道更新'
        listener = 'Bot + 用户账号' if self.is_user_monitor_active() else '仅 Bot'
        logger.info('[启动/就绪] Bot 已启动，开始接收更新；监听客户端=%s；自动同步=%s；管理员面板成功 %d/%d；启动耗时 %.2f 秒',
                    listener, sync, notified, len(ADMIN_IDS), time.monotonic() - started)

        self._startup_phase = None
        await self.client.run_until_disconnected()

    async def stop(self) -> None:
        self.begin_shutdown()
        if self._admission_alert is not None:
            await asyncio.gather(self._admission_alert, return_exceptions=True)
            self._admission_alert = None
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
        if not await asyncio.to_thread(self.forward_log.flush):
            logger.error("停服时未能刷新转发历史；部分内存记录未保存")
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
    logger.info('[启动/配置] 必填项 API_ID、API_HASH、BOT_TOKEN 已填写；尚未验证凭据')
    try:
        bot = TelegramBot()
    except Exception as exc:
        logger.error('[启动/失败] 阶段=初始化，异常=%s；请检查本地存储和客户端配置', type(exc).__name__)
        raise SystemExit(1) from None
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
        logger.error('[%s] 机器人令牌无效或已过期，请检查 .env 中的 BOT_TOKEN 并重建容器；不要分享令牌。',
                     bot._startup_phase or '运行')
        raise SystemExit(2) from None
    except errors.FloodWaitError as exc:
        logger.error('[%s] Telegram 限流，请在 %s 秒后重试', bot._startup_phase or '运行', exc.seconds)
        raise SystemExit(1) from None
    except Exception as exc:
        if bot._startup_phase is not None:
            hint = {'Bot 登录': '请检查 Telegram 网络连接和应用凭据',
                    '队列恢复': '请检查任务队列文件及 monitor-data 卷权限和剩余空间',
                    '用户监听': '请检查用户会话配置和网络连接'}.get(bot._startup_phase, '请检查上一条启动日志')
            logger.error('[启动/失败] 阶段=%s，异常=%s；Bot 未就绪。%s', bot._startup_phase, type(exc).__name__, hint)
        else:
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


