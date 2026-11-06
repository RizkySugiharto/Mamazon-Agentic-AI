"""
Build the anonymized dummy dataset used for development and the demo.

Run:  python _refs/build_dummy.py

Produces `_refs/dummy_dataset_anonymized.xlsx` with:
  • Roster                 — 106 panitia
  • 12 submission sheets   — timestamps, IG handles, comma-joined Drive links
  • Angel Card Registry    — 17 entries across all four exemption tiers
  • Bukti Tambahan         — PJ story screenshots for C7 cross-reference
  • Evidence Vault         — historical violations for recurring detection

Multi-link ratios follow the real statistics in the planning document
(PP 29 Oktober = 42 submissions, 34 of them multi-link).
"""

from __future__ import annotations

import random
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

REFS_DIR = Path(__file__).resolve().parent
OUT_PATH = REFS_DIR / "dummy_dataset_anonymized.xlsx"
PLANNING_RULEBASE = REFS_DIR.parent / "planning" / "rulebase.json"
RULEBASE_OUT = REFS_DIR / "rulebase.json"

SEED = 20261105
TOTAL_PANITIA = 106

DIVISI = [
    "Fletch (Perlengkapan)",
    "Dupin (Visual)",
    "Columbo (Dokumentasi)",
    "Marple (Dekorasi)",
    "Marlowe (Public Relations)",
    "Kindaichi (Keamanan)",
    "Conan (Acara)",
    "Spade (Fresh Money)",
]

FIRST = [
    "Wildan", "Rizky", "Budi", "Sari", "Nadia", "Fajar", "Intan", "Yoga",
    "Citra", "Dimas", "Ayu", "Bayu", "Laras", "Hendra", "Maya", "Rangga",
    "Sinta", "Adit", "Putri", "Galih", "Nabila", "Reza", "Wulan", "Arif",
    "Dinda", "Tegar", "Mega", "Iqbal", "Rani", "Yusuf", "Kartika", "Bagas",
    "Anisa", "Farhan", "Ratna", "Surya", "Dewi", "Alif", "Novi", "Gilang",
]

LAST = [
    "Alhaqq", "Pratama", "Wijaya", "Santoso", "Nugroho", "Ramadhan", "Saputra",
    "Anggraini", "Kusuma", "Hidayat", "Permana", "Lestari", "Firmansyah",
    "Maharani", "Setiawan", "Wibowo", "Rahmawati", "Hartono", "Salsabila",
    "Prasetyo", "Utami", "Gunawan", "Fitriani", "Susanto", "Handayani",
    "Maulana", "Oktaviani", "Kurniawan", "Puspita", "Syahputra", "Wardani",
    "Nurhaliza", "Fadillah", "Simanjuntak", "Halim", "Yulianti", "Siregar",
    "Prabowo", "Azzahra", "Hermawan",
]

# sheet -> (row count, multi-link ratio, window start, window end, late ratio)
SHEETS: dict[str, tuple[int, float, datetime, datetime, float]] = {
    "PP - 5 Oktober": (
        62, 0.74,
        datetime(2026, 10, 9, 8, 0), datetime(2026, 10, 10, 20, 0), 0.10,
    ),
    "PP 07 Oktober": (
        30, 0.30,
        datetime(2026, 10, 7, 8, 0), datetime(2026, 10, 7, 21, 0), 0.08,
    ),
    "PP Story 09 Oktober": (
        48, 0.55,
        datetime(2026, 10, 9, 10, 0), datetime(2026, 10, 9, 22, 0), 0.06,
    ),
    "PP Bundle 9 Oktober": (
        55, 0.70,
        datetime(2026, 10, 9, 10, 0), datetime(2026, 10, 10, 20, 0), 0.12,
    ),
    "PP Bundle 26 Oktober": (
        68, 0.81,
        datetime(2026, 10, 26, 9, 0), datetime(2026, 10, 27, 12, 0), 0.09,
    ),
    "PP Feed 26 Oktober": (
        76, 0.11,
        datetime(2026, 10, 28, 8, 0), datetime(2026, 10, 29, 15, 0), 0.07,
    ),
    "PP 29 Oktober": (
        42, 0.81,
        datetime(2026, 10, 29, 9, 0), datetime(2026, 10, 30, 15, 0), 0.10,
    ),
    "buzzer-6 Oktober": (
        58, 0.00,
        datetime(2026, 10, 6, 10, 0), datetime(2026, 10, 7, 18, 0), 0.05,
    ),
    "Buzzer Gmaps 09 Oktober": (
        50, 0.42,
        datetime(2026, 10, 9, 10, 0), datetime(2026, 10, 10, 21, 0), 0.08,
    ),
    "Buzzer IG 09 Oktober": (
        85, 0.42,
        datetime(2026, 10, 9, 10, 0), datetime(2026, 10, 10, 18, 0), 0.11,
    ),
    "Buzzer vote 26 Oktober": (
        64, 0.15,
        datetime(2026, 10, 26, 10, 0), datetime(2026, 10, 27, 8, 0), 0.06,
    ),
    "Buzzer vote 26 Oktober (joccely": (
        20, 0.10,
        datetime(2026, 10, 26, 10, 0), datetime(2026, 10, 27, 8, 0), 0.05,
    ),
}

