"""Persistent prepared XLSX cache. Network access occurs only in refresh/prune."""
import asyncio
import csv
import json
import shutil
import sqlite3
import tempfile
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from wb_declarations import (PROJECT, KEY, SHIPMENTS, RECEIPTS, BASE_TEMPLATE, Sheets, parse_date,
    normalize, integer, index, value_at, shipment_queue, load_receipts, prepare, write_plan)

REPORT = 'СПИСОК_ФАЙЛОВ.csv'
FIELDS = ['Кабинет', 'Номер поставки', 'wild', 'Результат', 'Файл', 'Причина', 'Источник артикула и баркода']
DELIVERIES = PROJECT / 'runs' / 'deliveries'


def now():
    return datetime.now(ZoneInfo('Europe/Moscow'))


def group_key(record):
    return normalize(record['cabinet']), str(integer(record['supply_id']))


def all_records(rows):
    """All approved shipment dates, including overdue and already checked rows."""
    col = index(rows[0], 'Дата отгрузки (склад)')
    dates = set()
    status = index(rows[0], 'Статус отгрузки')
    for row in rows[1:]:
        if normalize(value_at(row, status)) == 'одобрено':
            dates.add(parse_date(value_at(row, col)))
    result = []
    for date in sorted(dates):
        _, records = shipment_queue(rows, date, now().date(), include_checked=True)
        result.extend(records)
    return result


def signature(records):
    """Delivery identity, independent of volatile stock and checkbox formatting.

    Stock is an input of the scheduled snapshot, not a reason to evict that
    snapshot between refreshes. A title change matters only if country changes.
    Pending/checked state is checked separately by pending_groups.
    """
    def number(value):
        try:
            return str(integer(value))
        except ValueError:
            return normalize(value)
    return sorted((number(r['nm_id']), parse_date(r['date']).isoformat(),
                   number(r['quantity']), normalize(r['wild']),
                   '860' if 'казан' in normalize(r['product']) else '156') for r in records)


def pending_groups(records):
    groups = defaultdict(list)
    for record in records:
        groups[group_key(record)].append(record)
    return {key: goods for key, goods in groups.items()
            if all(normalize(r['checked']) in ('', 'false') for r in goods)
            and len({r['date'] for r in goods}) == 1}


