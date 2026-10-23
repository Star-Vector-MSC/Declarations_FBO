"""Prepare WB customs declaration files; Google Sheets and PostgreSQL are read-only."""
import argparse
import csv
import json
import os
import re
from collections import defaultdict
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import quote
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

PROJECT = Path(__file__).resolve().parent
load_dotenv(PROJECT / '.env', override=False, interpolate=False)
def configured_path(value):
    path = Path(value).expanduser()
    return path if path.is_absolute() else PROJECT / path


KEY = configured_path(os.environ.get('GOOGLE_APPLICATION_CREDENTIALS', 'secrets/google-service-account.json'))
RECEIPTS = (os.environ.get('RECEIPTS_SPREADSHEET_ID', ''), int(os.environ.get('RECEIPTS_SHEET_GID') or '0'))
SHIPMENTS = (os.environ.get('SHIPMENTS_SPREADSHEET_ID', ''), int(os.environ.get('SHIPMENTS_SHEET_GID') or '0'))
BASE_TEMPLATE = configured_path(os.environ.get('WB_BASE_TEMPLATE', 'assets/wb_declaration_template.xlsx'))


def normalize(value):
    return ' '.join(str(value).split()).casefold()


def base_vendor(value):
    value = normalize(value)
    match = re.fullmatch(r'(wild\d+)(?:d\d*)?', value)
    if not match:
        raise ValueError(f'Unsupported vendor code: {value!r}')
    return match.group(1)


def integer(value):
    try:
        number = Decimal(re.sub(r'\s', '', str(value)).replace(',', '.'))
    except InvalidOperation:
        raise ValueError('Missing or invalid quantity') from None
    if not number.is_finite() or number < 0 or number != number.to_integral_value():
        raise ValueError('Quantity must be a nonnegative integer')
    return int(number)


def parse_date(value):
    for fmt in ('%d.%m.%Y', '%Y-%m-%d'):
        try:
            return datetime.strptime(str(value).strip(), fmt).date()
        except ValueError:
            pass
    raise ValueError(f'Invalid date: {value!r}')


def value_at(row, index):
    return row[index] if index < len(row) else ''


def index(headers, name):
    found = [i for i, h in enumerate(headers) if normalize(h) == normalize(name)]
    if len(found) != 1:
        raise ValueError(f'Expected one header {name!r}; found {len(found)}')
    return found[0]


class Sheets:
    def __init__(self, key):
        from google.auth.transport.requests import AuthorizedSession
        from google.oauth2.service_account import Credentials
        credentials = Credentials.from_service_account_file(
            str(key), scopes=['https://www.googleapis.com/auth/spreadsheets.readonly'])
        self.session = AuthorizedSession(credentials)

    def read(self, source):
        spreadsheet, gid = source
        base = f'https://sheets.googleapis.com/v4/spreadsheets/{spreadsheet}'
        response = self.session.get(base, params={
            'fields': 'sheets.properties(sheetId,title,gridProperties)'}, timeout=40)
        if response.status_code != 200:
            raise ValueError(f'Cannot read spreadsheet {spreadsheet}: HTTP {response.status_code}')
        props = next((s['properties'] for s in response.json()['sheets']
                      if s['properties']['sheetId'] == gid), None)
        if props is None:
            raise ValueError(f'Sheet gid {gid} not found')
        title = props['title'].replace("'", "''")
        response = self.session.get(base + '/values/' + quote(f"'{title}'", safe=''), timeout=60)
        if response.status_code != 200:
            raise ValueError(f'Cannot read sheet gid {gid}: HTTP {response.status_code}')
        return response.json().get('values', [])


def load_receipts(rows):
    header_row = next((n for n, r in enumerate(rows) if any(normalize(h) == 'wild' for h in r)), None)
    if header_row is None:
        raise ValueError('Receipts header not found')
    headers = rows[header_row]
    cols = {k: index(headers, h) for k, h in {
        'wild': 'wild', 'quantity': 'Кол-во к заказу',
        'arrival': 'ФАКТИЧЕСКАЯ ДАТА ПРИБЫТИЯ НА СКЛАД ОТ НАС',
        'dt': 'НОМЕР ТАМОЖЕННОЙ ДЕКЛАРАЦИИ'}.items()}
    result = defaultdict(list)
    for n, row in enumerate(rows[header_row + 1:], header_row + 2):
        vendor = value_at(row, cols['wild'])
        if not str(vendor).strip():
            continue
        try:
            vendor = base_vendor(vendor)
        except ValueError:
            continue
        record = {k: value_at(row, v) for k, v in cols.items()}
        record['row'] = n
        result[vendor].append(record)
    return result