HEADER_FILL = PatternFill("solid", fgColor="1F3864")
HEADER_FONT = Font(color="FFFFFF", bold=True, size=11)
NOTE_FILL = PatternFill("solid", fgColor="FFF2CC")


def build_roster(rng: random.Random) -> list[dict]:
    used_names: set[str] = set()
    used_handles: set[str] = set()
    panitia: list[dict] = []

    combos = [(f, l) for f in FIRST for l in LAST]
    rng.shuffle(combos)

    for index in range(TOTAL_PANITIA):
        first, last = combos[index]
        nama = f"{first} {last}"
        while nama in used_names:
            first = rng.choice(FIRST)
            nama = f"{first} {last}"
        used_names.add(nama)

        handle = f"{first.lower()}{rng.choice(['_', '.', ''])}{last.lower()}"
        handle = handle[:20]
        while handle in used_handles:
            handle = f"{handle[:17]}{rng.randint(0, 99)}"
        used_handles.add(handle)

        panitia.append(
            {
                "nama": nama,
                "nim": f"00000{rng.randint(100000, 999999)}",
                "divisi": DIVISI[index % len(DIVISI)],
                "ig": handle,
            }
        )

    rng.shuffle(panitia)
    return panitia


def make_file_id(rng: random.Random) -> str:
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
    return "".join(rng.choice(alphabet) for _ in range(33))


def make_link(rng: random.Random, file_id: str) -> str:
    shape = rng.random()
    if shape < 0.45:
        return f"https://drive.google.com/open?id={file_id}"
    if shape < 0.80:
        return f"https://drive.google.com/file/d/{file_id}/view?usp=sharing"
    if shape < 0.92:
        return f"https://drive.google.com/open?id={file_id}&authuser=0"
    return f"https://drive.google.com/file/d/{file_id}/view"


def build_link_cell(rng: random.Random, multi: bool, empty: bool = False) -> str:
    if empty:
        return ""
    count = 1
    if multi:
        count = rng.choice([2, 2, 3, 3, 4])
    links = [make_link(rng, make_file_id(rng)) for _ in range(count)]
    separator = "\n" if rng.random() < 0.12 else ", "
    return separator.join(links)


def build_submission_sheet(
    rng: random.Random,
    sheet: str,
    rows: int,
    multi_ratio: float,
    window_start: datetime,
    window_end: datetime,
    late_ratio: float,
    panitia: list[dict],
) -> list[dict]:
    picked = rng.sample(panitia, min(rows, len(panitia)))
    records: list[dict] = []

    # Pick exactly round(rows * multi_ratio) rows to be multi-link, so the
    # statistics match the real sheets quoted in the planning document.
    multi_target = int(round(len(picked) * multi_ratio))
    multi_rows = set(rng.sample(range(len(picked)), multi_target))

    for row_index, person in enumerate(picked):
        if rng.random() < late_ratio:
            offset = timedelta(
                hours=rng.randint(2, 30), minutes=rng.randint(0, 59)
            )
            stamp = window_end + offset
        else:
            span = max(1, int((window_end - window_start).total_seconds()))
            stamp = window_start + timedelta(seconds=rng.randint(0, span))

        # One no-link row on a legacy sheet so the "no evidence" path is covered.
        empty = sheet == "PP 07 Oktober" and row_index == 3

        note = ""
        if rng.random() < 0.06:
            note = rng.choice(
                [
                    "caption kayaknya di-edit setelah upload",
                    "story sempat hilang",
                    "post sempat di-private",
                    "link sempat restricted",
                ]
            )

        records.append(
            {
                "timestamp": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                "nama": person["nama"],
                "nim": person["nim"],
                "divisi": person["divisi"],
                "ig": person["ig"],
                "link": build_link_cell(rng, row_index in multi_rows, empty),
                "catatan": note,
            }
        )

    records.sort(key=lambda record: record["timestamp"])
    return records


