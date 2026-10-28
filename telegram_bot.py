"""Owner-only Telegram delivery of cached WB declaration workbooks."""
import asyncio
import csv
import json
import logging
import os
import re
import sys
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from uuid import uuid4
from zoneinfo import ZoneInfo

from aiogram import Bot, Dispatcher, F
from aiogram.enums import ChatType
from aiogram.filters import Command, CommandStart
from aiogram.types import (BotCommand, FSInputFile, KeyboardButton, Message, ReplyKeyboardMarkup,
                           CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup)
from dotenv import dotenv_values

from wb_declarations import PROJECT, KEY, SHIPMENTS, Sheets, parse_date, normalize
from completion import save_batch, load_batch, Checklist, BATCHES
from file_cache import CacheService, cleanup_delivery
from planner_ui import PlannerController, ENTRY as PLANNER_ENTRY

_cache = None

def cache_service():
    global _cache
    if _cache is None:
        _cache = CacheService()
    return _cache


ENV = PROJECT / '.env'
MAX_UPLOAD = 45 * 1024 * 1024
REPORT_NAME = 'СПИСОК_ФАЙЛОВ.csv'


def setting(name):
    values = dotenv_values(ENV, interpolate=False)
    # The owner setting is edited locally; prefer the current file over the
    # startup copy loaded into os.environ, including blank values for revocation.
    if name == 'TELEGRAM_ALLOWED_USER_ID' and name in values:
        return (values[name] or '').strip()
    return (os.environ.get(name) or values.get(name) or '').strip()


def is_owner(user_id, chat_type):
    configured = setting('TELEGRAM_ALLOWED_USER_ID')
    return (chat_type == ChatType.PRIVATE and configured.isdigit()
            and int(configured) == user_id)


def requested_date(text):
    value = text.strip()
    if not re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', value):
        raise ValueError('Напишите дату в формате ДД.ММ.ГГГГ, например 25.10.2026.')
    return parse_date(value)


def report_counts(folder):
    with (folder / REPORT_NAME).open(encoding='utf-8-sig', newline='') as handle:
        records = list(csv.DictReader(handle, delimiter=';'))
    files = list(folder.glob('*/*.xlsx'))
    good = [r for r in records if r['Результат'] == 'ГОТОВО']
    if len(files) != len(good):
        raise ValueError('Число Excel не совпадает с отчётом; отправка остановлена.')
    return dict(total=len(records), created=len(files), missing=len(records) - len(files),
                cabinets=len({r['Кабинет'] for r in good}))


def build_archives(folder):
    """Allowlist: goods workbooks and user reports only, never settings or sources.json."""
    folder = folder.resolve()
    items = sorted(folder.glob('*/*.xlsx'))
    items += [p for p in (folder / REPORT_NAME, folder / 'ПРОЧИТАЙТЕ.txt',
                           folder / 'НЕДОСТАЮЩИЕ_ДАННЫЕ.csv') if p.is_file()]
    for item in items:
        if item.is_symlink() or not item.resolve().is_relative_to(folder):
            raise ValueError('Unexpected file in export')
    if any(p.stat().st_size >= MAX_UPLOAD for p in items):
        raise ValueError('Один из файлов превышает лимит отправки.')
    # Bound by uncompressed bytes so each compressed part is below Telegram's limit.
    batches, current, size = [], [], 0
    for item in items:
        item_size = item.stat().st_size + 4096
        if current and size + item_size > MAX_UPLOAD:
            batches.append(current)
            current, size = [], 0
        current.append(item)
        size += item_size
    if current:
        batches.append(current)
    archives = []
    for number, batch in enumerate(batches, 1):
        suffix = f'-часть-{number}' if len(batches) > 1 else ''
        path = folder / f'Декларации{suffix}.zip'
        with zipfile.ZipFile(path, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            for item in batch:
                archive.write(item, item.relative_to(folder).as_posix())
        if path.stat().st_size >= MAX_UPLOAD:
            raise ValueError('Архив превышает лимит отправки.')
        archives.append(path)
    return archives


async def generate_files(selected_date=None, cabinet=None, supply_id=None):
    # User requests never query PostgreSQL or Google Sheets.
    return await asyncio.to_thread(cache_service().store.materialize,
                                   selected_date, cabinet, supply_id)


def keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text='Новый план'), KeyboardButton(text='Мои планы')],
        [KeyboardButton(text='Декларации')]], resize_keyboard=True)


