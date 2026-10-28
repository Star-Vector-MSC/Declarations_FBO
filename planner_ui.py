"""Owner-only Telegram controller for the TEST-only FBO planner: immutable draft versions, one explicitly confirmed
publish into «Планирование FBO — ТЕСТ». Storage, memory and Google clients are lazy; settings are fixed commands."""
import asyncio
import copy
import os
import re
import secrets
from collections import Counter
from datetime import date, datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

import planner
from planner_sheets import MSK, PlannerSheets, PlannerSheetsError, SourceChangedError
from planner_storage import DraftError, DraftStore

ENTRY = ("Новый план", "Мои планы")
LEAVE = ("Декларации", "Главное меню")  # reset the planner state; the declarations handler then runs as before
OLD_BUTTONS = ("Ближайшая отгрузка", "Обновить базу", "База файлов", "Выбрать дату", "Проверить отметки",
               "Файлы по кабинету", "По номеру поставки")
GENERIC = "Операция не выполнена. Повторите позже."
MEM_DOWN = "Память помощника недоступна — создание и запись планов заблокированы, пока её не восстановят."
HELP = ("Команды настроек (по одной в сообщении):\n"
        "Продажи от 8 · Запас до 10 · Остаток wild от 30 · Цель 10 · Максимум 13 · Машина 20\n"
        "Исключить wild wild123 · Исключить кабинет Название\n"
        "Добавить АРТИКУЛ | КАБИНЕТ | КОЛИЧЕСТВО | ПРИЧИНА · Убрать АРТИКУЛ | КАБИНЕТ\n"
        "Свободный текст не поддерживается. Изменения применяются только после «Применить».")
_NUMCMD = (("остаток wild от", "min_wild_days"), ("продажи от", "min_sales"), ("запас до", "max_current_days"),
           ("цель", "target_days"), ("максимум", "max_days"))
_LABEL = {"min_sales": "Продажи от", "max_current_days": "Запас до", "min_wild_days": "Остаток wild от",
          "target_days": "Цель", "max_days": "Максимум", "capacity": "Машина (паллет)",
          "excluded_wild": "Исключённые wild", "excluded_cabinets": "Исключённые кабинеты", "fixed": "Обязательные строки"}
_ID = re.compile(r"[0-9a-f]{12}")
_DATE = re.compile(r"\d{2}\.\d{2}\.\d{4}")
PAGE = 10


class _Blocked(Exception):
    """User-facing refusal raised inside worker threads."""


def _err(e):
    if isinstance(e, (PlannerSheetsError, DraftError, _Blocked)):
        return str(e)
    if isinstance(e, ValueError):
        return "Не удалось построить план: " + str(e)[:300]
    return GENERIC


def _cb(*parts):
    data = ":".join(str(p) for p in parts)
    assert len(data.encode()) <= 64
    return data


def _btn(text, *parts):
    return InlineKeyboardButton(text=text, callback_data=_cb("pl", *parts))


def _kb(rows):
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _num(s):
    v = float(s.replace(",", "."))
    return int(v) if v == int(v) else v


