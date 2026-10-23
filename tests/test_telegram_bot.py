import asyncio
import os
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import telegram_bot as app
from aiogram.enums import ChatType


class BotTests(unittest.TestCase):
    def test_owner_private_chat_only(self):
        with patch.object(app, 'setting', return_value='123'):
            self.assertTrue(app.is_owner(123, 'private'))
            self.assertTrue(app.is_owner(123, ChatType.PRIVATE))
            self.assertFalse(app.is_owner(456, ChatType.PRIVATE))
            self.assertFalse(app.is_owner(123, ChatType.GROUP))
            self.assertFalse(app.is_owner(456, 'private'))
            self.assertFalse(app.is_owner(123, 'group'))
        with patch.object(app, 'setting', return_value=''):
            self.assertFalse(app.is_owner(123, 'private'))

    def test_owner_changes_in_env_file_apply_without_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            env = Path(directory) / '.env'
            env.write_text('TELEGRAM_ALLOWED_USER_ID=456\n', encoding='utf-8')
            with patch.object(app, 'ENV', env), patch.dict(os.environ, {'TELEGRAM_ALLOWED_USER_ID': '123'}):
                self.assertTrue(app.is_owner(456, ChatType.PRIVATE))
                self.assertFalse(app.is_owner(123, ChatType.PRIVATE))
                env.write_text('TELEGRAM_ALLOWED_USER_ID=\n', encoding='utf-8')
                self.assertFalse(app.is_owner(456, ChatType.PRIVATE))

    def test_date_validation(self):
        self.assertEqual(app.requested_date('25.10.2026'), date(2026, 10, 25))
        for invalid in ('31.02.2026', '../../.env', '2026-10-25', '25.10.2026 extra'):
            with self.assertRaises(ValueError):
                app.requested_date(invalid)

    def test_archive_contains_only_workbooks_and_user_reports(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            cabinet = folder / 'Тестовый кабинет'
            cabinet.mkdir()
            (cabinet / '123.xlsx').write_bytes(b'workbook')
            (folder / app.REPORT_NAME).write_text('report', encoding='utf-8')
            (folder / '.env').write_text('secret', encoding='utf-8')
            (folder / 'sources.json').write_text('private paths', encoding='utf-8')
            (cabinet / 'credentials.json').write_text('secret', encoding='utf-8')
            archives = app.build_archives(folder)
            with zipfile.ZipFile(archives[0]) as archive:
                self.assertEqual(set(archive.namelist()),
                                 {'Тестовый кабинет/123.xlsx', app.REPORT_NAME})


class HandlerTests(unittest.IsolatedAsyncioTestCase):
    async def test_unauthorized_request_never_reads_business_data(self):
        dispatcher = app.create_dispatcher()
        handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == 'files')
        message = SimpleNamespace(from_user=SimpleNamespace(id=456),
                                  chat=SimpleNamespace(type=ChatType.PRIVATE), text='/files 25.10.2026',
                                  answer=AsyncMock())
        with patch.object(app, 'setting', return_value='123'), patch.object(app, 'generate_files', new_callable=AsyncMock) as generation:
            await handler(message)
            generation.assert_not_awaited()
            message.answer.assert_awaited_once()

    async def test_owner_receives_generated_zip(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder/'Кабинет').mkdir()
            (folder/'Кабинет'/'123.xlsx').write_bytes(b'workbook')
            dispatcher = app.create_dispatcher()
            handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == 'files')
            progress = SimpleNamespace(edit_text=AsyncMock())
            message = SimpleNamespace(from_user=SimpleNamespace(id=123),
                chat=SimpleNamespace(type=ChatType.PRIVATE), text='/files 25.10.2026',
                answer=AsyncMock(return_value=progress), answer_document=AsyncMock())
            counts = dict(total=1, created=1, missing=0, cabinets=1)
            batch = dict(id='a'*12, records=[dict(cabinet='Кабинет', supply_id='123')])
            with patch.object(app, 'setting', return_value='123'), patch.object(app, 'generate_files',
                    new_callable=AsyncMock, return_value=(folder, counts, '2026-10-25')) as generation, \
                    patch.object(app, 'save_batch', return_value=batch):
                await handler(message)
                generation.assert_awaited_once_with(date(2026, 10, 25))
                message.answer_document.assert_awaited_once()
                document = message.answer_document.call_args.args[0]
                self.assertEqual(document.filename, 'Декларации_25.10.2026.zip')

    async def test_unauthorized_callback_never_writes_checkboxes(self):
        dispatcher = app.create_dispatcher()
        handler = next(h.callback for h in dispatcher.callback_query.handlers)
        callback = SimpleNamespace(from_user=SimpleNamespace(id=456),
            message=SimpleNamespace(chat=SimpleNamespace(type=ChatType.PRIVATE), answer=AsyncMock()),
            data='done:'+'a'*12, answer=AsyncMock())
        with patch.object(app, 'setting', return_value='123'), patch.object(app, 'cache_service') as cache, patch.object(app, 'Checklist') as client:
            await handler(callback)
            client.assert_not_called()
            callback.answer.assert_awaited_once()

    async def test_partial_confirmation_writes_only_selected_supply(self):
        with tempfile.TemporaryDirectory() as directory:
            dispatcher = app.create_dispatcher()
            handler = next(h.callback for h in dispatcher.callback_query.handlers)
            first = dict(cabinet='A', supply_id='1')
            second = dict(cabinet='B', supply_id='2')
            batch = dict(records=[first, second], shipment_date='2026-10-25')
            callback = SimpleNamespace(from_user=SimpleNamespace(id=123),
                message=SimpleNamespace(chat=SimpleNamespace(type=ChatType.PRIVATE), answer=AsyncMock()),
                data='one:'+'a'*12+':1', answer=AsyncMock())
            with patch.object(app, 'setting', return_value='123'), \
                 patch.object(app, 'load_batch', return_value=batch), \
                 patch.object(app, 'BATCHES', Path(directory)), patch.object(app, 'cache_service') as cache, patch.object(app, 'Checklist') as client:
                cache.return_value.evict_confirmed = AsyncMock()
                client.return_value.mark.return_value = dict(checked=1, updated=1, blocked=[])
                await handler(callback)
                client.return_value.mark.assert_called_once_with([second])


if __name__ == '__main__':
    unittest.main()