def declarations_keyboard():
    return ReplyKeyboardMarkup(keyboard=[
        [KeyboardButton(text='Ближайшая отгрузка')],
        [KeyboardButton(text='Обновить базу'), KeyboardButton(text='База файлов')],
        [KeyboardButton(text='Сегодня'), KeyboardButton(text='Завтра')],
        [KeyboardButton(text='Выбрать дату')],
        [KeyboardButton(text='Проверить отметки')],
        [KeyboardButton(text='Файлы по кабинету'), KeyboardButton(text='По номеру поставки')],
        [KeyboardButton(text='Главное меню')]], resize_keyboard=True)


def navigation_records():
    return cache_service().store.navigation()


def parse_cabinet_request(text):
    parts = text.strip().split()
    selected = None
    if parts and re.fullmatch(r'\d{2}\.\d{2}\.\d{4}', parts[-1]):
        selected = requested_date(parts.pop())
    cabinet = ' '.join(parts)
    if not cabinet or len(cabinet) > 150:
        raise ValueError('Пример: /cabinet МагазинА 25.10.2026')
    return cabinet, selected


def cabinet_names():
    return sorted({r['cabinet'] for r in navigation_records()}, key=normalize)


def direct_documents(folder):
    root = folder.resolve()
    with (folder/REPORT_NAME).open(encoding='utf-8-sig', newline='') as handle:
        rows = list(csv.DictReader(handle, delimiter=';'))
    documents, problems = [], []
    for row in rows:
        if row['Результат'] != 'ГОТОВО':
            problems.append(f'{row["Кабинет"]} / {row["Номер поставки"]}: {row["Причина"]}')
            continue
        path = folder/row['Файл']
        if (path.is_symlink() or not path.resolve().is_relative_to(root)
                or path.suffix.lower() != '.xlsx' or not path.is_file()):
            raise ValueError('Некорректный путь к Excel в отчёте.')
        documents.append((row['Кабинет'], str(row['Номер поставки']), path))
    return documents, problems


def batch_keyboard(batch):
    identifier = batch['id']
    supplies = len({(r['cabinet'], str(r['supply_id'])) for r in batch['records']})
    buttons = [[InlineKeyboardButton(text='Готово' if supplies == 1 else f'Готово — все {supplies}',
                                    callback_data=f'done:{identifier}')]]
    if supplies > 1:
        buttons.append([InlineKeyboardButton(text='Отметить отдельные поставки', callback_data=f'list:{identifier}')])
    buttons.append([InlineKeyboardButton(text='Проверить отметки', callback_data=f'audit:{identifier}')])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def supply_groups(batch):
    groups = {}
    for record in batch['records']:
        groups.setdefault((record['cabinet'], str(record['supply_id'])), []).append(record)
    return list(groups.items())


def audit_text(result):
    text = (f'В таблице отмечено строк: {result["checked"]} из {result["total"]}.\n'
            f'Без галочки: {len(result["pending"])}. Изменились/не найдены: {len(result["blocked"])}.\n'
            f'Новых строк на дату вне выгрузки: {len(result["new"])}.\n\n'
            'Это сверка отметок в таблице, а не проверка ДТ на стороне WB.')
    for label, lines in [('Без отметки', result['pending']), ('Нужна проверка', result['blocked']),
                         ('Добавлены после выгрузки', result['new'])]:
        if lines:
            text += '\n\n'+label+':\n'+'\n'.join(lines)
    return text


async def send_chunks(message, text):
    # Telegram limits message length; split on lines without dropping the tail.
    part = ''
    for line in text.splitlines():
        if len(part)+len(line)+1 > 3500:
            await message.answer(part)
            part = ''
        part += line+'\n'
    if part:
        await message.answer(part)