def build_angel_registry(rng: random.Random, panitia: list[dict]) -> list[dict]:
    tiers = [
        (3, datetime(2026, 10, 2)),    # early buyers — exempt for everything
        (5, datetime(2026, 10, 7)),    # exempt from 26 Okt onward
        (5, datetime(2026, 10, 18)),   # exempt from 26 Okt onward
        (4, datetime(2026, 10, 28)),   # only exempt from PP-03 / BZ-02 onward
    ]
    picked = rng.sample(panitia, 17)
    entries: list[dict] = []
    cursor = 0

    for count, base_date in tiers:
        for _ in range(count):
            person = picked[cursor]
            cursor += 1
            day = base_date + timedelta(days=rng.randint(0, 2))
            entries.append(
                {
                    "nama": person["nama"],
                    "divisi": person["divisi"],
                    "status": "ACTIVE",
                    "tanggal": day.strftime("%Y-%m-%d"),
                    "bukti": f"https://drive.google.com/file/d/{make_file_id(rng)}/view",
                    "catatan": "verified via mutasi BCA",
                }
            )

    entries.sort(key=lambda entry: entry["tanggal"])
    return entries


def build_bukti_tambahan(rng: random.Random, panitia: list[dict]) -> list[dict]:
    submissions = [
        "PP Feed 26 Oktober",
        "PP 29 Oktober",
        "PP Story 09 Oktober",
        "Buzzer IG 09 Oktober",
    ]
    descriptions = [
        "Story 24h masih live, screenshot jam 21.10",
        "Story sudah tayang sejak jam post, masih ada",
        "Story expired, tapi ada arsip",
        "Feed masih live, tidak dihapus",
    ]
    rows: list[dict] = []
    picked = rng.sample(panitia, 14)
    for index, person in enumerate(picked):
        stamp = datetime(2026, 10, 29, 9, 0) + timedelta(hours=index * 2)
        rows.append(
            {
                "timestamp": stamp.strftime("%Y-%m-%d %H:%M:%S"),
                "submission": submissions[index % len(submissions)],
                "ig": person["ig"],
                "deskripsi": descriptions[index % len(descriptions)],
                "link": f"https://drive.google.com/file/d/{make_file_id(rng)}/view",
                "pj": "spade_pj",
            }
        )
    return rows


def build_evidence_vault(rng: random.Random, panitia: list[dict]) -> list[dict]:
    """Seed two old violations for one panitia so RECURRING (3x) can trigger."""
    target = panitia[7]
    rows = [
        {
            "tanggal": "2026-10-10",
            "nama": target["nama"],
            "divisi": target["divisi"],
            "tugas": "PP-06",
            "pelanggaran": "C6: hashtag kurang #CaseOfTheUnseen",
            "denda": 5000,
            "link": f"https://drive.google.com/file/d/{make_file_id(rng)}/view",
            "catatan": "PJ screenshot sebelum diedit",
        },
        {
            "tanggal": "2026-10-26",
            "nama": target["nama"],
            "divisi": target["divisi"],
            "tugas": "BZ-03",
            "pelanggaran": "B1: tidak like postingan target",
            "denda": 20000,
            "link": f"https://drive.google.com/file/d/{make_file_id(rng)}/view",
            "catatan": "PJ screenshot sebelum diedit",
        },
    ]
    for index in range(3):
        person = panitia[20 + index]
        rows.append(
            {
                "tanggal": "2026-10-27",
                "nama": person["nama"],
                "divisi": person["divisi"],
                "tugas": "PP-05",
                "pelanggaran": "C4: caption tidak sesuai",
                "denda": 5000,
                "link": f"https://drive.google.com/file/d/{make_file_id(rng)}/view",
                "catatan": "",
            }
        )
    return rows


