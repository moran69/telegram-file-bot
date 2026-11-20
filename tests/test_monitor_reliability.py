import asyncio
import sqlite3
import unittest
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from bot_fixture import FakeClient, MonitorHarness, SOURCE, TARGET, app


class MonitorReliabilityTests(MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def reopen(self):
        await self.bot.stop()
        self.bot = self.make_bot()
        self.bot.task_queue._restore()
        return self.bot.task_queue

    async def retry_now(self):
        q = self.bot.task_queue
        timer, task = q._retry_timers.popitem()
        timer.cancel()
        q.queue.put_nowait(task)
        return await self.process()

    async def test_bot_only_updates_and_dual_account_album(self):
        bot = self.bot.client
        user = self.bot.user_client = FakeClient()
        self.bot.user_monitor_running = True
        self.bot._register_bot_handlers()
        self.bot.register_monitor_events(user)
        await bot.handlers[app.events.NewMessage](self.event(5))
        await self.process()
        await bot.handlers[app.events.MessageEdited](self.event(5))
        await self.process()
        self.assertEqual(bot.edit_message.await_count, 1)
        user.forward.assert_not_awaited()
        group = self.event(8, 9, album=44)
        group.client = user
        await user.handlers[app.events.Album](group)
        await bot.handlers[app.events.Album](self.event(8, 9, album=44))
        await self.process(); await self.process()
        self.assertEqual((bot.forward.await_count, user.forward.await_count), (1, 1))
        # A later edit seen only by Bot can still use the original sender's account.
        await bot.handlers[app.events.MessageEdited](self.event(8))
        await self.process()
        user.edit_message.assert_awaited_once()

    async def test_flood_during_edit_preserves_latest_intent_and_deadline(self):
        ev = self.event(20)
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {20: 800})
        async def rejected(*args, **kwargs):
            await self.enqueue(ev, edited=True)
            raise app.errors.FloodWaitError(None, capture=60)
        self.bot.client.edit_message.side_effect = rejected
        await self.enqueue(ev, edited=True)
        deadline = (await self.process()).ready_at
        q = await self.reopen()
        self.assertEqual(q.journal.reviews()[0], 0)
        self.assertTrue(q.queue.empty())
        restored, = q._retry_timers.values()
        self.assertEqual(restored.ready_at, deadline)
        self.bot.client.get_messages.return_value = NS(raw_text='edited again', entities=[], media=None)
        await self.retry_now()
        self.assertEqual(self.bot.client.edit_message.await_args.kwargs['text'], 'edited again')
        self.bot.client.forward.assert_not_awaited()

    async def test_may_switch_account_only_after_explicit_write_denial(self):
        user = self.bot.user_client = FakeClient()
        self.bot.user_monitor_running = True
        self.bot.client.forward.side_effect = app.errors.ChatWriteForbiddenError(None)
        await self.enqueue(self.event(11)); task = await self.process()
        self.assertEqual(task.status, app.TaskStatus.PENDING)
        self.assertEqual((await self.retry_now()).status, app.TaskStatus.COMPLETED)
        self.assertEqual(self.bot.monitor_message_map().sender_role(SOURCE, 11, TARGET), 'user')
        self.bot.client.forward.side_effect = ConnectionError('unknown')
        await self.enqueue(self.event(12))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.assertFalse(self.bot.task_queue._retry_timers)
        user.forward.assert_awaited_once()

    async def test_denied_on_both_accounts_does_not_ping_pong(self):
        user = self.bot.user_client = FakeClient()
        self.bot.user_monitor_running = True
        for client in (user, self.bot.client):
            client.forward.side_effect = app.errors.ChatAdminRequiredError(None)
        self.bot.tdl.forward_clone = AsyncMock()
        await self.enqueue(self.event(11)); await self.process()
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.retry_now()).status, app.TaskStatus.FAILED)
        self.assertFalse(self.bot.task_queue._retry_timers)
        self.bot.tdl.forward_clone.assert_not_awaited()

    async def test_changed_destination_after_unknown_edit_reloads_latest_source(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {77: 101})
        self.bot.client.edit_message.side_effect = [ConnectionError('lost acknowledgement'), None]
        def msg(text): return NS(raw_text=text, media=None, entities=[])
        self.bot.client.get_messages.side_effect = [msg('intermediate'), msg('latest'), msg('old')]
        await self.enqueue(self.event(77), edited=True); await self.process(); await self.retry_now()
        self.assertEqual([c.kwargs['text'] for c in self.bot.client.edit_message.await_args_list], ['intermediate','latest'])
        self.bot.client.forward.assert_not_awaited()

    async def test_rejected_flood_request_stays_on_original_sending_account(self):
        user = self.bot.user_client = FakeClient()
        self.bot.user_monitor_running = True
        ev = self.event(41); ev.client = user
        user.forward.side_effect = app.errors.FloodWaitError(None, capture=60)
        await self.enqueue(ev); await self.process()
        original = user.forward.await_args.args[0]
        self.bot.user_monitor_running = False
        user.forward.side_effect = user.forward_response
        self.assertEqual((await self.retry_now()).status, app.TaskStatus.COMPLETED)
        self.assertIs(user.forward.await_args.args[0], original)
        self.bot.client.forward.assert_not_awaited()

    async def test_exhausted_ambiguous_edits_remain_for_review_without_reposting(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {17: 88})
        def read(peer, ids): return NS(raw_text='old' if ids == 88 else 'latest', entities=[], media=None)
        self.bot.client.get_messages.side_effect = read
        self.bot.client.edit_message.side_effect = ConnectionError('lost receipt')
        await self.enqueue(self.event(17), edited=True); await self.process(); await self.retry_now()
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.retry_now()).status, app.TaskStatus.UNCONFIRMED)
        q = await self.reopen()
        self.assertEqual(q.journal.reviews()[0], 1)
        self.assertTrue(q.queue.empty()); self.assertFalse(q._retry_timers)
        self.bot.client.forward.assert_not_awaited()

    async def test_uncertain_edit_compares_destination_before_writing(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 99})
        self.bot.client.edit_message.side_effect = ConnectionError('lost acknowledgement')
        await self.enqueue(self.event(7), edited=True); await self.process()
        await self.reopen(); await self.retry_now()
        self.assertEqual([c.kwargs['ids'] for c in self.bot.client.get_messages.await_args_list], [7, 99])
        self.bot.client.edit_message.assert_not_awaited()
        self.bot.client.forward.assert_not_awaited()

    async def test_cold_bot_cache_preserves_waiter_and_wakes_on_source_update(self):
        await self.enqueue(self.event(30))
        q = await self.reopen()
        def lookup(peer):
            if peer.channel_id == SOURCE:
                raise ValueError('cold cache')
            return app.types.InputPeerChannel(peer.channel_id, 42)
        self.bot.client.get_input_entity.side_effect = lookup
        await self.process(); await self.retry_now(); await self.retry_now()
        self.assertEqual(len(q._peer_wait), 1)
        self.bot.client.forward.assert_not_awaited()
        self.assertEqual(q.journal.open().execute('SELECT state FROM jobs').fetchone()[0], 'wait_peer')
        await self.enqueue(self.event(31)); await self.process(); await self.process()
        self.assertEqual([c.args[0].id for c in self.bot.client.forward.await_args_list], [[30], [31]])
        self.assertFalse(q._peer_wait)

    async def test_explicit_private_denial_is_not_cache_miss(self):
        await self.enqueue(self.event(30)); q = await self.reopen()
        self.bot.client.get_input_entity.side_effect = app.errors.ChannelPrivateError(None)
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.assertFalse(q._peer_wait or q._retry_timers)

    async def test_admission_retries_storage_and_groups_failed_ids(self):
        q = self.bot.task_queue
        actual_save = q.journal.save
        failures = [True]
        def unstable(*args):
            if failures:
                failures.pop(); raise sqlite3.OperationalError('locked')
            return actual_save(*args)
        with patch.object(app, 'MONITOR_ADMISSION_DELAYS', (0, 0)), self.assertLogs(app.logger, 'ERROR'):
            with patch.object(q.journal, 'save', side_effect=unstable):
                await self.enqueue(self.event(4)); await self.process()
            self.bot.notify_admins = AsyncMock()
            with patch.object(q.journal, 'save', side_effect=sqlite3.OperationalError('full')), \
                    patch.object(app, 'MONITOR_ALERT_WINDOW', 0):
                await asyncio.gather(self.enqueue(self.event(5)), self.enqueue(self.event(8), edited=True))
                await self.bot._admission_alert
            self.bot.notify_admins.assert_awaited_once()
            alert = self.bot.notify_admins.await_args.args[0]
            self.assertIn(f'{SOURCE}/5', alert); self.assertIn(f'{SOURCE}/8', alert)
            self.assertTrue(q.queue.empty())

    async def test_stop_during_failed_admission_does_not_revive_an_old_post(self):
        q = self.bot.task_queue
        ev = self.event(11); ev.message.edit_date = True
        attempted = asyncio.Event()
        def fail(*args):
            attempted.set(); raise sqlite3.OperationalError('full')
        with self.assertLogs(app.logger, 'ERROR'), patch.object(app, 'MONITOR_ADMISSION_DELAYS', (0.03,)), \
                patch.object(q.journal, 'save', side_effect=fail) as save:
            pending = asyncio.create_task(self.enqueue(ev))
            await attempted.wait(); await q.stop_tdl_tasks(); await pending
            self.assertEqual(save.call_count, 1)
        self.assertTrue(q.queue.empty()); self.assertIsNone(self.bot._admission_alert)

    async def test_confirmed_copy_committed_before_status_notification(self):
        entered = asyncio.Event()
        async def notify(*args, **kw):
            entered.set(); await asyncio.Future()
        self.bot.report_monitor_status = notify
        await self.enqueue(self.event(10)); await self.bot.task_queue.start()
        await asyncio.wait_for(entered.wait(), 2)
        q = await self.reopen()
        self.assertEqual(self.bot.monitor_message_map().get(SOURCE, 10, TARGET), 730)
        self.assertEqual(q.journal.reviews()[0], 0); self.assertTrue(q.queue.empty())

    async def test_mixed_album_old_queue_recovery_reviews_only_unconfirmed_ids(self):
        await self.enqueue(self.event(3, 4, album=60))
        q = self.bot.task_queue
        task = q.queue.get_nowait(); q.queue.task_done(); q.begin_send(task)
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {3: 77})
        q = await self.reopen()
        count, reviews = q.journal.reviews()
        self.assertEqual((count, reviews[0]['message_ids']), (1, [4]))
        self.assertTrue(reviews[0]['link'].endswith('/4')); self.assertTrue(q.queue.empty())

    async def test_pruned_edits_do_not_evict_a_waiting_new_edit(self):
        m = self.bot.monitor_message_map(); m.remember(SOURCE, TARGET, {11: 750, 12: 751})
        with m.db:
            m.db.execute('UPDATE monitor_messages SET confirmed_at=1')
        m.cleanup_if_due(7)
        with patch.object(app, 'MONITOR_EARLY_EDIT_LIMIT', 2):
            for mid in (10, 11, 12):
                await self.enqueue(self.event(mid), edited=True); await self.process()
            self.assertEqual(set(self.bot.task_queue._early_edits), {(SOURCE, 10, TARGET)})
            await self.enqueue(self.event(10)); await self.process(); await self.process()
            self.bot.client.edit_message.assert_awaited_once()
