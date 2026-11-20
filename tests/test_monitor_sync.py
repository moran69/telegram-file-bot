import asyncio
import json
import threading
import sqlite3
from contextlib import closing
import importlib.util
import logging
import os
import signal
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, Mock, patch

from telethon import errors, events, functions, types, utils

source = Path(__file__).resolve().parents[1] / 'tdl-forward.py'
spec = importlib.util.spec_from_file_location('tdl_forward', source)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
with patch.object(sys, 'path', [str(source.parent), *sys.path]):
    spec.loader.exec_module(app)

SOURCE, TARGET = 1012345678, 1098765432


def photo(mid, ref=b'photo-ref', spoiler=False):
    return types.MessageMediaPhoto(photo=types.Photo(mid, 42, ref, None, [], 1), spoiler=spoiler)


def document(mid, mime='video/mp4', attributes=None, ref=b'video-ref', spoiler=False):
    attrs = [types.DocumentAttributeVideo(3, 32, 32)] if attributes is None else attributes
    doc = types.Document(mid, 42, ref, None, mime, 100, 1, attrs)
    return types.MessageMediaDocument(document=doc, spoiler=spoiler)


class FakeClient:
    def __init__(self, *args, **kwargs):
        self.handlers = {}
        self.get_input_entity = AsyncMock(side_effect=lambda peer: types.InputPeerChannel(peer.channel_id, 10))
        self.get_messages = AsyncMock(return_value=NS(raw_text='new caption', entities=None, media=None))
        self.edit_message = AsyncMock()
        self.send_message = AsyncMock()
        self.disconnect = AsyncMock()
        self.start = AsyncMock()
        self.run_until_disconnected = AsyncMock()
        self.forward = AsyncMock(side_effect=self.forward_response)

    def is_connected(self):
        return True

    def on(self, builder):
        def register(handler):
            self.handlers[builder] = handler
            return handler
        return register

    async def __call__(self, request):
        return await self.forward(request)

    @staticmethod
    def forward_response(request):
        # Use noncontiguous target IDs and reversed update order.
        updates = [types.UpdateMessageID(id=700 + mid * 3, random_id=rid)
                   for mid, rid in zip(request.id, request.random_id)]
        return NS(updates=list(reversed(updates)))


class MonitorSyncTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addCleanup(patch.stopall)
        patch.object(app, 'MONITOR_MAP_FILE', str(Path(tmp.name) / 'map.sqlite3')).start()
        patch.object(app, 'ADMIN_IDS', []).start()
        patch.object(app, 'USER_MONITOR_ENABLED', False).start()
        patch.object(app, 'TelegramClient', FakeClient).start()
        patch.object(app, 'ForwardLog').start()
        self.bot = self.make_bot()

    async def asyncTearDown(self):
        await self.bot.stop()

    @staticmethod
    def make_bot():
        bot = app.TelegramBot()
        bot.monitor_chat_ids = [SOURCE]
        bot.target_chat_id = utils.get_peer_id(types.PeerChannel(TARGET))
        return bot

    def event(self, *ids, channel=SOURCE, album=None, post=True):
        peer = types.PeerChannel(channel)
        messages = [NS(id=mid, peer_id=peer, post=post, grouped_id=album,
                       text='https://t.me/unrelated/1', forward=None) for mid in ids]
        return NS(client=self.bot.client, chat_id=utils.get_peer_id(peer), messages=messages,
                  message=messages[0], get_input_chat=AsyncMock(return_value=types.InputPeerChannel(channel, 20)),
                  sender_id=None, is_channel=True, out=False)

    async def process(self):
        task = self.bot.task_queue.queue.get_nowait()
        await self.bot.task_queue._process_task(task)
        self.bot.task_queue.queue.task_done()
        return task

    async def enqueue(self, event, edited=False):
        await self.bot.enqueue_monitor_post(event, event.messages, edited=edited)

    async def test_album_exact_ids_and_in_place_edit_after_restart(self):
        await self.enqueue(self.event(12, 10, album=50))
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        request = self.bot.client.forward.await_args.args[0]
        self.assertIsInstance(request, functions.messages.ForwardMessagesRequest)
        self.assertTrue(request.drop_author)
        self.assertEqual([request.from_peer.channel_id, request.to_peer.channel_id], [SOURCE, TARGET])
        self.assertEqual(request.id, [10, 12])
        self.assertEqual(self.bot._monitor_message_map.get(SOURCE, 10, TARGET), 730)
        self.assertEqual(self.bot._monitor_message_map.get(SOURCE, 12, TARGET), 736)
        await self.bot.stop()
        self.bot = self.make_bot()
        client = self.bot.client
        entities = [types.MessageEntityBold(offset=0, length=3)]
        client.get_messages.return_value.entities = entities
        await self.enqueue(self.event(12, album=50), edited=True)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        client.get_messages.assert_awaited_once_with(types.InputPeerChannel(SOURCE, 20), ids=12)
        client.edit_message.assert_awaited_once_with(
            types.InputPeerChannel(TARGET, 10), 736, text='new caption', formatting_entities=entities,
            parse_mode=None, link_preview=False, file=None,
        )
        client.forward.assert_not_awaited()

    async def test_legacy_edit_and_edit_after_target_change_never_repost(self):
        event = self.event(9)
        client = self.bot.client
        await self.enqueue(event, edited=True)
        with self.assertLogs(app.logger, 'INFO') as logged:
            self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertTrue(any('没有转发映射' in line for line in logged.output))
        client.forward.assert_not_awaited()
        client.edit_message.assert_not_awaited()
        client.send_message.assert_not_awaited()
        await self.enqueue(event)
        await self.process()
        await self.process()  # The previously unmapped edit now has an exact receipt.
        client.edit_message.assert_awaited_once()
        self.assertEqual(client.edit_message.await_args.args[:2], (types.InputPeerChannel(TARGET, 10), 727))
        client.edit_message.reset_mock()
        self.bot.target_chat_id = TARGET + 1
        await self.enqueue(event, edited=True)
        await self.process()
        client.forward.assert_awaited_once()
        client.edit_message.assert_not_awaited()

    async def test_edit_while_copy_is_queued_and_repeated_event(self):
        event = self.event(9)
        for edited in (False, True, False):
            await self.enqueue(event, edited=edited)
        client = self.bot.client
        client.get_messages.return_value.raw_text = ''
        client.edit_message.side_effect = errors.MessageNotModifiedError(request=None)
        for _ in range(3):
            self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        client.forward.assert_awaited_once()
        client.edit_message.assert_awaited_once_with(
            types.InputPeerChannel(TARGET, 10), 727, text='', formatting_entities=[], parse_mode=None, link_preview=False, file=None,
        )

    async def test_replaced_media_edits_only_mapped_album_member(self):
        client = self.bot.client
        await self.enqueue(self.event(7, 9, album=42)); await self.process()
        entities = [types.MessageEntityBold(0, 3)]
        for old, latest, kind in [
            (photo(1), photo(2, spoiler=True), types.InputMediaPhoto),
            (document(1), document(2), types.InputMediaDocument),
            (photo(2), document(2), types.InputMediaDocument),  # same numeric ID, different type
            (document(1, 'image/png', []), document(2, 'image/png', []), types.InputMediaDocument),
            (photo(2), photo(2, spoiler=True), types.InputMediaPhoto),
        ]:
            with self.subTest(media=kind, spoiler=latest.spoiler):
                client.get_messages.reset_mock(); client.edit_message.reset_mock()
                client.get_messages.side_effect = [NS(media=latest, raw_text='new', entities=entities), NS(media=old)]
                await self.enqueue(self.event(9, album=42), edited=True)
                self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
                self.assertEqual([a.args[0].channel_id for a in client.get_messages.await_args_list], [SOURCE, TARGET])
                self.assertEqual([a.kwargs['ids'] for a in client.get_messages.await_args_list], [9, 727])
                client.edit_message.assert_awaited_once()
                args, kw = client.edit_message.await_args
                self.assertEqual(args, (types.InputPeerChannel(TARGET, 10), 727))
                self.assertEqual((kw['text'], kw['formatting_entities']), ('new', entities))
                self.assertIsInstance(kw['file'], kind)
                asset = latest.photo if kind is types.InputMediaPhoto else latest.document
                self.assertEqual((kw['file'].id.id, kw['file'].id.file_reference), (2, asset.file_reference))
                self.assertEqual(kw['file'].spoiler, latest.spoiler)
        client.forward.assert_awaited_once()
        client.send_message.assert_not_awaited()

    async def test_caption_only_media_edit_does_not_replace_same_file(self):
        client = self.bot.client
        await self.enqueue(self.event(8)); await self.process()
        client.get_messages.side_effect = [NS(media=photo(2, b'fresh'), raw_text='', entities=[]),
                                           NS(media=photo(2, b'older'))]
        await self.enqueue(self.event(8), edited=True)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        client.edit_message.assert_awaited_once_with(types.InputPeerChannel(TARGET, 10), 724, text='',
                                                     file=None, formatting_entities=[], parse_mode=None,
                                                     link_preview=False)

    async def test_web_preview_and_other_documents_keep_text_only_path(self):
        client = self.bot.client
        await self.enqueue(self.event(8)); await self.process()
        for media in (types.MessageMediaWebPage(types.WebPageEmpty(1)), document(1, 'application/pdf', []),
                      document(1, 'video/mp4', [types.DocumentAttributeVideo(3, 32, 32, round_message=True)])):
            with self.subTest(media=media):
                client.get_messages.reset_mock(); client.edit_message.reset_mock()
                client.get_messages.return_value = NS(media=media, raw_text='edited', entities=None)
                await self.enqueue(self.event(8), edited=True)
                self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
                client.get_messages.assert_awaited_once_with(types.InputPeerChannel(SOURCE, 20), ids=8)
                kw = client.edit_message.await_args.kwargs
                self.assertEqual((kw['text'], kw['file'], kw['link_preview']),
                                 ('edited', None, isinstance(media, types.MessageMediaWebPage)))

    async def test_missing_media_target_or_rejected_replacement_does_not_repost(self):
        client = self.bot.client
        await self.enqueue(self.event(6)); await self.process()
        latest = NS(media=photo(10), raw_text='', entities=None)
        client.get_messages.side_effect = [latest, None, latest, NS(media=photo(9))]
        client.edit_message.side_effect = errors.MediaNewInvalidError(request=None)
        for _ in range(2):
            await self.enqueue(self.event(6), edited=True)
            with self.assertLogs(app.logger, 'ERROR'):
                self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        client.edit_message.assert_awaited_once()
        client.forward.assert_awaited_once()
        client.send_message.assert_not_awaited()
        self.assertFalse(self.bot.task_queue._retry_timers)

    async def test_expired_reference_retries_with_latest_media_and_caption(self):
        client = self.bot.client
        await self.enqueue(self.event(3)); await self.process()
        target = NS(media=document(1))
        client.get_messages.side_effect = [NS(media=document(2, ref=b'expired'), raw_text='old', entities=[]), target,
                                           NS(media=document(3, ref=b'fresh'), raw_text='', entities=[]), target]
        client.edit_message.side_effect = [errors.FileReferenceExpiredError(request=None), None]
        await self.enqueue(self.event(3), edited=True)
        loop = asyncio.get_running_loop()
        call_later = loop.call_later
        with patch.object(loop, 'call_later', side_effect=lambda delay, cb: call_later(0, cb)):
            self.assertEqual((await self.process()).status, app.TaskStatus.PENDING)
        await asyncio.sleep(0.01)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertEqual([(a.args[1], a.kwargs['text'], a.kwargs['file'].id.file_reference)
                          for a in client.edit_message.await_args_list], [(709, 'old', b'expired'), (709, '', b'fresh')])
        client.forward.assert_awaited_once()
        client.send_message.assert_not_awaited()

    async def test_missing_target_message_does_not_repost(self):
        event = self.event(7)
        await self.enqueue(event)
        await self.process()
        self.bot.client.edit_message.side_effect = errors.MessageIdInvalidError(request=None)
        await self.enqueue(event, edited=True)
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.bot.client.forward.assert_awaited_once()

    async def test_partial_receipt_only_stores_confirmed_ids(self):
        self.bot.client.forward.side_effect = lambda request: NS(updates=[
            types.UpdateMessageID(id=900, random_id=request.random_id[1]),
            types.UpdateMessageID(id=999, random_id=-1),
        ])
        await self.enqueue(self.event(5, 6, album=42))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.assertIsNone(self.bot._monitor_message_map.get(SOURCE, 5, TARGET))
        self.assertEqual(self.bot._monitor_message_map.get(SOURCE, 6, TARGET), 900)

        await self.enqueue(self.event(5, 6, album=42))
        with self.assertLogs(app.logger, 'WARNING'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.bot.client.forward.assert_awaited_once()

    async def test_bot_fallback_and_filters(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        handlers = self.bot.client.handlers
        album = self.event(15, 16, album=12)
        self.bot.user_client = FakeClient()
        self.bot.user_monitor_running = True
        for builder in (events.NewMessage, events.Album, events.MessageEdited):
            await handlers[builder](album)
        self.assertTrue(self.bot.task_queue.queue.empty())
        self.bot.user_monitor_running = False
        await handlers[events.NewMessage](album)
        for ignored in (self.event(15, post=False), self.event(15, channel=SOURCE + 1)):
            await handlers[events.MessageEdited](ignored)
        self.bot.remember_forward_status(album.chat_id, 18)
        await handlers[events.MessageEdited](self.event(18))
        self.assertTrue(self.bot.task_queue.queue.empty())
        await handlers[events.Album](album)
        self.assertEqual((await self.process()).mode, 'monitor_copy')
        await handlers[events.MessageEdited](self.event(15))
        self.assertEqual((await self.process()).mode, 'monitor_edit')

    async def test_same_source_and_target_are_never_copied(self):
        self.bot.target_chat_id = SOURCE
        with self.assertLogs(app.logger, 'WARNING'):
            await self.enqueue(self.event(8))
        self.assertTrue(self.bot.task_queue.queue.empty())

    async def test_receipt_loss_keeps_durable_request_and_never_resends(self):
        def lost_reply(request):
            with closing(sqlite3.connect(app.MONITOR_MAP_FILE)) as reader:
                stored = reader.execute('SELECT source_message_id,random_id FROM monitor_copy_intents').fetchall()
            self.assertEqual(stored, list(zip(request.id, request.random_id)))
            raise ConnectionError('receipt lost')
        self.bot.client.forward.side_effect = lost_reply
        await self.enqueue(self.event(3))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        await self.bot.stop()
        self.bot = self.make_bot()
        await self.enqueue(self.event(3))
        with self.assertLogs(app.logger, 'WARNING'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        await self.enqueue(self.event(3), edited=True)
        await self.process()
        self.bot.client.forward.assert_not_awaited()
        self.bot.client.edit_message.assert_not_awaited()

    async def test_explicit_copy_permission_denial_is_not_an_unknown_delivery(self):
        client = self.bot.client
        client.forward.side_effect = errors.ChatWriteForbiddenError(request=None)
        await self.enqueue(self.event(4))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        db = self.bot._monitor_message_map.db
        self.assertEqual(db.execute('SELECT COUNT(*) FROM monitor_copy_intents').fetchone()[0], 0)
        await self.enqueue(self.event(4), edited=True)
        await self.process()
        client.forward.assert_awaited_once()
        client.edit_message.assert_not_awaited()

    async def test_v1_database_migration_preserves_confirmed_mapping(self):
        with closing(sqlite3.connect(app.MONITOR_MAP_FILE)) as db, db:
            db.execute('CREATE TABLE monitor_messages(source_chat_id INTEGER,source_message_id INTEGER,'
                       'target_chat_id INTEGER,target_message_id INTEGER,'
                       'PRIMARY KEY(source_chat_id,source_message_id,target_chat_id))')
            db.execute('INSERT INTO monitor_messages VALUES(?,?,?,?)', (SOURCE, 91, TARGET, 350))
        await self.enqueue(self.event(91))
        await self.process()
        self.bot.client.forward.assert_not_awaited()
        await self.enqueue(self.event(91), edited=True)
        await self.process()
        self.assertEqual(self.bot.client.edit_message.await_args.args[1], 350)
        mappings = self.bot.monitor_message_map()
        mappings.cleanup_if_due(7)
        self.assertEqual(mappings.get(SOURCE, 91, TARGET), 350)

    async def test_album_copy_flood_wait_reuses_all_reserved_ids(self):
        client = self.bot.client
        event = self.event(3, 4, album=42)
        await self.enqueue(event)
        wait = errors.FloodWaitError(request=None, capture=75)
        client.forward.side_effect = wait
        loop = asyncio.get_running_loop()
        wake = loop.call_later
        with patch.object(loop, 'call_later', side_effect=lambda d, cb: wake(0, cb)) as timer:
            self.assertEqual((await self.process()).status, app.TaskStatus.PENDING)
        self.assertEqual(timer.call_args.args[0], 75)
        original = client.forward.await_args.args[0]
        with closing(sqlite3.connect(app.MONITOR_MAP_FILE)) as db:
            self.assertEqual(db.execute('SELECT source_message_id,random_id FROM monitor_copy_intents '
                                        'ORDER BY source_message_id').fetchall(), list(zip(original.id, original.random_id)))
        # Replay while the timer is waiting cannot acquire the reserved nonces.
        await self.enqueue(event)
        self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        client.forward.assert_awaited_once()
        await asyncio.sleep(0.01)
        client.forward.side_effect = client.forward_response
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertIs(client.forward.await_args.args[0], original)
        await self.enqueue(event); await self.process()
        self.assertEqual(client.forward.await_count, 2)
        m = self.bot.monitor_message_map()
        self.assertEqual([m.get(SOURCE, mid, TARGET) for mid in (3, 4)], [709, 712])
        self.assertEqual(m.db.execute('SELECT COUNT(*) FROM monitor_copy_intents').fetchone()[0], 0)

    async def test_copy_retries_stop_on_unknown_outcome(self):
        event = self.event(9)
        await self.enqueue(event)
        loop = asyncio.get_running_loop(); wake = loop.call_later
        self.bot.client.forward.side_effect = [errors.FloodWaitError(None, capture=80), ConnectionError('lost reply')]
        with patch.object(loop, 'call_later', side_effect=lambda d, cb: wake(0, cb)):
            self.assertEqual((await self.process()).status, app.TaskStatus.PENDING)
        await asyncio.sleep(0.01)
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        await self.enqueue(event); await self.process()
        self.assertEqual(self.bot.client.forward.await_count, 2)
        self.assertFalse(self.bot.task_queue._retry_timers)
        self.assertEqual(self.bot.monitor_message_map().db.execute('SELECT COUNT(*) FROM monitor_copy_intents').fetchone()[0], 1)

    async def test_copy_flood_retry_exhaustion_and_explicit_cancellation(self):
        self.bot.client.forward.side_effect = errors.FloodWaitError(None, capture=75)
        loop = asyncio.get_running_loop(); wake = loop.call_later
        await self.enqueue(self.event(9))
        for _ in range(2):
            with patch.object(loop, 'call_later', side_effect=lambda d, cb: wake(0, cb)):
                self.assertEqual((await self.process()).status, app.TaskStatus.PENDING)
            await asyncio.sleep(0.01)
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        m = self.bot.monitor_message_map()
        self.assertEqual(m.db.execute('SELECT COUNT(*) FROM monitor_copy_intents').fetchone()[0], 0)
        self.assertEqual(self.bot.client.forward.await_count, 3)
        await self.enqueue(self.event(12)); await self.process()
        self.assertEqual(await self.bot.task_queue.clear_pending_tasks(), 1)
        self.assertEqual(m.db.execute('SELECT COUNT(*) FROM monitor_copy_intents').fetchone()[0], 0)

    async def test_coalesces_backlog_and_keeps_distinct_album_members_and_targets(self):
        for _ in range(8):
            await self.enqueue(self.event(7, album=9), edited=True)
        await self.enqueue(self.event(8, album=9), edited=True)
        self.bot.target_chat_id = TARGET + 1
        await self.enqueue(self.event(7, album=9), edited=True)
        queue = self.bot.task_queue
        self.assertEqual(queue.queue.qsize(), 3)
        self.assertEqual(await queue.clear_pending_tasks(), 3)
        self.assertFalse(queue._edits)
        await self.enqueue(self.event(7), edited=True)
        self.assertEqual(queue.queue.qsize(), 1)

    async def test_inflight_edits_schedule_one_followup_with_fresh_content(self):
        event = self.event(7)
        await self.enqueue(event); await self.process()
        client = self.bot.client
        client.get_messages.side_effect = [NS(raw_text='first', media=None, entities=[]),
                                           NS(raw_text='last', media=None, entities=[])]
        async def more_edits(*args, **kwargs):
            for _ in range(5):
                await self.enqueue(event, edited=True)
        client.edit_message.side_effect = more_edits
        await self.enqueue(event, edited=True); await self.process()
        self.assertEqual(self.bot.task_queue.queue.qsize(), 1)
        client.edit_message.side_effect = None
        await self.process()
        self.assertEqual([a.kwargs['text'] for a in client.edit_message.await_args_list], ['first', 'last'])
        self.assertFalse(self.bot.task_queue._edits)

    async def test_edits_during_delayed_retry_do_not_bypass_floodwait(self):
        e = self.event(7)
        await self.enqueue(e); await self.process()
        self.bot.client.edit_message.side_effect = errors.FloodWaitError(None, capture=75)
        await self.enqueue(e, edited=True); await self.process()
        for _ in range(6):
            await self.enqueue(e, edited=True)
        self.assertEqual(self.bot.task_queue.get_queue_status()['queue_size'], 1)
        self.assertTrue(self.bot.task_queue.queue.empty())
        self.assertEqual(await self.bot.task_queue.clear_pending_tasks(), 1)
        self.assertFalse(self.bot.task_queue._edits)

    async def test_cleanup_does_not_block_event_loop_and_shutdown_joins_thread(self):
        loop = asyncio.get_running_loop(); started = asyncio.Event(); release = threading.Event()
        real_cleanup = self.bot.cleanup_monitor_db
        thread_ids = []
        def slow_sweep(path, days):
            thread_ids.append(threading.get_ident())
            loop.call_soon_threadsafe(started.set)
            if not release.wait(3):
                raise RuntimeError('cleanup blocked the event loop')
            return real_cleanup(path, days)
        self.bot.task_queue.start = AsyncMock()
        with patch.object(self.bot, 'cleanup_monitor_db', side_effect=slow_sweep):
            try:
                await self.bot.start()
                await asyncio.wait_for(started.wait(), timeout=2)
                self.assertNotEqual(thread_ids, [threading.get_ident()])
                await self.enqueue(self.event(7), edited=True)
                stop = asyncio.create_task(self.bot.stop())
                await asyncio.sleep(0)
                self.assertFalse(stop.done())
            finally:
                release.set()
            await asyncio.wait_for(stop, timeout=3)
        self.assertIsNone(self.bot._monitor_cleanup_task)

    async def test_invalid_token_exits_nonzero_without_disclosing_token(self):
        secret = '123456789:mock-private-token'
        for cls in (errors.AccessTokenInvalidError, errors.AccessTokenExpiredError):
            bot = NS(start=AsyncMock(side_effect=cls(request=secret)), stop=AsyncMock())
            with patch.object(app, 'TelegramBot', return_value=bot), patch.multiple(app, BOT_TOKEN=secret, API_ID=1,
                    API_HASH='mock-hash'), self.assertLogs(app.logger, 'ERROR') as logged, self.assertRaises(SystemExit) as ex:
                await app.main()
            self.assertEqual(ex.exception.code, 2)
            self.assertIn('BOT_TOKEN', ' '.join(logged.output))
            self.assertNotIn(secret, ' '.join(logged.output))
            bot.stop.assert_awaited_once()

    async def test_restart_replaces_process_only_after_cleanup(self):
        calls = []
        bot = NS(restart_requested=True, start=AsyncMock(), stop=AsyncMock(side_effect=lambda: calls.append('stop')))
        with patch.object(app, 'TelegramBot', return_value=bot), patch.multiple(app, BOT_TOKEN='mock', API_ID=1,
                API_HASH='mock'), patch.object(app.os, 'execv', side_effect=lambda *_: calls.append('exec')) as execv:
            await app.main()
        self.assertEqual(calls, ['stop', 'exec'])
        execv.assert_called_once_with(sys.executable, [sys.executable, *sys.argv])

    async def test_weekly_retention_survives_restart_and_never_reposts(self):
        start = 1800000000
        week = 7 * 86400
        m = self.bot.monitor_message_map()
        with patch.object(app.time, 'time', return_value=start):
            m.remember(SOURCE, TARGET, {9: 90})
            reserved = m.reserve(SOURCE, TARGET, [12])
            self.assertIn(12, reserved)
            self.assertEqual(m.cleanup_if_due(7), week)
        with patch.object(app.time, 'time', return_value=start + week - 1):
            m.remember(SOURCE, TARGET, {10: 100})
            await self.bot.stop()
            self.bot = self.make_bot()
            m = self.bot.monitor_message_map()
            self.assertEqual(m.cleanup_if_due(7), 1)
            self.assertEqual(m.get(SOURCE, 9, TARGET), 90)
        with patch.object(app.time, 'time', return_value=start + week):
            m.cleanup_if_due(0)
            self.assertEqual(m.get(SOURCE, 9, TARGET), 90)
            self.assertEqual(m.cleanup_if_due(7), week)
            self.assertIsNone(m.get(SOURCE, 9, TARGET))
            self.assertEqual(m.get(SOURCE, 10, TARGET), 100)
            self.assertEqual(m.reserve(SOURCE, TARGET, [9, 12]), {})
            self.assertEqual(m.db.execute('SELECT random_id FROM monitor_copy_intents').fetchall(), [(reserved[12],)])
            self.assertEqual(m.copy_state(SOURCE, 9, TARGET+1), app.TaskStatus.PENDING)
            for edited in (False, True):
                await self.enqueue(self.event(9), edited=edited)
                self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
            self.bot.client.forward.assert_not_awaited()
            self.bot.client.edit_message.assert_not_awaited()

    async def test_cleanup_runs_even_without_events_and_stops_with_bot(self):
        m = self.bot.monitor_message_map()
        m.remember(SOURCE, TARGET, {1: 42})
        with m.db:
            m.db.execute('UPDATE monitor_messages SET confirmed_at=1')
        self.bot.task_queue.start = AsyncMock()
        with patch.object(app, 'MONITOR_MAP_RETENTION_DAYS', 7):
            await self.bot.start()
            async def wait_for_sweep():
                while m.get(SOURCE, 1, TARGET) is not None:
                    await asyncio.sleep(0.01)
            await asyncio.wait_for(wait_for_sweep(), timeout=3)
            self.assertIsNone(m.get(SOURCE, 1, TARGET))
            timer = self.bot._monitor_cleanup_task
            await self.bot.stop()
            self.assertTrue(timer.cancelled())
        with patch.object(app, 'MONITOR_MAP_RETENTION_DAYS', 0):
            await self.bot.start()
            self.assertIsNone(self.bot._monitor_cleanup_task)

    async def test_transient_edit_reloads_latest_text_on_retry(self):
        await self.enqueue(self.event(3)); await self.process()
        client = self.bot.client
        client.get_messages.side_effect = [NS(raw_text='old', entities=None, media=None),
                                          NS(raw_text='latest', entities=None, media=None)]
        client.edit_message.side_effect = [ConnectionError('offline'), None]
        await self.enqueue(self.event(3), edited=True)
        loop = asyncio.get_running_loop()
        # Schedule a real next-tick wakeup, without sleeping for the backoff in a test.
        call_later = loop.call_later
        with patch.object(loop, 'call_later', side_effect=lambda delay, cb: call_later(0, cb)) as timer:
            task = await self.process()
            self.assertEqual(task.status, app.TaskStatus.PENDING)
            self.assertEqual(timer.call_args.args[0], 2)
        await asyncio.sleep(0.01)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertEqual([a.kwargs['text'] for a in client.edit_message.await_args_list], ['old', 'latest'])
        client.forward.assert_awaited_once()
        self.assertFalse(self.bot.task_queue._retry_timers)

    async def test_exhausted_edit_retry_never_resends(self):
        await self.enqueue(self.event(13)); await self.process()
        self.bot.client.edit_message.side_effect = errors.TimedOutError(request=None, message='timeout')
        await self.enqueue(self.event(13), edited=True)
        loop = asyncio.get_running_loop()
        call_later = loop.call_later
        for delay in (2, 4):
            with patch.object(loop, 'call_later', side_effect=lambda d, cb: call_later(0, cb)) as timer:
                self.assertEqual((await self.process()).status, app.TaskStatus.PENDING)
                self.assertEqual(timer.call_args.args[0], delay)
            await asyncio.sleep(0.01)
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.assertEqual(self.bot.client.edit_message.await_count, 3)
        self.bot.client.forward.assert_awaited_once()
        self.assertFalse(self.bot.task_queue._retry_timers)

    async def test_flood_wait_is_deferred_and_can_be_cleared(self):
        await self.enqueue(self.event(13)); await self.process()
        self.bot.client.get_messages.side_effect = errors.FloodWaitError(request=None, capture=75)
        await self.enqueue(self.event(13), edited=True)
        start = asyncio.get_running_loop().time()
        self.assertEqual((await self.process()).status, app.TaskStatus.PENDING)
        timer, = self.bot.task_queue._retry_timers
        self.assertGreaterEqual(timer.when() - start, 75)
        self.assertEqual(self.bot.task_queue.get_queue_status()['queue_size'], 1)
        self.assertEqual(await self.bot.task_queue.clear_pending_tasks(), 1)
        self.assertTrue(timer.cancelled())
        self.assertTrue(self.bot.task_queue.queue.empty())
        self.bot.client.forward.assert_awaited_once()

    async def test_manual_links_and_forwards_require_authorization(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        handler = self.bot.client.handlers[events.NewMessage]
        self.bot.do_forward = AsyncMock(wraps=self.bot.do_forward)
        event = self.event(3, channel=SOURCE+1, post=False)
        event.respond = AsyncMock(return_value=None)
        with patch.object(app, 'ADMIN_IDS', [101]), patch.object(app, 'SUBMITTER_IDS', {102}):
            for is_forward in (False, True):
                event.message.text = '' if is_forward else 'https://t.me/example/5'
                event.message.forward = object() if is_forward else None
                for sender, out, permitted in [(999, False, False), (None, False, False), (101, True, False),
                                                (101, False, True), (102, False, True)]:
                    event.sender_id, event.out = sender, out
                    event.respond.reset_mock(); self.bot.do_forward.reset_mock()
                    await handler(event)
                    self.assertEqual(event.respond.await_count, int(permitted))
                    self.assertEqual(self.bot.do_forward.await_count, int(permitted))
            event.sender_id = 102
            event.message.text = '/forwardto 555'
            event.message.forward = None
            await handler(event)
            self.assertNotEqual(self.bot.target_chat_id, 555)

    async def test_manual_and_export_keep_the_displayed_destination(self):
        first = self.bot.target_chat_id
        event = self.event(5, post=False)
        status = NS(id=10, chat_id=123, client=self.bot.client, edit=AsyncMock())
        self.bot.client.get_messages = AsyncMock(return_value=status)
        self.bot.tdl.forward_clone = AsyncMock(return_value=(True, 'ok'))
        self.bot.tdl.export_range = AsyncMock(return_value=(True, 'ok'))
        self.bot.tdl.forward_from_export = AsyncMock(return_value=(True, 'ok'))
        self.bot.schedule_status_delete = Mock()
        event.respond = AsyncMock(return_value=status)
        await self.bot.do_forward(event)
        event.sender_id = 88
        event.message.text = f'/export_range {SOURCE} 20 22'
        with patch.object(app, 'ADMIN_IDS', [88]):
            self.assertTrue(await self.bot.handle_admin_command(event))
        for call in status.edit.await_args_list:
            self.assertIn(str(first), call.args[0])
        self.bot.target_chat_id = TARGET+1
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertEqual(self.bot.tdl.forward_clone.await_args.args[1], first)
        self.assertEqual(self.bot.tdl.forward_from_export.await_args.args[1], first)

    async def test_cancel_and_reader_error_both_reap_real_children(self):
        downloader = self.bot.tdl
        for fail_callback in (False, True):
            with self.subTest(fail_callback=fail_callback):
                ready, children = asyncio.Event(), []

                async def progress(line):
                    self.assertEqual(line, 'ready')
                    children.extend(downloader.active_processes)
                    ready.set()
                    if fail_callback:
                        raise RuntimeError('test progress callback')

                job = asyncio.create_task(downloader._run_command(
                    [sys.executable, '-u', '-c', "import time; print('ready'); time.sleep(30)"], progress))
                try:
                    await asyncio.wait_for(ready.wait(), 5)
                    if not fail_callback:
                        job.cancel()
                    with self.assertRaises(RuntimeError if fail_callback else asyncio.CancelledError):
                        await asyncio.wait_for(job, 8)
                    self.assertEqual(len(children), 1)
                    self.assertIsNotNone(children[0].returncode)
                    self.assertFalse(downloader.active_processes)
                finally:
                    for child in children:
                        if child.returncode is None:
                            child.kill()
                            await child.wait()
                    job.cancel()
                    await asyncio.gather(job, return_exceptions=True)

    async def test_cancel_during_subprocess_creation_still_reaps_child(self):
        spawn = asyncio.create_subprocess_exec
        spawned, handoff = asyncio.Event(), asyncio.Event()
        children = []

        async def delayed(*args, **kwargs):
            children.append(await spawn(*args, **kwargs))
            spawned.set()
            await handoff.wait()
            return children[0]

        with patch.object(asyncio, 'create_subprocess_exec', side_effect=delayed):
            job = asyncio.create_task(self.bot.tdl._run_command([sys.executable, '-c', 'import time; time.sleep(30)']))
            try:
                await asyncio.wait_for(spawned.wait(), 5)
                job.cancel()
                handoff.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(job, 8)
                self.assertIsNotNone(children[0].returncode)
                self.assertFalse(self.bot.tdl.active_processes)
            finally:
                handoff.set()
                for child in children:
                    if child.returncode is None:
                        child.kill()
                        await child.wait()
                job.cancel()
                await asyncio.gather(job, return_exceptions=True)

    async def test_cancelled_spawn_failure_does_not_swallow_worker_stop(self):
        q = self.bot.task_queue
        spawning, fail = asyncio.Event(), asyncio.Event()

        async def spawn(*args, **kwargs):
            spawning.set()
            await fail.wait()
            raise OSError('test spawn failure')

        with patch.object(asyncio, 'create_subprocess_exec', side_effect=spawn):
            await q.add_task(self.event(1), 'A', None, 1, target_chat_id=TARGET)
            await q.start()
            await asyncio.wait_for(spawning.wait(), 2)
            stopping = asyncio.create_task(q.stop_tdl_tasks())
            await asyncio.sleep(0)  # Dispatch cancellation while spawn is pending.
            fail.set()
            self.assertEqual(await asyncio.wait_for(stopping, 2), (True, 0))
            self.assertFalse(self.bot.tdl.active_processes)

    async def test_reap_escalates_to_kill_then_waits(self):
        child = Mock(returncode=None)
        child.stdout.read = AsyncMock(return_value=b'')
        child.stderr.read = AsyncMock(return_value=b'')
        child.wait = AsyncMock(side_effect=[asyncio.TimeoutError(), -9])
        child.kill.side_effect = lambda: setattr(child, 'returncode', -9)
        self.bot.tdl.active_processes.add(child)
        await self.bot.tdl._reap(child, [])
        child.terminate.assert_called_once()
        child.kill.assert_called_once()
        self.assertEqual(child.wait.await_count, 2)
        self.assertFalse(self.bot.tdl.active_processes)

    async def test_stop_joins_current_and_preserves_later_submissions(self):
        q = self.bot.task_queue
        entered, cancelling, release, cleaned = (asyncio.Event() for _ in range(4))
        executed = []

        async def clone(link, *args):
            executed.append(link)
            if link == 'A':
                entered.set()
                try:
                    await asyncio.Future()
                finally:
                    cancelling.set()
                    await release.wait()
                    cleaned.set()
            return True, 'ok'

        self.bot.tdl.forward_clone = AsyncMock(side_effect=clone)
        event = self.event(1)
        await q.add_task(event, 'A', None, 1, target_chat_id=TARGET)
        await q.add_task(event, 'B', None, 1, target_chat_id=TARGET)
        await q.start()
        await asyncio.wait_for(entered.wait(), 2)
        current = q.current_task
        stopping = asyncio.create_task(q.stop_tdl_tasks())
        try:
            await asyncio.wait_for(cancelling.wait(), 2)
            await q.add_task(event, 'C', None, 1, target_chat_id=TARGET)
            self.assertEqual(executed, ['A'])
            self.assertFalse(stopping.done())
        finally:
            release.set()
        self.assertEqual(await asyncio.wait_for(stopping, 2), (True, 1))
        self.assertTrue(cleaned.is_set())
        self.assertEqual(current.status, app.TaskStatus.FAILED)
        await asyncio.wait_for(q.queue.join(), 2)
        self.assertEqual(executed, ['A', 'C'])
        self.bot.forward_log.add.assert_called_once_with('C', TARGET, True, '')

    async def test_stop_between_export_and_forward(self):
        q = self.bot.task_queue
        between = asyncio.Event()

        async def edit(text):
            if '正在转发导出文件' in text:
                between.set()
                await asyncio.Future()

        self.bot.client.get_messages.return_value = NS(edit=edit)
        self.bot.tdl.export_range = AsyncMock(return_value=(True, 'exported'))
        self.bot.tdl.forward_from_export = AsyncMock()
        await q.add_task(self.event(1), 'range', 9, 1, mode='export_range', message_ids=[2, 4],
                         source_chat_id=SOURCE, target_chat_id=TARGET)
        await q.start()
        await asyncio.wait_for(between.wait(), 2)
        path = Path(self.bot.tdl.export_range.await_args.args[3])
        self.assertEqual(await asyncio.wait_for(q.stop_tdl_tasks(), 2), (True, 0))
        self.bot.tdl.forward_from_export.assert_not_awaited()
        self.assertFalse(path.parent.exists())

    async def test_stop_does_not_cancel_inflight_monitor_or_completed_manual(self):
        q = self.bot.task_queue
        await q.start()
        for mode in ('monitor_copy', 'clone'):
            entered, release = asyncio.Event(), asyncio.Event()

            async def pause():
                entered.set()
                await release.wait()

            async def monitor(task):
                await pause()
                task.status = app.TaskStatus.COMPLETED

            async def edit(text):
                if text.startswith('✅'):
                    await pause()

            self.bot.process_monitor_task = AsyncMock(side_effect=monitor)
            self.bot.client.get_messages.return_value = NS(edit=edit)
            self.bot.tdl.forward_clone = AsyncMock(return_value=(True, 'ok'))
            await q.add_task(self.event(1), mode, 5, 1, mode=mode, target_chat_id=TARGET)
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(await asyncio.wait_for(q.stop_tdl_tasks(), 2), (False, 0))
            current = q.current_task
            self.assertIsNotNone(current)
            release.set()
            await asyncio.wait_for(q.queue.join(), 2)
            self.assertEqual(current.status, app.TaskStatus.COMPLETED)

    async def test_live_monitor_fallback_and_verified_recovery(self):
        user = FakeClient()
        connected = False
        verifying, confirm, recovered = (asyncio.Event() for _ in range(3))
        notices = []

        async def connect():
            nonlocal connected
            connected = True
            user.disconnected = asyncio.get_running_loop().create_future()

        async def get_me():
            if user.connect.await_count > 1:
                verifying.set()
                await confirm.wait()
            return NS(id=7)

        async def notify(text):
            notices.append(text)
            if '切回' in text:
                recovered.set()

        user.connect, user.get_me = AsyncMock(side_effect=connect), AsyncMock(side_effect=get_me)
        user.is_connected = lambda: connected
        self.bot.notify_admins = AsyncMock(side_effect=notify)
        self.bot.task_queue.start = AsyncMock()
        self.bot.enqueue_monitor_post = AsyncMock()

        async def assert_only(expected):
            for builder in (events.NewMessage, events.Album, events.MessageEdited):
                for client in (self.bot.client, user):
                    event = self.event(17)
                    event.client = client
                    await client.handlers[builder](event)
                self.bot.enqueue_monitor_post.assert_awaited_once()
                self.assertIs(self.bot.enqueue_monitor_post.await_args.args[0].client, expected)
                self.bot.enqueue_monitor_post.reset_mock()

        with patch.object(app, 'USER_MONITOR_ENABLED', True), patch.object(app, 'USER_SESSION_STRING', ''), \
                patch.object(app, 'USER_SESSION_FILE', 'mock'), patch.object(app, 'TelegramClient', return_value=user), \
                patch.object(app, 'USER_MONITOR_RETRY_SECONDS', 0):
            await self.bot.start()
            await assert_only(user)
            self.assertFalse(notices)
            connected = False
            user.disconnected.set_exception(ConnectionError('final disconnect'))
            self.assertFalse(self.bot.is_user_monitor_active())
            await asyncio.wait_for(verifying.wait(), 2)
            self.assertIn('Bot 回退监听', self.bot.format_monitor_chat_ids())
            await assert_only(self.bot.client)  # Transport connected; identity not yet confirmed.
            self.assertEqual(len(notices), 1)
            self.assertIn('回退', notices[0])
            confirm.set()
            await asyncio.wait_for(recovered.wait(), 2)
            await assert_only(user)
            await self.bot.stop()
            self.assertIsNone(self.bot._user_monitor_watch)
            self.assertEqual(len(notices), 2)
            self.assertEqual(user.connect.await_count, 2)

    async def test_forward_source_is_never_inferred_from_receiving_message(self):
        event = self.event(999, post=False)
        event.message.text = ''
        event.message.link = f'https://t.me/c/{SOURCE}/999'
        for sender, mid in [(types.PeerUser(91), 17), (types.PeerChannel(SOURCE), None), (None, 17)]:
            event.message.forward = NS(from_id=sender, channel_post=mid)
            self.assertIsNone(await self.bot.resolve_link(event))
        status = NS(id=1, chat_id=2, edit=AsyncMock())
        event.respond = AsyncMock(return_value=status)
        await self.bot.do_forward(event)
        self.assertIn('直接发送原帖', event.respond.await_args.args[0])
        self.assertTrue(self.bot.task_queue.queue.empty())
        event.message.text = '原帖：https://t.me/example/17?single'
        self.assertEqual(await self.bot.resolve_link(event), 'https://t.me/example/17')
        self.assertEqual(self.bot.extract_link('https://t.me/example/17/'), 'https://t.me/example/17')
        for invalid in ('https://t.me/c/123/22/44', 'https://t.me/example/22/44', 'https://t.me/c/abc/12'):
            self.assertIsNone(self.bot.extract_link(invalid))
        event.message.forward = NS(from_id=types.PeerChannel(SOURCE), channel_post=5)
        self.assertEqual(await self.bot.resolve_link(event), f'https://t.me/c/{SOURCE}/5')
        event.message.forward.chat = NS(username='origin')
        self.assertEqual(await self.bot.resolve_link(event), 'https://t.me/origin/5')

    async def test_status_failures_do_not_change_manual_delivery(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        handler = self.bot.client.handlers[events.NewMessage]
        self.bot.schedule_status_delete = Mock()
        for mode in ('clone', 'export_range'):
            for fault in ('send', 'deleted', 'lookup', 'edit'):
                for success in (True, False):
                    with self.subTest(mode=mode, fault=fault, success=success):
                        event = self.event(61, channel=SOURCE+1, post=False)
                        event.sender_id = 88
                        status = NS(id=4, chat_id=event.chat_id, edit=AsyncMock(
                            side_effect=RuntimeError('edit failed') if fault == 'edit' else None))
                        event.respond = AsyncMock(return_value=status, side_effect=(
                            RuntimeError('send failed') if fault == 'send' else None))
                        self.bot.client.get_messages = AsyncMock(
                            return_value=None if fault == 'deleted' else status,
                            side_effect=RuntimeError('lookup failed') if fault == 'lookup' else None)
                        with patch.object(app, 'ADMIN_IDS', [88]):
                            if mode == 'export_range':
                                event.message.text = f'/export_range {SOURCE} 20 22'
                            await handler(event)

                        async def deliver(*args):
                            await args[-1]('test progress')
                            return success, ('ok' if success else 'delivery failed')

                        self.bot.tdl.forward_clone = AsyncMock(side_effect=deliver)
                        self.bot.tdl.export_range = AsyncMock(return_value=(True, 'ok'))
                        self.bot.tdl.forward_from_export = AsyncMock(side_effect=deliver)
                        self.bot.forward_log.add.reset_mock()
                        task = await self.process()
                        self.assertEqual(task.mode, mode)
                        self.assertEqual(task.status, app.TaskStatus.COMPLETED if success else app.TaskStatus.FAILED)
                        self.bot.forward_log.add.assert_called_once_with(task.link, task.target_chat_id, success,
                                                                        '' if success else 'delivery failed')
                        self.assertEqual(self.bot.tdl.forward_clone.await_count, int(mode == 'clone'))
                        self.assertEqual(self.bot.tdl.export_range.await_count, int(mode == 'export_range'))
                        self.assertEqual(self.bot.tdl.forward_from_export.await_count, int(mode == 'export_range'))
                        if fault == 'send':
                            self.assertIsNone(task.status_msg_id)
                            self.bot.client.get_messages.assert_not_awaited()


    async def test_tdl_long_lines_carriage_returns_and_bounded_history(self):
        # A progress line above asyncio's default readline limit must not abort a healthy child.
        code = ("import sys; o=sys.stdout.buffer; o.write(b'x'*180000+b'END\\r'); "
                "o.write('中文进度'.encode()+b'\\r'+b'line\\n'*10000+b'done'); "
                "sys.stderr.write('problem')")
        seen = set()

        async def progress(line):
            if line.endswith('END') or line in ('中文进度', 'done', 'problem'):
                seen.add(line[-10:])

        rc, out, err = await asyncio.wait_for(self.bot.tdl._run_command(
            [sys.executable, '-c', code], progress), 10)
        self.assertEqual((rc, err), (0, 'problem'))
        self.assertLess(len(out), 1000)
        self.assertTrue(out.endswith('done'))
        self.assertIn('中文进度', seen)
        self.assertTrue(any(s.endswith('END') for s in seen))
        self.assertFalse(self.bot.tdl.active_processes)

    async def test_queue_start_twice_owns_only_one_worker(self):
        q = self.bot.task_queue
        await q.start()
        first = q.worker_task
        try:
            await q.start()
            self.assertIs(q.worker_task, first)
            await q.stop()
            self.assertTrue(first.done())
        finally:
            first.cancel()
            await asyncio.gather(first, return_exceptions=True)

    async def test_pruning_does_not_turn_a_missing_receipt_into_success(self):
        m = self.bot.monitor_message_map()
        now = 1800000000
        with patch.object(app.time, 'time', return_value=now):
            intent = m.reserve(SOURCE, TARGET, [3])
            m.remember(SOURCE, TARGET, {7: 701})
        with patch.object(app.time, 'time', return_value=now + 8 * 86400):
            m.cleanup_if_due(7)
        await self.bot.stop()
        self.bot = self.make_bot()
        event = self.event(3, 7, album=45)
        await self.enqueue(event)
        self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        event.get_input_chat.assert_not_awaited()
        self.bot.client.forward.assert_not_awaited()
        m = self.bot.monitor_message_map()
        self.assertEqual(m.db.execute('SELECT random_id FROM monitor_copy_intents').fetchall(), [(intent[3],)])
        # A fresh member can still be sent, but its receipt cannot confirm the missing member.
        await self.enqueue(self.event(3, 9, album=45))
        self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.assertEqual(self.bot.client.forward.await_args.args[0].id, [9])
        self.assertEqual(m.get(SOURCE, 9, TARGET), 727)

    async def test_both_monitor_clients_delegate_short_flood_waits_to_queue(self):
        from telethon import TelegramClient as RealClient
        from telethon.sessions import MemorySession
        clients = []

        def offline_factory(session, api_id, api_hash, **kwargs):
            client = RealClient(MemorySession(), 123, 'dummy', **kwargs)
            clients.append(client)
            # No transport or authentication; retain the pinned library's real client settings.
            client.connect = AsyncMock()
            client.get_me = AsyncMock(return_value=None)
            return client

        with patch.object(app, 'TelegramClient', side_effect=offline_factory), \
                patch.object(app, 'USER_MONITOR_ENABLED', True), patch.object(app, 'USER_SESSION_STRING', ''), \
                patch.object(app, 'USER_SESSION_FILE', 'mock'):
            bot = app.TelegramBot()
            try:
                await bot.start_user_monitor()
                self.assertEqual([client.flood_sleep_threshold for client in clients], [0, 0])
            finally:
                await bot.stop()

    async def test_removed_chatid_is_not_a_command_or_a_monitor_filter(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        handler = self.bot.client.handlers[events.NewMessage]
        with patch.object(app, 'ADMIN_IDS', [81]):
            for peer, is_post in ((types.PeerUser(81), False), (types.PeerChat(51), False),
                                  (types.PeerChannel(SOURCE), True)):
                with self.subTest(peer=peer):
                    event = self.event(20, post=is_post)
                    event.sender_id, event.chat_id = 81, utils.get_peer_id(peer)
                    event.message.text, event.message.peer_id = '/chatid', peer
                    event.respond = AsyncMock()
                    await handler(event)
                    event.respond.assert_not_awaited()
                    self.assertEqual(self.bot.task_queue.queue.qsize(), int(is_post))
        self.assertEqual((await self.process()).mode, 'monitor_copy')

    async def test_manual_album_with_unusable_first_member_still_accepts_next(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        handler = self.bot.client.handlers[events.NewMessage]
        def member(chat, mid, gid):
            event = self.event(mid, channel=chat, post=False, album=gid)
            event.sender_id = 51
            event.respond = AsyncMock(return_value=None)
            event.message.forward = NS(from_id=types.PeerChannel(88), channel_post=mid)
            return event
        with patch.object(app, 'ADMIN_IDS', [51]):
            first = member(SOURCE, 1, 10)
            first.message.forward.channel_post, first.message.text = None, ''
            await handler(first)
            self.assertTrue(self.bot.task_queue.queue.empty())
            second, duplicate, other_chat = member(SOURCE, 2, 10), member(SOURCE, 3, 10), member(SOURCE+1, 4, 10)
            for event in (second, duplicate, other_chat):
                await handler(event)
            self.assertEqual(self.bot.task_queue.queue.qsize(), 2)
            duplicate.respond.assert_not_awaited()
            other_chat.respond.assert_awaited_once()
            task = self.bot.task_queue.queue.get_nowait(); self.bot.task_queue.queue.task_done()
            self.assertEqual(task.link, 'https://t.me/c/88/2')

    async def test_manual_album_concurrent_arrivals_share_one_status(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        handler = self.bot.client.handlers[events.NewMessage]
        entered, release = asyncio.Event(), asyncio.Event()
        async def status(*args):
            entered.set()
            await release.wait()
            return None
        a, b = [self.event(n, post=False, album=20) for n in (1, 2)]
        a.sender_id = b.sender_id = 51
        a.respond, b.respond = AsyncMock(side_effect=status), AsyncMock(return_value=None)
        with patch.object(app, 'ADMIN_IDS', [51]):
            job = asyncio.create_task(handler(a))
            try:
                await asyncio.wait_for(entered.wait(), 2)
                await asyncio.wait_for(handler(b), 2)
            finally:
                release.set()
                await asyncio.wait_for(job, 2)
            self.assertEqual(self.bot.task_queue.queue.qsize(), 1)
            b.respond.assert_not_awaited()

    async def test_manual_album_cache_evicts_only_oldest(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        handler = self.bot.client.handlers[events.NewMessage]
        with patch.object(app, 'ADMIN_IDS', [51]), patch.object(app, 'MANUAL_ALBUM_CACHE_SIZE', 2, create=True):
            for mid, group in enumerate([101, 102, 103, 103, 102, 101], 1):
                event = self.event(mid, post=False, album=group)
                event.sender_id, event.respond = 51, AsyncMock(return_value=None)
                await handler(event)
        self.assertEqual(self.bot.task_queue.queue.qsize(), 4)

    async def test_queued_copy_and_edit_resolve_peers_on_active_account(self):
        bot, user = self.bot.client, FakeClient()
        self.bot.user_client = user
        for source_client, selected, mid in [(user, bot, 15), (bot, user, 16)]:
            with self.subTest(mid=mid):
                self.bot.user_monitor_running = (selected is user)
                event = self.event(mid)
                event.client = source_client
                event.get_input_chat.side_effect = AssertionError('old account peer cannot be reused')
                selected.get_input_entity = AsyncMock(side_effect=lambda p: types.InputPeerChannel(p.channel_id, 44))
                selected.get_messages.return_value = NS(raw_text='fresh', entities=[], media=None)
                await self.enqueue(event)
                self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
                req = selected.forward.await_args.args[0]
                self.assertEqual((req.from_peer.access_hash, req.to_peer.access_hash), (44, 44))
                await self.enqueue(event, edited=True)
                self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
                selected.get_messages.assert_awaited_with(types.InputPeerChannel(SOURCE, 44), ids=mid)
                self.assertEqual(selected.edit_message.await_args.args[:2], (types.InputPeerChannel(TARGET, 44), 700+mid*3))
                event.get_input_chat.assert_not_awaited()

    async def test_copy_flood_retry_stays_with_actual_sending_account(self):
        user = self.bot.user_client = FakeClient()
        self.bot.user_monitor_running = True
        user.forward.side_effect = errors.FloodWaitError(None, capture=10)
        # Event originally arrived on Bot; first actual send happens after user recovered.
        await self.enqueue(self.event(8))
        self.assertEqual((await self.process()).status, app.TaskStatus.PENDING)
        original = user.forward.await_args.args[0]
        self.bot.user_monitor_running = False
        user.forward.side_effect = user.forward_response
        timer, task = self.bot.task_queue._retry_timers.popitem()
        timer.cancel()
        self.bot.task_queue.queue.put_nowait(task)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertIs(user.forward.await_args.args[0], original)
        self.bot.client.forward.assert_not_awaited()

    async def test_known_rejected_copy_not_replayed_by_different_account(self):
        user = self.bot.user_client = FakeClient()
        self.bot.user_monitor_running = True
        event = self.event(5)
        event.client = user
        user.forward.side_effect = errors.FloodWaitError(None, capture=10)
        await self.enqueue(event)
        self.assertEqual((await self.process()).status, app.TaskStatus.PENDING)
        timer, task = self.bot.task_queue._retry_timers.popitem()
        timer.cancel()
        user.is_connected = lambda: False
        self.bot.task_queue.queue.put_nowait(task)
        self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.bot.client.forward.assert_not_awaited()
        user.forward.assert_awaited_once()
        db = self.bot.monitor_message_map().db
        self.assertEqual(db.execute('SELECT count(*) FROM monitor_copy_intents').fetchone()[0], 0)

    async def test_monitor_commands_do_not_partially_apply_bad_input(self):
        self.bot.schedule_command_delete = Mock()
        with patch.object(app, 'ADMIN_IDS', [99]):
            for text in ('/monitor set 200,bad,201', '/monitor set 200,,201', '/monitor clear later',
                         '/monitor add 88 99', f'/monitor del {SOURCE} extra'):
                with self.subTest(text=text):
                    self.bot.monitor_chat_ids = [SOURCE]
                    event = self.event(1, post=False)
                    event.sender_id, event.message.text, event.respond = 99, text, AsyncMock(return_value=None)
                    await self.bot.handle_admin_command(event)
                    self.assertEqual(self.bot.monitor_chat_ids, [SOURCE])
                    self.assertIn('❌', event.respond.await_args.args[0])
            event = self.event(2, post=False)
            event.sender_id, event.message.text, event.respond = 99, '/monitor set 200，201, 200', AsyncMock(return_value=None)
            await self.bot.handle_admin_command(event)
            self.assertEqual(self.bot.monitor_chat_ids, [200, 201])

    async def test_invalid_export_range_cannot_enqueue_or_spawn_tdl(self):
        self.bot.schedule_command_delete = Mock()
        with patch.object(app, 'ADMIN_IDS', [99]):
            for args in ('0 1 2', '88 0 2', '88 -2 3', '88 3 2147483648', '88 1 2 extra',
                         'https://t.me/c/88/0 2', 'https://t.me/c/88/2 3 extra',
                         'https://t.me/c/88/2?comment=5 6', 'https://t.me/c/88/2 https://t.me/c/88/3?thread=1'):
                with self.subTest(args=args):
                    event = self.event(1, post=False)
                    event.sender_id, event.message.text = 99, '/export_range ' + args
                    event.respond = AsyncMock(return_value=None)
                    await self.bot.handle_admin_command(event)
                    try:
                        self.assertTrue(self.bot.task_queue.queue.empty())
                        self.assertIn('❌', event.respond.await_args.args[0])
                    finally:
                        await self.bot.task_queue.clear_pending_tasks(notify=False)

    def test_export_range_accepts_all_documented_forms(self):
        for args in ('88 1 2', 'https://t.me/c/88/ 1 2', 'https://t.me/c/88/1?single 2',
                     'https://t.me/c/88/1 https://t.me/c/88/2'):
            with self.subTest(args=args):
                self.assertEqual(app.parse_export_range_args(('/export_range ' + args).split()), (88, 1, 2, None))
        self.assertEqual(app.parse_export_range_args(['/export_range', 'https://t.me/c/88/1',
                         'https://t.me/c/89/2'])[-1], 'chat_mismatch')

    def test_tdl_ids_do_not_strip_digits_from_positive_bare_ids(self):
        for bare in (7, 1234567890, 10012345678):
            with self.subTest(bare=bare):
                self.assertEqual(app.parse_monitor_chat_id(str(bare)), bare)
                self.assertIsNone(app.parse_monitor_chat_id(str(utils.get_peer_id(types.PeerChannel(bare)))))
                self.assertEqual(app.TDLDownloader.normalize_chat_id(bare), str(bare))
                self.assertEqual(app.TDLDownloader.normalize_chat_id(utils.get_peer_id(types.PeerChannel(bare))), str(bare))

    async def test_unknown_admin_command_does_not_replace_event_responder(self):
        self.bot.schedule_command_delete = Mock()
        event = self.event(31, post=False)
        event.sender_id, event.message.text = 42, '/not-a-command'
        responder = event.respond = AsyncMock(return_value=object())
        with patch.object(app, 'ADMIN_IDS', [42]):
            self.assertFalse(await self.bot.handle_admin_command(event))
            self.assertIs(event.respond, responder)
            event.message.text = '/queue'
            self.assertTrue(await self.bot.handle_admin_command(event))
            self.bot.schedule_command_delete.assert_called_once_with(responder.return_value)

    async def test_extra_admin_arguments_never_trigger_destructive_actions(self):
        self.bot._restart = AsyncMock()
        self.bot.task_queue.stop_tdl_tasks = AsyncMock(return_value=(False, 0))
        target = self.bot.target_chat_id
        with patch.object(app, 'ADMIN_IDS', [61]):
            for text in ('/flog clear typo', '/tdlstop later', '/restart later', '/forwardto 123 extra', '/forwardto 0'):
                with self.subTest(text=text):
                    event = self.event(1, post=False)
                    event.sender_id, event.message.text, event.respond = 61, text, AsyncMock(return_value=None)
                    await self.bot.handle_admin_command(event)
                    self.assertIn('❌', event.respond.await_args.args[0])
        self.bot.forward_log.clear.assert_not_called()
        self.bot._restart.assert_not_awaited()
        self.bot.task_queue.stop_tdl_tasks.assert_not_awaited()
        self.assertEqual(self.bot.target_chat_id, target)

    async def test_manual_comment_or_topic_never_forwards_parent_post(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        handler = self.bot.client.handlers[events.NewMessage]
        with patch.object(app, 'SUBMITTER_IDS', {61}):
            for suffix in ('?comment=33', '/?thread=3', '?single&%63omment=33', '?THREAD=1'):
                with self.subTest(suffix=suffix):
                    event = self.event(51, post=False)
                    event.message.text = '请转： https://t.me/c/88/17' + suffix
                    event.sender_id, event.respond = 61, AsyncMock(return_value=None)
                    await handler(event)
                    try:
                        self.assertTrue(self.bot.task_queue.queue.empty())
                        event.respond.assert_awaited_once()
                        self.assertIn('原帖', event.respond.await_args.args[0])
                    finally:
                        await self.bot.task_queue.clear_pending_tasks(notify=False)
            event = self.event(52, post=False)
            event.message.forward = NS(from_id=types.PeerChannel(88), channel_post=19)
            event.message.text = 'https://t.me/c/88/17?comment=33'
            self.assertEqual(await self.bot.resolve_link(event), 'https://t.me/c/88/19')

    async def test_review_history_large_error_does_not_hide_newest_records(self):
        rows = [dict(id=str(i), success=False, error='😞'*5000 + f' ERR-{i}',
                     date='2026-11-19', to=TARGET, **{'from': 'mock-source'}) for i in range(1, 11)]
        self.bot.forward_log.get_recent.return_value = rows
        self.bot.forward_log.get_all.return_value = rows
        event = self.event(1, post=False)
        event.sender_id, event.message.text, event.respond = 61, '/flog', AsyncMock(return_value=None)
        with patch.object(app, 'ADMIN_IDS', [61]):
            await self.bot.handle_admin_command(event)
        answer = event.respond.await_args.args[0]
        for row in rows:
            self.assertIn('ERR-' + row['id'], answer)
        self.assertLess(answer.index('#10'), answer.index('#9'))
        self.assertLessEqual(len(answer.encode('utf-16-le')) // 2, 4096)
        self.assertEqual(rows[-1]['error'].count('😞'), 5000)

    async def test_review_command_forwarded_admin_text_is_content_not_control(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        event = self.event(1, post=False)
        event.sender_id, event.respond = 61, AsyncMock(return_value=None)
        event.message.forward = NS(from_id=types.PeerChannel(88), channel_post=19)
        event.message.text = '/flog clear'
        with patch.object(app, 'ADMIN_IDS', [61]):
            await self.bot.client.handlers[events.NewMessage](event)
        self.bot.forward_log.clear.assert_not_called()
        q = self.bot.task_queue.queue
        self.assertEqual(q.qsize(), 1)
        task = q.get_nowait(); q.task_done()
        self.assertEqual(task.link, 'https://t.me/c/88/19')

    async def test_admin_startup_and_start_share_panel_but_help_opens_guide(self):
        self.bot.task_queue.start = AsyncMock()
        with patch.object(app, 'ADMIN_IDS', [61]):
            await self.bot.start()
            self.assertEqual(self.bot.client.send_message.await_args.args, (61, app.START_MESSAGE_ADMIN))
            self.assertEqual(len(self.bot.client.send_message.await_args.kwargs['buttons']), 4)
            handler = self.bot.client.handlers[events.NewMessage]
            event = self.event(1, post=False)
            event.sender_id = 61
            for command, expected in (('/start', app.START_MESSAGE_ADMIN), ('/help', app.HELP_MESSAGE_ADMIN)):
                with self.subTest(command=command):
                    event.message.text, event.respond = command, AsyncMock()
                    await handler(event)
                    event.respond.assert_awaited_once_with(expected, link_preview=False)
            self.assertIn('管理面板', app.START_MESSAGE_ADMIN)
            self.assertIn('① 先选一个接收消息的频道', app.HELP_MESSAGE_ADMIN)
            self.assertNotEqual(app.START_MESSAGE_ADMIN, app.HELP_MESSAGE_ADMIN)
            event.sender_id, event.respond = 62, AsyncMock()
            await handler(event)  # /help is still admin-only.
            event.respond.assert_not_awaited()

    async def test_review_command_names_and_recipients_are_exact(self):
        self.bot.task_queue.start = AsyncMock()
        self.bot.client.get_me = AsyncMock(return_value=NS(username='Example_Bot'))
        await self.bot.start()
        event = self.event(1, post=False)
        event.sender_id = 61
        self.bot.do_forward = AsyncMock()
        with patch.object(app, 'ADMIN_IDS', [61]):
            for text, expected in (('/start hello', '管理面板'), ('/start@EXAMPLE_bot hello', '管理面板'),
                                   ('/queue@example_bot', '转发队列为空'), ('/starter', None),
                                   ('/start@Other_bot', None), ('/unknown https://t.me/x/10', None),
                                   (f'/resyn https://t.me/c/{SOURCE}/7', None),
                                   ('/forwardto@Other_bot https://t.me/x/10', None)):
                with self.subTest(text=text):
                    event.message.text, event.respond = text, AsyncMock(return_value=None)
                    for builder, handler in self.bot.client.handlers.items():
                        if builder is events.NewMessage or (isinstance(builder, events.NewMessage) and builder.pattern(text)):
                            await handler(event)
                    if expected:
                        event.respond.assert_awaited_once()
                        self.assertIn(expected, event.respond.await_args.args[0])
                    else:
                        event.respond.assert_not_awaited()
                    self.bot.do_forward.assert_not_awaited()

    async def test_review_command_channel_posts_never_run_admin_actions(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        handler = self.bot.client.handlers[events.NewMessage]
        self.bot._restart = AsyncMock()
        for channel in (SOURCE, TARGET):
            event = self.event(1, channel=channel)
            event.message.text, event.sender_id, event.respond = '/restart', 61, AsyncMock(return_value=None)
            with patch.object(app, 'ADMIN_IDS', [61]):
                await handler(event)
            event.respond.assert_not_awaited()
        self.bot._restart.assert_not_awaited()
        self.assertEqual(self.bot.task_queue.queue.qsize(), 1)

    async def test_review_shutdown_joins_delayed_deletes_and_rejects_new_ones(self):
        entered, finished = asyncio.Event(), asyncio.Event()
        async def delayed(*args):
            entered.set()
            try:
                await asyncio.Future()
            finally:
                finished.set()
        with patch.object(app, 'AUTO_DELETE_BOT_MESSAGES', True), \
                patch.object(self.bot, '_delete_message_later', side_effect=delayed) as delete:
            self.bot.schedule_delete(object(), 60)
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(self.bot.stop(), 2)
            self.assertTrue(finished.is_set())
            self.bot.schedule_delete(object(), 60)
            await asyncio.sleep(0)
            delete.assert_awaited_once()

    async def test_review_shutdown_inflight_submissions_never_claim_enqueued(self):
        release = asyncio.Event()
        jobs, statuses, starts = [], [], []
        with patch.object(app, 'ADMIN_IDS', [44]):
            for mode in ('clone', 'range'):
                event = self.event(9, post=False, album=51)
                status = NS(chat_id=event.chat_id, id=1, edit=AsyncMock())
                started = asyncio.Event()
                async def reply(*args, msg=status, entered=started):
                    entered.set()
                    await release.wait()
                    return msg
                event.sender_id, event.respond = 44, reply
                if mode == 'range':
                    event.message.text = '/export_range 88 1 2'
                fn = self.bot.do_forward if mode == 'clone' else self.bot.handle_admin_command
                jobs.append(asyncio.create_task(fn(event))); starts.append(started); statuses.append(status)
            try:
                await asyncio.wait_for(asyncio.gather(*(x.wait() for x in starts)), 2)
                await self.bot.stop()
            finally:
                release.set()
                await asyncio.wait_for(asyncio.gather(*jobs), 2)
        await self.enqueue(self.event(4))
        self.assertTrue(self.bot.task_queue.queue.empty())
        self.assertFalse(self.bot._album_processed)
        for msg in statuses:
            self.assertIn('未入队', msg.edit.await_args.args[0])

    async def test_review_sigterm_drains_once_and_overrides_pending_restart(self):
        loop = asyncio.get_running_loop()
        entered, cleaning, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def start():
            entered.set()
            await asyncio.Future()
        async def stop():
            cleaning.set()
            await finish.wait()
        bot = NS(restart_requested=True, start=start, stop=AsyncMock(side_effect=stop), begin_shutdown=Mock())
        with patch.object(app, 'TelegramBot', return_value=bot), patch.multiple(app, BOT_TOKEN='mock', API_ID=1,
                API_HASH='mock'), patch.object(loop, 'add_signal_handler') as install, \
                patch.object(loop, 'remove_signal_handler') as remove, patch.object(app.os, 'execv') as restart:
            job = asyncio.create_task(app.main())
            try:
                await asyncio.wait_for(entered.wait(), 2)
                install.assert_called_once()
                sig, callback = install.call_args.args
                self.assertEqual(sig, signal.SIGTERM)
                callback(); callback()
                bot.begin_shutdown.assert_called_once()
                await asyncio.wait_for(cleaning.wait(), 2)
                callback()
                self.assertFalse(job.done())
                finish.set()
                await asyncio.wait_for(job, 2)
                bot.stop.assert_awaited_once(); remove.assert_called_once_with(signal.SIGTERM)
                restart.assert_not_called()
            finally:
                finish.set()
                if not job.done():
                    job.cancel()
                await asyncio.gather(job, return_exceptions=True)

    async def test_review_sigterm_unsupported_loop_preserves_external_cancellation(self):
        entered = asyncio.Event()
        async def start():
            entered.set(); await asyncio.Future()
        bot = NS(restart_requested=False, start=start, stop=AsyncMock())
        loop = asyncio.get_running_loop()
        with patch.object(app, 'TelegramBot', return_value=bot), patch.multiple(app, BOT_TOKEN='mock', API_ID=1,
                API_HASH='mock'), patch.object(loop, 'add_signal_handler', side_effect=NotImplementedError) as install, \
                patch.object(loop, 'remove_signal_handler') as remove:
            job = asyncio.create_task(app.main())
            await asyncio.wait_for(entered.wait(), 2); job.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await job
            install.assert_called_once(); remove.assert_not_called(); bot.stop.assert_awaited_once()

    def test_identity_lists_only_accept_distinct_positive_user_ids(self):
        with self.assertLogs(app.logger, 'WARNING') as logs:
            ids = app.parse_admin_ids('12, -42, 0, 12, 34, private-test-marker, 1_2, 9223372036854775808')
        self.assertEqual(ids, [12, 34])
        self.assertNotIn('private-test-marker', ' '.join(logs.output))

    async def test_destination_rejects_basic_groups_and_preserves_previous_target(self):
        event = self.event(1, post=False)
        event.sender_id, event.respond = 99, AsyncMock(return_value=None)
        marked = utils.get_peer_id(types.PeerChannel(TARGET))
        with patch.object(app, 'ADMIN_IDS', [99]):
            for value in (TARGET, marked, 997852516352, -1997852516352):
                event.message.text = f'/forwardto {value}'
                await self.bot.handle_admin_command(event)
                self.assertEqual(self.bot.target_chat_id, value)
            for value in ('0', '-42', '-1000000000000', '997852516353', '-1997852516353', '1_234'):
                self.assertIsNone(app.parse_chat_id(value))
                event.message.text = f'/forwardto {value}'
                await self.bot.handle_admin_command(event)
                self.assertIn('❌', event.respond.await_args.args[0])
                self.assertEqual(self.bot.target_chat_id, -1997852516352)

    def test_monitor_configuration_is_deduplicated_and_local_to_each_bot(self):
        with patch.object(app, 'MONITOR_CHAT_IDS', [SOURCE]) as defaults:
            first, second = app.TelegramBot(), app.TelegramBot()
            self.assertTrue(first.add_monitor_chat_id(440))
            self.assertEqual(defaults, [SOURCE])
            self.assertEqual(second.monitor_chat_ids, [SOURCE])
            self.assertFalse(second.remove_monitor_chat_id(440))
            self.assertEqual(first.monitor_chat_ids, [SOURCE, 440])
        self.assertEqual(app.parse_chat_ids('44,21,44,021'), [44, 21])

    async def test_bot_restart_registers_each_event_once(self):
        client = self.bot.client
        self.bot.task_queue.start = AsyncMock()
        callbacks = {}
        def on(kind):
            def register(fn):
                callbacks.setdefault(kind, []).append(fn)
                return fn
            return register
        with patch.object(client, 'on', side_effect=on):
            await self.bot.start()
            await self.bot.stop()
            await self.bot.start()
        self.assertEqual({k: len(v) for k, v in callbacks.items()},
                         {events.NewMessage: 1, events.MessageEdited: 1, events.Album: 1, events.CallbackQuery: 1})
        self.bot.enqueue_monitor_post = AsyncMock()
        for fn in callbacks[events.NewMessage]:
            await fn(self.event(1))
        self.bot.enqueue_monitor_post.assert_awaited_once()

    async def test_initial_monitor_network_failure_recovers_without_restart(self):
        user = FakeClient()
        user.connect = AsyncMock(side_effect=[ConnectionError('TEST-private-value'), None])
        user.get_me = AsyncMock(return_value=NS(id=8))
        user.disconnected = asyncio.get_running_loop().create_future()
        recovered = asyncio.Event()
        def notified(text):
            if '切回' in text:
                recovered.set()
        self.bot.notify_admins = AsyncMock(side_effect=notified)
        with patch.object(app, 'USER_MONITOR_ENABLED', True), patch.object(app, 'USER_SESSION_STRING', ''), \
                patch.object(app, 'USER_SESSION_FILE', 'mock'), patch.object(app, 'TelegramClient', return_value=user), \
                patch.object(app, 'USER_MONITOR_RETRY_SECONDS', 0), self.assertLogs(app.logger, 'INFO') as logs:
            await self.bot.start_user_monitor()
            self.assertFalse(self.bot.is_user_monitor_active())
            self.assertIsNotNone(self.bot._user_monitor_watch)
            await asyncio.wait_for(recovered.wait(), 2)
            self.assertTrue(self.bot.is_user_monitor_active())
            self.assertEqual(user.connect.await_count, 2)
            self.assertEqual(self.bot.notify_admins.await_count, 2)
            self.assertNotIn('TEST-private-value', str(logs.output) + str(self.bot.notify_admins.await_args_list))

    async def test_unauthorized_user_monitor_does_not_schedule_reconnect(self):
        user = FakeClient()
        user.connect, user.get_me = AsyncMock(), AsyncMock(return_value=None)
        self.bot.notify_admins = AsyncMock()
        with patch.object(app, 'USER_MONITOR_ENABLED', True), patch.object(app, 'USER_SESSION_STRING', ''), \
                patch.object(app, 'USER_SESSION_FILE', 'mock'), patch.object(app, 'TelegramClient', return_value=user):
            await self.bot.start_user_monitor()
        self.assertIsNone(self.bot._user_monitor_watch)
        self.assertFalse(self.bot.is_user_monitor_active())
        self.assertIn('session', self.bot.notify_admins.await_args.args[0])
        user.disconnect.assert_awaited_once()

    async def test_stop_drains_other_items_if_one_sqlite_release_fails(self):
        db = self.bot.monitor_message_map()
        db.reserve(SOURCE, TARGET, [3])  # Unknown delivery; never released just because we stop.
        self.bot.client.forward.side_effect = errors.FloodWaitError(None, capture=600)
        tasks = []
        for mid in (1, 2):
            await self.enqueue(self.event(mid))
            tasks.append(await self.process())
        release = db.release_rejected
        def fail_first(src, dst, ids):
            if 1 in ids:
                raise sqlite3.OperationalError('read-only database')
            release(src, dst, ids)
        with patch.object(db, 'release_rejected', side_effect=fail_first), self.assertLogs(app.logger, 'ERROR'):
            await self.bot.stop()
        self.bot.client.disconnect.assert_awaited_once()
        self.assertFalse(self.bot.task_queue._retry_timers)
        self.assertTrue(all(task.status == app.TaskStatus.FAILED for task in tasks))
        self.assertIn('核对', tasks[0].error_msg)
        with closing(sqlite3.connect(app.MONITOR_MAP_FILE)) as check:
            self.assertEqual(check.execute('SELECT source_message_id FROM monitor_copy_intents ORDER BY 1').fetchall(),
                             [(1,), (3,)])
        self.assertEqual(self.bot.client.forward.await_count, 2)

    async def test_initial_monitor_flood_wait_is_respected_and_cancelled_on_stop(self):
        user = FakeClient()
        user.connect = AsyncMock(side_effect=errors.FloodWaitError(None, capture=640))
        sleeping = asyncio.Event()
        async def wait_for_retry(seconds):
            self.assertEqual(seconds, 640)
            sleeping.set()
            await asyncio.Future()
        with patch.object(app, 'USER_MONITOR_ENABLED', True), patch.object(app, 'USER_SESSION_STRING', ''), \
                patch.object(app, 'USER_SESSION_FILE', 'mock'), patch.object(app, 'TelegramClient', return_value=user), \
                patch.object(asyncio, 'sleep', side_effect=wait_for_retry):
            await self.bot.start_user_monitor()
            await asyncio.wait_for(sleeping.wait(), 2)
            self.assertFalse(self.bot.is_user_monitor_active())
            await self.bot.stop()
            self.assertIsNone(self.bot._user_monitor_watch)
            user.connect.assert_awaited_once()

    async def test_delivery_grouped_new_messages_survive_without_album_event(self):
        self.bot.task_queue.start = AsyncMock()
        await self.bot.start()
        handler = self.bot.client.handlers[events.NewMessage]
        with patch.object(app, 'MONITOR_ALBUM_WAIT', 0, create=True):
            for mid in (8, 6, 8):
                await handler(self.event(mid, album=35))
            await asyncio.sleep(0.01)
        self.assertEqual(self.bot.task_queue.queue.qsize(), 1)
        await self.process()
        self.assertEqual(self.bot.client.forward.await_args.args[0].id, [6, 8])
        await self.bot.client.handlers[events.Album](self.event(6, 8, 11, album=35))
        await self.process()
        self.assertEqual(self.bot.client.forward.await_args.args[0].id, [11])
        for mid in (6, 8, 11):
            self.assertEqual(self.bot.monitor_message_map().get(SOURCE, mid, TARGET), 700 + 3 * mid)

    async def test_delivery_early_edit_only_reconciles_confirmed_album_members(self):
        client = self.bot.client
        for mid in (6, 7):
            await self.enqueue(self.event(mid, album=35), edited=True)
            await self.process()
        client.forward.assert_not_awaited()
        client.edit_message.assert_not_awaited()
        client.forward.side_effect = lambda req: NS(updates=[types.UpdateMessageID(id=96, random_id=req.random_id[0])])
        await self.enqueue(self.event(6, 7, album=35))
        self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.assertEqual(self.bot.task_queue.queue.qsize(), 1)
        client.get_messages.side_effect = [NS(media=photo(24), raw_text='fresh caption', entities=[]), NS(media=photo(24))]
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        client.edit_message.assert_awaited_once_with(types.InputPeerChannel(TARGET, 10), 96, text='fresh caption',
                                                     formatting_entities=[], parse_mode=None, link_preview=False, file=None)
        client.forward.assert_awaited_once()
        self.assertTrue(self.bot.task_queue.queue.empty())

    async def test_delivery_edited_snapshot_replayed_as_new_repairs_existing_mapping(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {8: 451})
        event = self.event(8)
        event.message.edit_date = 123456
        await self.enqueue(event)
        while not self.bot.task_queue.queue.empty():
            await self.process()
        self.bot.client.forward.assert_not_awaited()
        self.bot.client.edit_message.assert_awaited_once()
        self.assertEqual(self.bot.client.edit_message.await_args.args[1], 451)

    async def test_delivery_missing_event_peer_resolves_source_before_rpc(self):
        event = self.event(9)
        event.get_input_chat.return_value = None
        await self.enqueue(event)
        await self.process()
        self.assertEqual(self.bot.client.forward.await_args.args[0].from_peer, types.InputPeerChannel(SOURCE, 10))
        await self.enqueue(event, edited=True)
        await self.process()
        self.bot.client.get_messages.assert_awaited_once_with(types.InputPeerChannel(SOURCE, 10), ids=9)

    async def test_delivery_user_singleton_album_targets_and_stop_barrier(self):
        user = FakeClient()
        user.connect, user.get_me = AsyncMock(), AsyncMock(return_value=NS(id=8))
        user.disconnected = asyncio.get_running_loop().create_future()
        with patch.object(app, 'USER_MONITOR_ENABLED', True), patch.object(app, 'USER_SESSION_STRING', ''), \
                patch.object(app, 'USER_SESSION_FILE', 'mock'), patch.object(app, 'TelegramClient', return_value=user), \
                patch.object(app, 'MONITOR_ALBUM_WAIT', 0):
            await self.bot.start_user_monitor()
            for mid, target in ((10, TARGET), (12, TARGET + 1)):
                self.bot.target_chat_id = target
                ev = self.event(mid, album=41)
                ev.client = user
                await user.handlers[events.NewMessage](ev)
            await asyncio.sleep(0.01)
            for expected in (TARGET, TARGET + 1):
                self.assertEqual((await self.process()).target_chat_id, expected)
            self.assertEqual([c.args[0].id for c in user.forward.await_args_list], [[10], [12]])
            for stop in (self.bot.task_queue.stop_tdl_tasks, self.bot.stop):
                await user.handlers[events.NewMessage](ev)
                self.assertEqual(self.bot.task_queue.get_queue_status()['queue_size'], 1)
                await stop()
                await asyncio.sleep(0.01)
                self.assertEqual(self.bot.task_queue.get_queue_status()['queue_size'], 0)

    async def test_resync_with_post_link_edits_exact_mapping(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 456})
        event = self.event(11, post=False)
        event.chat_id = event.sender_id = 98
        event.respond = AsyncMock(return_value=None)
        link = f'https://t.me/c/{SOURCE}/7'
        with patch.object(app, 'ADMIN_IDS', [98]):
            for text in (f'/resync {link}', f'/resync {link}?single'):
                with self.subTest(text=text):
                    event.message.text = text
                    self.assertTrue(await self.bot.handle_admin_command(event))
                    self.assertIn('已加入', event.respond.await_args.args[0])
                    self.assertEqual((await self.process()).mode, 'monitor_edit')
                    self.assertEqual(self.bot.client.edit_message.await_args.args[1], 456)
        self.bot.client.forward.assert_not_awaited()

    async def test_resync_rejects_ambiguous_links_without_queueing(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 456})
        event = self.event(11, post=False)
        event.chat_id = event.sender_id = 98
        event.respond = AsyncMock(return_value=None)
        base = f'https://t.me/c/{SOURCE}'
        with patch.object(app, 'ADMIN_IDS', [98]):
            for arg in (base, f'{base}/7?comment=3', f'{base}/7?single&%74hread=8', f'{base}/7/3',
                        f'{base}/0', f'{base}/2147483648', f'{base}/7 other', 'https://example.com/c/1/7'):
                with self.subTest(arg=arg):
                    event.message.text = '/resync ' + arg
                    self.assertTrue(await self.bot.handle_admin_command(event))
                    self.assertIn('用法', event.respond.await_args.args[0])
                    self.assertTrue(self.bot.task_queue.queue.empty())
            event.message.text = f'/resync {base}/8'
            await self.bot.handle_admin_command(event)
            self.assertIn('没有转发映射', event.respond.await_args.args[0])
            self.assertTrue(self.bot.task_queue.queue.empty())

    async def test_delivery_resync_from_admin_chat_only_uses_exact_existing_mapping(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {7: 456})
        event = self.event(11, post=False)
        event.chat_id = event.sender_id = 98
        event.respond = AsyncMock(return_value=None)
        event.get_input_chat.side_effect = AssertionError('never fetch from admin chat')
        event.message.text = f'/resync {SOURCE} 7'
        self.assertFalse(await self.bot.handle_admin_command(event))
        with patch.object(app, 'ADMIN_IDS', [98]):
            for args in (f'{SOURCE} 0', f'{SOURCE} 2147483648', f'{SOURCE} 7 extra', f'{SOURCE+1} 7', f'{SOURCE} 8'):
                event.message.text = '/resync ' + args
                self.assertTrue(await self.bot.handle_admin_command(event))
                self.assertTrue(self.bot.task_queue.queue.empty())
            self.assertIn('没有转发映射', event.respond.await_args.args[0])
            event.message.text = f'/resync {SOURCE} 7'
            self.assertTrue(await self.bot.handle_admin_command(event))
            self.assertIn('已加入', event.respond.await_args.args[0])
            await self.process()
        self.bot.client.get_messages.assert_awaited_once_with(types.InputPeerChannel(SOURCE, 10), ids=7)
        self.bot.client.edit_message.assert_awaited_once()
        self.assertEqual(self.bot.client.edit_message.await_args.args[1], 456)
        self.bot.client.forward.assert_not_awaited()

    async def test_delivery_copy_retries_pre_send_connection_failure_without_reserving_ids(self):
        event = self.event(32)
        event.get_input_chat.side_effect = [ConnectionError('offline'), types.InputPeerChannel(SOURCE, 20)]
        await self.enqueue(event)
        loop = asyncio.get_running_loop()
        wake = loop.call_later
        with patch.object(loop, 'call_later', side_effect=lambda delay, cb: wake(0, cb)):
            self.assertEqual((await self.process()).status, app.TaskStatus.PENDING)
        self.bot.client.forward.assert_not_awaited()
        self.assertEqual(self.bot.monitor_message_map().copy_state(SOURCE, 32, TARGET), app.TaskStatus.PENDING)
        await asyncio.sleep(0.01)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.bot.client.forward.assert_awaited_once()

    async def test_delivery_media_caption_is_sent_in_real_edit_rpc_without_reupload(self):
        from telethon import TelegramClient
        from telethon.sessions import MemorySession
        sent = []
        class OfflineClient(TelegramClient):
            async def __call__(self, request, **kwargs):
                sent.append(request)
                raise errors.MessageNotModifiedError(request)  # Stop at the RPC boundary; no network.
        client = OfflineClient(MemorySession(), 123, 'test')
        peer = types.InputPeerChannel(TARGET, 10)
        client.get_messages = AsyncMock(return_value=NS(media=document(19)))
        bold = types.MessageEntityBold(0, 2)
        for caption, entities in (('新说明', [bold]), ('', [])):
            await self.bot.edit_monitor_message(client, peer, 451, NS(media=document(19, ref=b'fresh'),
                                                                      raw_text=caption, entities=entities))
            request = sent[-1]
            self.assertIsInstance(request, functions.messages.EditMessageRequest)
            self.assertEqual((request.peer, request.id, request.message, request.entities, request.media),
                             (peer, 451, caption, entities, None))
            self.assertTrue(bytes(request))
        self.assertEqual(len(sent), 2)

    async def test_delivery_unreadable_source_does_not_erase_caption_or_report_success(self):
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {6: 800})
        self.bot.client.get_messages.return_value = types.MessageEmpty(id=6)
        self.bot.schedule_status_delete = Mock()
        await self.enqueue(self.event(6), edited=True)
        with patch.object(app, 'ADMIN_IDS', [42]), self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.assertIn('源帖 6 已删除或不可读取', self.bot.client.send_message.await_args.args[1])
        self.bot.client.edit_message.assert_not_awaited()
        self.bot.client.forward.assert_not_awaited()

    async def test_malformed_session_notifies_without_secret_and_keeps_bot_running(self):
        value = 'INVALID-private-credential-TEST'
        self.bot.task_queue.start = AsyncMock()
        self.bot.notify_admins = AsyncMock()
        with patch.object(app, 'USER_MONITOR_ENABLED', True), patch.object(app, 'USER_SESSION_STRING', value), \
                self.assertLogs(app.logger, 'INFO') as logs:
            await self.bot.start()
        self.assertFalse(self.bot.is_user_monitor_active())
        self.assertNotIn(value, '\n'.join(logs.output) + str(self.bot.notify_admins.await_args))
        self.bot.notify_admins.assert_awaited_once()
        await self.bot.client.handlers[events.NewMessage](self.event(71))
        self.assertEqual((await self.process()).mode, 'monitor_copy')


class HistoryTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.path = Path(tmp.name) / 'forward_history.json'

    def test_delete_delay_env_defaults_zero_and_invalid_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            check = "import runpy, sys, pathlib; sys.path.insert(0, str(pathlib.Path(sys.argv[1]).parent)); m=runpy.run_path(sys.argv[1]); print(m['AUTO_DELETE_STATUS_SECONDS'], m['AUTO_DELETE_COMMAND_SECONDS'])"
            env = dict(os.environ, AUTO_DELETE_STATUS_SECONDS=' ', AUTO_DELETE_COMMAND_SECONDS='0',
                       AUTO_DELETE_ERROR_SECONDS='600', PYTHONIOENCODING='utf-8')
            def run():
                return subprocess.run([sys.executable, '-B', '-c', check, str(source)], cwd=directory,
                                      env=env, capture_output=True, timeout=10, encoding='utf-8')
            result = run()
            self.assertEqual((result.returncode, result.stdout.strip()), (0, '300 0'))
            for value in ('-1', 'private-test-marker', '9' * 500):
                env['AUTO_DELETE_ERROR_SECONDS'] = value
                result = run()
                self.assertNotEqual(result.returncode, 0)
                self.assertIn('AUTO_DELETE_ERROR_SECONDS', result.stderr)
                self.assertNotIn(value, result.stderr)
                self.assertNotIn('Traceback', result.stderr)

    def test_invalid_history_fields_preserve_original_bytes(self):
        good = dict(id='1', date='test-date', to=123, success=False, error='test',
                    timestamp=1_800_000_000, **{'from': 'test-source'})
        for changes in ({'timestamp': float('nan')}, {'timestamp': float('inf')}, {'timestamp': True},
                        {'success': 'false'}, {'error': []}, {'from': {}}, {'date': None}, {'to': True}):
            with self.subTest(changes=changes):
                raw = json.dumps([dict(good, **changes)]).encode()
                self.path.write_bytes(raw)
                with self.assertLogs(app.logger, 'WARNING'):
                    store = app.ForwardLog(self.path)
                self.assertEqual(store.logs, [])
                self.assertIn(raw, [p.read_bytes() for p in self.path.parent.glob('*.corrupt-*')])
        self.path.write_text(json.dumps([good]), encoding='utf-8')
        self.assertEqual(app.ForwardLog(self.path).logs, [good])

    def test_idle_history_expires_on_read_without_rewriting_the_file(self):
        day = 86400
        start = 1_800_000_000
        with patch.object(app.time, 'time', return_value=start):
            store = app.ForwardLog(self.path)
            store.add('old', 4, True)
        with patch.object(app.time, 'time', return_value=start + day):
            store.add('new', 4, True)
        original = self.path.read_bytes()
        with patch.object(app.time, 'time', return_value=start + 7 * day):
            for history in (store, app.ForwardLog(self.path)):
                self.assertEqual([r['id'] for r in history.get_recent()], ['2'])
                self.assertEqual(len(history.get_all()), 1)
                self.assertEqual(history.get_recent(0), [])
                self.assertEqual(history.get_recent(-1), [])
        self.assertEqual(self.path.read_bytes(), original)
        with patch.object(app.time, 'time', return_value=start + 9 * day):
            self.assertEqual(store.get_all(), [])
            self.assertEqual(store.add('next', 4, True), '3')
            self.assertEqual(len(json.loads(self.path.read_bytes())), 1)

    def test_unwritable_runtime_log_does_not_mask_startup_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / 'bot.log').mkdir()  # Reliable open failure even when running as root.
            env = dict(os.environ, API_ID='0', API_HASH='', BOT_TOKEN='', PYTHONIOENCODING='utf-8')
            result = subprocess.run([sys.executable, '-B', str(source)], cwd=directory, env=env,
                                    capture_output=True, timeout=10, encoding='utf-8')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('API_ID', result.stderr)
            self.assertIn('标准错误', result.stderr)
            self.assertNotIn('Traceback', result.stderr)

    def test_runtime_log_rotates_without_creating_files_on_import(self):
        with tempfile.TemporaryDirectory() as directory:
            script = """import logging, pathlib, runpy, sys
sys.path.insert(0, str(pathlib.Path(sys.argv[1]).parent))
m = runpy.run_path(sys.argv[1]); path = pathlib.Path('bot.log'); assert not path.exists()
m['configure_logging']()
logger = logging.getLogger('rotation-test')
handler = next(h for h in logging.getLogger().handlers if hasattr(h, 'maxBytes'))
assert (handler.maxBytes, handler.backupCount) == (5 * 1024 * 1024, 3)
handler.maxBytes = 120
for n in range(9): logger.warning('record %s %s', n, 'x' * 50)
logging.shutdown()
"""
            result = subprocess.run([sys.executable, '-B', '-c', script, str(source)], cwd=directory,
                                    capture_output=True, timeout=10)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(sorted(p.name for p in Path(directory).iterdir()),
                             ['bot.log', 'bot.log.1', 'bot.log.2', 'bot.log.3'])
            self.assertIn('record 8', (Path(directory) / 'bot.log').read_text(encoding='utf-8'))

    def test_entrypoint_leaves_existing_session_file_untouched(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Path(directory) / 'bot_session.session'
            session.write_bytes(b'test marker, not a real session')
            # Missing credentials exits before any connection attempt.
            env = dict(os.environ, API_ID='0', API_HASH='', BOT_TOKEN='', USER_SESSION_FILE=str(session),
                       USER_SESSION_STRING='', PYTHONIOENCODING='utf-8')
            result = subprocess.run([sys.executable, '-B', str(source)], cwd=directory, env=env,
                                    capture_output=True, timeout=10)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(b'API_ID', result.stderr)
            self.assertTrue(session.is_file())
            self.assertEqual(session.read_bytes(), b'test marker, not a real session')

    def test_review_history_numbers_survive_pruning_and_reopen(self):
        start = 1800000000
        store = app.ForwardLog(self.path)
        for day in (0, 6, 8):
            with patch.object(app.time, 'time', return_value=start+day*86400):
                store.add('test', 4, True)
        with patch.object(app.time, 'time', return_value=start+9*86400):
            store = app.ForwardLog(self.path)
            store.add('next', 4, True)
            self.assertEqual([r['id'] for r in store.get_all()], ['2', '3', '4'])

    def test_blank_history_is_clean_and_corrupt_bytes_are_preserved(self):
        self.path.write_bytes(b' \r\n')
        with self.assertNoLogs(app.logger, 'WARNING'):
            self.assertEqual(app.ForwardLog(self.path).logs, [])
        for i, damaged in enumerate((b'{broken', b'\xff\xf1', b'{}')):
            self.path.write_bytes(damaged)
            with self.assertLogs(app.logger, 'WARNING'):
                store = app.ForwardLog(self.path)
            store.add('source', 42, True)
            backups = list(self.path.parent.glob('*.corrupt-*'))
            self.assertEqual(len(backups), i + 1)
            self.assertIn(damaged, [p.read_bytes() for p in backups])
            self.assertTrue(json.loads(self.path.read_text(encoding='utf-8'))[0]['success'])

    def test_history_backup_failure_never_overwrites_original(self):
        raw = b'{partial'; self.path.write_bytes(raw)
        with patch.object(Path, 'rename', side_effect=PermissionError()), self.assertLogs(app.logger, 'ERROR'):
            store = app.ForwardLog(self.path)
        store.add('source', 42, True)
        self.assertFalse(store.clear())
        self.assertEqual(self.path.read_bytes(), raw)

    def test_atomic_replace_failure_keeps_valid_file_and_removes_temp(self):
        store = app.ForwardLog(self.path); store.add('source', 42, True)
        original = self.path.read_bytes()
        with patch.object(app.os, 'replace', side_effect=OSError('disk error')), self.assertLogs(app.logger, 'ERROR'):
            store.add('other', 42, True)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        self.assertEqual(len(app.ForwardLog(self.path).get_all()), 1)


if __name__ == '__main__':
    unittest.main()

