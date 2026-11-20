"""Offline regressions for batch export, native album delivery, and review pagination."""
import asyncio
import json
import re
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bot_fixture import MonitorHarness, SOURCE, TARGET, app


class ReviewFixTests(MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def test_range_passes_all_messages_to_forward(self):
        forwarded = []
        async def tdl(cmd, progress):
            if cmd[1:3] == ['chat', 'export']:
                self.assertEqual(cmd[cmd.index('-i') + 1], '11,12')
                rows = [{'id': 11, 'type': 'text'}, {'id': 12, 'type': 'photo'}]
                Path(cmd[cmd.index('-o') + 1]).write_text(json.dumps(
                    rows if '--all' in cmd else rows[1:]))
            else:
                forwarded.extend(json.loads(Path(cmd[cmd.index('--from') + 1]).read_text()))
            return 0, '', ''
        self.bot.tdl._run_command = tdl
        await self.bot.task_queue.add_task(self.event(11), 'range', None, 1, mode='export_range',
            message_ids=[11, 12], source_chat_id=SOURCE, target_chat_id=TARGET)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertEqual([row['id'] for row in forwarded], [11, 12])

    async def test_timer_before_album_event_never_sends_subset(self):
        q = self.bot.task_queue
        client = self.bot.client
        e = self.event(12, album=44)
        client.get_messages.return_value = e.messages
        with patch.object(app, 'MONITOR_ALBUM_WAIT', 0):
            await self.bot.enqueue_monitor_post(e, e.messages, album_member=True)
            await asyncio.sleep(0.01)
        task = await self.process()
        self.assertEqual(task.status, app.TaskStatus.PENDING)
        self.assertFalse(task.send_started)
        client.forward.assert_not_awaited()
        full = self.event(12, 14, album=44)
        client.get_messages.return_value = [*full.messages, *self.event(13, album=45).messages]
        await self.enqueue(full)
        await self.process()
        timer, delayed = q._retry_timers.popitem(); timer.cancel()
        q.queue.put_nowait(delayed); await self.process()
        client.forward.assert_awaited_once()
        self.assertEqual(client.forward.await_args.args[0].id, [12, 14])

    async def test_parked_album_wakes_only_on_matching_update(self):
        e = self.event(8, album=10)
        await self.enqueue(e)
        q = self.bot.task_queue
        self.bot.client.get_messages.return_value = e.messages
        t = q.queue.get_nowait(); q.queue.task_done(); t.retries = 2
        await q._process_task(t)
        self.assertEqual(q.journal.restore()[0][1], 'wait_album')
        self.bot.client.forward.assert_not_awaited()
        await self.enqueue(self.event(9, album=12))
        self.assertIn(t.job_id, q._peer_wait)
        full = self.event(8, 11, album=10)
        await self.enqueue(full)
        self.assertNotIn(t.job_id, q._peer_wait)
        self.assertEqual(t.message_ids, [8, 11])
        await q.stop_tdl_tasks()
        self.assertEqual(q.journal.restore(), [])

    async def test_parked_album_restores_or_cancels_without_sending(self):
        q = self.bot.task_queue
        await self.enqueue(self.event(8, album=10))
        t = q.queue.get_nowait(); q.queue.task_done(); t.retries = 2
        self.bot.client.get_messages.return_value = self.event(8, album=10).messages
        await q._process_task(t)
        await self.bot.stop()
        self.bot = self.make_bot()
        q = self.bot.task_queue
        q._restore()
        self.bot.client.get_messages.return_value = self.event(8, album=10).messages
        await self.process()
        self.assertIn(t.job_id, q._peer_wait)
        self.assertEqual(await q.stop_tdl_tasks(), (False, 1))
        self.assertEqual(q.journal.restore(), [])
        self.bot.client.forward.assert_not_awaited()

    async def test_review_cursor_keeps_older_rows_reachable_on_new_insert(self):
        q = self.bot.task_queue
        async def review(i):
            await q.add_task(self.event(1), f'post{i}', None, 1, target_chat_id=TARGET)
            t = q.queue.get_nowait(); q.queue.task_done(); q.journal.save(t, 'review')
        async def page(*args):
            respond = AsyncMock()
            with patch.object(app, 'ADMIN_IDS', [61]):
                await self.bot.run_admin_action(NS(sender_id=61), ['/queue', *args], respond)
            return respond.await_args.args[0]
        for i in range(6): await review(i)
        first = await page()
        self.assertEqual(re.findall(r'#(\d+) clone', first), ['6','5','4','3','2'])
        cursor = re.search(r'下一页：/queue (\d+)', first)[1]
        await review(6)
        last = await page(cursor)
        self.assertEqual(re.findall(r'#(\d+) clone', last), ['1'])
        self.assertNotIn('下一页', last)
        for args in [('0',), ('9999999999999999999',), ('2', 'x')]:
            self.assertIn('用法', await page(*args))
