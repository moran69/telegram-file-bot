"""Private admin UI. Callback payloads are opaque; only the current screen owns its actions."""
import asyncio
import logging
import secrets
import time
from dataclasses import dataclass, field
from functools import partial

from telethon import Button, types

logger = logging.getLogger(__name__)

PREFIX = b'ap1:'
PROMPT = '⌨️ 管理面板输入｜'
TTL = 300
HOME = [[('🎯 目标频道', 'target'), ('📡 监听频道', 'monitor')],
        [('🔄 手动同步', 'input:sync'), ('📦 批量转发', 'input:export')],
        [('📋 任务队列', 'queue'), ('📝 转发记录', 'history')],
        [('📖 使用向导', 'help'), ('⚙️ 系统管理', 'system')]]
BACK = [('⬅️ 返回面板', 'home')]
PAGES = {
    'target': ('/showto', [[('✏️ 更换目标', 'input:target')]]),
    'monitor': ('/monitor', [[('➕ 添加', 'input:add'), ('➖ 点选移除', 'remove:0')],
                             [('替换整个列表', 'input:set'), ('清空监听…', 'ask:monclear')]]),
    'queue': ('/queue', [[('⏹ 停止任务…', 'ask:stop')]]),
    'history': ('/flog', [[('🗑 清空本地记录…', 'ask:logclear')]]),
}
QUESTIONS = {
    'sync': '请回复这条提示，粘贴源频道原帖链接（如 https://t.me/c/1234567890/42）。机器人会回复是否已入队；只更新已映射的目标帖，不另发。',
    'export': '第 1 步：请回复起始原帖的 t.me/c 消息链接。',
    'end': '已收到起始链接，尚未入队。第 2 步：请回复这条提示，粘贴同一源频道的结束原帖链接。',
    'target': '请回复新目标的频道裸 ID 或 Telegram 标记后的频道 ID。',
    'add': '请回复要监听的源频道裸 ID。',
    'set': '请回复完整的新列表，用英文逗号分隔源频道裸 ID（这将替换旧列表）。',
}
DANGERS = {
    'monclear': (('/monitor', 'clear'), '清空当前运行中的全部监听源。不会删除频道消息。'),
    'logclear': (('/flog', 'clear'), '清空全部本地转发记录。不会删除频道消息或同步映射。'),
    'stop': (('/tdlstop',), '停止当前手动转发，清空等待任务和定时重试；已开始的自动同步不会被强停。'),
    'restart': (('/restart',), '重启机器人进程。运行中的任务可能中断，临时配置和面板输入不会保留。'),
}


@dataclass
class Session:
    owner: int
    mid: int = 0
    choices: dict[bytes, str] = field(default_factory=dict)
    prompt_id: int = 0
    stage: str = ''
    start_link: str = ''
    command: tuple[str, ...] = ()
    guard: object = None
    expires: float = 0


