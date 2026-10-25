import hashlib
import secrets
import time
from datetime import datetime
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select

from app.journal.config import JournalSettings
from app.journal.domain import (
    SHOT_TYPES,
    ConflictError,
    JournalError,
    SyncError,
    net_pnl,
    now_iso,
    number,
    qty_from_risk,
    risk_amount,
    show_time,
    timestamp,
    validate_trade,
)
from app.journal.models import Account, Audit, Draft, Event, Job, Screenshot, Slot, Trade
from app.journal.store import Store

MENU = [
    [("➕ Открыть сделку", "open", ""), ("📂 Открытые сделки", "list", "0")],
    [("✅ Закрыть сделку", "pickclose", "0"), ("📷 Добавить скрин", "pickshot", "0")],
    [("🔎 Найти сделку", "find", ""), ("📊 Итоги счёта", "summary", "")],
    [("🔄 Статус синхронизации", "sync", ""), ("❓ Помощь", "help", "")],
]
OPEN_STEPS = ["account", "symbol", "side", "opened", "entry", "stop", "leverage",
              "balance", "size_mode", "quantity", "comment", "images", "confirm"]
CLOSE_STEPS = ["closed", "exit", "result_mode", "costs", "comment", "images", "confirm"]
SHOT_STEPS = ["shot_kind", "images", "comment", "confirm"]
SYNC_LABELS = {"pending": "Ожидает", "done": "Синхронизировано", "error": "Ошибка", "conflict": "Конфликт"}


def screen(text, buttons=None):
    return {"text": text, "buttons": [[list(button) for button in row] for row in (buttons or [])],
            "nonce": secrets.token_hex(4)}


def money(value):
    return f"{Decimal(str(value)):,.2f}".replace(",", " ")


