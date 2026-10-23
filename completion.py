"""User-confirmed checklist updates; never claims verification inside WB."""
import json
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4
from zoneinfo import ZoneInfo

from wb_declarations import PROJECT, KEY, SHIPMENTS, integer, normalize, index, value_at, parse_date

BATCHES = PROJECT / 'runs' / 'telegram_batches'


def key(record):
    return (normalize(record['cabinet']), str(integer(record['supply_id'])),
            str(integer(record['nm_id'])))


def sheet_records(rows):
    headers = rows[0]
    columns = {k: index(headers, h) for k, h in {
        'supply_id': 'Номер поставки', 'nm_id': 'Артикул', 'quantity': 'Количество',
        'date': 'Дата отгрузки (склад)', 'checked': 'Чек лист Декларация',
        'status': 'Статус отгрузки'}.items()}
    cabinets = [i for i, h in enumerate(headers) if normalize(h) == 'название магазина']
    if len(cabinets) != 2:
        raise ValueError('Не удалось определить столбец кабинета.')
    result = []
    for number, row in enumerate(rows[1:], 2):
        record = {k: value_at(row, col) for k, col in columns.items()}
        record.update(cabinet=value_at(row, cabinets[0]), row=number)
        if any(not str(record[k]).strip() for k in ('cabinet', 'supply_id', 'nm_id')):
            continue
        try:
            key(record)
        except ValueError:
            continue
        result.append(record)
    return result, columns['checked']


def save_batch(folder, owner_id, delivered_supplies=None):
    folder = Path(folder)
    sources = json.loads((folder/'sources.json').read_text(encoding='utf-8'))
    plan_path = Path(sources['plan']).resolve()
    if not plan_path.is_relative_to((PROJECT/'runs').resolve()):
        raise ValueError('Unexpected plan')
    plan = json.loads(plan_path.read_text(encoding='utf-8'))
    records = []
    selected = set(delivered_supplies) if delivered_supplies is not None else None
    for record in plan['records']:
        if selected is not None and (record['cabinet'], str(record['supply_id'])) not in selected:
            continue
        if record['cabinet']+'/'+str(record['supply_id']) in sources['identities']:
            records.append({k: record[k] for k in ('cabinet', 'supply_id', 'nm_id', 'quantity', 'date')})
    if not records:
        raise ValueError('Нет выданных файлов для подтверждения.')
    identifier = uuid4().hex[:12]
    BATCHES.mkdir(parents=True, exist_ok=True)
    payload = dict(id=identifier, owner_id=owner_id, shipment_date=plan['shipment_date'],
                   created_at=datetime.now(ZoneInfo('Europe/Moscow')).isoformat(), records=records)
    if selected is not None:
        payload['scope_supplies'] = [list(item) for item in sorted(selected)]
    (BATCHES/(identifier+'.json')).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding='utf-8')
    return payload


