"""Startup observability without connecting accounts, invoking TDL or publishing posts."""
import sqlite3
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot_fixture import MonitorHarness, SOURCE, TARGET, app


class StartupLogTests(MonitorHarness, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await super().asyncSetUp()
        self.bot.tdl.tdl_path = sys.executable  # An executable file; must not actually be invoked.
        self.bot.tdl._run_command = AsyncMock(side_effect=AssertionError('unexpected subprocess'))
        patch.multiple(app, API_ID=123, API_HASH='fake-api-secret', BOT_TOKEN='fake-token-secret',
                       USER_SESSION_STRING='fake-session-secret', USER_SESSION_FILE='private-session-location',
                       MONITOR_MAP_RETENTION_DAYS=0).start()

    async def test_successful_startup_is_ordered_and_reports_empty_queue(self):
        with self.assertLogs(app.logger, 'INFO') as logs:
            await self.bot.start()
        out = '\n'.join(logs.output)
        stages = ['[启动/配置]', '[启动/TDL]', '正在连接 Telegram', 'Bot 身份验证成功', '[启动/用户监听]',
                  '正在恢复持久化任务', '读取 0 条持久化记录', '[启动/就绪]']
        self.assertEqual(sorted(stages, key=out.index), stages)
        for expected in (str(SOURCE), str(TARGET), '未检查 TDL 登录状态', '仅 Bot', '永久保留', '待核对 0'):
            self.assertIn(expected, out)
        for secret in (app.API_HASH, app.BOT_TOKEN, app.USER_SESSION_STRING, app.USER_SESSION_FILE):
            self.assertNotIn(secret, out)
        self.bot.tdl._run_command.assert_not_awaited()

    async def test_missing_tdl_and_target_warn_without_blocking_login(self):
        self.bot.tdl.tdl_path = str(Path(app.MONITOR_MAP_FILE).parent / 'no-tdl')
        self.bot.target_chat_id = None
        with self.assertLogs(app.logger, 'INFO') as logs:
            await self.bot.start()
        self.assertTrue(any(r.levelname == 'WARNING' and '请检查挂载' in r.getMessage() for r in logs.records))
        self.assertIn('自动同步=等待设置目标频道', '\n'.join(logs.output))
        self.bot.client.start.assert_awaited_once()

    async def test_failed_panel_does_not_leak_exception_or_claim_delivery(self):
        self.bot.admin_panel.open = AsyncMock(side_effect=[None, ValueError(app.BOT_TOKEN)])
        with patch.object(app, 'ADMIN_IDS', [10, 20]), self.assertLogs(app.logger, 'INFO') as logs:
            await self.bot.start()
        out = '\n'.join(logs.output)
        self.assertIn('启动面板发送失败（ValueError）', out)
        self.assertIn('管理员面板成功 1/2', out)
        self.assertNotIn(app.BOT_TOKEN, out)

    async def test_restore_counts_are_measured_not_assumed(self):
        q = self.bot.task_queue
        for mid, state in enumerate(('pending', 'pending', 'review', 'early'), 1):
            task = app.ForwardTask(None, 'hidden-payload', None, 1, TARGET, source_chat_id=SOURCE,
                                   message_ids=[mid], mode='monitor_edit' if state == 'early' else 'clone')
            if mid == 2:
                task.ready_at = time.time() + 3600
            q.journal.save(task, state)
        with self.assertLogs(app.logger, 'INFO') as logs:
            q._restore()
        out = '\n'.join(logs.output)
        self.assertIn('读取 4 条持久化记录：立即入队 1，定时重试 1，等待编辑映射 1，待核对 1', out)
        self.assertNotIn('hidden-payload', out)

    async def test_failure_reports_stage_not_secret_or_ready(self):
        self.bot.task_queue.start = AsyncMock(side_effect=sqlite3.OperationalError(app.USER_SESSION_STRING))
        with patch.object(app, 'TelegramBot', return_value=self.bot), self.assertLogs(app.logger, 'INFO') as logs:
            with self.assertRaises(SystemExit) as exc:
                await app.main()
        self.assertEqual(exc.exception.code, 1)
        out = '\n'.join(logs.output)
        self.assertIn('阶段=队列恢复，异常=OperationalError', out)
        self.assertNotIn('[启动/就绪]', out)
        self.assertNotIn(app.USER_SESSION_STRING, out)

    def test_invalid_source_setting_does_not_echo_raw_input(self):
        with self.assertLogs(app.logger, 'WARNING') as logs:
            self.assertEqual(app.parse_chat_ids(f'{app.BOT_TOKEN},{SOURCE}'), [SOURCE])
        self.assertNotIn(app.BOT_TOKEN, '\n'.join(logs.output))