class JournalService:
    def __init__(self, store: Store, settings: JournalSettings, google):
        self.store, self.settings, self.google = store, settings, google

    def can_account(self, user, account):
        allowed = self.settings.allowed_accounts(user)
        return allowed is None or account in allowed

    def can_edit(self, user, trade):
        return self.can_account(user, trade.payload["account"]) and (
            user in self.settings.admin_ids or trade.author == user
        )

    async def accounts(self, session, user):
        return [a for a in (await session.scalars(select(Account).order_by(Account.id))).all()
                if self.can_account(user, a.id)]

    async def trade(self, session, user, ident):
        trade = await session.get(Trade, ident)
        if not trade or not self.can_account(user, trade.payload["account"]):
            raise JournalError("Сделка не найдена или недоступна.")
        return trade

    async def card(self, session, trade):
        d = trade.payload
        account = await session.get(Account, d["account"])
        jobs = (await session.scalars(select(Job).where(Job.trade_id == trade.id))).all()
        jobs = [j for j in jobs if j.kind != "trade" or j.payload["version"] == trade.version]
        state = "done"
        for candidate in ("error", "conflict", "pending"):
            if any(j.status == candidate for j in jobs):
                state = candidate
                break
        risk = risk_amount(d)
        lines = [f"{trade.id} · {account.payload['name'] if account else d['account']}",
                 f"{d['symbol']} · {d['side']}",
                 f"Вход: {d['entry']} · {show_time(d['opened'], self.settings.timezone)}",
                 f"Стоп: {d.get('stop') or 'Без стопа'} · Плечо: {d['leverage']}×",
                 f"Количество: {d['quantity']} · Баланс перед входом: ${money(d['balance'])}"]
        if risk is None:
            lines.append("Риск: не задан (нет стопа).")
        else:
            lines.append(f"Риск: ${money(risk)} / {risk / Decimal(d['balance']) * 100:.2f}% без издержек.")
        if d.get("closed"):
            pnl = net_pnl(d)
            hours = (datetime.fromisoformat(d["closed"]) - datetime.fromisoformat(d["opened"])).total_seconds() / 3600
            lines.extend([f"Выход: {d['exit']} · {show_time(d['closed'], self.settings.timezone)}",
                          f"Удержание: {hours:.2f} ч",
                          f"Чистый PnL: ${money(pnl)} / {pnl / Decimal(d['balance']) * 100:.2f}%"])
        else:
            lines.append("Позиция открыта.")
        if d.get("comment"):
            lines.append("Комментарий: " + d["comment"])
        lines.append("Google Sheets: " + SYNC_LABELS[state])
        shots = (await session.scalars(select(Screenshot).where(Screenshot.trade_id == trade.id))).all()
        for shot in shots:
            lines.append(f"Скрин · {shot.kind}: {shot.drive_url or 'ожидает загрузки'}")
        return "\n".join(lines)[:3600]

    async def handle(self, user: int, event_id: str, *, action=None, value="", nonce=None,
                     text=None, image=None):
        if not self.settings.authorized(user):
            return screen("Доступ закрыт. Администратор должен добавить ваш Telegram ID: " + str(user))
        async with self.store.locked() as session:
            previous = await session.get(Event, event_id)
            if previous:
                return previous.response
            draft = await session.get(Draft, user)
            if not draft:
                draft = Draft(user_id=user, payload={"mode": "menu"})
                session.add(draft)
            state = dict(draft.payload)
            try:
                if action and nonce != state.get("screen", {}).get("nonce"):
                    result = screen("Эта кнопка устарела. Продолжите с последней карточки.",
                                    [[("Продолжить", "resume", "")]])
                else:
                    result = await self._handle(session, user, state, action, value, text, image, event_id)
            except JournalError as exc:
                result = await self.render(session, user, state)
                result["text"] = str(exc) + "\n\n" + result["text"]
            state["screen"] = result
            draft.payload = state
            session.add(Event(id=event_id, response=result))
            return result

    async def _handle(self, session, user, state, action, value, text, image, event_id):
        if text in ("/start", "/menu"):
            if state.get("mode") in ("open", "close", "shot"):
                return screen("Есть сохранённый черновик.", [[("Продолжить", "resume", ""),
                              ("Удалить черновик", "cancel", "")]])
            state.clear(); state["mode"] = "menu"
            return screen("Журнал сделок. Выберите действие.", MENU)
        if action == "resume":
            return await self.render(session, user, state)
        if action in ("cancel", "menu"):
            await self._discard_images(session, state)
            state.clear(); state["mode"] = "menu"
            return screen("Журнал сделок.", MENU)
        if action == "help":
            state["mode"] = "menu"
            return screen(
                "После /start используйте кнопки. Даты — время Томска, UTC+7. "
                "Риск — возможный убыток до стопа от баланса перед входом. "
                "Плечо не умножает прибыль повторно. Поддерживаются линейные USD/USDT-позиции.\n"
                "При частичных выходах укажите средневзвешенную цену и чистый PnL платформы. "
                "Скриншоты доступны по закрытым ссылкам Drive; изображение в ячейку можно вставить вручную.\n"
                "Ожидающие операции сохраняются после перезапуска. Формулы Sheets бот не переписывает.\n"
                "Таблица: https://docs.google.com/spreadsheets/d/" + self.settings.spreadsheet_id,
                [[("В меню", "menu", "")]],
            )
        if action == "open":
            if state.get("mode") in ("open", "close", "shot"):
                raise JournalError("Сначала завершите или отмените текущий черновик.")
            state.clear(); state.update(mode="open", index=0, data={}, images=[])
            accounts = await self.accounts(session, user)
            if len(accounts) == 1:
                state["data"] = {"account": accounts[0].id}; state["index"] = 1
            return await self.render(session, user, state)
        if action in ("list", "pickclose", "pickshot"):
            if state.get("mode") in ("open", "close", "shot"):
                raise JournalError("Сначала завершите или отмените текущий черновик.")
            state.update(mode=action, page=int(value or 0))
            return await self.render(session, user, state)
        if action == "find":
            state.update(mode="find")
            return screen("Введите ID сделки, например TR-000001.", [[("В меню", "menu", "")]])
        if state.get("mode") == "find" and text:
            action, value = "view", text.strip().upper()
        if action == "view":
            trade = await self.trade(session, user, value)
            state.update(mode="card", trade_id=trade.id)
            buttons = [[("📷 Добавить скрин", "shot", trade.id)]]
            if self.can_edit(user, trade):
                buttons.append([("Исправить закрытие" if trade.payload.get("closed") else "✅ Закрыть",
                                 "close", trade.id)])
            buttons.append([("В меню", "menu", "")])
            return screen(await self.card(session, trade), buttons)
        if action in ("close", "shot"):
            trade = await self.trade(session, user, value)
            if not self.can_edit(user, trade):
                raise JournalError("Изменять чужую сделку может только администратор.")
            state.clear(); state.update(mode=action, index=0, trade_id=trade.id,
                                        version=trade.version, data=dict(trade.payload), images=[])
            if action == "close":
                for key in ("closed", "exit", "actual", "costs"):
                    state["data"].pop(key, None)
            return await self.render(session, user, state)
        if action == "summary":
            state["mode"] = "summary"
            accounts = await self.accounts(session, user)
            return screen("Выберите счёт для итогов Google Sheets.",
                          [[(a.payload["name"], "account_summary", a.id)] for a in accounts]
                          + [[("В меню", "menu", "")]])
        if action == "account_summary":
            if not self.can_account(user, value):
                raise JournalError("Нет доступа к счёту.")
            row = await self.google.summary(value)
            trades = (await session.scalars(select(Trade))).all()
            account_ids = [t.id for t in trades if t.payload["account"] == value]
            jobs = (await session.scalars(select(Job).where(Job.status != "done"))).all()
            pending = sum(j.trade_id in account_ids for j in jobs)
            last = max((t.synced_at or "" for t in trades if t.id in account_ids), default="")
            return screen(
                f"{row[1]} · ID {value}\nНачальный: ${money(row[2])}\n"
                f"Реализованный PnL: ${money(row[4])}\nДоходность: {Decimal(str(row[5])) * 100:.2f}%\n"
                f"Закрыто: {row[7]} · Открыто: {row[8]}\n"
                f"Незавершённых операций: {pending}; {'итоги неполные' if pending else 'очередь обработана'}.\n"
                f"Последняя синхронизация бота: {show_time(last, self.settings.timezone) if last else 'ещё не было'}\n"
                "Это реализованный торговый результат без выводов, пополнений и доли проп-фирмы.",
                [[("В меню", "menu", "")]],
            )
        if action == "sync":
            state["mode"] = "sync"
            jobs = (await session.scalars(select(Job).where(Job.status != "done"))).all()
            allowed = []
            for job in jobs:
                trade = await session.get(Trade, job.trade_id)
                if trade and self.can_account(user, trade.payload["account"]):
                    allowed.append(job)
            lines, buttons = [f"Незавершённых операций: {len(allowed)}"], []
            for job in allowed[:15]:
                lines.append(f"{job.trade_id} · {SYNC_LABELS[job.status]} · {job.error or 'в очереди'}")
                trade = await session.get(Trade, job.trade_id)
                if self.can_edit(user, trade):
                    if job.status in ("error", "pending"):
                        buttons.append([("Повторить " + job.trade_id, "retry", job.id)])
                    elif job.status == "conflict" and job.kind == "trade":
                        buttons.append([("Конфликт " + job.trade_id, "conflict", job.id)])
            return screen("\n".join(lines)[:3500], buttons + [[("В меню", "menu", "")]])
        if action in ("retry", "conflict", "resolve"):
            job = await session.get(Job, value)
            if not job:
                raise JournalError("Операция не найдена.")
            trade = await self.trade(session, user, job.trade_id)
            if not self.can_edit(user, trade):
                raise JournalError("Нет права исправления.")
            if action == "retry":
                if job.status == "conflict":
                    raise JournalError("Сначала разрешите конфликт.")
                job.status, job.next_at, job.attempts, job.error = "pending", 0, 0, None
                return screen("Операция возвращена в очередь.", [[("Статус", "sync", "")]])
            current = await self.google.current_inputs(trade.id)
            if action == "conflict":
                state["conflict_baseline"] = current
                state["conflict_version"] = trade.version
                text_view = "\n".join(f"Колонка {int(k)+1}: {str(v)[:100]}" for k,v in current.items())
                return screen("Ручные значения Google Sheets:\n" + text_view +
                              "\n\nПерезаписать их подтверждёнными данными бота? "
                              "Для сохранения ручных правок отмените и исправьте карточку бота.",
                              [[("Подтверждаю замену данными бота", "resolve", job.id)],
                               [("Отмена — сохранить ручные правки", "sync", "")]])
            if current != state.get("conflict_baseline") or trade.version != state.get("conflict_version"):
                raise JournalError("Данные снова изменились. Повторно откройте конфликт.")
            trade.baseline = current
            job.status, job.next_at, job.attempts, job.error = "pending", 0, 0, None
            session.add(Audit(id=uuid4().hex, user_id=user, action="resolve_conflict", trade_id=trade.id,
                              at=now_iso(), payload={"replaced": current, "version": trade.version}))
            return screen("Замена подтверждена, поставлена в очередь.", [[("Статус", "sync", "")]])
        if state.get("mode") not in ("open", "close", "shot"):
            return screen("Выберите действие.", MENU)
        if action == "back":
            state["index"] = max(0, state["index"] - 1)
            return await self.render(session, user, state)
        if action == "edit":
            state["index"] = 0
            return await self.render(session, user, state)
        if action == "manual_qty" and self.step(state) == "quantity":
            state["data"] = dict(state["data"]) | {"size_mode": "quantity"}
            state.pop("calculated_qty", None)
            return await self.render(session, user, state)
        if action == "save":
            if self.step(state) != "confirm":
                raise JournalError("Сначала завершите заполнение.")
            return await self.save(session, user, state)
        if image:
            if self.step(state) != "images":
                raise JournalError("Для изображения перейдите к шагу скриншотов.")
            shot_id = hashlib.sha256(event_id.encode()).hexdigest()[:32]
            if len(state.get("images", [])) >= 20:
                raise JournalError("До 20 изображений за одно подтверждение.")
            if not await session.get(Screenshot, shot_id):
                session.add(Screenshot(id=shot_id, trade_id="", telegram_file=image["file_id"],
                                      filename=image["filename"], mime=image["mime"],
                                      content=image["content"], kind="Вход", captured=now_iso()))
            state["images"] = [*state.get("images", []), shot_id]
            return await self.render(session, user, state)
        step = self.step(state)
        if step == "images" and action in ("next", "skip"):
            state["index"] += 1
            return await self.render(session, user, state)
        await self.consume(session, user, state, action, value, text)
        return await self.render(session, user, state)

    def steps(self, state):
        if state["mode"] == "open":
            return OPEN_STEPS
        if state["mode"] == "shot":
            return SHOT_STEPS
        steps = list(CLOSE_STEPS)
        if state.get("data", {}).get("result_mode") == "actual":
            steps[3] = "actual"
        return steps

    def step(self, state):
        return self.steps(state)[state["index"]]

    async def consume(self, session, user, state, action, value, text):
        step, data = self.step(state), dict(state["data"])
        val = value if action in ("value", "accept_stop") else text
        if step == "account":
            if action != "value" or not self.can_account(user, value) or not await session.get(Account, value):
                raise JournalError("Выберите доступный счёт кнопкой.")
            data[step] = value
        elif step == "symbol":
            symbol = (text or "").replace(" ", "").upper()
            if not symbol or len(symbol) > 40 or not all(c.isalnum() or c in "/_-:." for c in symbol):
                raise JournalError("Введите инструмент, например BTCUSDT.")
            data[step] = symbol
        elif step in ("side", "result_mode", "size_mode", "shot_kind"):
            allowed = {"side": ["Long", "Short"], "result_mode": ["calculate", "actual"],
                       "size_mode": ["quantity", "risk"], "shot_kind": SHOT_TYPES}[step]
            if action != "value" or value not in allowed:
                raise JournalError("Выберите вариант кнопкой.")
            if step == "size_mode" and value == "risk":
                qty_from_risk(data, Decimal(1))  # validates stop distance
            data[step] = value
        elif step in ("opened", "closed"):
            if action == "value" and value == "manual":
                raise JournalError("Введите дату: 25.10.2026 14:30, время Томска.")
            date = now_iso() if action == "value" and value == "now" else timestamp(text or "", self.settings.timezone)
            if step == "closed" and datetime.fromisoformat(date) < datetime.fromisoformat(data["opened"]):
                raise JournalError("Закрытие не может быть раньше открытия.")
            data[step] = date
        elif step == "comment":
            if action == "skip":
                data.setdefault("comment", "")
            else:
                if text is None or len(text) > 1000:
                    raise JournalError("Комментарий: текст до 1000 символов.")
                data["comment"] = text
        elif step == "stop" and action == "skip":
            data["stop"] = None
        elif step in ("entry", "stop", "leverage", "balance", "quantity", "exit", "costs", "actual"):
            if step == "balance" and action == "value" and value == "suggested":
                val = state["suggested_balance"]
                parsed = Decimal(val)
            elif step == "leverage" and action == "value" and value == "other":
                raise JournalError("Введите плечо числом, например 5.")
            elif step == "quantity" and action == "value" and value == "accept_qty":
                if not state.get("calculated_qty"):
                    raise JournalError("Сначала рассчитайте количество.")
                parsed = Decimal(state["calculated_qty"])
            else:
                parsed = number(val or "", positive=step not in ("costs", "actual"))
            if step == "stop":
                unusual = ((data["side"] == "Long" and parsed > Decimal(data["entry"])) or
                           (data["side"] == "Short" and parsed < Decimal(data["entry"])))
                if unusual and action != "accept_stop":
                    state["unusual_stop"] = str(parsed)
                    raise JournalError("Стоп находится с прибыльной стороны входа. Подтвердите или введите другой.")
            if step == "quantity" and data.get("size_mode") == "risk":
                if action == "value" and value == "accept_qty":
                    parsed = Decimal(state["calculated_qty"])
                else:
                    calculated = qty_from_risk(data, parsed)
                    state["calculated_qty"] = str(calculated)
                    raise JournalError(f"Расчётное количество: {calculated}. Без издержек. Подтвердите или измените.")
            data[step] = str(parsed)
            if step == "actual":
                data["costs"] = None
            elif step == "costs":
                data["actual"] = None
        else:
            raise JournalError("Используйте кнопки текущего шага.")
        if step in ("entry", "stop", "balance", "size_mode"):
            state.pop("calculated_qty", None)
        if step == "stop":
            state.pop("unusual_stop", None)
        state["data"] = data
        state["index"] += 1

    async def render(self, session, user, state):
        mode = state.get("mode", "menu")
        if mode in ("list", "pickclose", "pickshot"):
            items = (await session.scalars(select(Trade).order_by(Trade.id.desc()))).all()
            items = [t for t in items if self.can_account(user, t.payload["account"])
                     and (mode == "pickshot" or not t.payload.get("closed"))]
            page = max(0, min(state.get("page", 0), max(0, (len(items) - 1) // 6)))
            action = {"list": "view", "pickclose": "close", "pickshot": "shot"}[mode]
            buttons = [[(f"{t.id} · {t.payload['account']} · {t.payload['symbol']} {t.payload['side']}", action, t.id)]
                       for t in items[page * 6:page * 6 + 6]]
            if page:
                buttons.append([("←", mode, str(page-1))])
            if len(items) > (page+1)*6:
                buttons.append([("→", mode, str(page+1))])
            return screen(f"Сделки · {len(items)}. Страница {page+1}.", buttons + [[("В меню", "menu", "")]])
        if mode not in ("open", "close", "shot"):
            return screen("Журнал сделок.", MENU)
        step, data = self.step(state), state["data"]
        buttons = []
        labels = {
            "symbol": "Введите инструмент, например BTCUSDT.",
            "entry": "Введите цену входа.", "stop": "Введите цену стопа или выберите «Без стопа».",
            "side": "Направление сделки?", "leverage": "Выберите плечо или введите своё.",
            "opened": "Когда открыли? Время Томска, UTC+7.",
            "closed": "Когда закрыли? Время Томска, UTC+7.",
            "exit": "Введите цену выхода. При частичных выходах — средневзвешенную.",
            "size_mode": "Как указать размер позиции?", "quantity": "Введите количество монет.",
            "costs": "Издержки: комиссии + финансирование. Расход положительный; доход финансирования отрицательный.",
            "actual": "Введите фактический чистый PnL платформы со знаком. Уже после издержек; 0 допустим.",
            "result_mode": "Как определить результат сделки?", "comment": "Комментарий, до 1000 символов (необязательно).",
            "images": f"Пришлите скриншоты как фото или файлы. Добавлено: {len(state.get('images', []))}. Затем нажмите «Готово».",
            "shot_kind": "Какой тип скриншота?",
        }
        text = labels.get(step, "")
        if step == "account":
            text = "Выберите счёт."
            buttons = [[(f"{a.payload['name']} · ${money(a.payload['initial'])}", "value", a.id)]
                       for a in await self.accounts(session, user)]
        elif step == "side":
            buttons = [[("Long", "value", "Long"), ("Short", "value", "Short")]]
        elif step in ("opened", "closed"):
            buttons = [[("Сейчас", "value", "now"), ("Вручную", "value", "manual")]]
        elif step == "stop":
            buttons = [[("Без стопа", "skip", "")]]
            if state.get("unusual_stop"):
                buttons.append([("Подтвердить стоп " + state["unusual_stop"], "accept_stop", state["unusual_stop"])])
        elif step == "leverage":
            buttons = [[(f"{v}×", "value", str(v)) for v in (1, 2, 5)],
                       [(f"{v}×", "value", str(v)) for v in (10, 20)], [("Другое", "value", "other")]]
        elif step == "balance":
            trades = (await session.scalars(select(Trade).order_by(Trade.id.desc()))).all()
            previous = next((t for t in trades if t.payload["account"] == data["account"]), None)
            account = await session.get(Account, data["account"])
            suggested = previous.payload["balance"] if previous else account.payload["initial"]
            state["suggested_balance"] = suggested
            text = f"Баланс перед входом: ${money(suggested)}? Источник: " + (
                "последний введённый снимок; это не текущий баланс платформы." if previous else "начальный баланс счёта.")
            buttons = [[("Верно", "value", "suggested")]]
            text += "\nДля изменения введите фактическое число."
        elif step == "size_mode":
            buttons = [[("Количество монет", "value", "quantity")]]
            if data.get("stop") and (
                (data["side"] == "Long" and Decimal(data["stop"]) < Decimal(data["entry"])) or
                (data["side"] == "Short" and Decimal(data["stop"]) > Decimal(data["entry"]))
            ):
                buttons.append([("Рассчитать по риску", "value", "risk")])
        elif step == "quantity" and data.get("size_mode") == "risk":
            text = "Введите риск в процентах баланса, например 1."
            if state.get("calculated_qty"):
                text += "\nКоличество: " + state["calculated_qty"] + " (без издержек)."
                buttons = [[("Подтвердить количество", "value", "accept_qty")],
                           [("Ввести количество вручную", "manual_qty", "")]]
        elif step == "result_mode":
            buttons = [[("Рассчитать по ценам", "value", "calculate")],
                       [("Чистый PnL платформы", "value", "actual")]]
        elif step == "costs":
            buttons = [[("0 — нет издержек", "value", "0")]]
        elif step == "comment":
            buttons = [[("Пропустить", "skip", "")]]
        elif step == "images":
            buttons = [[("Готово", "next", ""), ("Пропустить", "skip", "")]]
        elif step == "shot_kind":
            buttons = [[(kind, "value", kind)] for kind in SHOT_TYPES]
        elif step == "confirm":
            if mode != "shot":
                validate_trade(data)
            if mode == "shot":
                text = f"{state['trade_id']} · {data['shot_kind']} · скриншотов: {len(state['images'])}"
            else:
                temp = Trade(id=state.get("trade_id", "Новая сделка"), author=user, payload=data)
                # The card formatter queries sync state; a new trade has no jobs yet.
                text = await self.card(session, temp)
                text = text.replace("Google Sheets: Синхронизировано", "Предпросмотр — ещё не сохранено")
                text += f"\nНовых скриншотов: {len(state['images'])}\nПроверьте перед сохранением."
            buttons = [[("Сохранить", "save", ""), ("Изменить", "edit", "")]]
        return screen(text, buttons + [[("⬅ Назад", "back", ""), ("Отмена", "cancel", "")]])

    async def _discard_images(self, session, state):
        for ident in state.get("images", []):
            shot = await session.get(Screenshot, ident)
            if shot and not shot.trade_id:
                await session.delete(shot)

    async def save(self, session, user, state):
        data = dict(state["data"])
        mode = state["mode"]
        if mode == "open":
            validate_trade(data)
            reserved = set((await session.scalars(select(Trade.id))).all())
            slots = (await session.scalars(select(Slot).where(Slot.blocked.is_(False)).order_by(Slot.id))).all()
            slot = next((s for s in slots if s.id not in reserved), None)
            if slot is None:
                raise JournalError("Свободных подготовленных ID нет. Администратор должен расширить шаблон.")
            trade = Trade(id=slot.id, author=user, payload=data, version=1, synced_version=0,
                          baseline=slot.baseline)
            session.add(trade)
        else:
            trade = await self.trade(session, user, state["trade_id"])
            if not self.can_edit(user, trade):
                raise JournalError("Нет права изменения этой сделки.")
            if trade.version != state["version"]:
                raise JournalError("Сделка изменена другим пользователем. Отмените черновик и откройте заново.")
            if mode == "close":
                validate_trade(data)
                trade.payload = data
                trade.version += 1
        if mode != "shot":
            enqueue_trade(session, trade)
        if mode == "shot" and not state["images"]:
            raise JournalError("Добавьте хотя бы один скриншот.")
        for index, ident in enumerate(state["images"], 1):
            shot = await session.get(Screenshot, ident)
            shot.trade_id = trade.id
            shot.kind = data.get("shot_kind", "Закрытие" if mode == "close" else "Вход")
            shot.comment = data.get("comment", "")
            extension = shot.filename.rsplit(".", 1)[-1]
            stamp = datetime.fromisoformat(shot.captured).strftime("%Y%m%d_%H%M%S")
            shot.filename = f"{trade.id}_{shot.kind}_{stamp}_{index:02}_{shot.id[:6]}.{extension}"
            session.add(Job(id="shot:" + shot.id, trade_id=trade.id, kind="shot", payload={"id": shot.id}))
        session.add(Audit(id=uuid4().hex, user_id=user, action=mode, trade_id=trade.id,
                          at=now_iso(), payload={"version": trade.version, "data": data}))
        ident = trade.id
        state.clear(); state.update(mode="menu")
        return screen(f"{ident}: сохранено в БД бота, ожидает синхронизации с Google Sheets.",
                      [[("Карточка", "view", ident), ("Добавить скрин", "shot", ident)],
                       [("В меню", "menu", "")]])


def enqueue_trade(session, trade):
    session.add(Job(id=f"trade:{trade.id}:{trade.version}", trade_id=trade.id, kind="trade",
                    payload={"version": trade.version}))


class SyncWorker:
    def __init__(self, service: JournalService):
        self.service = service

    async def once(self):
        # Small team: one DB mutex serializes writes across bot and worker processes.
        # Bounded HTTP timeouts cap lock duration. All job stages commit separately.
        async with self.service.store.locked() as session:
            job = await session.scalar(select(Job).where(Job.status == "pending", Job.next_at <= time.time())
                                       .order_by(Job.kind.desc(), Job.id).limit(1))
            if job is None:
                return False
            trade = await session.get(Trade, job.trade_id)
            try:
                if job.kind == "trade":
                    if job.payload["version"] != trade.version:
                        job.status = "done"
                        return True
                    baseline = await self.service.google.sync_trade(trade.id, trade.payload, trade.baseline)
                    trade.baseline = baseline
                    trade.synced_version = trade.version
                    trade.synced_at = now_iso()
                    job.status = "done"
                    older = (await session.scalars(select(Job).where(
                        Job.trade_id == trade.id, Job.kind == "trade", Job.id != job.id
                    ))).all()
                    for previous in older:
                        if previous.payload["version"] < trade.version:
                            previous.status = "done"
                else:
                    shot = await session.get(Screenshot, job.payload["id"])
                    if trade.synced_version == 0:
                        job.next_at = time.time() + 2
                        return True
                    if not shot.drive_id:
                        shot.drive_id = await self.service.google.reserve_file_id()
                        return True  # commit generated ID BEFORE uploading bytes
                    if shot.sheet_row is None:
                        occupied = set((await session.scalars(select(Screenshot.sheet_row)
                                                              .where(Screenshot.sheet_row.is_not(None)))).all())
                        shot.sheet_row = await self.service.google.reserve_screenshot_row(occupied)
                        return True
                    if not shot.drive_url:
                        shot.drive_url = await self.service.google.upload(shot)
                        return True  # commit upload result BEFORE writing sheet
                    await self.service.google.sync_screenshot(shot)
                    job.status = "done"
                    shot.content = None  # original is now in private Drive
                    if not trade.payload.get("shot_url"):
                        trade.payload = dict(trade.payload) | {"shot_url": shot.drive_url}
                        trade.version += 1
                        enqueue_trade(session, trade)
            except ConflictError as exc:
                job.status, job.error = "conflict", str(exc)
            except SyncError as exc:
                job.attempts += 1
                job.error = str(exc)
                if exc.retryable and job.attempts < self.service.settings.max_attempts:
                    job.next_at = time.time() + min(300, 2 ** job.attempts)
                else:
                    job.status = "error"
            return True
