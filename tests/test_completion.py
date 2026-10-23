import unittest
from unittest.mock import patch

import completion as app


def record(**overrides):
    return dict(cabinet='Кабинет', supply_id='123', nm_id='456', quantity='10',
                date='25.10.2026', checked='FALSE', status='Одобрено', row=10, **overrides)


class CompletionTests(unittest.TestCase):
    def test_checkbox_uses_current_row_not_snapshot_number(self):
        old = record()
        live = {**old, 'row': 99, 'cabinet': 'КАБИНЕТ', 'date': '2026-10-25'}
        writes, checked, blocked = app.select_changes([old], [live])
        self.assertEqual(writes[0]['row'], 99)
        self.assertEqual((checked, blocked), (0, []))

    def test_duplicate_missing_and_changed_records_are_not_written(self):
        expected = record()
        for live in ([], [expected, expected], [{**expected, 'quantity': '20'}],
                     [{**expected, 'date': '26.10.2026'}], [{**expected, 'cabinet': 'Другой'}]):
            writes, _, blocked = app.select_changes([expected], live)
            self.assertFalse(writes)
            self.assertTrue(blocked)

    def test_repeat_confirmation_is_idempotent(self):
        expected = record()
        writes, checked, blocked = app.select_changes([expected], [{**expected, 'checked': 'TRUE'}])
        self.assertEqual((writes, checked, blocked), ([], 1, []))

    def test_write_only_checkbox_and_verify_afterwards(self):
        client = object.__new__(app.Checklist)
        expected = record()
        before = [{**expected, 'row': 99}]
        after = [{**expected, 'row': 100, 'checked': 'TRUE'}]
        with patch.object(client, 'read', side_effect=[(before, 14, 'Отгрузка FBO'),
                                                     (after, 14, 'Отгрузка FBO')]), \
             patch.object(client, 'request', return_value={}) as request:
            client.base = 'https://sheets.googleapis.com/v4/spreadsheets/test'
            result = client.mark([expected])
        body = request.call_args.kwargs['json']
        self.assertEqual(body, {'valueInputOption': 'RAW', 'data': [
            {'range': "'Отгрузка FBO'!O99", 'values': [[True]]}]})
        self.assertEqual(result['checked'], 1)
        self.assertFalse(result['blocked'])

    def test_audit_reports_new_rows_and_unchecked_rows(self):
        expected = record()
        client = object.__new__(app.Checklist)
        new = {**expected, 'supply_id': '789'}
        with patch.object(client, 'read', return_value=([expected, new], 14, 'Отгрузка FBO')):
            result = client.status([expected], '2026-10-25')
        self.assertEqual(result['checked'], 0)
        self.assertEqual(len(result['pending']), 1)
        self.assertEqual(len(result['new']), 1)


if __name__ == '__main__':
    unittest.main()
