import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace

from file_cache import CacheStore, CacheService, signature, pending_groups
import telegram_bot as app
from aiogram.enums import ChatType


def record(supply='123', cabinet='Shop', checked='FALSE'):
    return dict(cabinet=cabinet, supply_id=supply, date='2026-10-25', checked=checked,
                nm_id='456', quantity='10', free_stock='20', wild='wild399', product='Item')


def entry(goods, blob=b'prepared workbook'):
    return ('shop', goods[0]['supply_id'], 'Shop', goods[0]['date'],
            json.dumps(goods), '[]', blob, '' if blob else 'Missing barcode')


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CacheStore(self.root/'cache.sqlite3')
        self.goods = [record()]
        self.store.publish([entry(self.goods)], self.goods)

    def test_workbook_persists_and_lookup_never_reads_external_sources(self):
        reopened = CacheStore(self.store.path)
        with patch('file_cache.Sheets', side_effect=AssertionError('No network')), \
             patch('export_templates.read_database_catalog', side_effect=AssertionError('No database')):
            folder, counts, date = reopened.materialize(supply_id='123', root=self.root/'delivery')
        self.assertEqual((folder/'Shop'/'123.xlsx').read_bytes(), b'prepared workbook')
        self.assertEqual(counts['created'], 1)
        self.assertEqual(date, '2026-10-25')

    def test_missing_number_does_not_refresh(self):
        with patch('file_cache.Sheets', side_effect=AssertionError('No network')):
            with self.assertRaises(RuntimeError):
                self.store.materialize(supply_id='999')

    def test_failed_publish_rolls_back_old_files(self):
        with self.assertRaises(Exception):
            self.store.publish([('broken',)], [])
        self.assertEqual(self.store.info()['files'], 1)

    def test_manual_checkbox_or_changed_stock_invalidates_file(self):
        self.assertEqual(self.store.prune([dict(record(), free_stock='21')]), 1)
        self.store.publish([entry(self.goods)], [])
        self.assertEqual(self.store.prune([record(checked='TRUE')]), 1)
        self.assertEqual(self.store.info()['files'], 0)

    def test_partial_checked_goods_do_not_form_partial_workbook(self):
        live = [record(), dict(record(checked='TRUE'), nm_id='789')]
        self.assertEqual(pending_groups(live), {})

    def test_confirmation_does_not_evict_newer_or_partial_supply(self):
        self.store.evict_confirmed([dict(record(), quantity='9')])
        self.assertEqual(self.store.info()['files'], 1)
        self.store.evict_confirmed([record()])
        self.assertEqual(self.store.info()['files'], 0)

    def test_unchanged_snapshot_survives_row_reordering(self):
        self.assertEqual(self.store.prune([dict(record(), row=900)]), 0)

    def test_failed_refresh_keeps_previous_snapshot(self):
        service = CacheService(self.store)
        with patch.object(service, 'refresh_sync', side_effect=OSError('private details')):
            with self.assertRaises(RuntimeError) as error:
                asyncio.run(service.refresh())
        self.assertNotIn('private details', str(error.exception))
        self.assertEqual(self.store.info()['files'], 1)
        self.assertEqual(self.store.info()['last_error'], 'OSError')


class PlainNumberTests(unittest.IsolatedAsyncioTestCase):
    async def test_plain_number_routes_to_cached_delivery(self):
        dispatcher = app.create_dispatcher()
        handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == 'text_message')
        message = SimpleNamespace(from_user=SimpleNamespace(id=123),
            chat=SimpleNamespace(type=ChatType.PRIVATE), text='12345678', answer=AsyncMock())
        with patch.object(app, 'setting', return_value='123'), \
             patch.object(app, 'generate_files', new_callable=AsyncMock,
                          side_effect=RuntimeError('Cache miss')) as generate:
            await handler(message)
        generate.assert_awaited_once_with(None, cabinet=None, supply_id='12345678')


if __name__ == '__main__':
    unittest.main()