def choose_dt(receipts, stock, as_of):
    if stock <= 0:
        raise ValueError('Free stock is zero; manual review required')
    arrived = []
    for receipt in receipts:
        raw = str(receipt['arrival']).strip()
        if not raw:
            continue  # Goods without an actual arrival date are not yet received.
        arrival = parse_date(raw)
        if arrival <= as_of:
            arrived.append({**receipt, 'arrival': arrival, 'quantity': integer(receipt['quantity'])})
    arrived.sort(key=lambda r: (r['arrival'], -r['row']), reverse=True)
    total, used = 0, []
    for receipt in arrived:
        if not receipt['quantity']:
            continue
        total += receipt['quantity']
        used.append({**receipt, 'arrival': receipt['arrival'].isoformat(), 'cumulative': total})
        if total >= stock:
            dt = str(receipt['dt']).strip()
            if not re.fullmatch(r'\d{8}/\d{6}/\d{5,7}', dt):
                raise ValueError(f'Missing or invalid DT at receipts row {receipt["row"]}')
            return dt, used
    raise ValueError(f'Arrived quantity {total} is below free stock {stock}')


def shipment_queue(rows, selected_date, as_of, cabinet=None, supply_id=None, include_checked=False):
    headers = rows[0]
    columns = {k: index(headers, h) for k, h in {
        'supply_id': 'Номер поставки', 'date': 'Дата отгрузки (склад)',
        'status': 'Статус отгрузки', 'wild': 'wild', 'nm_id': 'Артикул',
        'checked': 'Чек лист Декларация', 'free_stock': 'Свободный остаток',
        'quantity': 'Количество'}.items()}
    # The sheet contains two headers named "Название магазина"; the first is the cabinet.
    cabinets = [i for i, h in enumerate(headers) if normalize(h) == 'название магазина']
    if len(cabinets) != 2:
        raise ValueError('Expected cabinet and product title columns')
    queue = []
    for n, row in enumerate(rows[1:], 2):
        if normalize(value_at(row, columns['status'])) != 'одобрено':
            continue
        if not include_checked and normalize(value_at(row, columns['checked'])) == 'true':
            continue
        if cabinet is not None and normalize(value_at(row, cabinets[0])) != normalize(cabinet):
            continue
        if supply_id is not None and str(value_at(row, columns['supply_id'])).strip() != str(supply_id):
            continue
        scheduled = parse_date(value_at(row, columns['date']))
        record = {k: value_at(row, i) for k, i in columns.items()}
        record.update(row=n, date=scheduled.isoformat(), cabinet=value_at(row, cabinets[0]),
                      product=value_at(row, cabinets[1]))
        queue.append(record)
    if selected_date is None:
        if supply_id is not None:
            dates = {parse_date(r['date']) for r in queue}
            if len(dates) > 1:
                raise ValueError('Supply has more than one shipment date; specify --date')
            selected_date = next(iter(dates), None)
        else:
            selected_date = min((parse_date(r['date']) for r in queue if parse_date(r['date']) >= as_of), default=None)
    return selected_date, [r for r in queue if parse_date(r['date']) == selected_date]


