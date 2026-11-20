import asyncio
import json
import os
import runpy
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bot_fixture import MonitorHarness, SOURCE, TARGET, app, source


class RecoveryTests(MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def reopen(self):
        await self.bot.stop()
        self.bot = self.make_bot()
        self.bot.target_chat_id = TARGET + 1
        self.bot.task_queue._restore()
        return self.bot.task_queue

    async def test_precise_cleanup_keeps_gaps_and_legacy_uncertainty(self):
        m = self.bot.monitor_message_map()
        m.remember(SOURCE, TARGET, {100: 800, 102: 802})
        with patch.object(app.time, 'time', return_value=app.time.time()+8*86400):
            m.cleanup_if_due(7)
        S = app.TaskStatus
        self.assertEqual([m.copy_state(SOURCE, i, TARGET) for i in (99, 100, 101, 102)],
                         [S.PENDING, S.COMPLETED, S.PENDING, S.COMPLETED])
        with m.db:
            m.db.execute('INSERT INTO monitor_pruned_through VALUES(?,?,?)', (SOURCE, TARGET, 90))
        self.assertEqual(m.copy_state(SOURCE, 89, TARGET), S.UNCONFIRMED)
        self.assertEqual(m.copy_state(SOURCE, 90, TARGET), S.COMPLETED)
        self.assertEqual(set(m.reserve(SOURCE, TARGET, [89, 90, 99, 100, 101])), {99, 101})

    async def test_restore_album_and_latest_edit_to_admission_target(self):
        for mid in (8, 3, 8):
            ev = self.event(mid, album=55)
            await self.bot.enqueue_monitor_post(ev, ev.messages, album_member=True)
        for _ in range(3):
            await self.enqueue(self.event(8), edited=True)
        q = await self.reopen()
        self.assertEqual(q.queue.qsize(), 2)
        copy = await self.process()
        self.assertIsNone(copy.event)
        self.assertEqual((copy.message_ids, copy.target_chat_id, copy.status), ([3, 8], TARGET, app.TaskStatus.COMPLETED))
        edit = await self.process()
        self.assertEqual((edit.target_chat_id, edit.status), (TARGET, app.TaskStatus.COMPLETED))
        self.bot.client.edit_message.assert_awaited_once()
        self.assertEqual(q.journal.open().execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    async def test_early_edit_survives_restart_and_reconciles(self):
        await self.enqueue(self.event(6), edited=True)
        await self.process()
        q = await self.reopen()
        self.assertTrue(q.queue.empty())
        self.bot.target_chat_id = TARGET
        await self.enqueue(self.event(6))
        await self.process()
        await self.process()
        self.bot.client.edit_message.assert_awaited_once()

    async def test_stop_during_export_restores_unsent_range(self):
        q = self.bot.task_queue
        entered = asyncio.Event()
        async def exporting(*args):
            entered.set()
            await asyncio.Future()
        self.bot.tdl.export_range = AsyncMock(side_effect=exporting)
        await q.add_task(self.event(1), 'range', None, 1, mode='export_range', message_ids=[2, 5],
                         source_chat_id=SOURCE, target_chat_id=TARGET)
        await q.start()
        await asyncio.wait_for(entered.wait(), 2)
        q = await self.reopen()
        self.assertEqual(q.queue.qsize(), 1)
        self.bot.tdl.export_range = AsyncMock(return_value=(True, 'ok'))
        self.bot.tdl.forward_from_export = AsyncMock(return_value=(True, 'ok'))
        await self.process()
        self.assertEqual(self.bot.tdl.forward_from_export.await_args.args[1], TARGET)

    async def test_stop_during_send_only_restores_waiter(self):
        q = self.bot.task_queue
        entered = asyncio.Event()
        async def sending(*args):
            entered.set()
            await asyncio.Future()
        self.bot.tdl.forward_clone = AsyncMock(side_effect=sending)
        for link in ('first', 'waiting'):
            await q.add_task(self.event(1), link, None, 1, target_chat_id=TARGET)
        await q.start()
        await asyncio.wait_for(entered.wait(), 2)
        q = await self.reopen()
        self.assertEqual(q.queue.qsize(), 1)
        count, rows = q.journal.reviews()
        self.assertEqual((count, rows[0]['link']), (1, 'first'))
        self.bot.tdl.forward_clone = AsyncMock(return_value=(True, 'ok'))
        task = await self.process()
        self.assertEqual((task.link, task.target_chat_id), ('waiting', TARGET))

    async def test_edit_received_during_inflight_edit_survives_stop(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 99})
        entered = asyncio.Event()
        async def editing(*args, **kwargs):
            entered.set()
            await asyncio.Future()
        self.bot.client.edit_message.side_effect = editing
        await self.enqueue(self.event(7), edited=True)
        await self.bot.task_queue.start()
        await asyncio.wait_for(entered.wait(), 2)
        for _ in range(3):
            await self.enqueue(self.event(7), edited=True)
        q = await self.reopen()
        self.assertEqual(q.journal.reviews()[0], 0)  # Reconcile only the mapped destination, no new delivery.
        self.assertEqual(q.queue.qsize(), 1)
        self.bot.client.get_messages.side_effect = [NS(raw_text='latest', entities=[], media=None),
                                                    NS(raw_text='older', entities=[], media=None)]
        await self.process()
        self.bot.client.edit_message.assert_awaited_once()
        self.bot.client.forward.assert_not_awaited()

    async def test_pre_send_retry_keeps_original_time_and_target(self):
        ev = self.event(10)
        ev.get_input_chat.side_effect = app.errors.FloodWaitError(None, capture=60)
        await self.enqueue(ev)
        task = await self.process()
        self.assertEqual(task.status, app.TaskStatus.PENDING)
        q = await self.reopen()
        self.assertTrue(q.queue.empty())
        restored, = q._retry_timers.values()
        self.assertEqual((restored.ready_at, restored.retries, restored.target_chat_id),
                         (task.ready_at, 1, TARGET))
        self.assertEqual(await q.stop_tdl_tasks(), (False, 1))
        self.assertEqual((await self.reopen()).get_queue_status()['queue_size'], 0)

    async def test_tdlstop_cancels_waiters_without_recovery(self):
        for mid in (5, 7):
            ev = self.event(mid, album=83)
            await self.bot.enqueue_monitor_post(ev, ev.messages, album_member=True)
        await self.enqueue(self.event(5), edited=True)
        self.assertEqual(await self.bot.task_queue.stop_tdl_tasks(), (False, 2))
        q = await self.reopen()
        self.assertTrue(q.queue.empty())
        self.assertEqual(q.journal.reviews(), (0, []))

    async def test_failed_admission_never_claims_queued(self):
        ev = self.event(1)
        status = NS(id=88, chat_id=1, edit=AsyncMock(), client=self.bot.client)
        ev.respond = AsyncMock(return_value=status)
        with patch.object(self.bot.task_queue.journal, 'save', side_effect=sqlite3.OperationalError('disk full')), \
                self.assertLogs(app.logger, 'ERROR'):
            await self.bot.do_forward(ev)
        self.assertIn('未入队', status.edit.await_args.args[0])
        self.assertTrue(self.bot.task_queue.queue.empty())

    async def test_streamed_item_errors_survive_progress_tail_in_both_paths(self):
        async def run(cmd, report):
            for mid in (4, 19):
                await report(f'\x1b[31mSource(12):{mid} -> Target(33) error: upload failed\x1b[0m')
            for _ in range(40):
                await report('progress')
            return 0, 'finished', ''
        with patch.object(self.bot.tdl, '_run_command', side_effect=run):
            for method, arg in ((self.bot.tdl.forward_clone, 'link'), (self.bot.tdl.forward_from_export, 'file')):
                ok, reason = await method(arg, TARGET)
                self.assertFalse(ok)
                self.assertIsInstance(reason, app.UnconfirmedReceipt)
                for expected in ('12/4', '12/19', 'upload failed'):
                    self.assertIn(expected, reason)

    async def test_disabled_fallback_does_not_invoke_tdl(self):
        self.bot.client.forward.side_effect = app.errors.ChatForwardsRestrictedError(None)
        self.bot.tdl.forward_clone = AsyncMock()
        with patch.object(app, 'MONITOR_TDL_FALLBACK', False), self.assertLogs(app.logger, 'ERROR'):
            await self.enqueue(self.event(44))
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.bot.tdl.forward_clone.assert_not_awaited()


class HistoryWriterTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_background_writer_coalesces_and_preserves_new_add_during_clear(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            store = app.ForwardLog(path)
            writes = []
            entered, release = threading.Event(), threading.Event()
            original = store._save
            def slow(snapshot):
                self.assertNotEqual(threading.get_ident(), loop_thread)
                writes.append(len(snapshot))
                if len(writes) == 1 or not snapshot:
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError('event loop stalled')
                return original(snapshot)
            loop_thread = threading.get_ident()
            with patch.object(store, '_save', side_effect=slow):
                store.add('old', 1, True)
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                for i in range(10):
                    store.add(str(i), 1, True)
                release.set()
                self.assertTrue(await asyncio.to_thread(store.flush))
                self.assertEqual(writes, [1, 11])
                entered.clear()
                release.clear()
                clear = asyncio.create_task(asyncio.to_thread(store.clear))
                self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                store.add('after', 1, True)
                release.set()
                self.assertTrue(await clear)
                self.assertTrue(await asyncio.to_thread(store.flush))
            self.assertEqual([row['from'] for row in json.loads(path.read_bytes())], ['after'])

    async def test_flush_failure_can_be_retried_without_destroying_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'history.json'
            store = app.ForwardLog(path)
            store.add('old', 2, True)
            await asyncio.to_thread(store.flush)
            raw = path.read_bytes()
            with patch.object(app.os, 'replace', side_effect=OSError()), self.assertLogs(app.logger, 'ERROR'):
                store.add('new', 2, False, 'failure')
                self.assertFalse(await asyncio.to_thread(store.flush))
                self.assertTrue(store.write_failed)
                self.assertEqual(path.read_bytes(), raw)
            self.assertTrue(await asyncio.to_thread(store.flush))
            self.assertEqual(len(json.loads(path.read_bytes())), 2)


class DefaultTests(unittest.TestCase):
    def test_unset_empty_and_unrecognized_are_off(self):
        for value, expected in ((None, False), (' ', False), ('0', False), ('typo', False), ('1', True)):
            with patch.dict(os.environ), self.subTest(value=value):
                if value is None:
                    os.environ.pop('MONITOR_TDL_FALLBACK', None)
                else:
                    os.environ['MONITOR_TDL_FALLBACK'] = value
                self.assertEqual(runpy.run_path(str(source))['MONITOR_TDL_FALLBACK'], expected)
