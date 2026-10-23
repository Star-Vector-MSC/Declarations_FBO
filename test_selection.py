import csv
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatType
import telegram_bot as app
import completion
from wb_declarations import shipment_queue


class SelectionTests(unittest.TestCase):
    def rows(self):
        return [
            ['Номер поставки', 'Дата отгрузки (склад)', 'Статус отгрузки', 'wild', 'Артикул',
             'Чек лист Декларация', 'Свободный остаток', 'Количество',
             'Название магазина', 'Название магазина'],
            ['1', '24.10.2026', 'Одобрено', 'wild1', '11', 'FALSE', '100', '10', 'Другой', 'Товар'],
            ['2', '25.10.2026', 'Одобрено', 'wild2', '22', 'FALSE', '100', '10', 'МагазинА', 'Товар'],
            ['3', '25.10.2026', 'Одобрено', 'wild3', '33', 'TRUE', '100', '10', 'МагазинА', 'Товар']]

    def test_cabinet_filter_happens_before_nearest_date_selection(self):
        selected, rows = shipment_queue(self.rows(), None, date(2026, 10, 22),
                                       cabinet='МАГАЗИНА', include_checked=True)
        self.assertEqual(selected, date(2026, 10, 25))
        self.assertEqual([r['supply_id'] for r in rows], ['2', '3'])

    def test_explicit_supply_can_be_requested_again_after_confirmation(self):
        selected, rows = shipment_queue(self.rows(), None, date(2026, 10, 22),
                                       supply_id='3', include_checked=True)
        self.assertEqual([r['supply_id'] for r in rows], ['3'])
        _, general = shipment_queue(self.rows(), date(2026, 10, 25), date(2026, 10, 22))
        self.assertEqual([r['supply_id'] for r in general], ['2'])

    def test_cabinet_command_with_spaces_and_optional_date(self):
        self.assertEqual(app.parse_cabinet_request('Мой кабинет 25.10.2026'),
                         ('Мой кабинет', date(2026, 10, 25)))
        self.assertEqual(app.parse_cabinet_request('МагазинА'), ('МагазинА', None))
        with self.assertRaises(ValueError):
            app.parse_cabinet_request('')

    def test_confirmation_snapshot_contains_only_the_sent_supply(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root/'runs').mkdir()
            folder = root/'export'
            folder.mkdir()
            records = [dict(cabinet='A', supply_id=str(n), nm_id=str(n), quantity='10', date='2026-10-25')
                       for n in (1, 2)]
            plan = root/'runs'/'plan.json'
            plan.write_text(json.dumps(dict(records=records, shipment_date='2026-10-25')), encoding='utf-8')
            (folder/'sources.json').write_text(json.dumps(dict(plan=str(plan), identities={'A/1': [], 'A/2': []})), encoding='utf-8')
            with patch.object(completion, 'PROJECT', root), patch.object(completion, 'BATCHES', root/'batches'):
                batch = completion.save_batch(folder, 123, delivered_supplies=[('A', '2')])
            self.assertEqual([r['supply_id'] for r in batch['records']], ['2'])

    def test_single_file_audit_does_not_report_other_supplies_as_new(self):
        original = dict(cabinet='A', supply_id='1', nm_id='11', quantity='10',
                        date='2026-10-25', checked='FALSE', status='Одобрено', row=2)
        other_supply = {**original, 'supply_id': '2', 'row': 3}
        new_goods_same_supply = {**original, 'nm_id': '33', 'row': 4}
        client = object.__new__(completion.Checklist)
        with patch.object(client, 'read', return_value=(
                [original, other_supply, new_goods_same_supply], 14, 'Отгрузка FBO')):
            result = client.status([original], '2026-10-25', scope_supplies=[['A', '1']])
        self.assertEqual(result['new'], ['A / 1'])


class DirectDeliveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_supply_sends_xlsx_with_confirmation_for_only_that_supply(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            (folder/'МагазинА').mkdir()
            path = folder/'МагазинА'/'123.xlsx'
            path.write_bytes(b'workbook')
            batch = dict(id='a'*12, records=[dict(cabinet='МагазинА', supply_id='123')])
            dispatcher = app.create_dispatcher()
            handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == 'supply')
            message = SimpleNamespace(from_user=SimpleNamespace(id=123),
                chat=SimpleNamespace(type=ChatType.PRIVATE), text='/supply 123',
                answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())),
                answer_document=AsyncMock())
            with patch.object(app, 'setting', return_value='123'), \
                 patch.object(app, 'generate_files', new_callable=AsyncMock,
                    return_value=(folder, dict(total=1, created=1, missing=0, cabinets=1), '2026-10-25')) as generate, \
                 patch.object(app, 'direct_documents', return_value=([('МагазинА', '123', path)], [])), \
                 patch.object(app, 'save_batch', return_value=batch) as save:
                await handler(message)
                generate.assert_awaited_once_with(None, cabinet=None, supply_id='123')
                save.assert_called_once_with(folder, 123, delivered_supplies=[('МагазинА', '123')])
                message.answer_document.assert_awaited_once()
                self.assertEqual(message.answer_document.call_args.args[0].filename, '123.xlsx')
                self.assertEqual(message.answer_document.call_args.kwargs['reply_markup'].inline_keyboard[0][0].text, 'Готово')

    async def test_cabinet_routes_exact_name_and_date(self):
        dispatcher = app.create_dispatcher()
        handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == 'cabinet_request')
        message = SimpleNamespace(from_user=SimpleNamespace(id=123),
            chat=SimpleNamespace(type=ChatType.PRIVATE), text='/cabinet МагазинА 25.10.2026',
            answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))
        with patch.object(app, 'setting', return_value='123'), patch.object(app, 'generate_files',
                new_callable=AsyncMock, return_value=(Path('unused'), dict(total=0), None)) as generate:
            await handler(message)
            generate.assert_awaited_once_with(date(2026, 10, 25), cabinet='МагазинА', supply_id=None)


if __name__ == '__main__':
    unittest.main()