def db_vendors(records):
    import psycopg
    required = ('PGHOST', 'PGUSER', 'PGPASSWORD', 'PGDATABASE')
    if not all(os.environ.get(name) for name in required):
        raise ValueError('Database requires secrets PGUSER, PGPASSWORD, PGDATABASE')
    # No DISTINCT: ambiguity must be visible, not hidden by an arbitrary selected row.
    sql = '''SELECT wsg.id, wsg.nm_id, wsg.vendor_code, wsg.created_at,
                    a.local_vendor_code, a.account
             FROM wb_supplies_goods wsg
             LEFT JOIN article a ON a.nm_id = wsg.nm_id
             WHERE wsg.id = ANY(%s)
             ORDER BY wsg.created_at DESC'''
    result = defaultdict(list)
    with psycopg.connect(host=os.environ['PGHOST'],
                         port=int(os.environ.get('PGPORT', '5432')),
                         user=os.environ['PGUSER'], password=os.environ['PGPASSWORD'],
                         dbname=os.environ['PGDATABASE'], connect_timeout=15,
                         sslmode=os.environ.get('PGSSLMODE', 'prefer'),
                         options='-c default_transaction_read_only=on -c statement_timeout=30000') as connection:
        for supply, nm, vendor, created, local, account in connection.execute(
                sql, ([integer(r['supply_id']) for r in records],)):
            result[(str(supply), str(nm))].append(dict(vendor=vendor, created=created,
                                                     local=local, account=account))
    return result


def prepare(records, receipts, as_of, database=None):
    for row in records:
        row.update(state='READY', reason='', vendor_code='', dt='', country='', receipts=[])
        try:
            vendor = base_vendor(row['wild'])
            stock = integer(row['free_stock'])
            integer(row['quantity'])
            if not str(row['cabinet']).strip():
                raise ValueError('Cabinet is empty')
            if database is not None:
                candidates = database.get((str(integer(row['supply_id'])), str(integer(row['nm_id']))), [])
                candidates = [c for c in candidates if normalize(c['account']) == normalize(row['cabinet'])]
                if not candidates:
                    raise ValueError('No database match for cabinet + supply + WB article')
                latest = max(c['created'] for c in candidates)
                candidates = [c for c in candidates if c['created'] == latest]
                variants = {(c['vendor'], c['local']) for c in candidates}
                if len(variants) != 1:
                    raise ValueError('Ambiguous database vendor mapping')
                exact, local = next(iter(variants))
                if base_vendor(exact) != vendor or base_vendor(local) != vendor:
                    raise ValueError('Database vendor does not match shipment plan')
                row['vendor_code'] = exact
            row['dt'], row['receipts'] = choose_dt(receipts.get(vendor, []), stock, as_of)
            row['country'] = '860' if 'казан' in normalize(row['product']) else '156'
        except (ValueError, TypeError) as exc:
            row.update(state='REVIEW', reason=str(exc))
    return records


def write_plan(records, selected, as_of, output):
    output.mkdir(parents=True, exist_ok=True)
    payload = dict(schema=1, shipment_date=selected.isoformat() if selected else None,
                   as_of=as_of.isoformat(), generated_at=datetime.now(ZoneInfo('Europe/Moscow')).isoformat(),
                   records=records)
    (output / 'plan.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    fields = ['row', 'date', 'cabinet', 'supply_id', 'wild', 'nm_id', 'vendor_code',
              'product', 'free_stock', 'quantity', 'dt', 'country', 'state', 'reason']
    titles = ['Строка плана', 'Дата отгрузки', 'Кабинет', 'Номер поставки', 'wild',
              'Артикул WB', 'Артикул продавца из БД', 'Товар', 'Свободный остаток',
              'Количество в поставке', 'Номер ДТ', 'Страна', 'Результат', 'Причина']
    with (output / 'plan.csv').open('w', encoding='utf-8-sig', newline='') as handle:
        writer = csv.writer(handle, delimiter=';')
        writer.writerow(titles + ['Расчёт по приходам: дата / количество / сумма / ДТ'])
        for record in records:
            values = [record.get(k, '') for k in fields]
            values[-2] = 'Рассчитано' if record['state'] == 'READY' else 'Нужна проверка'
            if values[-1] == 'Different DTs on the boundary arrival date; manual review required':
                values[-1] = 'На граничную дату есть несколько приходов с разными ДТ'
            trace = ' | '.join(f'{r["arrival"]}: {r["quantity"]} / {r["cumulative"]} / {r["dt"]}'
                               for r in record['receipts'])
            writer.writerow(values + [trace])


