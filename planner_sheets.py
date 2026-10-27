"""Google Sheets gateway for FBO planning: read-only sources; append-only rows to the pinned TEST sheet
via one atomic batchUpdate. Config/credentials/session are created lazily (nothing at import/constructor)."""
import hashlib
import json
import re
from collections import Counter
from datetime import date, datetime, timedelta, timezone
from fractions import Fraction

import planner

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
_API = "https://sheets.googleapis.com/v4/spreadsheets/"
T_TEST, T_PACK, T_ORIG, T_TARGET = "тест", "кол-во на палете и в коробе", "Отгрузка FBO", "Планирование FBO — ТЕСТ"
ORIGINAL = (
    "Фактическая Дата отгрузки на вб", "Номер поставки", "Плановая дата поставки на ВБ",
    "Плановая дата прибытия авто на вб", "Дата отгрузки (склад)", "Сборка товара (Склад)",
    "Факт Сборка товара (Склад)", "Готовность склада (Алексей)", "Склад Wildberries", "Статус отгрузки", "wild",
    "Артикул", "Название магазина", "Название магазина", "Чек лист Декларация", "Свободный остаток",
    "Остаток на дней по стоку", "Тип поставки", "Количество", "Итого паллет", "Палето-мест в машине",
    "Шт в коробе", "Итого коробов", "Шт на паллете", "средние продажи в день по артикулу",
    "Текущий остаток на ФБО", "Рейтинг", "Остаток на (дней)", "К отгрузке", "Дней загрузки план",
    "Вес товара ед", "Вес товара в поставке", "Комментарии", "Собрано по факту (Склад)", "Факт Иван",
    "Факт сборка (Дима)", "Итого паллет", "Общее кол-во к отгрузке на палете")
META = ("ID плана", "Версия плана", "Прогноз до", "Прогноз после", "Причина", "Снимок данных", "Правила плана")
MSK = timezone(timedelta(hours=3), "MSK")  # Moscow: fixed UTC+3, no DST
_EPOCH = date(1899, 12, 30)
_DF = {"numberFormat": {"type": "DATE", "pattern": "dd.mm.yyyy"}}
_SPILL = re.compile(r"ARRAYFORMULA|IMPORTRANGE|FLATTEN|QUERY\s*\(|SEQUENCE\s*\(|^\s*=\s*\{", re.I)
_HEX12 = re.compile(r"[0-9a-f]{12}")
_UNSURE = ("Связь с Google Sheets прервалась во время записи, результат неизвестен. Повторите публикацию "
           "той же версии: повтор проверит маркеры и не создаст дубликатов.")


class PlannerSheetsError(Exception):
    """Friendly sanitized error, safe to show to the user."""


class SourceChangedError(PlannerSheetsError):
    pass


class PublishConflictError(PlannerSheetsError):
    pass


class PublishUncertainError(PlannerSheetsError):
    pass


def _c(name, occ=0):
    return planner._norm(name), occ


def _rng(title):
    return "'" + title.replace("'", "''") + "'"


def _cell(row, i):
    return row[i] if i < len(row) else None