def create_dispatcher():
    dispatcher = Dispatcher()
    delivery_slots = asyncio.Semaphore(3)
    checklist_lock = asyncio.Lock()
    navigation = {}
    planner_ui = PlannerController()  # lazy: no storage, memory or Google access until the first planner action

    async def show_navigation(message, mode, records, stage, cabinet=None, selected=None, page=0):
        filtered = [r for r in records if (mode == 'status' or not r.get('navigation_only'))
                    and (cabinet is None or normalize(r['cabinet']) == normalize(cabinet))]
        actions = {}
        if stage == 'cabinets':
            items = [('Кабинет: '+name, ('cabinet', name))
                     for name in sorted({r['cabinet'] for r in filtered}, key=normalize)]
            prompt = 'Выберите кабинет кнопкой.'
        elif stage == 'dates':
            dates = sorted({parse_date(r['date']) for r in filtered})
            items = [('Дата: '+d.strftime('%d.%m.%Y'), ('date', d)) for d in dates]
            prompt = 'Выберите дату отгрузки кнопкой.'
        else:
            choices = sorted({str(r['supply_id']) for r in filtered if parse_date(r['date']) == selected})
            items = [('Поставка: '+s, ('supply', s)) for s in choices]
            prompt = 'Выберите номер поставки кнопкой — пришлю Excel.'
        if not items:
            navigation.pop(message.from_user.id, None)
            await message.answer('Подходящих одобренных отгрузок нет. Выберите другой способ.',
                                 reply_markup=declarations_keyboard())
            return
        page = max(0, min(page, (len(items)-1)//20))
        buttons = []
        for label, action in items[page*20:(page+1)*20]:
            buttons.append([KeyboardButton(text=label)])
            actions[label] = action
        if page:
            buttons.append([KeyboardButton(text='Предыдущие')])
            actions['Предыдущие'] = ('page', page-1)
        if (page+1)*20 < len(items):
            buttons.append([KeyboardButton(text='Следующие')])
            actions['Следующие'] = ('page', page+1)
        if stage != 'cabinets' and mode not in ('archive', 'status'):
            buttons.append([KeyboardButton(text='Назад')])
            actions['Назад'] = ('back', 'cabinets' if stage == 'dates' else 'dates')
        if mode == 'supply':
            buttons.append([KeyboardButton(text='Ввести номер')])
            actions['Ввести номер'] = ('manual', None)
        buttons.append([KeyboardButton(text='Главное меню')])
        navigation[message.from_user.id] = dict(mode=mode, records=records, stage=stage,
            cabinet=cabinet, selected=selected, actions=actions)
        await message.answer(prompt, reply_markup=ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True))

    async def navigate(message, action, state):
        kind, value = action
        mode, records = state['mode'], state['records']
        cabinet, selected = state['cabinet'], state['selected']
        if kind == 'manual':
            navigation.pop(message.from_user.id, None)
            await message.answer('Напишите только номер поставки, без команды.', reply_markup=declarations_keyboard())
        elif kind == 'page':
            await show_navigation(message, mode, records, state['stage'], cabinet, selected, value)
        elif kind == 'back':
            await show_navigation(message, mode, records, value,
                                  cabinet if value == 'dates' else None, selected)
        elif kind == 'cabinet':
            await show_navigation(message, mode, records, 'dates', value)
        elif kind == 'date':
            if mode == 'supply':
                await show_navigation(message, mode, records, 'supplies', cabinet, value)
            else:
                navigation.pop(message.from_user.id, None)
                if mode == 'status':
                    try:
                        result = await asyncio.to_thread(lambda: Checklist().status(selected_date=value))
                        await send_chunks(message, audit_text(result))
                    except Exception:
                        await message.answer('Не удалось проверить отметки. Повторите позже.')
                else:
                    await deliver(message, value, cabinet=cabinet)
                await message.answer('Выберите следующее действие.', reply_markup=declarations_keyboard())
        elif kind == 'supply':
            navigation.pop(message.from_user.id, None)
            await deliver(message, selected, cabinet=cabinet, supply_id=value)
            await message.answer('Выберите следующее действие.', reply_markup=declarations_keyboard())

    async def authorized(message):
        if not message.from_user or not is_owner(message.from_user.id, message.chat.type):
            await message.answer('Доступ к рабочим файлам закрыт. Команда /myid покажет ваш Telegram ID.')
            return False
        return True

    @dispatcher.message(Command('myid'))
    async def myid(message: Message):
        if message.chat.type == ChatType.PRIVATE and message.from_user:
            await message.answer(f'Ваш Telegram ID: {message.from_user.id}\n'
                                 'Укажите его в TELEGRAM_ALLOWED_USER_ID в .env проекта.')

    @dispatcher.message(CommandStart())
    @dispatcher.message(Command('help'))
    async def start(message: Message):
        if not await authorized(message):
            return
        await message.answer(
            'Главное меню: «Новый план» (тестовое планирование отгрузок ФБО), «Мои планы» и «Декларации».\n\n'
            'Пришлите просто номер поставки — например, 12345678. Выдам готовый Excel из локальной базы.\n'
            'База обновляется каждые 6 часов. «Обновить базу» запускает обновление вручную.\n'
            'После галочек файл удаляется из базы.\n\n'
            'Все действия доступны кнопками внизу чата. Если клавиатура скрыта, '
            'нажмите значок клавиатуры возле поля сообщения.\n\n'
            '«Файлы по кабинету»: кабинет → дата → Excel.\n'
            '«По номеру поставки»: кабинет → дата → номер поставки → Excel.\n'
            '«Выбрать дату»: дата → общий архив.\n'
            '«Проверить отметки»: дата → список незавершённых.\n\n'
            'Ручные команды также сохранены:\n'
            'Можно командой: /files 25.10.2026\n\n'
            'Отдельный Excel: /supply 12345678\n'
            'Файлы кабинета: /cabinet МагазинА 25.10.2026\n'
            'Для этих запросов отправляю Excel прямо в чат, с кнопкой «Готово» у каждого.\n\n'
            'В ответ получите ZIP: папка кабинета → файл с номером поставки. '
            'Распакуйте его и загрузите Excel в соответствующую поставку WB.\n'
            'После загрузки файлов нажмите «Готово» под архивом — отмечу поставки в таблице. '
            '«Проверить отметки» покажет незавершённые. /status 25.10.2026 — сверка даты. '
            'На стороне WB сохранение ДТ пока не проверяется.', reply_markup=keyboard())

    async def deliver(message, selected=None, cabinet=None, supply_id=None):
        if not await authorized(message):
            return
        if delivery_slots.locked():
            await message.answer('Запрос принят в очередь. Пришлю файлы автоматически, повторять номер не нужно.')
        async with delivery_slots:
            folder = None
            progress = await message.answer('Достаю готовые файлы из локальной базы…')
            try:
                direct = cabinet is not None or supply_id is not None
                if direct:
                    folder, counts, scheduled = await generate_files(selected, cabinet=cabinet, supply_id=supply_id)
                else:
                    folder, counts, scheduled = await generate_files(selected)
                if not counts['total']:
                    await progress.edit_text('Подходящих одобренных поставок не найдено. '
                        'Проверьте номер, название кабинета или дату. Для общего архива учитываются '
                        'только строки без галочки «Декларация».')
                    return
                if not is_owner(message.from_user.id, message.chat.type):
                    return
                human_date = datetime.strptime(scheduled, '%Y-%m-%d').strftime('%d.%m.%Y')
                await progress.edit_text(
                    f'Отгрузка {human_date}\n'
                    + (f'База обновлена: {counts["cache_updated_at"][:16].replace("T", " ")} МСК\n' if counts.get('cache_updated_at') else '') +
                    f'Готово Excel: {counts["created"]} из {counts["total"]}\n'
                    f'Кабинетов с готовыми файлами: {counts["cabinets"]}\n'
                    f'Требуют проверки: {counts["missing"]}')
                if direct:
                    documents, problems = direct_documents(folder)
                    if supply_id and len({c for c, _, _ in documents}) > 1:
                        await message.answer('Этот номер найден в нескольких кабинетах. '
                            'Запросите файлы нужного кабинета через /cabinet.')
                        return
                    for position, (name, supply, path) in enumerate(documents):
                        if not is_owner(message.from_user.id, message.chat.type):
                            return
                        batch = await asyncio.to_thread(save_batch, folder, message.from_user.id,
                                                        delivered_supplies=[(name, supply)])
                        await message.answer_document(FSInputFile(path, filename=f'{supply}.xlsx'),
                            caption=f'Кабинет: {name}\nПоставка: {supply}\nДата: {human_date}\n'
                                    'После загрузки ДТ в WB нажмите «Готово» под этим файлом.',
                            reply_markup=batch_keyboard(batch))
                        if position + 1 < len(documents):
                            await asyncio.sleep(1.05)
                    if problems:
                        await send_chunks(message, 'Не сформированы:\n'+'\n'.join(problems))
                    return
                archives = await asyncio.to_thread(build_archives, folder)
                for part, archive in enumerate(archives, 1):
                    if not is_owner(message.from_user.id, message.chat.type):
                        return
                    name = f'Декларации_{human_date}'
                    if len(archives) > 1:
                        name += f'_часть_{part}'
                    await message.answer_document(FSInputFile(archive, filename=name + '.zip'),
                        caption='Распакуйте ZIP. Внутри — папки кабинетов, Excel по номерам поставок '
                                'и список файлов с причинами пропусков.')
                batch = await asyncio.to_thread(save_batch, folder, message.from_user.id)
                await message.answer(
                    'Когда загрузите файлы в WB, нажмите «Готово». Кнопка подтверждает вашу '
                    'ручную загрузку и ставит галочки только у выданных файлов. '
                    'Для частичной загрузки выберите отдельные поставки.',
                    reply_markup=batch_keyboard(batch))
            except asyncio.TimeoutError:
                await message.answer('Подготовка заняла слишком много времени. Попробуйте позже.')
            except RuntimeError as exc:
                await message.answer(str(exc))  # Only our explicitly sanitized messages.
            except Exception as exc:
                print(f'Request failed: {type(exc).__name__}', flush=True)
                await message.answer('Не удалось завершить отправку. Повторите запрос позже.')
            finally:
                if folder is not None:
                    await asyncio.to_thread(cleanup_delivery, folder)

    @dispatcher.message(Command('files'))
    async def files(message: Message):
        if not await authorized(message):
            return
        parts = (message.text or '').split(maxsplit=1)
        try:
            selected = requested_date(parts[1]) if len(parts) > 1 else None
        except ValueError:
            await message.answer('Пример: /files 25.10.2026. Без даты — ближайшая отгрузка.')
            return
        await deliver(message, selected)

    @dispatcher.message(Command('status'))
    async def status(message: Message):
        if not await authorized(message):
            return
        parts = (message.text or '').split(maxsplit=1)
        if len(parts) != 2:
            await message.answer('Пример: /status 25.10.2026')
            return
        try:
            selected = requested_date(parts[1])
            result = await asyncio.to_thread(lambda: Checklist().status(selected_date=selected))
            await send_chunks(message, audit_text(result))
        except ValueError as exc:
            await message.answer(str(exc))
        except Exception as exc:
            print(f'Checklist status failed: {type(exc).__name__}', flush=True)
            await message.answer('Не удалось прочитать отметки. Повторите позже.')

    @dispatcher.message(Command('supply'))
    async def supply(message: Message):
        if not await authorized(message):
            return
        parts = (message.text or '').split()
        try:
            if len(parts) not in (2, 3) or not re.fullmatch(r'\d{1,20}', parts[1]):
                raise ValueError('Пример: /supply 12345678 или /supply 12345678 25.10.2026')
            selected = requested_date(parts[2]) if len(parts) == 3 else None
            await deliver(message, selected, supply_id=parts[1])
        except ValueError as exc:
            await message.answer(str(exc))

    @dispatcher.message(Command('cabinet'))
    async def cabinet_request(message: Message):
        if not await authorized(message):
            return
        try:
            name, selected = parse_cabinet_request((message.text or '').partition(' ')[2])
            await deliver(message, selected, cabinet=name)
        except ValueError as exc:
            await message.answer(str(exc))

    @dispatcher.callback_query(F.data.regexp(r'^(done|list|audit|one):'))
    async def completion_callback(callback: CallbackQuery):
        if (not callback.message or not callback.from_user
                or not is_owner(callback.from_user.id, callback.message.chat.type)):
            await callback.answer('Доступ закрыт.', show_alert=True)
            return
        await callback.answer()
        try:
            parts = callback.data.split(':')
            if len(parts) not in (2, 3):
                raise ValueError('Некорректная кнопка.')
            action, identifier = parts[:2]
            batch = load_batch(identifier, callback.from_user.id)
            if action == 'list':
                buttons = []
                for number, ((cabinet, supply), _) in enumerate(supply_groups(batch)):
                    buttons.append([InlineKeyboardButton(text=f'Готово: {cabinet} / {supply}',
                        callback_data=f'one:{identifier}:{number}')])
                buttons.append([InlineKeyboardButton(text='Проверить отметки', callback_data=f'audit:{identifier}')])
                await callback.message.answer('Нажимайте только те поставки, куда уже загрузили ДТ.',
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons))
                return
            if checklist_lock.locked():
                await callback.message.answer('Подтверждение принято в очередь. Обработаю автоматически; '
                                              'следующий файл можно запросить уже сейчас.')
            async with checklist_lock:
                if action == 'audit':
                    result = await asyncio.to_thread(lambda: Checklist().status(
                        batch['records'], batch['shipment_date'], scope_supplies=batch.get('scope_supplies')))
                    await send_chunks(callback.message, audit_text(result))
                    return
                expected = batch['records']
                if action == 'one':
                    if len(parts) != 3 or not parts[2].isdigit():
                        raise ValueError('Некорректная поставка.')
                    groups = supply_groups(batch)
                    number = int(parts[2])
                    if not 0 <= number < len(groups):
                        raise ValueError('Поставка не найдена.')
                    expected = groups[number][1]
                elif action != 'done':
                    raise ValueError('Неизвестное действие.')
                if not is_owner(callback.from_user.id, callback.message.chat.type):
                    return
                await callback.message.answer('Проставляю галочки в листе «Отгрузка FBO» и перепроверяю…')
                result = await asyncio.to_thread(lambda: Checklist().mark(expected))
                if result['checked'] == len(expected) and not result['blocked']:
                    await cache_service().evict_confirmed(expected)
                event = dict(time=datetime.now(ZoneInfo('Europe/Moscow')).isoformat(),
                             action=action, targets=expected, result=result,
                             confirmation='USER_REPORTED_UPLOAD_NOT_WB_VERIFIED')
                with (BATCHES/(identifier+'-events.jsonl')).open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(event, ensure_ascii=False)+'\n')
                text = (f'Галочки подтверждены: {result["checked"]} из {len(expected)} строк.\n'
                        'Основание: ваше подтверждение загрузки. ДТ в WB автоматически не проверялись.')
                if result['blocked']:
                    text += '\n\nНужна проверка:\n'+'\n'.join(result['blocked'])
                await send_chunks(callback.message, text)
        except ValueError as exc:
            await callback.message.answer(str(exc))
        except Exception as exc:
            print(f'Checklist action failed: {type(exc).__name__}', flush=True)
            await callback.message.answer('Операция не завершена. Нажмите «Проверить отметки» '
                                          'перед повторным подтверждением.')

    @dispatcher.callback_query(F.data.startswith('pl:'))
    async def planner_callback(callback: CallbackQuery):
        await planner_ui.handle_callback(callback)

    @dispatcher.message(F.text)
    async def text_message(message: Message):
        if not await authorized(message):
            return
        text = (message.text or '').strip()
        if text in PLANNER_ENTRY:
            navigation.pop(message.from_user.id, None)
        if await planner_ui.handle_message(message):
            return
        if text == 'Декларации':
            navigation.pop(message.from_user.id, None)
            await message.answer('Выберите действие с декларациями.', reply_markup=declarations_keyboard())
            return
        today = datetime.now(ZoneInfo('Europe/Moscow')).date()
        if text in ('Обновить базу', 'База файлов'):
            if text == 'Обновить базу':
                if cache_service().lock.locked():
                    await message.answer('База уже обновляется. Подождите немного.')
                    return
                await message.answer('Обновляю локальную базу из таблиц и PostgreSQL…')
                try:
                    await cache_service().refresh()
                except RuntimeError as exc:
                    await message.answer(str(exc))
                    return
            info = await asyncio.to_thread(cache_service().store.info)
            updated = info.get('updated_at', 'ещё не обновлялась')
            await message.answer(f'Excel в базе: {info["files"]}\nТребуют проверки: {info["problems"]}\n'
                f'Строки без номера поставки или кабинета: {info.get("unassigned_rows", "0")} (пропущены)\n'
                f'Последнее обновление: {updated}\nАвтообновление: каждые 6 часов.\n'
                'Очистка по галочкам: каждые 5 минут и после «Готово».\n'
                + ('Последнее обновление не удалось; выдаются сохранённые файлы.' if info.get('last_error') else ''),
                reply_markup=declarations_keyboard())
            return
        if text == 'Главное меню':
            navigation.pop(message.from_user.id, None)
            await message.answer('Выберите действие кнопкой.', reply_markup=keyboard())
            return
        state = navigation.get(message.from_user.id)
        if state and text in state['actions']:
            await navigate(message, state['actions'][text], state)
            return
        if text in ('Выбрать дату', 'Файлы по кабинету', 'По номеру поставки', 'Проверить отметки'):
            try:
                modes = {'Выбрать дату': 'archive', 'Файлы по кабинету': 'cabinet',
                         'По номеру поставки': 'supply', 'Проверить отметки': 'status'}
                mode = modes[text]
                records = await asyncio.to_thread(navigation_records)
                await show_navigation(message, mode, records,
                                      'dates' if mode in ('archive', 'status') else 'cabinets')
            except Exception:
                await message.answer('Не удалось прочитать локальную базу. Нажмите «Обновить базу».')
        elif text == 'Назад':
            await message.answer('Выберите способ получения файлов.', reply_markup=declarations_keyboard())
        elif text.startswith('Кабинет: '):
            await deliver(message, cabinet=text.removeprefix('Кабинет: ').strip())
        elif re.fullmatch(r'\d{1,20}', text):
            await deliver(message, supply_id=text)
        elif text in ('Ближайшая отгрузка', 'Сегодня', 'Завтра'):
            selected = None if text == 'Ближайшая отгрузка' else (
                today if text == 'Сегодня' else today + timedelta(days=1))
            await deliver(message, selected)
        else:
            try:
                selected = requested_date(text)
            except ValueError:
                await message.answer('Выберите кнопку или напишите дату в формате ДД.ММ.ГГГГ.',
                                     reply_markup=keyboard())
                return
            await deliver(message, selected)

    return dispatcher


