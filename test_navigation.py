import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

from aiogram.enums import ChatType
import telegram_bot as app


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    def setup_flow(self):
        dispatcher = app.create_dispatcher()
        handler = next(h.callback for h in dispatcher.message.handlers if h.callback.__name__ == 'text_message')
        selected = datetime.now(ZoneInfo('Europe/Moscow')).date()+timedelta(days=3)
        records = [dict(cabinet='МагазинА', supply_id='123', date=selected.isoformat(),
                        status='Одобрено', nm_id='456', quantity='10', checked='FALSE')]
        message = SimpleNamespace(from_user=SimpleNamespace(id=123), chat=SimpleNamespace(type=ChatType.PRIVATE),
            text='', answer=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())))
        return handler, selected, records, message

    async def test_cabinet_and_date_are_chosen_using_buttons_only(self):
        handler, selected, records, message = self.setup_flow()
        with patch.object(app, 'setting', return_value='123'), \
             patch.object(app, 'navigation_records', return_value=records), \
             patch.object(app, 'generate_files', new_callable=AsyncMock,
                          return_value=(Path('unused'), dict(total=0), None)) as generate:
            for text in ('Файлы по кабинету', 'Кабинет: МагазинА', 'Дата: '+selected.strftime('%d.%m.%Y')):
                message.text = text
                await handler(message)
            generate.assert_awaited_once_with(selected, cabinet='МагазинА', supply_id=None)

    async def test_supply_is_chosen_using_buttons_only(self):
        handler, selected, records, message = self.setup_flow()
        with patch.object(app, 'setting', return_value='123'), \
             patch.object(app, 'navigation_records', return_value=records), \
             patch.object(app, 'generate_files', new_callable=AsyncMock,
                          return_value=(Path('unused'), dict(total=0), None)) as generate:
            for text in ('По номеру поставки', 'Кабинет: МагазинА',
                         'Дата: '+selected.strftime('%d.%m.%Y'), 'Поставка: 123'):
                message.text = text
                await handler(message)
            generate.assert_awaited_once_with(selected, cabinet='МагазинА', supply_id='123')

    async def test_checklist_status_is_available_by_date_button(self):
        handler, selected, records, message = self.setup_flow()
        with patch.object(app, 'setting', return_value='123'), \
             patch.object(app, 'navigation_records', return_value=records), \
             patch.object(app, 'Checklist') as client:
            client.return_value.status.return_value = dict(total=1, checked=0, pending=['A / 123'], blocked=[], new=[])
            for text in ('Проверить отметки', 'Дата: '+selected.strftime('%d.%m.%Y')):
                message.text = text
                await handler(message)
            client.return_value.status.assert_called_once_with(selected_date=selected)
            client.return_value.mark.assert_not_called()

    async def test_unauthorized_menu_does_not_read_table(self):
        handler, selected, records, message = self.setup_flow()
        message.from_user.id = 456
        message.text = 'Файлы по кабинету'
        with patch.object(app, 'setting', return_value='123'), patch.object(app, 'navigation_records') as read:
            await handler(message)
            read.assert_not_called()


if __name__ == '__main__':
    unittest.main()
