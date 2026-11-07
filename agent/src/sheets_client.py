"""
Spreadsheet sources.

Two implementations behind one interface, so `DataLayer` does not care where the
rows come from:

    XlsxSource          — local .xlsx via openpyxl (offline dev, fixtures)
    GoogleSheetsSource  — live Google Sheets via a service account

Both return raw cell values; header detection and column mapping stay in
`data_layer`, so a sheet behaves identically whichever source feeds it.

Google-specific detail: dates in Sheets are serial numbers (days since
1899-12-30). We request UNFORMATTED_VALUE (locale-independent numbers/strings)
and convert serials to datetimes in `to_datetime`, instead of trusting the
display format, which varies by spreadsheet locale.
"""

from __future__ import annotations

import json
import re
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from threading import RLock
from pathlib import Path
from typing import Any, Sequence

from . import config


# ─── Errors ─────────────────────────────────────────────────────────────────
class SheetSourceError(RuntimeError):
    """Base error for spreadsheet access."""


class SheetAccessError(SheetSourceError):
    """403/404 — the spreadsheet is not shared with the service account."""


class SheetRateLimitError(SheetSourceError):
    """429 or quota-related 403 after retry exhaustion."""


SHEETS_SCOPE = "https://www.googleapis.com/auth/spreadsheets"
DRIVE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"

_SHEET_ID_RE = re.compile(r"/spreadsheets/d/([A-Za-z0-9_-]{20,})")
_BARE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{20,}$")


def extract_spreadsheet_id(value: str | None) -> str:
    """Accept a full Sheets URL or a bare id and return the id."""
    if not value:
        return ""
    text = str(value).strip()
    match = _SHEET_ID_RE.search(text)
    if match:
        return match.group(1)
    if _BARE_ID_RE.match(text):
        return text
    return text


def quote_sheet_title(title: str) -> str:
    """Sheets A1 notation: wrap in single quotes, doubling inner ones."""
    return "'" + title.replace("'", "''") + "'"


def col_to_letter(col: int) -> str:
    """Convert 1-based column number to A1 letter notation (e.g. 1 -> 'A', 26 -> 'Z', 27 -> 'AA')."""
    if col <= 0:
        return "A"
    result = ""
    while col > 0:
        col, remainder = divmod(col - 1, 26)
        result = chr(65 + remainder) + result
    return result


def serial_to_datetime(value: float) -> datetime | None:
    """
    Convert a Google Sheets date serial to a naive datetime.

    The epoch is 1899-12-30 (Sheets' own convention, which accounts for the
    Lotus 1-2-3 leap-year bug). Out-of-range numbers return None.
    """
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (1 <= number <= 2958465):  # 1899-12-31 .. 9999-12-31
        return None
    try:
        return datetime(1899, 12, 30) + timedelta(days=number)
    except (OverflowError, ValueError):
        return None


def to_datetime(value: Any) -> datetime | None:
    """Best-effort datetime from a cell value (datetime, serial, or string)."""
    from .utils import parse_dt

    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return serial_to_datetime(value)
    return parse_dt(str(value))


