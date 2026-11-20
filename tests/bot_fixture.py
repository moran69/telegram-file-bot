"""Shared offline harness, independent of optional historical regression suites."""
import importlib.util
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock, patch

from telethon import types, utils

source = Path(__file__).resolve().parents[1] / 'tdl-forward.py'
spec = importlib.util.spec_from_file_location('tdl_forward', source)
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
with patch.object(sys, 'path', [str(source.parent), *sys.path]):
    spec.loader.exec_module(app)

SOURCE, TARGET = 1012345678, 1098765432


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


class MonitorHarness:
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