class CacheStore:
    def __init__(self, path=None):
        self.path = Path(path) if path else PROJECT / 'data' / 'declarations.sqlite3'

    @contextmanager
    def connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute('PRAGMA journal_mode=WAL')
        connection.execute('PRAGMA secure_delete=ON')
        connection.executescript('''
            CREATE TABLE IF NOT EXISTS files (
                cabinet_key TEXT NOT NULL, supply TEXT NOT NULL, cabinet TEXT NOT NULL,
                shipment_date TEXT NOT NULL, records TEXT NOT NULL, identities TEXT NOT NULL,
                workbook BLOB, reason TEXT NOT NULL,
                PRIMARY KEY(cabinet_key, supply));
            CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL);
        ''')
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def publish(self, entries, navigation):
        # A failed source read/build never reaches this atomic replacement.
        with self.connect() as db:
            db.execute('DELETE FROM files')
            db.executemany('INSERT INTO files VALUES (?,?,?,?,?,?,?,?)', entries)
            for key, value in [('updated_at', now().isoformat()), ('last_error', ''),
                               ('status_dates', json.dumps(sorted({r['date'] for r in navigation})))]:
                db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', (key, value))

    def info(self):
        with self.connect() as db:
            meta = dict(db.execute('SELECT name,value FROM metadata').fetchall())
            meta['files'] = db.execute('SELECT count(*) FROM files WHERE workbook IS NOT NULL').fetchone()[0]
            meta['problems'] = db.execute('SELECT count(*) FROM files WHERE workbook IS NULL').fetchone()[0]
            return meta

    def navigation(self):
        with self.connect() as db:
            records = [r for row in db.execute('SELECT records FROM files') for r in json.loads(row[0])]
            # Keep dates available for checklist audits after the last file is removed.
            dates = db.execute("SELECT value FROM metadata WHERE name='status_dates'").fetchone()
            for date in json.loads(dates[0]) if dates else []:
                records.append(dict(date=date, cabinet='', navigation_only=True))
            return records

    def evict_confirmed(self, expected):
        """Called only after successful Sheets re-read; retain newer/partial snapshots."""
        groups = defaultdict(list)
        for record in expected:
            groups[group_key(record)].append(record)
        def confirmed_signature(goods):
            return sorted((str(integer(r['nm_id'])), parse_date(r['date']).isoformat(),
                           integer(r['quantity'])) for r in goods)
        with self.connect() as db:
            for key, goods in groups.items():
                row = db.execute('SELECT records FROM files WHERE cabinet_key=? AND supply=?', key).fetchone()
                if row and confirmed_signature(json.loads(row[0])) == confirmed_signature(goods):
                    db.execute('DELETE FROM files WHERE cabinet_key=? AND supply=?', key)

    def prune(self, live):
        groups = pending_groups(live)
        with self.connect() as db:
            removed = 0
            for row in db.execute('SELECT cabinet_key,supply,records FROM files').fetchall():
                key = row['cabinet_key'], row['supply']
                if key not in groups:
                    db.execute('DELETE FROM files WHERE cabinet_key=? AND supply=?', key)
                    removed += 1
                elif signature(json.loads(row['records'])) != signature(groups[key]):
                    # Keep the supply discoverable and explain the blocked file.
                    db.execute('UPDATE files SET workbook=NULL, reason=? WHERE cabinet_key=? AND supply=?',
                        ('В плане изменились состав, дата, количество или страна товара. '
                         'Нажмите «Обновить базу».', *key))
            db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('pruned_at', now().isoformat()))
            db.execute('PRAGMA incremental_vacuum')
        return removed

    def materialize(self, selected_date=None, cabinet=None, supply_id=None, root=None):
        with self.connect() as db:
            rows = list(db.execute('SELECT * FROM files ORDER BY cabinet, supply'))
            updated = db.execute("SELECT value FROM metadata WHERE name='updated_at'").fetchone()
        if cabinet is not None:
            rows = [r for r in rows if r['cabinet_key'] == normalize(cabinet)]
        if supply_id is not None:
            rows = [r for r in rows if r['supply'] == str(integer(supply_id))]
            if not cabinet and len({r['cabinet_key'] for r in rows}) > 1:
                raise RuntimeError('Этот номер есть в нескольких кабинетах. Выберите кабинет кнопкой.')
        dates = {parse_date(r['shipment_date']) for r in rows}
        if selected_date is None:
            selected_date = (min(dates) if supply_id and dates else
                             min((d for d in dates if d >= now().date()), default=None))
        rows = [r for r in rows if parse_date(r['shipment_date']) == selected_date]
        if not rows:
            raise RuntimeError('В локальной базе нет этой поставки или даты. Возможно, галочки уже '
                'проставлены либо поставка ещё не обновилась. Нажмите «Обновить базу».')
        from export_templates import safe_name
        folder = (Path(root) if root else DELIVERIES) / uuid4().hex
        folder.mkdir(parents=True)
        records, identities, report = [], {}, []
        for row in rows:
            goods = json.loads(row['records'])
            records.extend(goods)
            filename = ''
            if row['workbook'] is not None:
                filename = str(Path(safe_name(row['cabinet'])) / f'{row["supply"]}.xlsx')
                path = folder / filename
                path.parent.mkdir(exist_ok=True)
                path.write_bytes(row['workbook'])
                identities[row['cabinet']+'/'+row['supply']] = json.loads(row['identities'])
            report.append([row['cabinet'], row['supply'], ', '.join(r['wild'] for r in goods),
                           'ГОТОВО' if filename else 'НЕ СОЗДАН', filename, row['reason'], 'DATABASE'])
        with (folder / REPORT).open('w', encoding='utf-8-sig', newline='') as handle:
            writer = csv.writer(handle, delimiter=';')
            writer.writerow(FIELDS)
            writer.writerows(report)
        (folder / 'plan.json').write_text(json.dumps(dict(shipment_date=selected_date.isoformat(),
            records=records), ensure_ascii=False), encoding='utf-8')
        (folder / 'sources.json').write_text(json.dumps(dict(plan=str(folder/'plan.json'),
            identities=identities, generated_at=updated[0] if updated else None), ensure_ascii=False), encoding='utf-8')
        created = sum(r['workbook'] is not None for r in rows)
        counts = dict(total=len(rows), created=created, missing=len(rows)-created,
                      cabinets=len({r['cabinet_key'] for r in rows if r['workbook'] is not None}),
                      cache_updated_at=updated[0] if updated else None)
        return folder, counts, selected_date.isoformat()