class AdminPanel:
    def __init__(self, bot, *, allowed, home, guide, target_id, source_id, source_list, post_link, export_range):
        self.bot, self.allowed = bot, allowed
        self.home, self.guide = home, guide
        self.target_id, self.source_id, self.source_list = target_id, source_id, source_list
        self.post_link, self.export_range = post_link, export_range
        self.sessions: dict[int, Session] = {}  # At most one current panel and lock per administrator.
        self.locks: dict[int, asyncio.Lock] = {}

    def _lock(self, uid):
        if uid not in self.locks:
            self.locks[uid] = asyncio.Lock()
        return self.locks[uid]

    @staticmethod
    def _keyboard(session, rows):
        token = PREFIX + secrets.token_hex(8).encode() + b':'
        buttons, choices = [], {}
        for row in rows:
            line = []
            for label, action in row:
                data = token + str(len(choices)).encode()
                choices[data] = action
                line.append(Button.inline(label, data))
            buttons.append(line)
        session.choices = choices
        return buttons

    async def open(self, uid, send, *, guide=False):
        if not self.allowed(uid):
            return
        async with self._lock(uid):
            if not self.allowed(uid):
                return
            session = Session(uid)
            msg = await send(self.guide if guide else self.home, link_preview=False,
                             buttons=self._keyboard(session, [BACK] if guide else HOME))
            session.mid = msg.id
            if self.allowed(uid):
                self.sessions[uid] = session

    @staticmethod
    def _fit(text):
        # Both the panel and its receipts may contain long runtime lists.
        encoded = text.encode('utf-16-le', errors='replace')
        if len(encoded) > 8192:
            return encoded[:8000].decode('utf-16-le', errors='ignore') + '\n…（列表已截短）'
        return text

    async def _screen(self, s, text, rows):
        await self.bot.client.edit_message(s.owner, s.mid, self._fit(text), buttons=self._keyboard(s, rows),
                                           parse_mode=None, link_preview=False)

    async def callback(self, event):
        if not isinstance(event.data, bytes) or not event.data.startswith(PREFIX):
            return
        uid = event.sender_id
        if uid != event.chat_id or getattr(event, 'via_inline', False) or not self.allowed(uid):
            await event.answer('仅限管理员在与机器人的私聊中操作。', alert=True)
            return
        async with self._lock(uid):
            s = self.sessions.get(uid)
            if not self.allowed(uid) or not s or event.message_id != s.mid or event.data not in s.choices:
                await event.answer('按钮已失效，请使用最新面板或发送 /start。', alert=True)
                return
            action = s.choices[event.data]
            s.choices.clear()  # Consume this generation before any network I/O.
            s.prompt_id = 0
            try:
                if action == 'confirm':
                    await event.answer('已收到确认，正在核对操作…')
                elif action == 'cancel':
                    await event.answer('已取消，本次操作未执行。')
                elif action in PAGES:
                    await event.answer('正在读取当前状态…')
                else:
                    await event.answer()
                if not self.allowed(uid):
                    return
                if s.expires and time.monotonic() >= s.expires and action not in ('home', 'cancel'):
                    await self._result(s, '⌛ 操作已过期，未执行。')
                elif action == 'confirm':
                    await self._execute(s, event, s.command, s.guard, partial(self._confirmed_result, s))
                else:
                    s.command, s.stage, s.expires = (), '', 0
                    await self._navigate(s, event, action)
            except Exception:
                await self._unavailable(s)

    async def _unavailable(self, s):
        self.sessions.pop(s.owner, None)
        logger.exception('管理面板操作或消息更新失败')
        try:
            await self.bot.client.send_message(s.owner, '面板未能完成反馈。请用 /start 重开并核对当前状态，不要直接重复操作。')
        except Exception:
            logger.warning('无法投递面板恢复提示，旧面板已失效', exc_info=True)

    async def _navigate(self, s, event, action):
        if action in ('home', 'cancel'):
            await self._screen(s, self.home, HOME)
        elif action == 'help':
            await self._screen(s, self.guide, [BACK])
        elif action == 'system':
            await self._screen(s, '⚙️ 系统管理', [[('🔄 重启机器人…', 'ask:restart')], BACK])
        elif action in PAGES:
            cmd, options = PAGES[action]
            async def display(text, **kwargs):
                if action == 'monitor':
                    text = '📌 修改仅本次运行有效；重启以环境变量为准。\n\n' + text
                await self._screen(s, text, [*options, [('🔄 刷新', action), *BACK]])
            await self.bot.run_admin_action(event, [cmd], display)
        elif action.startswith('remove:'):
            ids = self.bot.monitor_chat_ids
            page = min(int(action.split(':')[1]), max(0, (len(ids) - 1) // 8))
            rows = [[(str(cid), f'del:{cid}')] for cid in ids[page*8:page*8+8]]
            nav = []
            if page:
                nav.append(('上一页', f'remove:{page-1}'))
            if (page+1)*8 < len(ids):
                nav.append(('下一页', f'remove:{page+1}'))
            await self._screen(s, '请选择要移除的源频道：' if ids else '监听列表为空。',
                               rows + ([nav] if nav else []) + [[('⬅️ 返回监听', 'monitor')]])
        elif action.startswith('del:'):
            cid = action.split(':')[1]
            await self._confirm(s, ('/monitor', 'del', cid), f'从监听列表移除 {cid}。')
        elif action.startswith('ask:'):
            cmd, warning = DANGERS[action.split(':')[1]]
            await self._confirm(s, cmd, warning)
        elif action.startswith('input:'):
            stage = action.split(':')[1]
            if stage in ('sync', 'export') and self.bot.target_chat_id is None:
                await self._result(s, '❌ 请先在「目标频道」设置接收频道。')
                return
            s.guard = self.bot.target_chat_id if stage in ('sync', 'export') else None
            await self._prompt(s, stage)

    def _snapshot(self, cmd):
        if cmd[0] in ('/forwardto', '/export_range', '/resync'):
            return self.bot.target_chat_id
        if cmd[0] == '/monitor':
            return tuple(self.bot.monitor_chat_ids)
        return None

    async def _prompt(self, s, stage, *, reply_to=None):
        s.stage, s.expires = stage, time.monotonic() + TTL
        await self._screen(s, '⌨️ 等待输入：请回复下方专用提示。直接发送链接仍按普通投稿处理。',
                           [[('取消并返回', 'cancel')]])
        text = PROMPT + QUESTIONS[stage] + '\n5 分钟内有效；回复「取消」可退出。'
        if stage in ('sync', 'export', 'end'):
            text += f'\n当前目标：{s.guard}'
        prompt = await self.bot.client.send_message(s.owner, text, buttons=Button.force_reply(single_use=True),
                                                    link_preview=False, reply_to=reply_to)
        s.prompt_id = prompt.id

    async def remind_if_waiting(self, event):
        """An unthreaded, otherwise ignored text is not consent to run a panel action."""
        uid = event.sender_id
        if uid != event.chat_id or not self.allowed(uid) or getattr(event.message, 'reply_to_msg_id', None):
            return
        s = self.sessions.get(uid)
        if s and s.prompt_id and time.monotonic() < s.expires:
            await event.respond('↩️ 这条消息没有回复到输入提示，未执行操作。请对「⌨️ 管理面板输入」那条消息点「回复」，再发送内容。',
                                reply_to=event.message.id)

    async def reply(self, event):
        """Return True for a panel input, including stale replies; never reinterpret one as a post."""
        uid = event.sender_id
        reply_id = getattr(event.message, 'reply_to_msg_id', None)
        if not reply_id or uid != event.chat_id or not uid or uid <= 0:
            return False
        s = self.sessions.get(uid)
        if not s or s.prompt_id != reply_id:
            # The visible marker on our own prompt survives expiry, cancellation and process restart.
            try:
                original = await event.get_reply_message()
            except Exception:
                original = None
            if original is None or isinstance(original, types.MessageEmpty):
                if self.allowed(uid):
                    await event.respond('无法识别被回复的消息，未执行或投稿。请单独发送，或重新打开面板。')
                return True
            if not original.out or not (original.raw_text or '').startswith(PROMPT):
                return False
        if not self.allowed(uid):
            return True
        async with self._lock(uid):
            s = self.sessions.get(uid)
            if not self.allowed(uid):
                return True
            if not s or s.prompt_id != reply_id:
                await event.respond('此输入提示已失效，未执行或投稿。请使用最新面板。', reply_to=event.message.id)
                return True
            s.prompt_id = 0  # a second reply cannot consume the same prompt
            result = partial(self._input_result, s, event)
            try:
                if time.monotonic() >= s.expires:
                    await result('⌛ 输入已过期，未执行或投稿。')
                elif event.message.forward or (event.message.media and not isinstance(event.message.media, types.MessageMediaWebPage)):
                    await result('请回复纯文本或原帖链接，不要转发消息。未执行或投稿。')
                else:
                    await self._submit(s, event, (event.raw_text or '').strip(), result)
            except Exception:
                await self._unavailable(s)
            return True

    def _is_post(self, value):
        if len(value.split()) != 1:
            return False
        src, mid = self.post_link(value)
        return src is not None and self.source_id(str(src)) is not None and mid is not None and 0 < mid < 2**31

    async def _submit(self, s, event, value, result):
        stage = s.stage
        if value == '取消':
            await result('已取消。')
            return
        if stage in ('sync', 'export', 'end') and self.bot.target_chat_id != s.guard:
            await result('目标频道已改变，请重新开始。')
            return
        if stage == 'sync':
            if self._is_post(value):
                await self._execute(s, event, ('/resync', value), s.guard, result)
            else:
                await result('❌ 链接无效，未加入同步队列。请重新点「手动同步」，回复源频道原帖链接，如 https://t.me/c/1234567890/42。')
        elif stage in ('export', 'end') and self._is_post(value):
            if stage == 'export':
                s.start_link = value
                await self._prompt(s, 'end', reply_to=event.message.id)
            else:
                cmd = ('/export_range', s.start_link, value)
                src, first, last, error = self.export_range(cmd)
                if not error:
                    await self._confirm(s, cmd, f'源频道：{src}\n目标频道：{s.guard}\n消息 ID 范围：{first} 至 {last}', event=event)
                else:
                    await result('❌ 起止链接必须是同一频道的有效原帖，并按 ID 升序填写。未入队。')
        elif stage == 'target' and (cid := self.target_id(value)) is not None:
            await self._confirm(s, ('/forwardto', str(cid)), f'更换目标频道：{self.bot.target_chat_id} → {cid}', event=event)
        elif stage == 'add' and (cid := self.source_id(value)) is not None:
            await self._execute(s, event, ('/monitor', 'add', str(cid)), self._snapshot(('/monitor',)), result)
        elif stage == 'set' and (ids := self.source_list(value)):
            listing = ', '.join(map(str, ids))
            if len(listing) > 3200:
                await result('列表太长，无法完整预览。请分批添加，或使用 /monitor set。')
            else:
                await self._confirm(s, ('/monitor', 'set', ','.join(map(str, ids))), f'替换整个监听列表，新列表：\n{listing}', event=event)
        else:
            await result('❌ 输入无效，未执行或投稿。请从面板重新开始。')

    async def _confirm(self, s, cmd, detail, *, event=None):
        s.command, s.guard, s.expires = cmd, self._snapshot(cmd), time.monotonic() + TTL
        text = f'⚠️ 待确认 · 尚未执行\n\n{detail}\n\n请在 5 分钟内选择「确认执行」或「取消」。'
        rows = [[('确认执行', 'confirm'), ('取消', 'cancel')]]
        if event is not None:
            # Put the actionable card next to the input, not above the ForceReply prompt.
            try:
                card = await event.respond(text, reply_to=event.message.id, buttons=self._keyboard(s, rows),
                                           parse_mode=None, link_preview=False)
            except Exception:
                logger.warning('确认卡片发送失败，尝试更新原面板', exc_info=True)
            else:
                old_mid, s.mid = s.mid, card.id
                try:
                    await self.bot.client.edit_message(s.owner, old_mid, '已收到输入，请使用下方确认卡片。', buttons=None)
                except Exception:
                    logger.debug('旧面板未能更新，其按钮已失效', exc_info=True)
                return
        await self._screen(s, text, rows)

    async def _result(self, s, text, **kwargs):
        s.command, s.stage, s.prompt_id, s.expires = (), '', 0, 0
        await self._screen(s, text, [BACK])

    async def _confirmed_result(self, s, text, **kwargs):
        # Update the clicked card. If it disappeared, deliver the actual result, not a retry.
        try:
            await self._result(s, text)
        except Exception:
            logger.warning('确认结果未能写入面板，改发新面板', exc_info=True)
            msg = await self.bot.client.send_message(s.owner, self._fit(text), buttons=self._keyboard(s, [BACK]),
                                                     parse_mode=None, link_preview=False)
            s.mid = msg.id

    async def _input_result(self, s, event, text, *, link_preview=False):
        # Keep a durable receipt next to the submitted message even after navigating away.
        try:
            await event.respond(self._fit(text), reply_to=event.message.id, parse_mode=None, link_preview=link_preview)
        except Exception:
            logger.warning('输入回执发送失败，尝试更新原面板', exc_info=True)
            await self._result(s, text)
            return
        try:
            await self._result(s, text)
        except Exception:
            self.sessions.pop(s.owner, None)
            logger.exception('输入回执已发送，但面板更新失败')

    async def _execute(self, s, event, cmd, guard, result):
        if not cmd or not self.allowed(s.owner) or self._snapshot(cmd) != guard:
            await result('配置已改变或操作已失效。未执行，请重新开始。')
            return
        s.command, s.prompt_id, s.expires = (), 0, 0
        await self.bot.run_admin_action(event, list(cmd), result, panel_feedback=True)