def parse_command(text, rules):
    """One settings command -> (new validated rules, summary). ValueError carries a user-facing message."""
    t = " ".join(text.split())
    if not t or len(t) > 300:
        raise ValueError(HELP)
    new, low = copy.deepcopy(rules), t.casefold()
    for prefix, key in _NUMCMD:
        if low.startswith(prefix + " "):
            rest = low[len(prefix):].strip()
            if not re.fullmatch(r"\d{1,4}(?:[.,]\d{1,2})?", rest) or _num(rest) > 1000:
                raise ValueError("Нужно число от 0 до 1000, например: " + prefix + " 8")
            new[key] = _num(rest)
            break
    else:
        m = re.fullmatch(r"(?i)машина\s+(20|33)", t)
        if m:
            new["capacity"] = int(m.group(1))
        elif re.fullmatch(r"(?i)исключить wild\s+\S.{0,80}", t):
            new["excluded_wild"] = list(new["excluded_wild"]) + [t[len("исключить wild"):].strip()]
        elif re.fullmatch(r"(?i)исключить кабинет\s+\S.{0,120}", t):
            new["excluded_cabinets"] = list(new["excluded_cabinets"]) + [t[len("исключить кабинет"):].strip()]
        elif low.startswith("добавить "):
            p = [x.strip() for x in t[len("добавить "):].split("|")]
            if len(p) != 4 or not all(p) or not re.fullmatch(r"\d{1,7}", p[2]) or int(p[2]) <= 0 or len(p[3]) > 200:
                raise ValueError("Формат: Добавить АРТИКУЛ | КАБИНЕТ | КОЛИЧЕСТВО | ПРИЧИНА")
            if any(f["article"] == p[0] and f["cabinet"] == p[1] for f in new["fixed"]):
                raise ValueError("Такая обязательная строка уже есть; сначала уберите её.")
            new["fixed"] = list(new["fixed"]) + [dict(article=p[0], cabinet=p[1], quantity=int(p[2]), reason=p[3])]
        elif low.startswith("убрать "):
            p = [x.strip() for x in t[len("убрать "):].split("|")]
            keep = [f for f in new["fixed"] if (f["article"], f["cabinet"]) != tuple(p)]
            if len(p) != 2 or len(keep) == len(new["fixed"]):
                raise ValueError("Такой обязательной строки нет. Формат: Убрать АРТИКУЛ | КАБИНЕТ")
            new["fixed"] = keep
        else:
            raise ValueError("Не понял указание, ничего не изменено.\n" + HELP)
    try:
        new = planner.validate_rules(new)
    except ValueError as e:
        raise ValueError("Недопустимые настройки: " + str(e)[:200]) from None
    diff = [f"{_LABEL[k]}: {rules[k]} → {new[k]}" for k in new if new[k] != rules[k]]
    if not diff:
        raise ValueError("Это уже так настроено, изменений нет.")
    return new, "\n".join(diff)


def warning_summary(warnings):
    cats = Counter()
    for w in warnings:
        kind, _, rest = w.partition(":")
        rest = (rest or kind).split(";")[0].split()
        cats[(kind.split()[0], " ".join(rest[:4]))] += 1
    lines = [f"• {k[0]}: {k[1]} — {n}" for k, n in cats.most_common(5)]
    return lines, [w[:160] for w in warnings[:3]]


def preview_text(d):
    p, r = d["plan"], d["plan"]["rules"]
    lines = [f"ТЕСТОВЫЙ план {d['id']} · версия {d['version']}",
             "Запись возможна только в лист «Планирование FBO — ТЕСТ».",
             f"Отгрузка {p['date']} · склад {p['warehouse']} · машина {r['capacity']} паллет",
             f"Строк: {len(p['lines'])} · загрузка {p['total_pallets']} паллет",
             f"Правила: продажи от {r['min_sales']}, запас до {r['max_current_days']}, остаток wild от "
             f"{r['min_wild_days']}, цель {r['target_days']}, максимум {r['max_days']}; "
             f"исключений wild {len(r['excluded_wild'])}, кабинетов {len(r['excluded_cabinets'])}, "
             f"обязательных строк {len(r['fixed'])}"]
    if p["remaining_pallets"] and p["remaining_pallets"] > 0:
        lines.append(f"Недобор: {p['remaining_pallets']} паллет (правила сами не ослабляются; смените машину или условия).")
    if not p["lines"]:
        lines.append("Подходящих строк нет.")
    if p["warnings"]:
        cats, ex = warning_summary(p["warnings"])
        lines.append(f"Предупреждений: {len(p['warnings'])} (полный список сохранён в версии). По видам:")
        lines += cats + ["Примеры:"] + ["– " + e for e in ex]
    if d.get("exported_version") == d["version"]:
        lines.append("Выгружено в тестовый лист: " + d["export_url"])
    elif d["version"] != d["latest_version"]:
        lines.append(f"Это старая версия; актуальная — {d['latest_version']}.")
    elif d.get("exported_version") is not None:
        lines.append(f"Версия {d['exported_version']} уже выгружена; изменения запрещены.")
    return "\n".join(lines)