def cleanup_delivery(folder):
    """Delete only our disposable UUID delivery directories, never old exports."""
    if folder is None:
        return
    folder = Path(folder)
    if (not folder.is_symlink() and folder.resolve().parent == DELIVERIES.resolve()
            and len(folder.name) == 32 and all(c in '0123456789abcdef' for c in folder.name)):
        shutil.rmtree(folder)


class CacheService:
    def __init__(self, store=None):
        self.store = store or CacheStore()
        self.lock = asyncio.Lock()

    def refresh_sync(self):
        from export_templates import export
        sheets = Sheets(KEY)
        live = all_records(sheets.read(SHIPMENTS))
        groups = pending_groups(live)
        records = [r for goods in groups.values() for r in goods]
        prepare(records, load_receipts(sheets.read(RECEIPTS)), now().date())
        with tempfile.TemporaryDirectory(prefix='wb-cache-') as temp:
            temp = Path(temp)
            write_plan(records, None, now().date(), temp/'plan')
            if records:
                export(temp/'plan'/'plan.json', Path.home()/'Downloads', temp/'files', database=True, base_template=BASE_TEMPLATE)
                sources = json.loads((temp/'files'/'sources.json').read_text(encoding='utf-8'))
                with (temp/'files'/REPORT).open(encoding='utf-8-sig', newline='') as handle:
                    reports = list(csv.DictReader(handle, delimiter=';'))
            else:
                sources, reports = {'identities': {}}, []
            # Do not resurrect a checkbox or changed goods during a slow build.
            latest = all_records(sheets.read(SHIPMENTS))
            current = pending_groups(latest)
            entries = []
            for report in reports:
                key = normalize(report['Кабинет']), str(integer(report['Номер поставки']))
                goods = groups[key]
                if key not in current or signature(goods) != signature(current[key]):
                    continue
                success = report['Результат'] == 'ГОТОВО'
                blob = (temp/'files'/report['Файл']).read_bytes() if success else None
                ids = sources['identities'].get(report['Кабинет']+'/'+key[1], [])
                entries.append((*key, report['Кабинет'], goods[0]['date'],
                    json.dumps(goods, ensure_ascii=False), json.dumps(ids, ensure_ascii=False),
                    blob, report['Причина']))
            self.store.publish(entries, latest)
        return self.store.info()

    async def refresh(self):
        async with self.lock:
            try:
                return await asyncio.to_thread(self.refresh_sync)
            except Exception as exc:
                with self.store.connect() as db:
                    db.execute('INSERT OR REPLACE INTO metadata VALUES (?,?)', ('last_error', type(exc).__name__))
                raise RuntimeError('Не удалось обновить базу. Предыдущие файлы сохранены; проверьте доступ к источникам.') from None

    async def prune(self):
        async with self.lock:
            def operation():
                return self.store.prune(all_records(Sheets(KEY).read(SHIPMENTS)))
            return await asyncio.to_thread(operation)

    async def evict_confirmed(self, expected):
        async with self.lock:
            await asyncio.to_thread(self.store.evict_confirmed, expected)

    async def background(self):
        """Refresh every six hours; sweep manually changed checkboxes every five minutes."""
        due = 0
        while True:
            try:
                if asyncio.get_running_loop().time() >= due:
                    await self.refresh()
                    due = asyncio.get_running_loop().time() + 6*60*60
                else:
                    await self.prune()
            except Exception as exc:
                print(f'Cache sync failed: {type(exc).__name__}', flush=True)
            await asyncio.sleep(300)