def to_text(value: Any) -> str:
    """Cell -> string, without scientific notation for ids/numbers."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d %H:%M:%S")
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, float):
        return str(int(value)) if value.is_integer() else repr(value)
    return str(value).strip()


# ─── Interface ──────────────────────────────────────────────────────────────
class SheetSource:
    name = "base"
    label = ""

    def sheet_names(self) -> list[str]:  # pragma: no cover - interface
        raise NotImplementedError

    def rows(self, sheet_name: str) -> list[list[Any]]:  # pragma: no cover
        raise NotImplementedError

    def append_row(self, sheet_name: str, values: list[Any]) -> None:  # pragma: no cover
        raise NotImplementedError

    def update_cell(self, sheet_name: str, row: int, col: int, value: Any) -> None:  # pragma: no cover
        """1-based row and 1-based col."""
        raise NotImplementedError

    def update_row(self, sheet_name: str, row: int, values: list[Any]) -> None:  # pragma: no cover
        """1-based row, updates starting from column 1."""
        raise NotImplementedError

    def close(self) -> None:
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()


# ─── Local xlsx ─────────────────────────────────────────────────────────────
class XlsxSource(SheetSource):
    name = "xlsx"

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.label = self.path.name
        self._workbook = None

    def _open(self):
        if not self.path.exists():
            raise SheetSourceError(
                f"Dataset not found: {self.path}. "
                "Run `python _refs/build_dummy.py` to generate fixtures."
            )
        from openpyxl import load_workbook

        return load_workbook(self.path, read_only=True, data_only=True)

    @property
    def workbook(self):
        if self._workbook is None:
            self._workbook = self._open()
        return self._workbook

    def sheet_names(self) -> list[str]:
        return list(self.workbook.sheetnames)

    def rows(self, sheet_name: str) -> list[list[Any]]:
        worksheet = self.workbook[sheet_name]
        return [list(row) for row in worksheet.iter_rows(values_only=True)]

    def append_row(self, sheet_name: str, values: list[Any]) -> None:
        from openpyxl import load_workbook

        self.close()
        wb = load_workbook(self.path, read_only=False, data_only=False)
        if sheet_name not in wb.sheetnames:
            wb.close()
            raise SheetSourceError(f"Tab '{sheet_name}' tidak ditemukan di workbook.")
        ws = wb[sheet_name]
        ws.append(values)
        wb.save(self.path)
        wb.close()

    def update_cell(self, sheet_name: str, row: int, col: int, value: Any) -> None:
        from openpyxl import load_workbook

        self.close()
        wb = load_workbook(self.path, read_only=False, data_only=False)
        if sheet_name not in wb.sheetnames:
            wb.close()
            raise SheetSourceError(f"Tab '{sheet_name}' tidak ditemukan di workbook.")
        ws = wb[sheet_name]
        ws.cell(row=row, column=col, value=value)
        wb.save(self.path)
        wb.close()

    def update_row(self, sheet_name: str, row: int, values: list[Any]) -> None:
        from openpyxl import load_workbook

        self.close()
        wb = load_workbook(self.path, read_only=False, data_only=False)
        if sheet_name not in wb.sheetnames:
            wb.close()
            raise SheetSourceError(f"Tab '{sheet_name}' tidak ditemukan di workbook.")
        ws = wb[sheet_name]
        for col_idx, val in enumerate(values, start=1):
            ws.cell(row=row, column=col_idx, value=val)
        wb.save(self.path)
        wb.close()

    def close(self) -> None:
        if self._workbook is not None:
            self._workbook.close()
            self._workbook = None


GOOGLE_SHEET_MIME = "application/vnd.google-apps.spreadsheet"
OFFICE_XLSX_MIMES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.ms-excel",
}


class GoogleDriveWorkbookSource(XlsxSource):
    """
    A .xlsx stored in Drive (uploaded, not converted to a Google Sheet).

    The Sheets API rejects Office files, so instead of asking the user to
    convert, we download the bytes with drive.readonly and parse them with
    openpyxl. Same read-only guarantees, same tab semantics.
    """

    name = "drive-xlsx"

    def __init__(
        self,
        file_id: str,
        credentials_path: Path | str | None = None,
        service=None,
        file_name: str = "",
    ) -> None:
        self.file_id = extract_spreadsheet_id(file_id)
        self.credentials_path = Path(credentials_path or config.SERVICE_ACCOUNT_PATH)
        self._service = service
        self.path = None  # not a local file
        self.label = file_name or f"drive:{self.file_id[:10]}…"
        self._workbook = None

    @property
    def service(self):
        if self._service is None:
            try:
                from google.oauth2 import service_account
                from googleapiclient.discovery import build
            except ImportError as exc:  # pragma: no cover
                raise SheetSourceError(
                    "google-api-python-client belum terinstall."
                ) from exc
            if not self.credentials_path.exists():
                raise SheetSourceError(
                    f"Service account tidak ditemukan: {self.credentials_path}"
                )
            credentials = service_account.Credentials.from_service_account_file(
                str(self.credentials_path), scopes=[DRIVE_SCOPE]
            )
            self._service = build(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )
        return self._service

    def _download(self) -> bytes:
        from googleapiclient.errors import HttpError

        try:
            return self.service.files().get_media(fileId=self.file_id).execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            if status in (403, 404):
                raise SheetAccessError(
                    f"{status} — file {self.file_id[:10]}… tidak bisa diunduh. "
                    f"Share ke {_service_account_email(self.credentials_path)} (Viewer)."
                ) from exc
            raise SheetSourceError(f"Drive API {status}: {exc}") from exc

    def _open(self):
        from io import BytesIO

        from openpyxl import load_workbook

        data = self._download()
        if not data:
            raise SheetSourceError(
                f"File {self.file_id[:10]}… kosong atau tidak bisa diunduh."
            )
        return load_workbook(BytesIO(data), read_only=True, data_only=True)

    def append_row(self, sheet_name: str, values: list[Any]) -> None:
        raise SheetSourceError(
            "Tidak bisa menulis langsung ke file .xlsx di Google Drive. "
            "Konversi file ke Google Sheet: File -> Save as Google Sheets."
        )

    def update_cell(self, sheet_name: str, row: int, col: int, value: Any) -> None:
        raise SheetSourceError(
            "Tidak bisa menulis langsung ke file .xlsx di Google Drive. "
            "Konversi file ke Google Sheet: File -> Save as Google Sheets."
        )

    def update_row(self, sheet_name: str, row: int, values: list[Any]) -> None:
        raise SheetSourceError(
            "Tidak bisa menulis langsung ke file .xlsx di Google Drive. "
            "Konversi file ke Google Sheet: File -> Save as Google Sheets."
        )


def _service_account_email(credentials_path: Path) -> str:
    try:
        data = json.loads(Path(credentials_path).read_text(encoding="utf-8"))
        return str(data.get("client_email", ""))
    except (OSError, json.JSONDecodeError):
        return "<service-account-email>"


def spreadsheet_metadata(
    spreadsheet_id: str,
    credentials_path: Path | str | None = None,
    service=None,
) -> dict:
    """Return {id, name, mimeType} for a Drive file id."""
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise SheetSourceError("google-api-python-client belum terinstall.") from exc

    if service is None:
        path = Path(credentials_path or config.SERVICE_ACCOUNT_PATH)
        if not path.exists():
            raise SheetSourceError(f"Service account tidak ditemukan: {path}")
        credentials = service_account.Credentials.from_service_account_file(
            str(path), scopes=[DRIVE_SCOPE]
        )
        service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    file_id = extract_spreadsheet_id(spreadsheet_id)
    try:
        return service.files().get(fileId=file_id, fields="id,name,mimeType").execute()
    except Exception as exc:
        status = getattr(getattr(exc, "resp", None), "status", 0)
        if status in (403, 404):
            raise SheetAccessError(
                f"{status} — spreadsheet {file_id[:10]}… tidak bisa diakses. "
                f"Share ke {_service_account_email(Path(credentials_path or config.SERVICE_ACCOUNT_PATH))} (Viewer)."
            ) from exc
        raise SheetSourceError(f"Drive API: {exc}") from exc


def resolve_spreadsheet_source(
    spreadsheet_id: str,
    credentials_path: Path | str | None = None,
    service=None,
    drive_service=None,
) -> SheetSource:
    """
    Pick the right reader for a Drive file:

      • native Google Sheet  -> GoogleSheetsSource (values API)
      • uploaded .xlsx       -> GoogleDriveWorkbookSource (download + openpyxl)
    """
    metadata = spreadsheet_metadata(spreadsheet_id, credentials_path, drive_service)
    mime = str(metadata.get("mimeType", ""))
    name = str(metadata.get("name", ""))
    file_id = str(metadata.get("id") or extract_spreadsheet_id(spreadsheet_id))

    if mime == GOOGLE_SHEET_MIME:
        return GoogleSheetsSource(
            file_id, credentials_path=credentials_path, service=service
        )
    if mime in OFFICE_XLSX_MIMES:
        return GoogleDriveWorkbookSource(
            file_id, credentials_path=credentials_path, file_name=name
        )
    raise SheetSourceError(
        f"'{name}' bertipe {mime or '?'} — bukan Google Sheet atau .xlsx. "
        "Upload sebagai .xlsx atau konversi ke Google Sheet."
    )


# ─── Google Sheets ──────────────────────────────────────────────────────────
class GoogleSheetsSource(SheetSource):
    """
    Live Google Sheets reader.

    Requires:
      • a service account JSON (agent/config/service_account.json)
      • the spreadsheet shared with the service account email as Viewer
      • Google Sheets API enabled in the project

    `service` can be injected for tests; otherwise a client is built lazily so
    importing this module never touches the network.
    """

    name = "gsheets"

    def __init__(
        self,
        spreadsheet_id: str,
        credentials_path: Path | str | None = None,
        service=None,
        cache: bool = True,
        max_retries: int | None = None,
    ) -> None:
        self.spreadsheet_id = extract_spreadsheet_id(spreadsheet_id)
        if not self.spreadsheet_id:
            raise SheetSourceError(
                "GOOGLE_SPREADSHEET_ID kosong — isi dengan id/URL spreadsheet."
            )
        self.credentials_path = Path(credentials_path or config.SERVICE_ACCOUNT_PATH)
        self._service = service
        self._lock = RLock()
        self._snapshots: dict[str, tuple[datetime, Any]] = {}
        self._warnings: dict[str, str] = {}
        self._cache_enabled = cache
        self.max_retries = (
            config.DRIVE_MAX_RETRIES if max_retries is None else max_retries
        )
        self.label = f"spreadsheet:{self.spreadsheet_id[:10]}…"

    @property
    def warnings(self) -> list[str]:
        """Active quota-fallback warnings, one per resource; not a history."""
        with self._lock:
            return list(self._warnings.values())

    @property
    def freshness_warnings(self) -> list[str]:
        return self.warnings

    # ── client ─────────────────────────────────────────────────────────────
    @property
    def service(self):
        with self._lock:
            if self._service is None:
                self._service = self._build_service()
            return self._service

    def _build_service(self):
        try:
            from google.oauth2 import service_account
            from googleapiclient.discovery import build
        except ImportError as exc:  # pragma: no cover - depends on install
            raise SheetSourceError(
                "google-api-python-client belum terinstall. "
                "Jalankan `pip install -r agent/requirements.txt`."
            ) from exc

        if not self.credentials_path.exists():
            raise SheetSourceError(
                f"Service account tidak ditemukan: {self.credentials_path}"
            )
        credentials = service_account.Credentials.from_service_account_file(
            str(self.credentials_path), scopes=[SHEETS_SCOPE]
        )
        return build("sheets", "v4", credentials=credentials, cache_discovery=False)

    def service_account_email(self) -> str:
        """Email the spreadsheet must be shared with (from the key file)."""
        try:
            data = json.loads(self.credentials_path.read_text(encoding="utf-8"))
            return str(data.get("client_email", ""))
        except (OSError, json.JSONDecodeError):
            return ""

    # ── retry wrapper ──────────────────────────────────────────────────────
    @staticmethod
    def _is_quota_error(error) -> bool:
        status = getattr(error.resp, "status", 0)
        if status == 429:
            return True
        if status != 403:
            return False
        try:
            payload = json.loads(error.content)
        except (TypeError, ValueError):
            return False
        details = payload.get("error", {}) if isinstance(payload, dict) else {}
        if not isinstance(details, dict):
            return False
        quota_reasons = {
            "rateLimitExceeded", "userRateLimitExceeded", "quotaExceeded",
            "dailyLimitExceeded", "dailyLimitExceededUnreg",
            "RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED", "RESOURCE_EXHAUSTED",
        }
        reasons = [
            item.get("reason")
            for key in ("errors", "details")
            for item in (details.get(key) or [])
            if isinstance(item, dict)
        ]
        return details.get("status") == "RESOURCE_EXHAUSTED" or any(
            reason in quota_reasons for reason in reasons
        )

    def _execute(self, request):
        from googleapiclient.errors import HttpError

        delay = 1.0
        # Preserve the configured attempt budget; zero still makes one live request.
        attempts = max(1, self.max_retries)
        for attempt in range(1, attempts + 1):
            try:
                return request.execute()
            except HttpError as exc:
                status = getattr(exc.resp, "status", 0)
                message = str(exc)
                if status == 400 and "Office file" in message:
                    raise SheetSourceError(
                        "File ini .xlsx yang di-upload ke Drive, bukan Google Sheet — "
                        "Sheets API menolaknya. Pakai `resolve_spreadsheet_source` "
                        "(otomatis) atau konversi: File → Save as Google Sheets."
                    ) from exc
                quota = self._is_quota_error(exc)
                if quota or status >= 500:
                    if attempt < attempts:
                        time.sleep(delay)
                        delay *= 2
                        continue
                    error_type = SheetRateLimitError if quota else SheetSourceError
                    raise error_type(
                        f"Sheets API {status} after {attempt} attempts"
                    ) from exc
                if status in (403, 404):
                    raise SheetAccessError(self._explain_http(status)) from exc
                raise SheetSourceError(f"Sheets API {status}: {exc}") from exc

    def _explain_http(self, status: int) -> str:
        email = self.service_account_email() or "<service-account-email>"
        if status == 403:
            return (
                "403 — spreadsheet tidak dibagikan ke service account atau izin tidak cukup. "
                f"Buka sheet → Share → tambahkan {email} sebagai Editor (atau Viewer untuk mode baca)."
            )
        return (
            f"404 — spreadsheet '{self.spreadsheet_id}' tidak ditemukan. Cek "
            "GOOGLE_SPREADSHEET_ID (boleh tempel URL lengkap)."
        )

    # ── reading ────────────────────────────────────────────────────────────
    def _read(self, resource: str, make_request):
        # Request construction and execution both touch the shared Google client.
        with self._lock:
            self._warnings.pop(resource, None)
            try:
                result = self._execute(make_request())
            except SheetRateLimitError as exc:
                if not self._cache_enabled or resource not in self._snapshots:
                    raise
                read_at, snapshot = self._snapshots[resource]
                self._warnings[resource] = (
                    f"Quota fallback for {self.label}, resource '{resource}': "
                    f"using stale snapshot read at {read_at.isoformat()} (UTC). {exc}"
                )
                return deepcopy(snapshot)
            if self._cache_enabled:
                self._snapshots[resource] = (datetime.now(timezone.utc), deepcopy(result))
            return deepcopy(result)

    def sheet_names(self) -> list[str]:
        metadata = self._read(
            "sheet_names",
            lambda: self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id,
                fields="sheets.properties.title",
            ),
        )
        names = [
            sheet.get("properties", {}).get("title", "")
            for sheet in metadata.get("sheets", [])
        ]
        return [name for name in names if name]

    def spreadsheet_title(self) -> str:
        metadata = self._read(
            "spreadsheet_title",
            lambda: self.service.spreadsheets().get(
                spreadsheetId=self.spreadsheet_id, fields="properties.title"
            ),
        )
        return str(metadata.get("properties", {}).get("title", ""))

    def rows(self, sheet_name: str) -> list[list[Any]]:
        result = self._read(
            f"rows:{sheet_name}",
            lambda: self.service.spreadsheets()
            .values()
            .get(
                spreadsheetId=self.spreadsheet_id,
                range=f"{quote_sheet_title(sheet_name)}!A:ZZ",
                majorDimension="ROWS",
                valueRenderOption="UNFORMATTED_VALUE",
            ),
        )
        values = result.get("values", []) or []
        # Sheets trims trailing empty cells; keep rows rectangular for the mapper.
        width = max((len(row) for row in values), default=0)
        return [list(row) + [None] * (width - len(row)) for row in values]

    def append_row(self, sheet_name: str, values: list[Any]) -> None:
        with self._lock:
            range_a1 = quote_sheet_title(sheet_name)
            request = (
                self.service.spreadsheets()
                .values()
                .append(
                    spreadsheetId=self.spreadsheet_id,
                    range=range_a1,
                    valueInputOption="USER_ENTERED",
                    insertDataOption="INSERT_ROWS",
                    body={"values": [values]},
                )
            )
            self._execute(request)
            self.invalidate()

    def update_cell(self, sheet_name: str, row: int, col: int, value: Any) -> None:
        """Update a single cell with 1-based row and 1-based col."""
        col_letter = col_to_letter(col)
        range_a1 = f"{quote_sheet_title(sheet_name)}!{col_letter}{row}"
        val_str = str(value) if value is not None else ""
        with self._lock:
            self._execute(
                self.service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self.spreadsheet_id,
                    range=range_a1,
                    valueInputOption="USER_ENTERED",
                    body={"values": [[val_str]]},
                )
            )
            self.invalidate()

    def update_row(self, sheet_name: str, row: int, values: list[Any]) -> None:
        with self._lock:
            end_col = col_to_letter(len(values))
            row_range = f"{quote_sheet_title(sheet_name)}!A{row}:{end_col}{row}"
            request = (
                self.service.spreadsheets()
                .values()
                .update(
                    spreadsheetId=self.spreadsheet_id,
                    range=row_range,
                    valueInputOption="USER_ENTERED",
                    body={"values": [values]},
                )
            )
            self._execute(request)
            self.invalidate()

    def invalidate(self) -> None:
        """Discard fallback snapshots and their active warnings."""
        with self._lock:
            self._snapshots.clear()
            self._warnings.clear()

    def close(self) -> None:
        with self._lock:
            self._service = None
            self.invalidate()


# ─── Discovery (helps the "why can't it see my sheet?" case) ────────────────
def find_spreadsheets(
    credentials_path: Path | str | None = None, limit: int = 20
) -> list[dict]:
    """
    List spreadsheets the service account can actually open.

    Uses the Drive API, so it also tells you whether sharing worked at all.
    Returns [{id, name, modified}]. Raises SheetSourceError on auth problems.
    """
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build
    except ImportError as exc:  # pragma: no cover
        raise SheetSourceError(
            "google-api-python-client belum terinstall."
        ) from exc

    path = Path(credentials_path or config.SERVICE_ACCOUNT_PATH)
    if not path.exists():
        raise SheetSourceError(f"Service account tidak ditemukan: {path}")

    credentials = service_account.Credentials.from_service_account_file(
        str(path), scopes=[DRIVE_SCOPE]
    )
    service = build("drive", "v3", credentials=credentials, cache_discovery=False)
    try:
        result = (
            service.files()
            .list(
                q="mimeType='application/vnd.google-apps.spreadsheet' and trashed=false",
                fields="files(id,name,modifiedTime)",
                pageSize=limit,
                includeItemsFromAllDrives=True,
                supportsAllDrives=True,
            )
            .execute()
        )
    except Exception as exc:
        raise SheetSourceError(f"Drive API: {exc}") from exc

    return [
        {
            "id": item.get("id", ""),
            "name": item.get("name", ""),
            "modified": item.get("modifiedTime", ""),
        }
        for item in result.get("files", [])
    ]


# ─── Factory ────────────────────────────────────────────────────────────────
def build_sheet_source(
    source: str | None = None,
    spreadsheet_id: str | None = None,
    credentials_path: Path | str | None = None,
    path: Path | str | None = None,
) -> SheetSource:
    kind = (source or config.SHEET_SOURCE).lower()
    if kind in {"gsheets", "google", "google-sheets", "sheets"}:
        # Probe the Drive file once: native Sheet vs uploaded .xlsx.
        return resolve_spreadsheet_source(
            spreadsheet_id or config.GOOGLE_SPREADSHEET_ID,
            credentials_path=credentials_path or config.SERVICE_ACCOUNT_PATH,
        )
    return XlsxSource(path or config.DUMMY_DATA_PATH)