def load_batch(identifier, owner_id):
    if not re.fullmatch(r'[a-f0-9]{12}', identifier):
        raise ValueError('Некорректная выгрузка.')
    try:
        payload = json.loads((BATCHES/(identifier+'.json')).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        raise ValueError('Выгрузка не найдена. Запросите файлы заново.') from None
    if payload['owner_id'] != owner_id:
        raise ValueError('Доступ закрыт.')
    return payload


def select_changes(expected, live):
    mapping = defaultdict(list)
    for record in live:
        mapping[key(record)].append(record)
    writes, already, blocked = [], 0, []
    for original in expected:
        candidates = mapping.get(key(original), [])
        reason = ''
        if len(candidates) != 1:
            reason = 'строка отсутствует или дублируется'
        else:
            current = candidates[0]
            try:
                if (parse_date(current['date']) != parse_date(original['date'])
                        or integer(current['quantity']) != integer(original['quantity'])):
                    reason = 'дата или количество изменились после выдачи файла'
                elif normalize(current['checked']) == 'true':
                    already += 1
                    continue
                elif normalize(current['checked']) not in ('false', ''):
                    reason = 'ячейка отметки содержит неожиданное значение'
                else:
                    writes.append(current)
            except ValueError:
                reason = 'дата или количество некорректны'
        if reason:
            blocked.append(f'{original["cabinet"]} / {original["supply_id"]}: {reason}')
    return writes, already, blocked


class Checklist:
    def __init__(self):
        from google.oauth2.service_account import Credentials
        from google.auth.transport.requests import AuthorizedSession
        credentials = Credentials.from_service_account_file(str(KEY),
            scopes=['https://www.googleapis.com/auth/spreadsheets'])
        self.session = AuthorizedSession(credentials)
        self.base = f'https://sheets.googleapis.com/v4/spreadsheets/{SHIPMENTS[0]}'

    def request(self, method, url, **kwargs):
        response = self.session.request(method, url, timeout=45, **kwargs)
        if response.status_code == 403:
            raise ValueError('Нужны права «Редактор» для сервисного аккаунта в таблице отгрузок.')
        if response.status_code != 200:
            raise ValueError(f'Google Sheets: HTTP {response.status_code}. Результат записи нужно перепроверить.')
        return response.json()

    def read(self):
        metadata = self.request('GET', self.base,
            params={'fields': 'sheets.properties(sheetId,title)'})
        properties = next((s['properties'] for s in metadata['sheets']
                           if s['properties']['sheetId'] == SHIPMENTS[1]), None)
        if not properties:
            raise ValueError('Лист отгрузок не найден.')
        title = properties['title']
        rows = self.request('GET', self.base+'/values/'+quote("'"+title.replace("'", "''")+"'", safe='')).get('values', [])
        records, checkbox = sheet_records(rows)
        return records, checkbox, title

    def mark(self, expected):
        live, checkbox, title = self.read()
        writes, already, blocked = select_changes(expected, live)
        if writes:
            # Only selected checkbox cells are changed; never overwrite a column.
            from openpyxl.utils import get_column_letter
            col = get_column_letter(checkbox+1)
            prefix = "'"+title.replace("'", "''")+"'!"
            data = [{'range': prefix+col+str(record['row']), 'values': [[True]]} for record in writes]
            self.request('POST', self.base+'/values:batchUpdate',
                         json={'valueInputOption': 'RAW', 'data': data})
        # Verify by business keys rather than assuming the saved row numbers are still valid.
        after, _, _ = self.read()
        pending, checked, changes = select_changes(expected, after)
        blocked = list(dict.fromkeys(blocked+changes))
        if pending:
            blocked.append('Часть отметок не подтвердилась при повторном чтении. Повторите проверку.')
        return dict(updated=len(writes), checked=checked, blocked=blocked)

    def status(self, expected=None, selected_date=None, scope_supplies=None):
        records, _, _ = self.read()
        if expected is not None:
            pending, checked, blocked = select_changes(expected, records)
            new = []
            known = {key(r) for r in expected}
            scope = {(normalize(c), str(integer(s))) for c, s in scope_supplies} if scope_supplies is not None else None
            for record in records:
                try:
                    if (parse_date(record['date']) == parse_date(selected_date)
                            and normalize(record['status']) == 'одобрено' and key(record) not in known
                            and (scope is None or key(record)[:2] in scope)):
                        new.append(record)
                except ValueError:
                    continue
        else:
            selected = []
            for record in records:
                try:
                    if (parse_date(record['date']) == parse_date(selected_date)
                            and normalize(record['status']) == 'одобрено'):
                        selected.append(record)
                except ValueError:
                    continue
            pending, checked, blocked = select_changes(selected, records)
            expected, new = selected, []
        return dict(total=len(expected), checked=checked,
                    pending=[f'{r["cabinet"]} / {r["supply_id"]}' for r in pending],
                    blocked=blocked, new=[f'{r["cabinet"]} / {r["supply_id"]}' for r in new])