def _canon(x):
    return json.dumps(x, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _s(v):
    return v.strip() if isinstance(v, str) else v


def _sv(v):
    return {} if v is None else {"userEnteredValue": {"stringValue": str(v)}}


def _nv(v):
    return {} if v is None else {"userEnteredValue": {"numberValue": v}}


def _dv(v):
    return dict(_nv(v), userEnteredFormat=_DF)


def _serial(iso):
    return None if iso is None else (date.fromisoformat(iso) - _EPOCH).days


def _sid(x):
    v = x.get("spreadsheet_id") or x.get("id") if isinstance(x, dict) else x
    if not isinstance(v, str) or not v.strip():
        raise PlannerSheetsError("Конфигурация таблиц некорректна")
    return v.strip()


def _rows(values):
    """Drop fully blank rows (header kept first) and trailing blank cells."""
    out = []
    for i, r in enumerate(values or []):
        r = list(r)
        while r and planner._blank(r[-1]):
            r.pop()
        if r or i == 0:
            out.append(r)
    return out


def _bind(header, names, where):
    """Map (normalized name, occurrence) -> column; each name must occur exactly as often as expected."""
    pos, want, seen, out = {}, Counter(planner._norm(n) for n in names), Counter(), {}
    for i, h in enumerate(header):
        if not planner._blank(h):
            pos.setdefault(planner._norm(h), []).append(i)
    for n in names:
        k = planner._norm(n)
        found = pos.get(k, [])
        if len(found) != want[k]:
            raise PlannerSheetsError(f"Лист «{where}»: столбец «{n}» ожидается {want[k]} раз, найдено {len(found)}")
        out[(k, seen[k])] = found[seen[k]]
        seen[k] += 1
    return out


def _draft(d):
    try:
        did, ver, plan = d["id"], d["version"], d["plan"]
        lines, cap = plan["lines"], plan["rules"]["capacity"]
        ok = bool(isinstance(did, str) and _HEX12.fullmatch(did) and type(ver) is int and ver > 0
                  and isinstance(plan["source_hash"], str) and isinstance(plan["warehouse"], str))
        _serial(plan["date"]), _serial(plan.get("arrival_date"))
        total = Fraction(0)
        for ln in lines:
            q, b, p = ln["quantity"], ln["box_qty"], ln["pallet_qty"]
            ok = ok and all(type(x) is int and x > 0 for x in (q, b, p)) and q % b == 0
            ok = ok and all(isinstance(ln[k], str) and ln[k] for k in ("wild", "article", "cabinet"))
            total += Fraction(q, p)
        over = total > cap
    except (TypeError, KeyError, ValueError, AttributeError, ZeroDivisionError):
        ok = False
    if not ok:
        raise PlannerSheetsError("Некорректный черновик плана (нужны целые коробки и полные строки)")
    if not lines:
        raise PlannerSheetsError("План пуст — публиковать нечего")
    if over:
        raise PlannerSheetsError("План превышает вместимость машины")
    return did, ver, plan


def _guard(reqs, tid, oid):
    """Every write destination must be the test sheet; only formula copy may read the original."""
    for r in reqs:
        (kind, b), = r.items()
        dst = {"appendDimension": b, "copyPaste": b.get("destination"), "updateCells": b.get("range")}.get(kind)
        src = b["source"]["sheetId"] if kind == "copyPaste" else tid
        if (not dst or dst.get("sheetId") != tid or src not in (tid, oid)
                or (src == oid and b.get("pasteType") != "PASTE_FORMULA")):
            raise PlannerSheetsError("Внутренняя проверка: запись вне тестового листа запрещена")


class PlannerSheets:
    """snapshot() -> dict; publish(draft) -> link to the first test row of that plan version."""

    def __init__(self, session=None):
        self._session, self._cfg, self._pinned = session, None, None

    def _config(self):
        if self._cfg is None:
            try:
                import wb_declarations as wb  # existing project config only
                base, prod, key = _sid(wb.SHIPMENTS[0]), {_sid(x) for x in list(wb.SHIPMENTS)[1:2]}, wb.KEY
            except PlannerSheetsError:
                raise
            except Exception:
                raise PlannerSheetsError("Конфигурация таблиц недоступна") from None
            if base in prod:
                raise PlannerSheetsError("Конфигурация таблиц некорректна")
            self._cfg = base, key
        return self._cfg

    def _http(self):
        if self._session is None:
            key = self._config()[1]
            try:
                from google.auth.transport.requests import AuthorizedSession
                from google.oauth2 import service_account as sa
                make = (sa.Credentials.from_service_account_info if isinstance(key, dict)
                        else sa.Credentials.from_service_account_file)
                self._session = AuthorizedSession(make(key, scopes=SCOPES))
            except Exception:
                raise PlannerSheetsError("Не удалось подключиться к Google Sheets") from None
        return self._session

    def _api(self, method, suffix="", params=None, body=None):
        base, http, write = self._config()[0], self._http(), method == "POST"
        try:
            r = http.request(method, _API + base + suffix, params=params, json=body, timeout=90)
            code = r.status_code
        except Exception:
            raise (PublishUncertainError(_UNSURE) if write
                   else PlannerSheetsError("Google Sheets недоступен, попробуйте позже")) from None
        if write and (code >= 500 or code in (408, 429)):
            raise PublishUncertainError(_UNSURE)
        if code != 200:
            raise PlannerSheetsError(f"Google Sheets отклонил запрос (код {code})" + ("; запись не выполнена" * write))
        try:
            return r.json()
        except Exception:
            if write:
                return {}
            raise PlannerSheetsError("Некорректный ответ Google Sheets") from None

    def _meta(self):
        d = self._api("GET", "", {"fields": "sheets.properties(sheetId,title,gridProperties.rowCount)"})
        props = [s.get("properties", {}) for s in d.get("sheets", [])]
        ids = {}
        for t in (T_TEST, T_PACK, T_ORIG, T_TARGET):
            m = [p for p in props if p.get("title") == t]
            if len(m) != 1:
                raise PlannerSheetsError(f"Лист «{t}» должен быть ровно один")
            ids[t] = {"sheetId": m[0].get("sheetId", 0), "rows": m[0].get("gridProperties", {}).get("rowCount", 0)}
        tid = ids[T_TARGET]["sheetId"]
        if tid in {ids[t]["sheetId"] for t in (T_TEST, T_PACK, T_ORIG)}:
            raise PlannerSheetsError("Тестовый лист совпадает с исходным")
        if self._pinned is None:
            self._pinned = tid
        elif self._pinned != tid:
            raise PlannerSheetsError("Тестовый лист был пересоздан; перезапустите бота")
        return ids

    def _values(self, titles, render):
        d = self._api("GET", "/values:batchGet", {"ranges": [_rng(t) for t in titles], "valueRenderOption": render,
                                                  "dateTimeRenderOption": "SERIAL_NUMBER", "majorDimension": "ROWS"})
        vr = d.get("valueRanges", [])
        if len(vr) != len(titles):
            raise PlannerSheetsError("Неполный ответ Google Sheets")
        return [[list(r) for r in v.get("values", [])] for v in vr]

    def _read(self):
        test, pack, orig = self._values((T_TEST, T_PACK, T_ORIG), "UNFORMATTED_VALUE")
        test, pack = _rows(test), _rows(pack)
        if not orig:
            raise PlannerSheetsError(f"Лист «{T_ORIG}» пуст")
        wc = _bind(orig[0], ("Склад Wildberries",), T_ORIG)[_c("Склад Wildberries")]
        whs = sorted({w for w in (planner._ident(_cell(r, wc)) for r in orig[1:]) if w})
        body = _canon({"test": test, "packing": pack, "warehouses": whs})
        return {"test": test, "packing": pack, "warehouses": whs,
                "source_hash": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "captured_at": datetime.now(MSK).isoformat(timespec="seconds")}, orig

    def snapshot(self):
        self._meta()
        return self._read()[0]

    def _link(self, tid, row):
        return f"https://docs.google.com/spreadsheets/d/{self._config()[0]}/edit#gid={tid}&range=A{row}"

    def publish(self, draft):
        did, ver, plan = _draft(draft)
        ids = self._meta()
        tid, oid = ids[T_TARGET]["sheetId"], ids[T_ORIG]["sheetId"]
        rows = self._values((T_TARGET,), "FORMULA")[0]
        if not rows:
            raise PlannerSheetsError("В тестовом листе нет заголовков")
        cols = _bind(rows[0], ORIGINAL + META, T_TARGET)

        def g(r, n, o=0):
            return _cell(r, cols[_c(n, o)])

        sd = _serial(plan["date"])
        # Markers first (before live-source hash): an exported revision is never written twice.
        mine = [i for i in range(1, len(rows)) if planner._ident(g(rows[i], "ID плана")) == did]
        if mine:
            if any(planner._num(g(rows[i], "Версия плана")) != ver for i in mine):
                raise PublishConflictError("Другая версия этого плана уже выгружена; повторная выгрузка запрещена")
            got = Counter((planner._ident(g(r, "Артикул")), planner._ident(g(r, "Название магазина")),
                           planner._num(g(r, "Количество")), planner._num(g(r, "Дата отгрузки (склад)")),
                           _s(g(r, "Склад Wildberries")), _s(g(r, "Снимок данных"))) for r in (rows[i] for i in mine))
            want = Counter((ln["article"], ln["cabinet"], Fraction(ln["quantity"]), Fraction(sd), plan["warehouse"],
                            plan["source_hash"]) for ln in plan["lines"])
            if got != want:
                raise PublishConflictError("Строки этой версии в тестовом листе неполны или изменены; "
                                           "запись не выполнена, проверьте лист вручную")
            return self._link(tid, mine[0] + 1)

        fresh, orig = self._read()
        if fresh["source_hash"] != plan["source_hash"] or plan["warehouse"] not in fresh["warehouses"]:
            raise SourceChangedError("Исходные данные изменились после расчёта. Пересчитайте план и сохраните новую версию.")
        try:
            new = planner.build_plan(fresh, plan["rules"], plan["date"], plan["warehouse"], plan.get("arrival_date"))
        except ValueError:
            new = None
        if new is None or any(_canon(new[k]) != _canon(plan.get(k))
                              for k in ("lines", "rules", "date", "warehouse", "arrival_date")):
            raise SourceChangedError("Сохранённый план не совпадает с пересчётом по текущим данным. Пересчитайте план.")

        last = len(rows)
        while last > 1 and all(planner._blank(c) for c in rows[last - 1]):
            last -= 1
        if any(isinstance(c, str) and c.startswith("=") and _SPILL.search(c) for r in rows for c in r):
            raise PlannerSheetsError("В тестовом листе есть формулы-массивы; дописывать строки под ними небезопасно")
        oc = _bind(orig[0], ORIGINAL, T_ORIG)
        act = oc[_c(ORIGINAL[0])]
        tpl = max((i for i in range(1, len(orig)) if not planner._blank(_cell(orig[i], act))), default=None)
        n, width, rc = len(plan["lines"]), max(cols.values()) + 1, ids[T_TARGET]["rows"]
        start, end = last, last + n

        def area(c0, c1, sid=tid, r0=start, r1=end):
            return {"sheetId": sid, "startRowIndex": r0, "endRowIndex": r1, "startColumnIndex": c0, "endColumnIndex": c1}

        reqs = [{"appendDimension": {"sheetId": tid, "dimension": "ROWS", "length": end - rc}}] if end > rc else []
        fr = last - 1 if last >= 2 else (1 if rc >= 2 else None)
        if fr is not None:
            reqs += [{"copyPaste": {"source": area(0, width, r0=fr, r1=fr + 1), "destination": area(0, width),
                                    "pasteType": t, "pasteOrientation": "NORMAL"}}
                     for t in ("PASTE_FORMAT", "PASTE_DATA_VALIDATION")]
        arr, rules = _serial(plan.get("arrival_date")), _canon(plan["rules"])

        def cells(ln):
            return {_c("Дата отгрузки (склад)"): _dv(sd), _c("Плановая дата прибытия авто на вб"): _dv(arr),
                    _c("Склад Wildberries"): _sv(plan["warehouse"]), _c("wild"): _sv(ln["wild"]),
                    _c("Артикул"): _sv(ln["article"]), _c("Название магазина"): _sv(ln["cabinet"]),
                    _c("Тип поставки"): _sv("Монопаллета"), _c("Количество"): _nv(ln["quantity"]),
                    _c("Шт в коробе"): _nv(ln["box_qty"]), _c("Шт на паллете"): _nv(ln["pallet_qty"]),
                    _c("Итого паллет"): _nv(ln["pallets"]), _c("Итого коробов"): _nv(ln["quantity"] // ln["box_qty"]),
                    _c("средние продажи в день по артикулу"): _nv(ln["sales7"]),
                    _c("Комментарии"): _sv(f"ТЕСТ. План {did} v{ver}: {ln['reason']}"),
                    _c("ID плана"): _sv(did), _c("Версия плана"): _nv(ver),
                    _c("Прогноз до"): _nv(ln["forecast_before"]), _c("Прогноз после"): _nv(ln["forecast_after"]),
                    _c("Причина"): _sv(ln["reason"]), _c("Снимок данных"): _sv(plan["source_hash"]),
                    _c("Правила плана"): _sv(rules)}

        per = [cells(ln) for ln in plan["lines"]]
        if tpl is not None:  # formula cells only, from the latest actual original row
            d = self._api("GET", "", {"ranges": f"{_rng(T_ORIG)}!{tpl + 1}:{tpl + 1}",
                                      "fields": "sheets(data(rowData(values(userEnteredValue))))"})
            try:
                vals = d["sheets"][0]["data"][0].get("rowData", [])[0].get("values", [])
            except (KeyError, IndexError, TypeError, AttributeError):
                vals = []
            for key, ocol in oc.items():
                f = ((vals[ocol] if ocol < len(vals) else {}).get("userEnteredValue") or {}).get("formulaValue")
                if f and key not in per[0] and not _SPILL.search(f):
                    reqs.append({"copyPaste": {"source": area(ocol, ocol + 1, oid, tpl, tpl + 1),
                                               "destination": area(cols[key], cols[key] + 1),
                                               "pasteType": "PASTE_FORMULA", "pasteOrientation": "NORMAL"}})
        for key in per[0]:
            c = cols[key]
            dated = "userEnteredFormat" in per[0][key]
            reqs.append({"updateCells": {"range": area(c, c + 1), "rows": [{"values": [p[key]]} for p in per],
                                         "fields": "userEnteredValue" + ",userEnteredFormat.numberFormat" * dated}})
        _guard(reqs, tid, oid)
        self._api("POST", ":batchUpdate", body={"requests": reqs})
        return self._link(tid, start + 1)
