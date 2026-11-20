"""Drive the registered bot handlers with real Telethon keyboard objects, without a Telegram session."""
import asyncio
import itertools
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telethon import TelegramClient, events, types
from test_monitor_sync import app, FakeClient, SOURCE, TARGET

UID = 61


class PrivatePanelTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(patch.stopall)
        for key, val in dict(ADMIN_IDS=[UID], SUBMITTER_IDS={71}, USER_MONITOR_ENABLED=False,
                             MONITOR_MAP_RETENTION_DAYS=0, MONITOR_MAP_FILE=str(Path(tmp.name)/'map.db'),
                             TelegramClient=FakeClient, ForwardLog=Mock).items():
            patch.object(app, key, val).start()
        self.bot = app.TelegramBot()
        self.bot.monitor_chat_ids, self.bot.target_chat_id = [SOURCE], TARGET
        self.client = self.bot.client
        self.message_ids = itertools.count(1)
        self.messages = {}
        self.client.send_message.side_effect = self.send
        self.client.edit_message.side_effect = self.edit
        self.bot.task_queue.start = AsyncMock()  # keep admitted tasks available for inspection
        await self.bot.start()
        self.new, self.cb = (self.client.handlers[k] for k in (events.NewMessage, events.CallbackQuery))
        self.bot.do_forward = AsyncMock()
        self.addAsyncCleanup(self.bot.stop)

    def send(self, chat, text, **kw):
        mid = next(self.message_ids)
        msg = NS(id=mid, chat_id=chat, raw_text=text, out=True, buttons=kw.get('buttons'),
                 client=self.client, edit=AsyncMock())
        self.messages[mid] = msg
        return msg

    def edit(self, chat, mid, text, **kw):
        msg = self.messages[mid]
        self.assertEqual(msg.chat_id, chat)
        msg.raw_text, msg.buttons = text, kw.get('buttons')
        return msg

    @property
    def session(self):
        return self.bot.admin_panel.sessions[UID]

    @property
    def page(self):
        return self.messages[self.session.mid]

    def button(self, label):
        markup = TelegramClient.build_reply_markup(self.page.buttons)
        return next(b.data for row in markup.rows for b in row.buttons if b.text == label)

    async def hit(self, data, uid=UID, chat=UID, mid=None, via_inline=False):
        ev = NS(data=data, sender_id=uid, chat_id=chat, message_id=self.session.mid if mid is None else mid,
                via_inline=via_inline, answer=AsyncMock(), respond=AsyncMock(side_effect=lambda t, **kw: self.send(chat,t,**kw)),
                client=self.client)
        await self.cb(ev)
        return ev

    async def click(self, label):
        ev = await self.hit(self.button(label))
        ev.answer.assert_awaited_once_with()

    async def text(self, value, *, reply=None, uid=UID, chat=UID, forwarded=False):
        msg = NS(id=next(self.message_ids), text=value, raw_text=value, forward=forwarded, grouped_id=None,
                 post=False, peer_id=types.PeerUser(uid) if chat > 0 else types.PeerChat(-chat),
                 reply_to_msg_id=reply, media=None)
        ev = NS(message=msg, sender_id=uid, chat_id=chat, out=False, is_channel=False,
                raw_text=value, client=self.client, get_reply_message=AsyncMock(return_value=self.messages.get(reply)),
                respond=AsyncMock(side_effect=lambda t, **kw: self.send(chat,t,**kw)))
        await self.new(ev)
        return ev

    async def reply(self, text):
        return await self.text(text, reply=self.session.prompt_id)

    async def test_home_pages_refresh_back_and_context(self):
        self.assertEqual(self.page.raw_text, app.START_MESSAGE_ADMIN)
        self.assertEqual(len(self.page.buttons), 4)
        for label, expected in [('🎯 目标频道', str(TARGET)), ('📡 监听频道', str(SOURCE)),
                                ('📋 任务队列', '队列为空'), ('📝 转发记录', '暂无转发'),
                                ('📖 使用向导', '①'), ('⚙️ 系统管理', '系统管理')]:
            with self.subTest(page=label):
                if label == '📝 转发记录':
                    self.bot.forward_log.get_all.return_value = []
                await self.click(label)
                self.assertIn(expected, self.page.raw_text)
                if label not in ('⚙️ 系统管理', '📖 使用向导'):
                    await self.click('🔄 刷新')
                    self.assertIn(expected, self.page.raw_text)
                await self.click('⬅️ 返回面板')
        self.client.send_message.assert_awaited_once()  # navigation edited the original
        group = await self.text('/start', chat=-90)
        self.assertNotIn('buttons', group.respond.await_args.kwargs)
        user = await self.text('/start', uid=71, chat=71)
        self.assertNotIn('buttons', user.respond.await_args.kwargs)
        await self.text('/help')
        self.assertEqual(self.page.raw_text, app.HELP_MESSAGE_ADMIN)
        await self.click('⬅️ 返回面板')

    async def test_button_owner_generation_and_message_binding(self):
        key = self.button('🎯 目标频道')
        for kw in (dict(uid=71, chat=71), dict(uid=71), dict(chat=-90), dict(mid=999), dict(via_inline=True)):
            ev = await self.hit(key, **kw)
            self.assertTrue(ev.answer.await_args.kwargs['alert'])
        with patch.object(app, 'ADMIN_IDS', [UID, 72]):
            ev = await self.hit(key, uid=72, chat=72)
            self.assertTrue(ev.answer.await_args.kwargs['alert'])
        with patch.object(app, 'ADMIN_IDS', []):
            ev = await self.hit(key)
            self.assertTrue(ev.answer.await_args.kwargs['alert'])
        ev = await self.hit(key + b'forged')
        self.assertTrue(ev.answer.await_args.kwargs['alert'])
        old_mid = self.session.mid
        await self.text('/start')
        stale = await self.hit(key, mid=old_mid)
        self.assertTrue(stale.answer.await_args.kwargs['alert'])
        self.client.edit_message.assert_not_awaited()

    async def test_sync_is_reply_only_and_mapped_edit_only(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 91})
        link = f'https://t.me/c/{SOURCE}/7'
        await self.click('🔄 手动同步')
        prompt = self.session.prompt_id
        self.assertIsInstance(self.messages[prompt].buttons, types.ReplyKeyboardForceReply)
        await self.text(link)
        self.bot.do_forward.assert_awaited_once()
        await self.reply(link)
        self.assertIn('已加入原地同步队列', self.page.raw_text)
        q = self.bot.task_queue.queue
        self.assertEqual(q.qsize(), 1)
        task = q.get_nowait(); q.task_done()
        self.assertEqual((task.mode, task.source_chat_id, task.message_ids, task.target_chat_id),
                         ('monitor_edit', SOURCE, [7], TARGET))
        await self.text(link, reply=prompt)
        self.assertTrue(q.empty())
        self.bot.do_forward.assert_awaited_once()
        await self.click('⬅️ 返回面板'); await self.click('🔄 手动同步')
        await self.reply(f'https://t.me/c/{SOURCE}/8')
        self.assertIn('没有转发映射', self.page.raw_text)
        self.assertTrue(q.empty())

    async def test_old_prompt_never_becomes_submission_and_plain_reply_is_preserved(self):
        link = f'https://t.me/c/{SOURCE}/23'
        for reason in ('cancel', 'expired', 'restart', 'new_panel', 'unauthorized', 'forwarded'):
            with self.subTest(reason=reason):
                await self.text('/start'); await self.click('🔄 手动同步')
                mid = self.session.prompt_id
                if reason == 'cancel':
                    await self.click('取消并返回')
                elif reason == 'expired':
                    self.session.expires = 1
                elif reason == 'restart':
                    self.bot.admin_panel.sessions.clear()
                elif reason == 'new_panel':
                    await self.text('/help')
                if reason == 'unauthorized':
                    with patch.object(app, 'ADMIN_IDS', []):
                        await self.text(link, reply=mid)
                else:
                    await self.text(link, reply=mid, forwarded=reason == 'forwarded')
                self.bot.do_forward.assert_not_awaited()
                self.assertTrue(self.bot.task_queue.queue.empty())
        normal = self.send(UID, 'ordinary message')
        await self.text(link, reply=normal.id)
        self.bot.do_forward.assert_awaited_once()

    async def test_export_preview_and_concurrent_confirmation_admit_once(self):
        await self.click('📦 批量转发'); await self.reply(f'https://t.me/c/{SOURCE}/100')
        await self.reply(f'https://t.me/c/{SOURCE}/110')
        for detail in (str(SOURCE), str(TARGET), '100 至 110'):
            self.assertIn(detail, self.page.raw_text)
        self.assertTrue(self.bot.task_queue.queue.empty())
        confirm = self.button('确认执行')
        await asyncio.gather(self.hit(confirm), self.hit(confirm))
        q = self.bot.task_queue.queue
        self.assertEqual(q.qsize(), 1)
        task = q.get_nowait(); q.task_done()
        self.assertEqual((task.mode, task.source_chat_id, task.message_ids, task.target_chat_id),
                         ('export_range', SOURCE, [100,110], TARGET))
        self.assertNotEqual(task.status_msg_id, self.session.mid)
        self.bot.do_forward.assert_not_awaited()

    async def test_invalid_export_and_changed_destination_are_never_admitted(self):
        for case in ('wrong_channel', 'reversed', 'bad_link', 'changed_before_confirm'):
            with self.subTest(case=case):
                await self.text('/start'); await self.click('📦 批量转发')
                await self.reply(f'https://t.me/c/{SOURCE}/100')
                dst = SOURCE + (case == 'wrong_channel')
                last = 99 if case == 'reversed' else 105
                await self.reply(f'https://t.me/c/{dst}/{last}' + ('?comment=3' if case == 'bad_link' else ''))
                if case == 'changed_before_confirm':
                    self.bot.target_chat_id += 1
                    await self.click('确认执行')
                    self.assertIn('已改变', self.page.raw_text)
                self.assertTrue(self.bot.task_queue.queue.empty())
                self.bot.do_forward.assert_not_awaited()

    async def test_target_and_monitor_changes_preview_and_share_command_state(self):
        await self.click('🎯 目标频道'); await self.click('✏️ 更换目标'); await self.reply(str(TARGET+1))
        self.assertIn(f'{TARGET} → {TARGET+1}', self.page.raw_text)
        self.assertEqual(self.bot.target_chat_id, TARGET)
        await self.click('确认执行'); self.assertEqual(self.bot.target_chat_id, TARGET+1)
        await self.click('⬅️ 返回面板'); await self.click('📡 监听频道'); await self.click('➕ 添加')
        await self.reply('12345'); self.assertIn(12345, self.bot.monitor_chat_ids)
        await self.click('⬅️ 返回面板'); await self.click('📡 监听频道'); await self.click('替换整个列表')
        await self.reply('123，456,123'); self.assertIn('123, 456', self.page.raw_text)
        self.assertNotIn(456, self.bot.monitor_chat_ids)
        await self.click('确认执行'); self.assertEqual(self.bot.monitor_chat_ids, [123,456])
        await self.click('⬅️ 返回面板'); await self.click('📡 监听频道'); await self.click('➖ 点选移除')
        await self.click('123'); await self.click('确认执行'); self.assertEqual(self.bot.monitor_chat_ids, [456])
        await self.click('⬅️ 返回面板'); await self.click('📡 监听频道'); await self.click('清空监听…')
        await self.click('取消'); self.assertEqual(self.bot.monitor_chat_ids, [456])
        await self.click('📡 监听频道'); await self.click('清空监听…'); await self.click('确认执行')
        self.assertEqual(self.bot.monitor_chat_ids, [])

    async def test_stop_clear_history_and_restart_require_fresh_confirmation(self):
        self.bot._restart = AsyncMock()
        self.bot.task_queue.stop_tdl_tasks = AsyncMock(return_value=(True, 2))
        self.bot.forward_log.get_all.return_value = []
        for page, control, op in [('📝 转发记录','🗑 清空本地记录…', self.bot.forward_log.clear),
                                  ('📋 任务队列','⏹ 停止任务…',self.bot.task_queue.stop_tdl_tasks),
                                  ('⚙️ 系统管理','🔄 重启机器人…',self.bot._restart)]:
            with self.subTest(action=control):
                await self.text('/start'); await self.click(page); await self.click(control)
                old = self.button('确认执行')
                self.session.expires = 1
                await self.hit(old)
                op.assert_not_called()
                await self.text('/start'); await self.click(page); await self.click(control)
                confirm = self.button('确认执行')
                await self.hit(old); op.assert_not_called()
                await asyncio.gather(self.hit(confirm), self.hit(confirm))
                op.assert_called_once()

    async def test_input_is_not_a_command_and_cancel_never_submits(self):
        self.bot._restart = AsyncMock()
        for arg in ('/restart', '取消', f'https://t.me/c/{SOURCE}/8?comment=3'):
            await self.text('/start'); await self.click('🔄 手动同步')
            await self.reply(arg)
            self.assertTrue(self.bot.task_queue.queue.empty())
            self.bot.do_forward.assert_not_awaited()
            self.bot._restart.assert_not_awaited()
        await self.text('/start'); await self.click('📦 批量转发')
        await self.reply(f'https://t.me/c/{SOURCE}/2147483648')
        self.assertEqual(self.session.prompt_id, 0)
        self.assertIn('无效', self.page.raw_text)

    async def test_changed_monitor_list_and_authorization_in_flight(self):
        await self.click('📡 监听频道'); await self.click('清空监听…')
        self.bot.monitor_chat_ids.append(123)
        await self.click('确认执行')
        self.assertIn('已改变', self.page.raw_text)
        self.assertEqual(self.bot.monitor_chat_ids, [SOURCE, 123])
        await self.click('⬅️ 返回面板'); await self.click('📋 任务队列'); await self.click('⏹ 停止任务…')
        self.bot.task_queue.stop_tdl_tasks = AsyncMock()
        key = self.button('确认执行')
        with patch.object(app, 'ADMIN_IDS', []):
            await self.hit(key)
        self.bot.task_queue.stop_tdl_tasks.assert_not_awaited()

    async def test_remove_pagination_does_not_assume_channel_index(self):
        self.bot.monitor_chat_ids = list(range(11, 22))
        await self.click('📡 监听频道'); await self.click('➖ 点选移除'); await self.click('下一页')
        await self.click('20'); self.assertIn('移除 20', self.page.raw_text)
        await self.click('确认执行')
        self.assertNotIn(20, self.bot.monitor_chat_ids)
        self.assertIn(19, self.bot.monitor_chat_ids)

    async def test_feedback_failure_does_not_replay_a_completed_action(self):
        self.bot.forward_log.get_all.return_value = []
        await self.click('📝 转发记录'); await self.click('🗑 清空本地记录…')
        key, mid = self.button('确认执行'), self.session.mid
        self.client.edit_message.side_effect = ConnectionError('screen update failed')
        with self.assertLogs('admin_panel', 'ERROR'):
            await self.hit(key, mid=mid)
        self.assertNotIn(UID, self.bot.admin_panel.sessions)
        await self.hit(key, mid=mid)
        self.bot.forward_log.clear.assert_called_once()
        self.assertIn('核对当前状态', self.client.send_message.await_args.args[1])