def lines_text(d, page):
    ls = d["plan"]["lines"]
    page = max(0, min(page, max(0, (len(ls) - 1) // PAGE)))
    out = [f"Состав плана {d['id']} v{d['version']}, страница {page + 1}/{max(1, -(-len(ls) // PAGE))}:"]
    for ln in ls[page * PAGE:(page + 1) * PAGE]:
        out.append(f"{ln['wild']} · {ln['article']} · {ln['cabinet']}: {ln['quantity']} шт ({ln['pallets']} пал.), "
                   f"прогноз {ln['forecast_before']}→{ln['forecast_after']}. {ln['reason']}")
    return "\n".join(out), page


class PlannerController:
    """handle_message(message)->bool (True = consumed), handle_callback(callback), reset(owner)."""

    def __init__(self, store=None, sheets=None, memory=None, is_owner=None, data_dir=None):
        self._store, self._sheets, self._memory = store, sheets, memory
        self._is_owner, self._data_dir = is_owner, data_dir
        self._lock, self._st, self._gen = asyncio.Lock(), {}, {}

    # lazy collaborators -------------------------------------------------------------------------------------
    def _dir(self):
        if self._data_dir is None:
            import wb_declarations
            self._data_dir = os.path.join(str(wb_declarations.PROJECT), "data")
        return self._data_dir

    @property
    def store(self):
        if self._store is None:
            self._store = DraftStore(os.path.join(self._dir(), "planner_drafts.sqlite3"))
        return self._store

    @property
    def sheets(self):
        if self._sheets is None:
            self._sheets = PlannerSheets()
        return self._sheets

    @property
    def memory(self):
        if self._memory is None:
            from planner_memory import MemoryStore
            self._memory = MemoryStore(os.path.join(self._dir(), "planner_memory.sqlite3"),
                                       os.path.join(self._dir(), "planner_memory-ledger.md"))
        return self._memory

    # helpers -------------------------------------------------------------------------------------------------
    def _owner(self, uid, chat_type):
        if self._is_owner is not None:
            return bool(self._is_owner(uid, chat_type))
        import telegram_bot  # lazy: avoids a circular import
        return bool(telegram_bot.is_owner(uid, chat_type))

    def reset(self, owner):
        self._gen[owner] = self._gen.get(owner, 0) + 1
        self._st.pop(owner, None)

    def _mem_ok(self):
        try:
            self.memory.applicable(on_date=datetime.now(MSK).date().isoformat())
        except Exception:
            raise _Blocked(MEM_DOWN) from None

    @staticmethod
    def _main():
        import telegram_bot
        return telegram_bot.keyboard()

    def _keys(self, d):
        i, v = d["id"], d["version"]
        top = [[_btn("Состав", "ln", i, v, 0), _btn("Память", "mem", i, v)]]
        if d["version"] != d["latest_version"] or d.get("exported_version") is not None:
            return _kb(top + [[_btn("Мои планы", "list")]])
        other = 20 if d["plan"]["rules"]["capacity"] == 33 else 33
        return _kb(top + [[_btn("Пересчитать", "rc", i, v), _btn("Настройки", "set", i, v)],
                          [_btn(f"Машина {other} паллет", "cap", i, v, other)],
                          [_btn("Записать в тестовый лист", "pub", i, v)]])

    async def _show(self, msg, d):
        await msg.answer(preview_text(d), reply_markup=self._keys(d))

    def _build(self, uid, rules, day, wh, arrival=None, draft=None, alive=None):  # runs in a worker thread
        self._mem_ok()
        snap = self.sheets.snapshot()
        if wh not in snap["warehouses"]:
            raise _Blocked("Такого склада сейчас нет в таблице. Начните новый план.")
        plan = planner.build_plan(snap, rules, day, wh, arrival)
        if alive is not None and not alive():  # owner/menu re-check after the slow read, before anything is stored
            raise _Blocked("Действие отменено.")
        return self.store.revise(uid, draft["id"], draft["version"], plan) if draft else self.store.create(uid, plan)

    async def _guarded(self, uid, chat_type, msg, work, *args, live=False):
        """Owner re-check around a long operation + menu race guard. Returns (ok, result).
        live=True passes work an `alive()` check to run right before it stores anything."""
        g = self._gen.get(uid, 0)
        kw = {"alive": lambda: self._owner(uid, chat_type) and self._gen.get(uid, 0) == g} if live else {}
        try:
            res = await asyncio.to_thread(lambda: work(*args, **kw))
        except Exception as e:
            if self._owner(uid, chat_type):
                await msg.answer(_err(e))
            return False, None
        if not self._owner(uid, chat_type):
            return False, None
        if self._gen.get(uid, 0) != g:
            await msg.answer("Действие отменено: вы вернулись в меню.")
            return False, None
        return True, res

    # messages ------------------------------------------------------------------------------------------------
    async def handle_message(self, message):
        user = message.from_user
        text = (message.text or "").strip()
        if user is None or text.startswith("/"):
            return False
        uid, ct = user.id, message.chat.type
        if text in LEAVE or text in OLD_BUTTONS:
            self.reset(uid)
            return False
        if text in ENTRY:
            if not self._owner(uid, ct):
                return False
            self.reset(uid)
            async with self._lock:
                return await (self._new(uid, message) if text == "Новый план" else self._list(uid, ct, message))
        st = self._st.get(uid)
        if not st or st["step"] not in ("date", "wh", "cap", "settings"):
            return False
        if st["step"] == "settings" and text.isdigit():
            return False  # supply numbers are never intercepted
        if not self._owner(uid, ct):
            self.reset(uid)
            return False
        async with self._lock:
            st = self._st.get(uid)
            if not st or not self._owner(uid, ct):
                return False
            return await getattr(self, "_t_" + st["step"])(uid, ct, message, text, st)

    async def _new(self, uid, msg):
        self._st[uid] = {"step": "date"}
        await msg.answer("Новый тестовый план. Напишите дату отгрузки ДД.ММ.ГГГГ или нажмите кнопку.",
                         reply_markup=ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="Завтра"),
                         KeyboardButton(text="Послезавтра")], [KeyboardButton(text="Главное меню")]],
                         resize_keyboard=True))
        return True

    async def _list(self, uid, ct, msg):
        try:
            ds = await asyncio.to_thread(self.store.list, uid, 10)
        except Exception as e:
            await msg.answer(_err(e))
            return True
        if not ds:
            await msg.answer("Сохранённых планов пока нет.", reply_markup=self._main())
            return True
        rows = [[_btn(f"{d['id']} v{d['latest_version']} · {d['plan']['date']} · {d['plan']['warehouse']}"[:60],
                      "op", d["id"], d["latest_version"])] for d in ds]
        await msg.answer("Ваши последние планы:", reply_markup=_kb(rows))
        return True

    async def _t_date(self, uid, ct, msg, text, st):
        today, low = datetime.now(MSK).date(), text.casefold()
        shortcut = {"сегодня": 0, "завтра": 1, "послезавтра": 2}
        if low in shortcut:
            day = today + timedelta(days=shortcut[low])
        elif _DATE.fullmatch(text):
            try:
                day = date(int(text[6:]), int(text[3:5]), int(text[:2]))
            except ValueError:
                await msg.answer("Такой даты нет. Формат ДД.ММ.ГГГГ.")
                return True
        else:
            return False
        if day < today:
            await msg.answer("Дата в прошлом. Напишите сегодняшнюю или будущую дату.")
            return True

        def work():
            self._mem_ok()
            return self.sheets.snapshot()["warehouses"]
        ok, whs = await self._guarded(uid, ct, msg, work)
        if not ok:
            return True
        if not whs:
            await msg.answer("В таблице нет складов Wildberries.")
            return True
        self._st[uid] = {"step": "wh", "date": day.isoformat(), "whs": whs[:40]}
        rows = [[_btn(w[:40], "wh", i)] for i, w in enumerate(self._st[uid]["whs"])]
        await msg.answer(f"Дата {day.strftime('%d.%m.%Y')}. Выберите склад:", reply_markup=_kb(rows))
        return True

    async def _t_wh(self, uid, ct, msg, text, st):
        return await self._pick_wh(uid, msg, st["whs"].index(text)) if text in st["whs"] else False

    async def _pick_wh(self, uid, msg, idx):
        st = self._st[uid]
        st.update(step="cap", wh=st["whs"][idx])
        await msg.answer(f"Склад {st['wh']}. Выберите машину:", reply_markup=_kb(
            [[_btn("33 паллет", "wc", 33), _btn("20 паллет", "wc", 20)]]))
        return True

    async def _t_cap(self, uid, ct, msg, text, st):
        m = re.fullmatch(r"(?i)(20|33) паллет", text)  # bare numbers belong to the supply-number handler
        return await self._make(uid, ct, msg, int(m.group(1))) if m else False

    async def _make(self, uid, ct, msg, cap):
        st = self._st.get(uid)
        if not st or st.get("step") != "cap":
            await msg.answer("Начните заново: «Новый план».")
            return True
        ok, d = await self._guarded(uid, ct, msg, self._build, uid, {"capacity": cap}, st["date"], st["wh"],
                                    live=True)
        if ok:
            self._st.pop(uid, None)
            await msg.answer("План рассчитан и сохранён как черновик (в таблицу ничего не записано).",
                             reply_markup=self._main())
            await self._show(msg, d)
        return True

    async def _t_settings(self, uid, ct, msg, text, st):
        try:
            d = await asyncio.to_thread(self.store.get, uid, st["id"])
            if d["version"] != st["ver"] or d.get("exported_version") is not None:
                raise _Blocked("План изменился; откройте актуальную версию в «Моих планах».")
            rules, summary = parse_command(text, d["plan"]["rules"])
        except (ValueError, _Blocked, DraftError) as e:
            await msg.answer(_err(e) if not isinstance(e, ValueError) else str(e))
            return True
        nonce = secrets.token_hex(3)
        st.update(proposal=rules, nonce=nonce)
        await msg.answer("Предлагаемые изменения (пока не применены):\n" + summary, reply_markup=_kb(
            [[_btn("Применить", "ap", st["id"], st["ver"], nonce), _btn("Отмена", "cn")]]))
        return True

    # callbacks -----------------------------------------------------------------------------------------------
    async def handle_callback(self, cb):
        msg, user = cb.message, cb.from_user
        if msg is None or user is None or not self._owner(user.id, msg.chat.type):
            await cb.answer("Доступ закрыт.", show_alert=True)
            return
        await cb.answer()
        uid, ct = user.id, msg.chat.type
        parts = (cb.data or "").split(":")
        try:
            if len(cb.data.encode()) > 64 or parts[0] != "pl":
                raise ValueError
            act, a = parts[1], parts[2:]
            if act in ("op", "ln", "mem", "rc", "set", "cap", "pub", "ok", "ap"):
                if not (len(a) >= 2 and _ID.fullmatch(a[0]) and a[1].isdigit()):
                    raise ValueError
                a = [a[0], int(a[1])] + a[2:]
            elif act not in ("list", "cn", "wh", "wc"):
                raise ValueError
        except (ValueError, IndexError, AttributeError):
            await msg.answer("Кнопка устарела. Откройте «Мои планы».")
            return
        async with self._lock:
            if not self._owner(uid, ct):
                return
            try:
                await getattr(self, "_c_" + act)(uid, ct, msg, *a)
            except Exception as e:
                await msg.answer(_err(e))

    async def _draft(self, uid, did, ver, latest=True):
        d = await asyncio.to_thread(self.store.get, uid, did, None if latest else ver)
        if latest and (d["version"] != ver or d.get("exported_version") is not None):
            raise _Blocked("Эта версия устарела или уже выгружена. Откройте актуальный план в «Моих планах».")
        return d

    _c_list = _list

    async def _c_cn(self, uid, ct, msg):
        st = self._st.get(uid)
        if st:
            st.pop("proposal", None)
            st.pop("confirm", None)
            if st["step"] == "settings":
                self._st.pop(uid)
        await msg.answer("Отменено, ничего не изменено.")

    async def _c_wh(self, uid, ct, msg, idx):
        st = self._st.get(uid)
        if not st or st["step"] != "wh" or not idx.isdigit() or int(idx) >= len(st["whs"]):
            await msg.answer("Кнопка устарела. Начните заново: «Новый план».")
            return
        await self._pick_wh(uid, msg, int(idx))

    async def _c_wc(self, uid, ct, msg, n):
        if n not in ("20", "33"):
            await msg.answer("Кнопка устарела.")
            return
        await self._make(uid, ct, msg, int(n))

    async def _c_op(self, uid, ct, msg, did, ver):
        await self._show(msg, await self._draft(uid, did, ver, latest=False))

    async def _c_ln(self, uid, ct, msg, did, ver, page="0"):
        d = await self._draft(uid, did, ver, latest=False)
        text, page = lines_text(d, int(page) if page.isdigit() else 0)
        n = -(-len(d["plan"]["lines"]) // PAGE)
        nav = ([_btn("◀", "ln", did, ver, page - 1)] if page else []) + ([_btn("▶", "ln", did, ver, page + 1)]
                                                                           if page + 1 < n else [])
        await msg.answer(text, reply_markup=_kb([nav]) if nav else None)

    async def _c_mem(self, uid, ct, msg, did, ver):
        d = await self._draft(uid, did, ver, latest=False)
        p = d["plan"]

        def work():
            seen = {r["id"]: r for r in self.memory.applicable(plan_id=d["id"], on_date=p["date"])}
            for key in sorted({ln[k] for ln in p["lines"] for k in ("wild", "article", "product") if ln.get(k)}):
                seen.update({r["id"]: r for r in self.memory.applicable(product=key, plan_id=d["id"], on_date=p["date"])})
            return list(seen.values())
        ok, recs = await self._guarded(uid, ct, msg, lambda: (self._mem_ok(), work())[1])
        if not ok:
            return
        out = ["Память помощника для этого плана (подтверждённые, не истёкшие):"]
        for r in recs[:15]:
            tag = "пример (не политика)" if r["kind"] == "case" else "правило"
            out.append(f"• {tag}, {r['scope']}{' ' + r['target'] if r['target'] else ''}"
                       f"{', до ' + r['expires_on'] if r['expires_on'] else ''}: {r['text'][:150]}")
        if not recs:
            out.append("Подходящих записей нет.")
        out.append("Текст памяти сам расчёт не меняет: действуют жёсткие правила расчёта и ваши настройки.")
        await msg.answer("\n".join(out))

    async def _c_rc(self, uid, ct, msg, did, ver):
        d = await self._draft(uid, did, ver)
        await self._revise(uid, ct, msg, d, d["plan"]["rules"])

    async def _revise(self, uid, ct, msg, d, rules):
        p = d["plan"]
        ok, new = await self._guarded(uid, ct, msg, self._build, uid, rules, p["date"], p["warehouse"],
                                      p.get("arrival_date"), d, live=True)
        if ok:
            await msg.answer("Пересчитано: сохранена новая неизменяемая версия (в таблицу ничего не записано).")
            await self._show(msg, new)

    async def _c_set(self, uid, ct, msg, did, ver):
        await self._draft(uid, did, ver)
        self._st[uid] = {"step": "settings", "id": did, "ver": ver}
        await msg.answer(HELP)

    async def _c_cap(self, uid, ct, msg, did, ver, n):
        d = await self._draft(uid, did, ver)
        if n not in ("20", "33"):
            raise ValueError("Кнопка устарела.")
        rules, summary = parse_command(f"Машина {n}", d["plan"]["rules"])
        nonce = secrets.token_hex(3)
        self._st[uid] = {"step": "proposal", "id": did, "ver": ver, "proposal": rules, "nonce": nonce}
        await msg.answer("Предлагаемые изменения (пока не применены):\n" + summary, reply_markup=_kb(
            [[_btn("Применить", "ap", did, ver, nonce), _btn("Отмена", "cn")]]))

    async def _c_ap(self, uid, ct, msg, did, ver, nonce=""):
        st = self._st.get(uid)
        if not st or st.get("nonce") != nonce or (st["id"], st["ver"]) != (did, ver) or "proposal" not in st:
            await msg.answer("Предложение устарело или отменено. Ничего не изменено.")
            return
        rules, _ = st.pop("proposal"), st.pop("nonce")
        self._st.pop(uid, None)
        await self._revise(uid, ct, msg, await self._draft(uid, did, ver), rules)

    async def _c_pub(self, uid, ct, msg, did, ver):
        d = await self._draft(uid, did, ver)
        self._st[uid] = {"step": "confirm", "confirm": (did, ver)}
        await msg.answer(f"Записать план {did} v{ver} ({len(d['plan']['lines'])} строк) в лист «Планирование FBO — ТЕСТ»? "
                         "Источники будут перепроверены перед записью; в «Отгрузка FBO» запись невозможна.",
                         reply_markup=_kb([[_btn("Подтвердить запись", "ok", did, ver), _btn("Отмена", "cn")]]))

    async def _c_ok(self, uid, ct, msg, did, ver):
        g = self._gen.get(uid, 0)  # taken before any await: a menu exit during the awaits below must cancel the write
        d = await asyncio.to_thread(self.store.get, uid, did, ver)
        if d.get("exported_version") == ver:  # double click / repeat: never write twice
            await msg.answer("Уже записано в тестовый лист: " + d["export_url"])
            return
        if (self._st.get(uid) or {}).get("confirm") != (did, ver) or d["version"] != d["latest_version"]:
            await msg.answer("Сначала нажмите «Записать в тестовый лист» у актуальной версии.")
            return
        self._st.pop(uid, None)
        await msg.answer("Перепроверяю источники и записываю в тестовый лист…")

        def work():
            self._mem_ok()
            if not self._owner(uid, ct) or self._gen.get(uid, 0) != g:  # last check right before the write
                raise _Blocked("Запись отменена.")
            url = self.sheets.publish(d)
            try:
                self.store.mark_exported(uid, did, ver, url)
            except Exception:
                return url, False
            return url, True
        try:
            url, saved = await asyncio.to_thread(work)
        except SourceChangedError as e:
            if self._owner(uid, ct):
                await msg.answer(str(e) + " Нажмите «Пересчитать».")
        except Exception as e:  # PublishUncertainError text tells how to retry safely
            if self._owner(uid, ct):
                await msg.answer(_err(e))
        else:
            if self._owner(uid, ct):
                await msg.answer("Готово. План записан только в тестовый лист:\n" + url + (
                    "" if saved else "\nОтметку о выгрузке сохранить не удалось: повторите запись той же версии — "
                                    "она проверит маркеры и не создаст дубликатов."))
