import asyncio
import sqlite3
from contextlib import closing
import importlib.util
import logging
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
with patch.object(logging, 'FileHandler', lambda *args: logging.NullHandler()):
    spec.loader.exec_module(app)

SOURCE, TARGET = 1012345678, 1098765432


def photo(mid, ref=b'photo-ref', spoiler=False):
    return types.MessageMediaPhoto(photo=types.Photo(mid, 42, ref, None, [], 1), spoiler=spoiler)


def document(mid, mime='video/mp4', attributes=None, ref=b'video-ref', spoiler=False):
    attrs = [types.DocumentAttributeVideo(3, 32, 32)] if attributes is None else attributes
    doc = types.Document(mid, 42, ref, None, mime, 100, 1, attrs)
    return types.MessageMediaDocument(document=doc, spoiler=spoiler)


class FakeClient:
    def __init__(self, *args):
        self.handlers = {}
        self.get_input_entity = AsyncMock(side_effect=lambda peer: types.InputPeerChannel(peer.channel_id, 10))
        self.get_messages = AsyncMock(return_value=NS(raw_text='new caption', entities=None, media=None))
        self.edit_message = AsyncMock()
        self.send_message = AsyncMock()
        self.disconnect = AsyncMock()
        self.start = AsyncMock()
        self.run_until_disconnected = AsyncMock()
        self.forward = AsyncMock(side_effect=self.forward_response)

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
            self.assertTrue(m.needs_copy(SOURCE, 9, TARGET+1))
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
            await asyncio.sleep(0)
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
        self.bot.do_forward = AsyncMock()
        event = self.event(3, channel=SOURCE+1, post=False)
        event.respond = AsyncMock()
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
        await self.bot.do_forward(event, status)
        event.respond = AsyncMock(return_value=status)
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


if __name__ == '__main__':
    unittest.main()

