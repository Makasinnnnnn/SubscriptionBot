"""Small authenticated REST adapter. Never changes sharing or sheet formatting."""

import asyncio
import json
import re
from urllib.parse import quote

import httpx

from app.journal.config import JournalSettings
from app.journal.domain import (
    ACCOUNT_HEADERS,
    FORMULA_COLUMNS,
    INPUT_COLUMNS,
    SHOT_HEADERS,
    TRADE_HEADERS,
    ConflictError,
    SyncError,
    cell_values_equal,
    date_serial,
    sheet_inputs,
)

SCOPES = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]


def a1(title: str, cells: str) -> str:
    return "'" + title.replace("'", "''") + "'!" + cells


def filled(row: list, size: int) -> list:
    return (row + [""] * size)[:size]


class GoogleJournal:
    def __init__(self, settings: JournalSettings, client=None, credentials=None):
        self.settings = settings
        self.client = client or httpx.AsyncClient(timeout=20)
        self.credentials = credentials
        self.refresh_lock = asyncio.Lock()
        self.sheet_ids: dict[str, int] = {}

    async def _token(self):
        async with self.refresh_lock:
            try:
                if self.credentials is None:
                    info = json.loads(self.settings.google_credentials.read_text(encoding="utf-8"))
                    if info.get("type") in (None, "authorized_user") and info.get("refresh_token"):
                        # InstalledAppFlow Credentials.to_json() omits the ADC type field.
                        from google.oauth2.credentials import Credentials

                        self.credentials = Credentials.from_authorized_user_info(info, scopes=SCOPES)
                    else:
                        import google.auth

                        self.credentials, _ = google.auth.load_credentials_from_file(
                            str(self.settings.google_credentials), scopes=SCOPES
                        )
                if not self.credentials.valid:
                    from google.auth.transport.requests import Request

                    await asyncio.to_thread(self.credentials.refresh, Request())
                return self.credentials.token
            except Exception as exc:
                raise SyncError(
                    "Нет рабочей Google-авторизации. Проверьте JOURNAL_GOOGLE_CREDENTIALS "
                    "и выполните python -m app.journal.authorize."
                ) from exc

    async def request(self, method: str, url: str, *, missing_ok=False, **kwargs):
        token = await self._token()
        headers = kwargs.pop("headers", {}) | {"Authorization": f"Bearer {token}"}
        try:
            response = await self.client.request(method, url, headers=headers, **kwargs)
        except httpx.TransportError as exc:
            raise SyncError("Google временно недоступен: повтор будет безопасным.", True) from exc
        if missing_ok and response.status_code == 404:
            return None
        if response.status_code >= 400:
            # Do not leak response bodies, tokens, Drive filenames or request payloads.
            retryable = response.status_code == 429 or response.status_code >= 500
            if response.status_code == 403:
                raise SyncError(
                    "Google отказал в доступе (403). Проверьте права, включённые API и "
                    "квоту; service account не может владеть файлами в личном My Drive."
                )
            raise SyncError(f"Google API: HTTP {response.status_code}.", retryable)
        return response.json() if response.content else {}

    @property
    def base(self):
        return f"https://sheets.googleapis.com/v4/spreadsheets/{self.settings.spreadsheet_id}"

    async def read(self, title: str, cells: str, render="FORMULA") -> list[list]:
        data = await self.request(
            "GET", self.base + "/values/" + quote(a1(title, cells), safe=""),
            params={"valueRenderOption": render, "dateTimeRenderOption": "SERIAL_NUMBER"},
        )
        return data.get("values", [])

    async def preflight(self):
        meta = await self.request(
            "GET", self.base,
            params={"fields": "properties(timeZone),sheets(properties,tables)"},
        )
        if meta["properties"].get("timeZone") != self.settings.timezone:
            raise SyncError("Часовой пояс Google Sheets отличается от настроенного; запись остановлена.")
        sheets = {s["properties"]["title"]: s for s in meta.get("sheets", [])}
        required = [(self.settings.trades_sheet, TRADE_HEADERS, 305),
                    (self.settings.screenshots_sheet, SHOT_HEADERS, 305),
                    (self.settings.accounts_sheet, ACCOUNT_HEADERS, 105)]
        for title, headers, end in required:
            if title not in sheets:
                raise SyncError(f"Не найдена вкладка {title}.")
            sheet = sheets[title]
            self.sheet_ids[title] = sheet["properties"]["sheetId"]
            # Native table coverage is distinct from the grid's row count.
            if not any(t["range"].get("startRowIndex", 0) == 4
                       and t["range"].get("endRowIndex", 0) >= end
                       and t["range"].get("startColumnIndex", 0) == 0
                       and t["range"].get("endColumnIndex", 0) >= len(headers)
                       for t in sheet.get("tables", [])):
                raise SyncError(f"Нативная таблица {title} не покрывает подготовленный диапазон.")
        trade_rows = await self.read(self.settings.trades_sheet, "A5:Z305")
        shot_rows = await self.read(self.settings.screenshots_sheet, "A5:F305")
        account_rows = await self.read(self.settings.accounts_sheet, "A5:J105", "UNFORMATTED_VALUE")
        for title, rows, headers in [(self.settings.trades_sheet, trade_rows, TRADE_HEADERS),
                                     (self.settings.screenshots_sheet, shot_rows, SHOT_HEADERS),
                                     (self.settings.accounts_sheet, account_rows, ACCOUNT_HEADERS)]:
            if not rows or rows[0] != headers:
                raise SyncError(f"Изменены заголовки {title}; проверьте схему перед записью.")
        slots, ids = [], set()
        for row_number, raw in enumerate(trade_rows[1:], 6):
            row = filled(raw, 26)
            trade_id = str(row[0])
            if not re.fullmatch(r"TR-\d{6}", trade_id) or trade_id in ids:
                raise SyncError(f"Неправильный или повторный ID в строке {row_number}.")
            ids.add(trade_id)
            for col in FORMULA_COLUMNS:
                if not isinstance(row[col], str) or not row[col].startswith("="):
                    raise SyncError(f"Нет ожидаемой формулы: {self.settings.trades_sheet}, строка {row_number}, колонка {col + 1}.")
            baseline = {str(col): row[col] for col in INPUT_COLUMNS}
            slots.append({"id": trade_id, "blocked": any(v not in (None, "") for v in baseline.values()),
                          "baseline": baseline})
        if len(slots) != 300:
            raise SyncError("Ожидаются 300 подготовленных ID с формулами. Расширьте шаблон отдельно.")
        accounts = []
        for raw in account_rows[1:]:
            row = filled(raw, 10)
            if row[0] in (None, ""):
                continue
            ident = str(row[0])
            if any(a["id"] == ident for a in accounts):
                raise SyncError("Повторный ID счёта.")
            if row[3] not in ("USD", "USDT") or not isinstance(row[2], (int, float)) or row[2] <= 0:
                raise SyncError(f"Неверные исходные параметры счёта {ident}.")
            accounts.append({"id": ident, "name": str(row[1]), "initial": str(row[2]),
                             "currency": row[3]})
        if not accounts:
            raise SyncError("Не найдено ни одного корректного счёта.")
        return accounts, slots

    async def update_cells(self, title: str, row: int, values: dict[str, object]):
        requests = []
        for col, value in values.items():
            # stringValue is literal text, including strings beginning with '='.
            typed = ({"numberValue": value} if isinstance(value, (int, float))
                     else {"stringValue": str(value or "")})
            requests.append({"updateCells": {
                "range": {"sheetId": self.sheet_ids[title], "startRowIndex": row - 1,
                          "endRowIndex": row, "startColumnIndex": int(col),
                          "endColumnIndex": int(col) + 1},
                "rows": [{"values": [{"userEnteredValue": typed}]}],
                "fields": "userEnteredValue",
            }})
        await self.request("POST", self.base + ":batchUpdate", json={"requests": requests})

    async def sync_trade(self, trade_id: str, payload: dict, baseline: dict):
        # Revalidate before every mutation; templates may change after process startup.
        _, slots = await self.preflight()
        if not any(s["id"] == trade_id for s in slots):
            raise ConflictError("ID сделки исчез из таблицы.")
        rows = await self.read(self.settings.trades_sheet, "A6:Z305")
        matches = [(i + 6, filled(r, 26)) for i, r in enumerate(rows) if r and str(r[0]) == trade_id]
        if len(matches) != 1:
            raise ConflictError("ID сделки отсутствует или продублирован.")
        row_number, row = matches[0]
        current = {str(c): row[c] for c in INPUT_COLUMNS}
        desired = sheet_inputs(payload, self.settings.timezone)
        if all(cell_values_equal(current[k], desired[k]) for k in desired):
            return desired  # retry after server committed but response was lost
        if not all(cell_values_equal(current[k], baseline.get(k, "")) for k in desired):
            raise ConflictError("В Google Sheets есть ручные изменения. Требуется разрешение конфликта.")
        await self.update_cells(self.settings.trades_sheet, row_number, desired)
        # Find ID again: row numbers can change after sorting.
        verified = await self.read(self.settings.trades_sheet, "A6:Z305")
        rows = [filled(r, 26) for r in verified if r and str(r[0]) == trade_id]
        if len(rows) != 1 or not all(cell_values_equal(rows[0][int(k)], v) for k, v in desired.items()):
            raise ConflictError("После записи данные отличаются: проверьте сортировку или ручные правки.")
        return desired

    async def current_inputs(self, trade_id: str):
        await self.preflight()
        rows = await self.read(self.settings.trades_sheet, "A6:Z305")
        rows = [filled(r, 26) for r in rows if r and str(r[0]) == trade_id]
        if len(rows) != 1:
            raise ConflictError("Сначала восстановите уникальный ID в таблице.")
        return {str(c): rows[0][c] for c in INPUT_COLUMNS}

    async def reserve_file_id(self):
        data = await self.request("GET", "https://www.googleapis.com/drive/v3/files/generateIds",
                                  params={"count": 1, "space": "drive", "type": "files"})
        return data["ids"][0]

    async def upload(self, screenshot):
        url = "https://www.googleapis.com/drive/v3/files/" + screenshot.drive_id
        existing = await self.request("GET", url, missing_ok=True,
                                      params={"fields": "id,webViewLink,appProperties", "supportsAllDrives": "true"})
        if existing is not None:
            if existing.get("appProperties", {}).get("journalScreenshot") != screenshot.id:
                raise ConflictError("Drive ID занят другим файлом.")
            return existing.get("webViewLink") or f"https://drive.google.com/file/d/{screenshot.drive_id}/view"
        metadata = {"id": screenshot.drive_id, "name": screenshot.filename,
                    "parents": [self.settings.screenshots_folder_id],
                    "appProperties": {"journalScreenshot": screenshot.id}}
        boundary = "journal_upload_boundary"
        body = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n"
                + json.dumps(metadata, ensure_ascii=False)
                + f"\r\n--{boundary}\r\nContent-Type: {screenshot.mime}\r\n\r\n").encode()
        body += screenshot.content + f"\r\n--{boundary}--\r\n".encode()
        result = await self.request(
            "POST", "https://www.googleapis.com/upload/drive/v3/files",
            params={"uploadType": "multipart", "supportsAllDrives": "true", "fields": "id,webViewLink"},
            headers={"Content-Type": f"multipart/related; boundary={boundary}"}, content=body,
        )
        return result.get("webViewLink") or f"https://drive.google.com/file/d/{screenshot.drive_id}/view"

    async def reserve_screenshot_row(self, occupied: set[int]):
        await self.preflight()
        rows = await self.read(self.settings.screenshots_sheet, "A6:F305")
        for i in range(300):
            number = i + 6
            if number not in occupied and not any(v not in (None, "") for v in (rows[i] if i < len(rows) else [])):
                return number
        raise SyncError("Все 300 строк скриншотов заняты. Расширьте журнал отдельно.")

    async def sync_screenshot(self, shot):
        await self.preflight()
        values = {"0": shot.trade_id, "1": shot.kind,
                  "2": date_serial(shot.captured, self.settings.timezone),
                  "3": shot.drive_url, "5": shot.comment}
        all_rows = await self.read(self.settings.screenshots_sheet, "A6:F305")
        existing = [(i + 6, filled(r, 6)) for i, r in enumerate(all_rows)
                    if len(r) > 3 and r[3] == shot.drive_url]
        if len(existing) > 1:
            raise ConflictError("Ссылка скриншота дублирована в таблице.")
        if existing:
            shot.sheet_row, row = existing[0]
        else:
            raw = all_rows[shot.sheet_row - 6] if shot.sheet_row - 6 < len(all_rows) else []
            row = filled(raw, 6)
        if all(cell_values_equal(row[int(k)], v) for k, v in values.items()):
            return
        # If another user inserted a record in our reserved row, do not overwrite it.
        if any(v not in (None, "") for v in row):
            raise ConflictError("Строка скриншота занята ручными данными. Нужна проверка администратора.")
        await self.update_cells(self.settings.screenshots_sheet, shot.sheet_row, values)
        rows = await self.read(self.settings.screenshots_sheet, f"A{shot.sheet_row}:F{shot.sheet_row}")
        row = filled((rows or [[]])[0], 6)
        if not all(cell_values_equal(row[int(k)], v) for k, v in values.items()):
            raise ConflictError("Не удалось подтвердить строку скриншота после записи.")

    async def summary(self, account_id: str):
        rows = await self.read(self.settings.accounts_sheet, "A6:J105", "UNFORMATTED_VALUE")
        rows = [filled(r, 10) for r in rows if r and str(r[0]) == account_id]
        if len(rows) != 1:
            raise SyncError("Счёт не найден или дублирован в Google Sheets.")
        row = rows[0]
        if any(isinstance(v, str) and v.startswith("#") for v in row):
            raise SyncError("В итогах счёта ошибка формулы Google Sheets.")
        return row

    async def close(self):
        await self.client.aclose()