@contextmanager
def single_instance():
    """OS-level process lock, released automatically on Windows and Linux."""
    path = PROJECT / 'runs' / 'telegram.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        handle.seek(0)
        if not os.fstat(handle.fileno()).st_size:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            acquire = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            release = lambda: msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            acquire = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            release = lambda: fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        try:
            acquire()
        except OSError:
            raise RuntimeError('Бот уже запущен. Используйте существующий процесс.') from None
        try:
            yield
        finally:
            handle.seek(0)
            release()


async def run():
    token = setting('TELEGRAM_BOT_TOKEN')
    if not token:
        print('Добавьте TELEGRAM_BOT_TOKEN в .env. Токен создаётся у @BotFather.', flush=True)
        return
    async with Bot(token) as bot:
        me = await bot.get_me()
        print(f'Бот запущен: https://t.me/{me.username}', flush=True)
        if not setting('TELEGRAM_ALLOWED_USER_ID'):
            print('Доступ к файлам закрыт. Отправьте /myid боту и внесите ID в .env.', flush=True)
        await bot.set_my_commands([
            BotCommand(command='start', description='Как пользоваться'),
            BotCommand(command='files', description='Актуальные файлы: /files 25.10.2026'),
            BotCommand(command='supply', description='Excel по номеру поставки'),
            BotCommand(command='cabinet', description='Excel выбранного кабинета'),
            BotCommand(command='status', description='Проверить отметки: /status 25.10.2026'),
            BotCommand(command='myid', description='Узнать свой Telegram ID')])
        task = asyncio.create_task(cache_service().background())
        try:
            await create_dispatcher().start_polling(bot, allowed_updates=['message', 'callback_query'])
        finally:
            task.cancel()
            from contextlib import suppress
            with suppress(asyncio.CancelledError):
                await task


if __name__ == '__main__':
    logging.basicConfig(level=logging.CRITICAL)
    try:
        with single_instance():
            asyncio.run(run())
    except (KeyboardInterrupt, SystemExit):
        pass
    except Exception as exc:
        print(f'Запуск остановлен ({type(exc).__name__}). Проверьте .env и соединение.', flush=True)