def fill_template(plan_path, template, supply_id, output):
    from openpyxl import load_workbook
    payload = json.loads(plan_path.read_text(encoding='utf-8'))
    selected = [r for r in payload['records'] if str(r['supply_id']) == supply_id]
    if not selected or any(r['state'] != 'READY' for r in selected):
        raise ValueError('Supply is missing from plan or requires manual review')
    if len({normalize(r['cabinet']) for r in selected}) != 1:
        raise ValueError('Supply number belongs to more than one cabinet')
    if output.resolve() == template.resolve() or output.exists():
        raise ValueError('Choose a new output file; originals are never overwritten')
    workbook = load_workbook(template)
    if len(workbook.worksheets) != 1:
        raise ValueError('Expected one template sheet')
    sheet = workbook.active
    headers = [c.value for c in sheet[1]]
    cols = {k: index(headers, h) + 1 for k, h in {
        'barcode': 'Баркод товара', 'dt': 'Номер ДТ', 'nm_id': 'Артикул',
        'vendor': 'Артикул продавца', 'quantity': 'Количество',
        'country': 'Код страны происхождения'}.items()}
    seen = set()
    updates = []
    for cells in sheet.iter_rows(min_row=2):
        if not any(c.value is not None for c in cells):
            continue
        def cell_value(key):
            return sheet.cell(cells[0].row, cols[key]).value
        nm = str(integer(cell_value('nm_id')))
        matches = [r for r in selected if str(integer(r['nm_id'])) == nm]
        if len(matches) != 1 or nm in seen:
            raise ValueError(f'Ambiguous or repeated WB article {nm}')
        row = matches[0]
        if base_vendor(cell_value('vendor')) != base_vendor(row['wild']):
            raise ValueError(f'Vendor mismatch for WB article {nm}')
        if row['vendor_code'] and str(cell_value('vendor')) != row['vendor_code']:
            raise ValueError(f'Database/template vendor mismatch for WB article {nm}')
        if integer(cell_value('quantity')) != integer(row['quantity']):
            raise ValueError(f'Template/plan quantity mismatch for WB article {nm}')
        seen.add(nm)
        updates.append((cells[0].row, row))
    if seen != {str(integer(r['nm_id'])) for r in selected}:
        raise ValueError('Template does not contain the full supply from the plan')
    for n, row in updates:
        sheet.cell(n, cols['dt'], row['dt']).number_format = '@'
        sheet.cell(n, cols['country'], row['country']).number_format = '@'
    output.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(output)
    print(f'Filled {len(updates)} goods rows: {output}')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    plan = commands.add_parser('plan', help='Read sheets and generate a DT preview')
    plan.add_argument('--date', type=parse_date, help='DD.MM.YYYY; default nearest unchecked date')
    plan.add_argument('--as-of', type=parse_date, default=datetime.now(ZoneInfo('Europe/Moscow')).date())
    plan.add_argument('--key', type=Path, default=KEY)
    plan.add_argument('--with-db', action='store_true')
    plan.add_argument('--output', type=Path)
    fill = commands.add_parser('fill', help='Fill an explicitly identified supply template')
    fill.add_argument('--plan', type=Path, required=True)
    fill.add_argument('--template', type=Path, required=True)
    fill.add_argument('--supply-id', required=True)
    fill.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'fill':
            fill_template(args.plan, args.template, args.supply_id, args.output)
            return
        sheets = Sheets(args.key)
        receipts = load_receipts(sheets.read(RECEIPTS))
        selected, records = shipment_queue(sheets.read(SHIPMENTS), args.date, args.as_of)
        database = db_vendors(records) if args.with_db and records else None
        prepare(records, receipts, args.as_of, database)
        output = args.output or PROJECT / 'runs' / datetime.now(ZoneInfo('Europe/Moscow')).strftime('%Y%m%d-%H%M%S-%f')
        write_plan(records, selected, args.as_of, output)
        print(f'Date: {selected}; supplies: {len(set(r["supply_id"] for r in records))}; '
              f'ready: {sum(r["state"] == "READY" for r in records)}; '
              f'review: {sum(r["state"] == "REVIEW" for r in records)}')
        print(f'Reports: {output}')
        if not args.with_db:
            print('Database mapping not checked. Exact vendor codes must come from original WB templates.')
    except Exception as exc:
        # Driver errors may echo connection parameters. Never print raw authentication errors.
        if isinstance(exc, (ValueError, FileNotFoundError)):
            parser.exit(1, f'{exc}\n')
        parser.exit(1, f'{type(exc).__name__}: operation failed; check access, network and configuration.\n')


if __name__ == '__main__':
    main()