def style_sheet(ws, widths: list[int]) -> None:
    for index, width in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(index)].width = width
    for cell in ws[1]:
        cell.fill = HEADER_FILL
        cell.font = HEADER_FONT
        cell.alignment = Alignment(vertical="center", horizontal="center")
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for row in ws.iter_rows(min_row=2):
        for cell in row:
            cell.alignment = Alignment(vertical="top", wrap_text=False)


def write_table(ws, headers: list[str], rows: list[dict], keys: list[str]) -> None:
    ws.append(headers)
    for record in rows:
        ws.append([record.get(key, "") for key in keys])
    style_sheet(ws, [22, 24, 14, 26, 22, 60, 32][: len(headers)])


def main() -> None:
    rng = random.Random(SEED)
    panitia = build_roster(rng)

    wb = Workbook()
    wb.remove(wb.active)

    # Roster
    ws = wb.create_sheet("Roster")
    write_table(
        ws,
        ["Nama Lengkap", "NIM", "Divisi", "Username Instagram"],
        panitia,
        ["nama", "nim", "divisi", "ig"],
    )

    # Submission sheets
    total_rows = 0
    total_links = 0
    total_multi = 0
    for sheet, (rows, multi_ratio, start, end, late) in SHEETS.items():
        records = build_submission_sheet(
            rng, sheet, rows, multi_ratio, start, end, late, panitia
        )
        ws = wb.create_sheet(sheet[:31])
        write_table(
            ws,
            [
                "Timestamp",
                "Nama Lengkap",
                "NIM",
                "Divisi",
                "Username Instagram",
                "Link Bukti (Google Drive)",
                "Catatan",
            ],
            records,
            ["timestamp", "nama", "nim", "divisi", "ig", "link", "catatan"],
        )
        total_rows += len(records)
        for record in records:
            links = [part for part in record["link"].replace("\n", ",").split(",") if part.strip()]
            total_links += len(links)
            if len(links) > 1:
                total_multi += 1

    # Angel Card Registry
    ws = wb.create_sheet("Angel Card Registry")
    write_table(
        ws,
        [
            "Nama Lengkap",
            "Divisi",
            "Status Angel Card",
            "Tanggal Pembelian",
            "Bukti Transfer",
            "Catatan Validasi",
        ],
        build_angel_registry(rng, panitia),
        ["nama", "divisi", "status", "tanggal", "bukti", "catatan"],
    )

    # Bukti Tambahan
    ws = wb.create_sheet("Bukti Tambahan")
    write_table(
        ws,
        ["Timestamp", "Submission", "Username Instagram", "Deskripsi", "Link Bukti", "PJ"],
        build_bukti_tambahan(rng, panitia),
        ["timestamp", "submission", "ig", "deskripsi", "link", "pj"],
    )

    # Evidence Vault
    ws = wb.create_sheet("Evidence Vault")
    write_table(
        ws,
        [
            "Tanggal",
            "Nama",
            "Divisi",
            "ID Tugas",
            "Jenis Pelanggaran",
            "Denda",
            "Evidence Link",
            "Catatan",
        ],
        build_evidence_vault(rng, panitia),
        ["tanggal", "nama", "divisi", "tugas", "pelanggaran", "denda", "link", "catatan"],
    )

    wb.save(OUT_PATH)

    if PLANNING_RULEBASE.exists():
        shutil.copyfile(PLANNING_RULEBASE, RULEBASE_OUT)
        print(f"Rulebase copied verbatim -> {RULEBASE_OUT}")

    print(f"Workbook written -> {OUT_PATH}")
    print(f"  tabs           : {len(wb.sheetnames)}")
    print(f"  panitia        : {len(panitia)}")
    print(f"  submission rows: {total_rows}")
    print(f"  link entries   : {total_links}")
    print(f"  multi-link rows: {total_multi} ({total_multi / max(1, total_rows):.0%})")

    pp29 = SHEETS["PP 29 Oktober"]
    print(
        "  PP 29 Oktober  : "
        f"{pp29[0]} rows, target {int(pp29[0] * pp29[1])} multi-link"
    )


if __name__ == "__main__":
    main()
