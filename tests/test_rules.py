import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from wb_declarations import base_vendor, choose_dt, fill_template, integer, choose_vendor_dt, load_receipts, prepare


class RulesTests(unittest.TestCase):
    def receipts(self):
        return [dict(arrival='20.10.2026', quantity='100', dt='10131010/201026/1234567', row=5),
                dict(arrival='19.10.2026', quantity='100', dt='10131010/191026/1234567', row=6)]

    def test_exact_stock_uses_second_receipt(self):
        dt, trace = choose_dt(self.receipts(), 200, date(2026, 10, 22))
        self.assertEqual(dt, '10131010/191026/1234567')
        self.assertEqual(trace[-1]['cumulative'], 200)

    def test_newest_receipt_can_cover_stock(self):
        self.assertEqual(choose_dt(self.receipts(), 50, date(2026, 10, 22))[0],
                         '10131010/201026/1234567')

    def test_insufficient_stock_and_missing_dt_stop(self):
        with self.assertRaises(ValueError):
            choose_dt(self.receipts(), 201, date(2026, 10, 22))
        receipts = self.receipts()
        receipts[0]['dt'] = ''
        with self.assertRaises(ValueError):
            choose_dt(receipts, 50, date(2026, 10, 22))

    def test_future_receipt_is_excluded(self):
        with self.assertRaises(ValueError):
            choose_dt(self.receipts(), 101, date(2026, 10, 19))

    def test_same_date_keeps_sheet_order_and_uses_quantity(self):
        receipts = self.receipts()
        receipts[1]['arrival'] = receipts[0]['arrival']
        self.assertEqual(choose_dt(receipts, 150, date(2026, 10, 22))[0],
                         '10131010/191026/1234567')
        self.assertEqual(choose_dt(list(reversed(receipts)), 50, date(2026, 10, 22))[0],
                         '10131010/201026/1234567')

    def test_vendor_suffix_and_quantity(self):
        self.assertEqual(base_vendor('wild399D'), 'wild399')
        self.assertEqual(base_vendor('wild399d'), 'wild399')
        self.assertEqual(base_vendor('wild1586d2'), 'wild1586')
        self.assertEqual(base_vendor('wild1863D3'), 'wild1863')
        self.assertEqual(integer('14\u00a0340'), 14340)
        with self.assertRaises(ValueError):
            integer('12.5')

    def test_fill_preserves_exact_vendor_and_original(self):
        from openpyxl import Workbook, load_workbook
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original, output, plan = root/'original.xlsx', root/'filled.xlsx', root/'plan.json'
            wb = Workbook()
            wb.active.append(['Баркод товара', 'Номер ДТ', 'Артикул', 'Артикул продавца',
                              'Наименование товара', 'Количество', 'Код страны происхождения'])
            wb.active.append(['1234567890123', '', 123456789, 'wild1826d', 'Вентиляторы', 42, ''])
            wb.save(original)
            original_bytes = original.read_bytes()
            record = dict(supply_id='12345678', state='READY', cabinet='МАГАЗИНА', nm_id='123456789',
                          wild='wild1826', vendor_code='wild1826d', quantity='42',
                          dt='10131010/201026/1234567', country='156')
            plan.write_text(json.dumps(dict(records=[record])), encoding='utf-8')
            fill_template(plan, original, '12345678', output)
            result = load_workbook(output).active
            self.assertEqual(result['D2'].value, 'wild1826d')
            self.assertEqual(result['A2'].value, '1234567890123')
            self.assertEqual(result['F2'].value, 42)
            self.assertEqual(result['B2'].value, record['dt'])
            self.assertEqual(result['G2'].value, '156')
            self.assertEqual(original.read_bytes(), original_bytes)
            record['quantity'] = '43'
            plan.write_text(json.dumps(dict(records=[record])), encoding='utf-8')
            with self.assertRaises(ValueError):
                fill_template(plan, original, '12345678', root/'wrong.xlsx')
            self.assertFalse((root/'wrong.xlsx').exists())


class KitRulesTests(unittest.TestCase):
    def receipts(self):
        return {'wild1945': [dict(arrival='20.10.2026',quantity='300',
            dt='10131010/201026/1234567',row=10,vehicle='',truck=''),
            dict(arrival='',quantity='100',dt='',row=20,vehicle='OTHER',truck='OTHER')]}

    def test_kit_uses_only_filled_declaration_from_authorized_component(self):
        dt,trace=choose_vendor_dt('wild103',self.receipts(),200,date(2026,10,22))
        self.assertEqual(dt,'10131010/201026/1234567')
        self.assertEqual({r['component'] for r in trace},{'wild1945'})

    def test_other_component_and_transport_do_not_block_authorized_source(self):
        receipts=self.receipts()
        receipts['wild2135']=[dict(arrival='',quantity='100',dt='',row=30,vehicle='OTHER',truck='OTHER')]
        self.assertEqual(choose_vendor_dt('wild103',receipts,200,date(2026,10,22))[0],
                         '10131010/201026/1234567')

    def test_multiple_filled_declarations_require_review(self):
        receipts=self.receipts()
        receipts['wild1945'][1]['dt']='10131010/191026/7654321'
        with self.assertRaisesRegex(ValueError,'несколько'):
            choose_vendor_dt('wild103',receipts,200,date(2026,10,22))

    def test_missing_source_is_not_replaced_with_other_component(self):
        receipts={'wild2135':self.receipts()['wild1945']}
        with self.assertRaisesRegex(ValueError,'wild1945'):
            choose_vendor_dt('wild103',receipts,200,date(2026,10,22))

    def test_unarrived_source_or_insufficient_quantity_is_not_accepted(self):
        receipts=self.receipts()
        with self.assertRaises(ValueError):
            choose_vendor_dt('wild103',receipts,301,date(2026,10,22))
        receipts['wild1945'][0]['arrival']=''
        with self.assertRaises(ValueError):
            choose_vendor_dt('wild103',receipts,200,date(2026,10,22))

    def test_invalid_filled_declaration_is_not_accepted(self):
        receipts=self.receipts()
        receipts['wild1945'][0]['dt']='invalid'
        with self.assertRaises(ValueError):
            choose_vendor_dt('wild103',receipts,200,date(2026,10,22))

    def test_prepare_keeps_finished_kit_article(self):
        records=[dict(wild='wild103D',free_stock='200',quantity='5',cabinet='Shop',product='Комплект')]
        prepare(records,self.receipts(),date(2026,10,22))
        self.assertEqual(records[0]['state'],'READY')
        self.assertEqual(records[0]['wild'],'wild103D')
        self.assertEqual(records[0]['dt'],'10131010/201026/1234567')

    def test_receipts_loader_reads_transport_by_header(self):
        rows=[['wild','Кол-во к заказу','ФАКТИЧЕСКАЯ ДАТА ПРИБЫТИЯ НА СКЛАД ОТ НАС',
               'НОМЕР ТАМОЖЕННОЙ ДЕКЛАРАЦИИ','Номер Трака','Номер ТС'],
              ['wild1945','100','20.10.2026','10131010/201026/1234567','TRUCK','CAR']]
        result=load_receipts(rows)['wild1945'][0]
        self.assertEqual((result['vehicle'],result['truck']),('CAR','TRUCK'))



if __name__ == '__main__':
    unittest.main()
