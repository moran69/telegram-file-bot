"""Offline checks for TDL-assisted publication, exact receipts and album integrity."""
import asyncio
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

import bot_fixture as fixtures

app, SOURCE, TARGET = fixtures.app, fixtures.SOURCE, fixtures.TARGET
T, F = app.types, app.functions.messages


def photo(mid, group=None, *, media_id=None, text='caption'):
    raw = T.Photo(media_id or mid, 10, b'ref', datetime.now(timezone.utc), [T.PhotoSize('x', 20, 20, 8)], 2)
    return NS(id=mid, grouped_id=group, media=T.MessageMediaPhoto(photo=raw), raw_text=text,
              entities=[T.MessageEntityBold(0, 3)])


def text(mid):
    return NS(id=mid, grouped_id=None, media=None, raw_text='text', entities=[])


class RestrictedCloneTests(fixtures.MonitorHarness, unittest.IsolatedAsyncioTestCase):
    def restrict(self, *posts):
        self.posts = {p.id: p for p in posts}
        self.bot.client.get_messages.side_effect = self.read
        self.bot.client.forward.side_effect = self.rpc
        self.bot.client.upload_file = AsyncMock(return_value=T.InputFile(1, 1, 'file', ''))
        self.bot.tdl.forward_clone = AsyncMock()
        self.bot.tdl.download_media = AsyncMock(side_effect=self.download)
        patch.object(app, 'MONITOR_TDL_FALLBACK', True).start()
        self.publish_error = None
        self.receipt = None

    async def read(self, peer, *, ids):
        if isinstance(ids, int):
            return self.posts.get(ids) if ids in self.posts else photo(ids)
        return [self.posts[mid] for mid in ids if mid in self.posts]

    async def download(self, link, output_file):
        Path(output_file).write_bytes(b'12345678')

    async def rpc(self, request):
        if isinstance(request, F.ForwardMessagesRequest):
            raise app.errors.ChatForwardsRestrictedError(None)
        if isinstance(request, F.UploadMediaRequest):
            if isinstance(request.media, T.InputMediaUploadedPhoto):
                return photo(9000).media
            return T.MessageMediaDocument(document=T.Document(9000, 1, b'ref', datetime.now(timezone.utc), 'video/mp4', 8, 2, []))
        if self.publish_error:
            raise self.publish_error
        if self.receipt is not None:
            return self.receipt
        ids = sorted(self.posts)
        nonces = ([m.random_id for m in request.multi_media] if isinstance(request, F.SendMultiMediaRequest)
                  else [request.random_id])
        return NS(updates=list(reversed([T.UpdateMessageID(500+mid*3, nonce) for mid, nonce in zip(ids, nonces)])))

    def publications(self):
        return [c.args[0] for c in self.bot.client.forward.await_args_list
                if isinstance(c.args[0], (F.SendMultiMediaRequest, F.SendMediaRequest, F.SendMessageRequest))]

    def state(self, mid):
        return self.bot.monitor_message_map().copy_state(SOURCE, mid, TARGET)

    async def test_album_one_request_exact_members_and_random_id_mapping(self):
        self.restrict(photo(3, 99), photo(7, 99), photo(12, 99))
        original_read = self.read
        async def read(peer, *, ids):
            return [text(8), *(await original_read(peer, ids=ids))] if len(ids) > 10 else await original_read(peer, ids=ids)
        self.bot.client.get_messages.side_effect = read
        await self.enqueue(self.event(3, 7, 12, album=99))
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        req, = self.publications()
        self.assertEqual([m.message for m in req.multi_media], ['caption'] * 3)
        self.assertTrue(all(m.entities == [T.MessageEntityBold(0, 3)] for m in req.multi_media))
        self.assertEqual([self.bot.monitor_message_map().get(SOURCE, i, TARGET) for i in (3,7,12)], [509,521,536])
        self.assertEqual([int(c.args[0].rsplit('/', 1)[1]) for c in self.bot.tdl.download_media.await_args_list], [3,7,12])
        self.bot.tdl.forward_clone.assert_not_awaited()

    async def test_missing_album_event_members_read_from_source(self):
        self.restrict(photo(3, 99), photo(7, 99), photo(12, 99))
        await self.enqueue(self.event(7, album=99))
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertEqual(len(self.publications()[0].multi_media), 3)

    async def test_previously_delivered_member_blocks_split_or_duplicate(self):
        self.restrict(photo(3, 99), photo(7, 99))
        self.bot.monitor_message_map().remember(SOURCE, TARGET, {3: 93})
        await self.enqueue(self.event(3, 7, album=99))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.assertFalse(self.publications())
        self.assertEqual(self.state(7), app.TaskStatus.PENDING)

    async def test_caption_edit_does_not_reattach_protected_media(self):
        self.restrict(photo(5))
        await self.enqueue(self.event(5)); await self.process()
        self.posts[5].raw_text = 'new caption'
        await self.enqueue(self.event(5), edited=True)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        call = self.bot.client.edit_message.await_args
        self.assertEqual((call.args[1], call.kwargs['text'], call.kwargs['file']), (515, 'new caption', None))
        self.bot.tdl.download_media.assert_awaited_once()

    async def test_media_replacement_edits_original_target(self):
        self.restrict(photo(5))
        await self.enqueue(self.event(5)); await self.process()
        self.posts[5] = photo(5, media_id=55)
        await self.enqueue(self.event(5), edited=True)
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertEqual(self.bot.client.edit_message.await_args.args[1], 515)
        self.assertIsInstance(self.bot.client.edit_message.await_args.kwargs['file'], T.InputMediaPhoto)
        self.assertEqual(self.bot.tdl.download_media.await_count, 2)
        self.assertEqual(len(self.publications()), 1)

    async def test_text_only_uses_send_message(self):
        self.restrict(text(5))
        await self.enqueue(self.event(5))
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertIsInstance(self.publications()[0], F.SendMessageRequest)
        self.bot.tdl.download_media.assert_not_awaited()

    async def test_video_attributes_and_spoiler_preserved(self):
        p = photo(5)
        attrs = [T.DocumentAttributeVideo(7, 640, 360, supports_streaming=True), T.DocumentAttributeFilename('v.mp4')]
        p.media = T.MessageMediaDocument(document=T.Document(5, 1, b'ref', datetime.now(timezone.utc), 'video/mp4', 8, 2, attrs), spoiler=True)
        self.restrict(p)
        await self.enqueue(self.event(5)); await self.process()
        upload, = [c.args[0] for c in self.bot.client.forward.await_args_list if isinstance(c.args[0], F.UploadMediaRequest)]
        self.assertEqual(upload.media.attributes, attrs)
        self.assertEqual(upload.media.mime_type, 'video/mp4')
        self.assertTrue(self.publications()[0].media.spoiler)

    async def test_download_failure_does_not_publish_partial_album(self):
        self.restrict(photo(3, 99), photo(7, 99))
        self.bot.tdl.download_media.side_effect = ValueError('failed')
        await self.enqueue(self.event(3, 7, album=99))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.assertFalse(self.publications())
        self.assertEqual([self.state(i) for i in (3,7)], [app.TaskStatus.PENDING]*2)

    async def test_missing_member_is_not_dropped(self):
        self.restrict(photo(3, 99))
        await self.enqueue(self.event(3, 7, album=99))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.assertFalse(self.publications())

    async def test_wrong_size_is_not_published(self):
        self.restrict(photo(5))
        async def small(link, path): Path(path).write_bytes(b'1')
        self.bot.tdl.download_media.side_effect = small
        await self.enqueue(self.event(5))
        with self.assertLogs(app.logger, 'ERROR'): await self.process()
        self.assertFalse(self.publications())

    async def test_partial_receipt_maps_only_confirmed_no_resend(self):
        self.restrict(photo(3, 99), photo(7, 99))
        async def rpc(req):
            if isinstance(req, F.SendMultiMediaRequest):
                return NS(updates=[T.UpdateMessageID(99, req.multi_media[0].random_id)])
            return await self.rpc(req)
        self.bot.client.forward.side_effect = rpc
        await self.enqueue(self.event(3, 7, album=99))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.assertEqual([self.state(i) for i in (3,7)], [app.TaskStatus.COMPLETED, app.TaskStatus.UNCONFIRMED])
        await self.enqueue(self.event(3, 7, album=99))
        self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.assertEqual(len(self.publications()), 1)

    async def test_lost_receipt_no_resend_across_restart(self):
        self.restrict(photo(3, 99), photo(7, 99))
        self.publish_error = ConnectionError('lost')
        await self.enqueue(self.event(3, 7, album=99))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        await self.bot.stop(); self.bot = self.make_bot()
        await self.enqueue(self.event(3, 7, album=99))
        self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.bot.client.forward.assert_not_awaited()

    async def test_database_failure_after_send_no_resend(self):
        self.restrict(photo(5))
        await self.enqueue(self.event(5))
        with patch.object(self.bot.monitor_message_map(), 'remember', side_effect=sqlite3.OperationalError('full')), self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        await self.enqueue(self.event(5))
        self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.assertEqual(len(self.publications()), 1)

    async def test_no_publish_without_durable_batch_intent(self):
        self.restrict(photo(5))
        await self.enqueue(self.event(5))
        with patch.object(self.bot.monitor_message_map(), 'start_tdl_upload', side_effect=sqlite3.OperationalError('full')), self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.assertFalse(self.publications())
        self.assertEqual(self.state(5), app.TaskStatus.UNCONFIRMED)

    async def test_latest_caption_after_download_and_no_obsolete_media(self):
        self.restrict(photo(5))
        async def download(link, path):
            await self.download(link, path)
            self.posts[5].raw_text = 'latest caption'
        self.bot.tdl.download_media.side_effect = download
        await self.enqueue(self.event(5)); await self.process()
        self.assertEqual(self.publications()[0].message, 'latest caption')

    async def test_source_media_changed_during_download_aborts_publication(self):
        self.restrict(photo(5))
        async def download(link, path):
            await self.download(link, path)
            self.posts[5] = photo(5, media_id=55)
        self.bot.tdl.download_media.side_effect = download
        await self.enqueue(self.event(5))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.assertFalse(self.publications())
        self.assertEqual(self.state(5), app.TaskStatus.PENDING)

    async def test_album_members_changed_during_download_are_not_silently_omitted(self):
        self.restrict(photo(3, 99), photo(7, 99))
        async def download(link, path):
            await self.download(link, path)
            self.posts[12] = photo(12, 99)
        self.bot.tdl.download_media.side_effect = download
        await self.enqueue(self.event(3, 7, album=99))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.assertFalse(self.publications())

    async def test_resync_command_uses_clone_mapping_and_returns_target_link(self):
        self.restrict(photo(5))
        await self.enqueue(self.event(5)); await self.process()
        event = NS(sender_id=61, chat_id=61, client=self.bot.client,
                   message=NS(text=f'/resync https://t.me/c/{SOURCE}/5'), respond=AsyncMock())
        with patch.object(app, 'ADMIN_IDS', [61]):
            self.assertTrue(await self.bot.handle_admin_command(event))
        self.assertIn(f'https://t.me/c/{TARGET}/515', event.respond.await_args.args[0])
        await self.process()
        self.assertEqual(self.bot.client.edit_message.await_args.args[1], 515)

    async def test_early_edit_is_reconciled_after_clone_mapping(self):
        self.restrict(photo(5))
        await self.enqueue(self.event(5), edited=True); await self.process()
        await self.enqueue(self.event(5)); await self.process()
        self.assertFalse(self.bot.task_queue.queue.empty())
        await self.process()
        self.bot.client.edit_message.assert_awaited_once()

    async def test_mapping_and_clone_media_state_survive_restart(self):
        self.restrict(photo(5))
        await self.enqueue(self.event(5)); await self.process()
        await self.bot.stop(); self.bot = self.make_bot()
        self.bot.client.get_messages.side_effect = self.read
        await self.enqueue(self.event(5), edited=True); await self.process()
        self.assertEqual(self.bot.client.edit_message.await_args.args[1], 515)
        self.assertIsNone(self.bot.client.edit_message.await_args.kwargs['file'])
        self.assertEqual(self.bot.monitor_message_map().sender_role(SOURCE, 5, TARGET), 'bot')

    async def test_user_sender_is_used_for_later_bot_observed_edit(self):
        self.restrict(photo(5))
        user = self.bot.user_client = fixtures.FakeClient()
        user.get_messages.side_effect = self.read
        user.forward.side_effect = self.rpc
        user.upload_file = AsyncMock(return_value=T.InputFile(1, 1, 'file', ''))
        self.bot.user_monitor_running = True
        ev = self.event(5); ev.client = user
        await self.enqueue(ev); await self.process()
        self.assertEqual(self.bot.monitor_message_map().sender_role(SOURCE, 5, TARGET), 'user')
        await self.enqueue(self.event(5), edited=True); await self.process()
        user.edit_message.assert_awaited_once()
        self.bot.client.edit_message.assert_not_awaited()

    async def test_album_boundary_and_unknown_member_block_publication(self):
        self.restrict(photo(150, 99), photo(250, 99))
        await self.enqueue(self.event(150, album=99))
        with self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.assertFalse(self.publications())

    async def test_old_schema_migrates_and_pending_intent_is_not_republished(self):
        with closing(sqlite3.connect(app.MONITOR_MAP_FILE)) as db, db:
            db.execute('CREATE TABLE monitor_copy_intents(source_chat_id INTEGER, source_message_id INTEGER, '
                       'target_chat_id INTEGER, random_id INTEGER UNIQUE, '
                       'PRIMARY KEY(source_chat_id,source_message_id,target_chat_id))')
            db.execute('INSERT INTO monitor_copy_intents VALUES(?,?,?,?)', (SOURCE, 5, TARGET, 42))
        self.restrict(photo(5))
        await self.enqueue(self.event(5))
        self.assertEqual((await self.process()).status, app.TaskStatus.UNCONFIRMED)
        self.bot.client.forward.assert_not_awaited()

    async def test_batch_intent_change_rolls_back_atomically(self):
        m = self.bot.monitor_message_map()
        reserved = m.reserve(SOURCE, TARGET, [3, 7])
        wrong = dict(reserved); wrong[7] += 1
        with self.assertRaises(app.CopyOutcomeUnknown): m.start_tdl_upload(SOURCE, TARGET, wrong)
        self.assertEqual(m.db.execute('SELECT DISTINCT transport FROM monitor_copy_intents').fetchall(), [('telegram',)])

    async def test_cancel_during_download_releases_all_and_cleans_files(self):
        self.restrict(photo(3, 99), photo(7, 99))
        entered, paths = asyncio.Event(), []
        async def hang(link, path):
            paths.append(Path(path).parent); entered.set(); await asyncio.Future()
        self.bot.tdl.download_media.side_effect = hang
        await self.enqueue(self.event(3, 7, album=99))
        worker = asyncio.create_task(self.process())
        await asyncio.wait_for(entered.wait(), 2); worker.cancel()
        with self.assertRaises(asyncio.CancelledError): await worker
        self.assertEqual([self.state(i) for i in (3,7)], [app.TaskStatus.PENDING]*2)
        self.assertFalse(self.publications())
        self.assertTrue(all(not p.exists() for p in paths))

    async def test_cancel_during_publish_keeps_whole_album_unconfirmed(self):
        self.restrict(photo(3, 99), photo(7, 99))
        entered = asyncio.Event()
        async def rpc(req):
            if isinstance(req, F.SendMultiMediaRequest):
                entered.set(); await asyncio.Future()
            return await self.rpc(req)
        self.bot.client.forward.side_effect = rpc
        await self.enqueue(self.event(3, 7, album=99))
        worker = asyncio.create_task(self.process())
        await asyncio.wait_for(entered.wait(), 2); worker.cancel()
        with self.assertRaises(asyncio.CancelledError): await worker
        self.assertEqual([self.state(i) for i in (3,7)], [app.TaskStatus.UNCONFIRMED]*2)

    async def test_only_restricted_forward_triggers_fallback(self):
        self.restrict(photo(5))
        for mid, error in enumerate((app.errors.ChatWriteForbiddenError(None), app.errors.ChannelPrivateError(None),
                                     app.errors.ChatAdminRequiredError(None), ConnectionError('lost')), start=1):
            self.bot.client.forward.side_effect = error
            await self.enqueue(self.event(mid))
            with self.assertLogs(app.logger, 'ERROR'): await self.process()
        self.bot.tdl.download_media.assert_not_awaited()

    async def test_disabled_switch(self):
        self.restrict(photo(5))
        await self.enqueue(self.event(5))
        with patch.object(app, 'MONITOR_TDL_FALLBACK', False), self.assertLogs(app.logger, 'ERROR'):
            self.assertEqual((await self.process()).status, app.TaskStatus.FAILED)
        self.bot.tdl.download_media.assert_not_awaited()

    async def test_legacy_completion_deduplicated_without_inventing_mapping(self):
        self.restrict(photo(5))
        m = self.bot.monitor_message_map()
        nonce = m.reserve(SOURCE, TARGET, [5])[5]
        m.start_tdl_clone(SOURCE, 5, TARGET, nonce); m.complete_tdl_clone(SOURCE, 5, TARGET, nonce)
        await self.enqueue(self.event(5))
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.bot.client.forward.assert_not_awaited()
        self.assertIsNone(m.get(SOURCE, 5, TARGET))

    async def test_cleanup_retains_clone_deduplication(self):
        self.restrict(photo(5))
        await self.enqueue(self.event(5)); await self.process()
        with patch.object(app.time, 'time', return_value=9999999999): self.bot.monitor_message_map().cleanup_if_due(7)
        await self.enqueue(self.event(5))
        self.assertEqual((await self.process()).status, app.TaskStatus.COMPLETED)
        self.assertEqual(len(self.publications()), 1)

    async def test_download_command_single_member_noninteractive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / '5.bin'
            async def run(cmd, *args): path.write_bytes(b'12345678'); return 0, '', ''
            with patch.object(self.bot.tdl, '_run_command', side_effect=run) as command:
                await self.bot.tdl.download_media(f'https://t.me/c/{SOURCE}/5', str(path))
            self.assertEqual(command.call_args.args[0], [app.TDL_PATH, 'download', '--url',
                f'https://t.me/c/{SOURCE}/5', '--dir', directory, '--template', '5.bin', '--restart'])

    async def test_manual_clone_zero_exit_with_message_error_still_fails(self):
        async def run(cmd, progress):
            await progress(f'Source({SOURCE}):5 -> Target({TARGET}) error: upload failed')
            return 0, '', ''
        with patch.object(self.bot.tdl, '_run_command', side_effect=run):
            ok, output = await self.bot.tdl.forward_clone(f'https://t.me/c/{SOURCE}/5', TARGET)
        self.assertFalse(ok)
        self.assertIn('upload failed', output)


class ReceiptTests(unittest.TestCase):
    def test_short_single_message_receipt(self):
        response = T.UpdateShortSentMessage(42, 1, 1, datetime.now(timezone.utc))
        self.assertEqual(app.receipt_pairs(response, {5: 55}), {5: 42})
        self.assertEqual(app.receipt_pairs(response, {5: 55, 7: 77}), {})

    def test_unrelated_updates_are_not_used_as_mapping(self):
        self.assertEqual(app.receipt_pairs(NS(updates=[T.UpdateMessageID(1, 999)]), {5: 55}), {})

    def test_conflicting_or_duplicate_or_invalid_ids_rejected(self):
        for updates in ([T.UpdateMessageID(1, 55), T.UpdateMessageID(2, 55)],
                        [T.UpdateMessageID(1, 55), T.UpdateMessageID(1, 77)],
                        [T.UpdateMessageID(-1, 55)]):
            with self.subTest(updates=updates), self.assertRaises(ValueError):
                app.receipt_pairs(NS(updates=updates), {5: 55, 7: 77})
