"""
CLI entry point for development, testing and the offline demo.

    python -m src.main list-sheets
    python -m src.main lint
    python -m src.main validate --sheet "PP 29 Oktober"
    python -m src.main validate --sheet "PP Feed 26 Oktober" --dry-run
    python -m src.main angel-card
    python -m src.main denda --nama "Wildan Alhaqq"
    python -m src.main ketentuan --task PP-03
    python -m src.main scan-blast --demo
    python -m src.main cleanup
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from . import config
from .blast_reader import BlastContextStore, parse_blast_message
from .bukti_tambahan import BuktiTambahanHandler
from .data_layer import DataLayer
from .image_manager import ImageManager
from .models import ResultStatus
from .output_generator import OutputGenerator
from .rulebase_engine import RulebaseEngine
from .utils import format_rupiah, fmt_dt, truncate
from .validator import SheetAmbiguousError, SheetNotFoundError, Validator, _load_rulebase


def _rulebase() -> dict:
    return _load_rulebase()


def _data_layer() -> DataLayer:
    return DataLayer(rulebase=_rulebase())


def _validator() -> Validator:
    return Validator(data_layer=_data_layer())


# ─── commands ───────────────────────────────────────────────────────────────
def cmd_list_sheets(args: argparse.Namespace) -> int:
    with _data_layer() as data:
        stats = data.dataset_stats()
        print(
            f"Dataset: {data.label} | {stats['panitia']} panitia | "
            f"{stats['sheets']} submission sheets | {stats['angel_cards']} Angel Card"
        )
        print()
        header = f"{'SHEET':34} {'ROWS':>5} {'MULTI':>6} {'NOLINK':>7} {'TASK':>7}  SOURCE"
        print(header)
        print("-" * len(header))
        for info in data.describe_sheets():
            if not info.is_submission:
                print(f"{info.name:34} {'—':>5} {'—':>6} {'—':>7} {'—':>7}  registry")
                continue
            print(
                f"{info.name:34} {info.rows:>5} {info.multi_link:>6} "
                f"{info.no_link:>7} {info.task_id or '—':>7}  {info.task_source}"
            )
        print()
        print(
            f"Total: {stats['rows']} rows, {stats['multi_link']} multi-link "
            f"({stats['multi_ratio']:.0%}), {stats['no_link']} tanpa link"
        )
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    from .rulebase_engine import lint_rulebase

    issues = lint_rulebase(_rulebase())
    if not issues:
        print("Rulebase bersih, tidak ada isu.")
        return 0
    for issue in issues:
        print(issue)
    errors = sum(1 for issue in issues if issue.severity == "ERROR")
    warns = sum(1 for issue in issues if issue.severity == "WARN")
    print()
    print(f"{len(issues)} isu: {errors} ERROR, {warns} WARN, {len(issues) - errors - warns} INFO")
    print("Catatan: rulebase.json TIDAK diubah. Perbaiki manual oleh developer.")
    return 0 if errors == 0 else 1


def cmd_validate(args: argparse.Namespace) -> int:
    validator = _validator()

    if args.dry_run:
        try:
            preview = validator.preview(args.sheet)
        except SheetNotFoundError as exc:
            print(f"❌ Sheet '{exc.query}' tidak ditemukan. Tersedia:")
            for option in exc.options:
                print(f"  • {option}")
            return 2
        except SheetAmbiguousError as exc:
            print(f"⚠️ Sheet '{exc.query}' ambigu, {len(exc.options)} kandidat:")
            for option in exc.options:
                print(f"  • {option} ({exc.scores.get(option, 0):.2f})")
            return 2

        print(f"DRY RUN — {preview['sheet_name']}")
        print(f"  task          : {preview['task_id']} ({preview['task_source']})")
        print(f"  tipe          : {preview['task_type']}")
        print(f"  rows          : {preview['rows']} ({preview['unique_rows']} unik)")
        print(f"  multi-link    : {preview['multi_link']}")
        print(f"  tanpa link    : {preview['no_link']}")
        print(f"  checks        : {', '.join(preview['checks'])}")
        print(f"  blast context : {'ADA' if preview['blast_context'] else 'TIDAK ADA'}")
        print(f"  deadline      : {fmt_dt(preview['deadline'])}")
        if preview["duplicates"]:
            print("  duplikat:")
            for note in preview["duplicates"]:
                print(f"    - {note}")
        if preview["warnings"]:
            print("  warnings:")
            for warning in preview["warnings"]:
                print(f"    - {warning}")
        return 0

    state = {"last": 0}

    def progress(stage: str, current: int, total: int, message: str) -> None:
        if stage in ("write", "resolve") or current == total or current - state["last"] >= 5:
            state["last"] = current
            print(f"  [{current:>3}/{total}] {message}", file=sys.stderr, end="\r")
            if current == total:
                print(file=sys.stderr)

    try:
        batch = validator.validate(
            args.sheet,
            force=args.force,
            limit=args.limit,
            progress=None if args.quiet else progress,
            actor=args.actor,
        )
    except SheetNotFoundError as exc:
        print(f"❌ Sheet '{exc.query}' tidak ditemukan. Tersedia:")
        for option in exc.options:
            print(f"  • {option}")
        return 2
    except SheetAmbiguousError as exc:
        print(f"⚠️ Sheet '{exc.query}' ambigu, {len(exc.options)} kandidat:")
        for option in exc.options:
            print(f"  • {option} ({exc.scores.get(option, 0):.2f})")
        print("Sebutkan nama lengkap sheet.")
        return 2

    _print_batch(batch, verbose=not args.quiet)

    if args.json:
        payload = {
            "sheet": batch.sheet_name,
            "task_id": batch.task_id,
            "total": batch.total,
            "verified": len(batch.verified),
            "flagged": len(batch.flagged),
            "exempt": len(batch.exempt),
            "pending": len(batch.pending),
            "total_denda": batch.total_denda,
            "results": [result.to_dict() for result in batch.results],
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))

    return 0


def _print_batch(batch, verbose: bool = True) -> None:
    print()
    print("=" * 72)
    print(f"HASIL VALIDASI — {batch.sheet_name}")
    print("=" * 72)
    print(f"Task          : {batch.task_id or '-'} ({batch.task_type})")
    print(f"Total         : {batch.total} submissions")
    print(f"AMAN          : {len(batch.verified)}")
    print(f"DENDA         : {len(batch.flagged)}")
    print(f"EXEMPT        : {len(batch.exempt)} (Angel Card)")
    print(f"REVIEW MANUAL : {len(batch.pending)}")
    print(f"Skip (sudah)  : {batch.skipped_existing}")
    print(f"Total denda   : {format_rupiah(batch.total_denda)}")
    print(f"Durasi        : {batch.duration_seconds}s")
    print(f"Context       : {'rulebase + blast_context' if batch.blast_context_used else 'rulebase only'}")

    if batch.warnings:
        print()
        print("Warnings:")
        for warning in batch.warnings[:8]:
            print(f"  - {truncate(warning, 110)}")

    if verbose and batch.flagged:
        print()
        print("Rincian denda:")
        for result in sorted(batch.flagged, key=lambda r: -r.total_denda)[:15]:
            print(
                f"  ❌ {result.submission.nama:26} "
                f"{format_rupiah(result.total_denda):>10}  "
                f"{result.submission.divisi}"
            )
            for check in result.violations:
                print(f"       {check.check_id}: {truncate(check.detail, 90)}")

    if verbose and batch.exempt:
        print()
        print("Exempt (Angel Card):")
        for result in batch.exempt[:8]:
            print(f"  🎫 {result.submission.nama:26} {truncate(result.angel_card_note, 80)}")

    if batch.duplicates:
        print()
        print("Duplikat / resubmit:")
        for note in batch.duplicates[:6]:
            print(f"  - {truncate(note, 110)}")

    print()
    print("Output:")
    print(f"  {config.VERIFICATION_REPORT_PATH}")
    print(f"  {config.VIOLATIONS_LOG_PATH}")
    print(f"  {config.BLAST_DRAFT_PATH}")
    print(f"  {config.AUDIT_LOG_PATH}")


def cmd_angel_card(args: argparse.Namespace) -> int:
    with _data_layer() as data:
        registry = data.load_angel_registry()
        if not registry:
            print("Tidak ada data Angel Card Registry.")
            return 0

        if args.check:
            engine = RulebaseEngine(_rulebase())
            entry, score = data.find_person(args.check)
            name = entry.nama if entry else args.check
            match = next(
                (item for item in registry if item.nama.lower() == name.lower()), None
            )
            if match is None:
                print(f"❌ {name} tidak punya Angel Card.")
                return 0
            print(f"🎫 {match.nama} | {match.divisi}")
            print(f"   Status      : {match.status}")
            print(f"   Dibeli      : {fmt_dt(match.tanggal_beli, with_time=False)}")
            print(f"   Catatan     : {match.catatan}")
            for task_id in ("PP-03", "PP-05", "BZ-06", "BZ-03"):
                exempt, note = engine.angel_exemption(name, task_id, registry)
                print(f"   {task_id:6} -> {'EXEMPT' if exempt else 'TIDAK'}  {note}")
            return 0

        print(f"Angel Card Registry — {len(registry)} entri")
        print()
        print(f"{'NAMA':28} {'DIVISI':26} {'STATUS':8} {'DIBELI':12}")
        print("-" * 78)
        for entry in registry:
            print(
                f"{entry.nama:28} {entry.divisi:26} {entry.status:8} "
                f"{(entry.tanggal_beli.strftime('%d %b %Y') if entry.tanggal_beli else '-'):12}"
            )
    return 0


def cmd_denda(args: argparse.Namespace) -> int:
    outputs = OutputGenerator()
    if args.nama:
        rows = outputs.denda_rows(args.nama, include_resolved=args.all)
        total = sum(int(float(row.get("denda") or 0)) for row in rows)
        print(f"Denda untuk '{args.nama}': {format_rupiah(total)} ({len(rows)} item)")
        for row in rows:
            print(
                f"  {row.get('tanggal','')} {row.get('id_tugas',''):7} "
                f"{row.get('rule_id',''):5} {format_rupiah(int(float(row.get('denda') or 0))):>10} "
                f"{row.get('status',''):9} {truncate(row.get('jenis_pelanggaran',''), 60)}"
            )
        return 0

    rows = outputs.denda_rows(include_resolved=args.all)
    totals: Counter = Counter()
    for row in rows:
        totals[row.get("nama_komitee", "?")] += int(float(row.get("denda") or 0))
    if not totals:
        print("Tidak ada denda tercatat.")
        return 0
    print(f"Total {len(totals)} panitia, {format_rupiah(sum(totals.values()))}")
    for nama, total in totals.most_common(30):
        print(f"  {nama:28} {format_rupiah(total):>12}")
    return 0


def cmd_ketentuan(args: argparse.Namespace) -> int:
    engine = RulebaseEngine(_rulebase())
    merged = engine.merge(args.task)
    if not merged.raw_task:
        print(f"❌ Task '{args.task}' tidak ditemukan.")
        print("Tersedia: " + ", ".join(engine.task_ids()))
        return 2

    print(f"📋 {merged.task_id} — {merged.task_type}")
    print(f"Platform    : {', '.join(merged.platforms) or '-'}")
    print(f"Jadwal post : {fmt_dt(merged.jadwal_post)}")
    print(f"Deadline    : {fmt_dt(merged.deadline)}")
    print(f"Checks      : {', '.join(engine.applicable_checks(args.task))}")
    if merged.tags:
        print(f"Tags wajib  : {', '.join(merged.tags)}")
    if merged.hashtags:
        print(f"Hashtag     : {', '.join(merged.hashtags)}")
    if merged.caption_reference:
        print(f"Caption     : {truncate(merged.caption_reference, 300)}")
    print(f"Media       : {merged.drive_link or '-'}")
    if merged.blast:
        print(f"Blast ctx   : {merged.blast.blast_message_id} ({merged.blast.confidence})")
    for warning in merged.warnings:
        print(f"⚠️  {warning}")
    return 0


DEMO_BLAST = """📋 BLAST TUGAS: PP Feed 26 Oktober 2026

Caption: Pusing dengan project akhir/TA codingmu? Coba jasa kami yuk, FREE diskusi sampai paham!

Media: https://drive.google.com/drive/folders/1EeG1JHNMHZUYbsy57egmmvmGwacv5RRl

Tags: @horizon_umn
Hashtag: #PPHorizon #PPHorizon_divisi #CaseOfTheUnseen #Openpaidpromote #paidpromotemahasiswamurah
Deadline: Kamis 29 Oktober 2026 15:00 WIB
Form: https://forms.gle/iubikzSdexns1pb69
Catatan: caption wajib verbatim termasuk emoji
"""


def cmd_scan_blast(args: argparse.Namespace) -> int:
    rulebase = _rulebase()
    texts: list[str] = []
    if args.text_file:
        texts = [Path(args.text_file).read_text(encoding="utf-8")]
    elif args.demo:
        texts = [DEMO_BLAST]
    else:
        print("Gunakan --demo atau --text-file PATH (Discord scanning via /scan-blast).")
        return 2

    store = BlastContextStore()
    for index, text in enumerate(texts, start=1):
        parsed = parse_blast_message(text, rulebase)
        print(f"Message #{index}")
        print(f"  task_id    : {parsed.task_id} (confidence {parsed.confidence})")
        print(f"  tipe       : {parsed.task_type}")
        print(f"  blast_date : {fmt_dt(parsed.blast_date, with_time=False)}")
        print(f"  deadline   : {fmt_dt(parsed.deadline)}")
        print(f"  tags       : {', '.join(parsed.tags) or '-'}")
        print(f"  hashtags   : {', '.join(parsed.hashtags) or '-'}")
        print(f"  drive      : {parsed.drive_folder or '-'}")
        print(f"  form       : {parsed.form_link or '-'}")
        print(f"  caption    : {truncate(parsed.caption_referensi, 120) or '-'}")
        for note in parsed.notes:
            print(f"  {note}")
        if parsed.task_id:
            store.upsert(parsed.to_context(message_id=f"cli-{index}"))
            print(f"  ✅ disimpan ke {config.BLAST_CONTEXT_PATH}")
        else:
            print("  ⚠️ task_id tidak terdeteksi — tidak disimpan (hindari salah merge)")
        print()
    print(store.summary())
    return 0


def cmd_demo(args: argparse.Namespace) -> int:
    """
    One-command demo for the hackathon video:
      blast extraction -> 3 sheets validated -> recap.
    """
    rulebase = _rulebase()
    validator = _validator()
    store = BlastContextStore()

    print("=" * 72)
    print("DEMO — Mamazon Spade Agent")
    print("=" * 72)

    print("\n[1/4] Extract ketentuan dari blast #blast-tugas…")
    parsed = parse_blast_message(DEMO_BLAST, rulebase)
    store.upsert(parsed.to_context(message_id="demo-1"))
    validator.reload_context()
    print(f"      task {parsed.task_id} | tags {len(parsed.tags)} | "
          f"hashtags {len(parsed.hashtags)} | deadline {fmt_dt(parsed.deadline)}")

    sheets = [
        "PP Feed 26 Oktober",
        "PP 29 Oktober",
        "Buzzer IG 09 Oktober",
    ]
    batches = []
    for index, sheet in enumerate(sheets, start=2):
        print(f"\n[{index}/4] Validasi {sheet}…")
        batch = validator.validate(sheet, force=True, actor="demo")
        batches.append(batch)
        print(
            f"      {batch.total} submissions · {len(batch.verified)} AMAN · "
            f"{len(batch.flagged)} DENDA · {len(batch.exempt)} EXEMPT · "
            f"{format_rupiah(batch.total_denda)}"
        )

    print("\nRekap")
    total_rows = sum(batch.total for batch in batches)
    total_flagged = sum(len(batch.flagged) for batch in batches)
    total_denda = sum(batch.total_denda for batch in batches)
    print(f"      {total_rows} submissions diproses")
    print(f"      {total_flagged} panitia kena denda, total {format_rupiah(total_denda)}")

    ranking: dict[str, int] = {}
    for batch in batches:
        for result in batch.flagged:
            ranking[result.submission.nama] = ranking.get(result.submission.nama, 0) + result.total_denda
    if ranking:
        print("\n      Denda terbesar:")
        for nama, amount in sorted(ranking.items(), key=lambda pair: -pair[1])[:5]:
            print(f"        {nama:28} {format_rupiah(amount)}")

    print("\n      Artefak:")
    for path in (
        config.VERIFICATION_REPORT_PATH,
        config.VIOLATIONS_LOG_PATH,
        config.BLAST_DRAFT_PATH,
        config.AUDIT_LOG_PATH,
        config.BLAST_CONTEXT_PATH,
    ):
        size = path.stat().st_size if path.exists() else 0
        print(f"        {path.name:26} {size / 1024:6.1f} KB")

    print("\n      Next: `python -m src.discord_bot` untuk approval gate via Discord.")
    return 0


def cmd_vision_status(args: argparse.Namespace) -> int:
    """Diagnose the Vision backend — the fastest way to debug OpenRouter."""
    from .vision_checker import (
        OpenRouterVision,
        TencentVision,
        UnavailableVision,
        build_vision_backend,
    )

    backend = build_vision_backend()
    print("=== Vision backend ===")
    print(f"provider      : {config.VISION_PROVIDER} (active: {backend.name})")
    print(f"mock mode     : {config.USE_MOCK_VISION}")
    print(f"min followers : {config.MIN_IG_FOLLOWERS}")

    if isinstance(backend, UnavailableVision):
        print(f"\n❌ {backend.reason}")
        print("\nPilihan:")
        print("  • Pakai OpenRouter: set VISION_PROVIDER=openrouter + OPENROUTER_MODEL")
        print("  • Pakai mock  : set VISION_PROVIDER=mock (untuk demo offline)")
        return 1

    if isinstance(backend, OpenRouterVision):
        print(f"base url      : {backend.base_url}")
        print(f"model         : {backend.model or '(kosong!)'}")
        print(f"api key       : {'SET' if backend.api_key else 'KOSONG'}")
        print(f"json mode     : {config.OPENROUTER_JSON_MODE}")
        print(f"timeout       : {backend.timeout}s")
        print()

        ok, message = backend.health()
        print(f"{'✅' if ok else '❌'} health: {message}")

        if args.models:
            try:
                vision_models = backend.list_vision_models()
                total = len(backend.list_models())
                print(
                    f"\n{len(vision_models)} model vision dari {total} model "
                    "(difilter: input_modalities mengandung 'image')"
                )
                for model in vision_models[: args.models]:
                    mark = "→" if model["id"] == backend.model else " "
                    price = model.get("prompt_price")
                    price_text = (
                        f"${float(price) * 1_000_000:.2f}/1M tok" if price else "gratis/?"
                    )
                    print(f"  {mark} {model['id']:52} {price_text}")
            except Exception as exc:
                print(f"\n❌ gagal list model: {exc}")

        if args.test_image:
            print("\nMengirim 1 gambar uji ke model…")
            path = _make_test_image()
            passed, detail, payload = backend.smoke_test(path)
            print(f"{'✅' if passed else '❌'} vision test: {detail}")
            if payload.get("__usage__"):
                print(f"   usage: {payload['__usage__']}")
            if not passed:
                print(
                    "\n   Kalau health OK tapi ini gagal, cek pesan di atas. "
                    "Model yang tidak bisa baca gambar akan membalas kosong/400."
                )
            return 0 if passed else 1

        return 0 if ok else 1

    if isinstance(backend, TencentVision):
        print(f"secret id     : {'SET' if config.TENCENT_SECRET_ID else 'kosong'}")
        print(f"region        : {config.TENCENT_REGION}")
        print(f"model         : {config.TENCENT_VISION_MODEL}")
        print("\nUntuk pindah ke OpenRouter:")
        print("  1. buat key di https://openrouter.ai/keys")
        print("  2. set VISION_PROVIDER=openrouter, OPENROUTER_API_KEY, OPENROUTER_MODEL")
        print("  3. python -m src.main vision-status --models 20 --test-image")
        return 0

    print("\nMock Vision aktif — hasil deterministik, bukan dari model sungguhan.")
    print("\nUntuk memakai OpenRouter:")
    print("  1. buat API key di https://openrouter.ai/keys")
    print("  2. di agent/.env:")
    print("       VISION_PROVIDER=openrouter")
    print("       OPENROUTER_API_KEY=sk-or-v1-...")
    print("       OPENROUTER_MODEL=google/gemini-2.0-flash-001")
    print("  3. python -m src.main vision-status --models 20 --test-image")
    print("\nModel harus multimodal — daftar model vision ada di langkah 3.")
    return 0


def _make_test_image() -> str:
    """Render a small image with known text for the vision smoke test."""
    from PIL import Image, ImageDraw

    image = Image.new("RGB", (640, 240), "white")
    draw = ImageDraw.Draw(image)
    draw.rectangle([20, 20, 620, 220], outline="black", width=4)
    draw.text((60, 90), "MAMAZON TEST 4711", fill="black")
    path = Path(config.TMP_DIR) / "vision_smoke.png"
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path)
    return str(path)


def cmd_google_status(args: argparse.Namespace) -> int:
    """
    Diagnose the Google integration: service account, Sheets access, Drive access.

    This is the command to run after sharing the spreadsheet — it tells you
    exactly which tab or folder is not reachable and who to share it with.
    """
    import json as _json

    from .sheets_client import (
        SheetAccessError,
        SheetRateLimitError,
        SheetSourceError,
    )
    from .utils import extract_drive_folder_id

    print("=== Google integration ===")
    print(f"sheet source        : {config.SHEET_SOURCE}")
    print(f"spreadsheet id      : {config.GOOGLE_SPREADSHEET_ID or '(kosong)'}")
    print(f"credentials         : {config.GOOGLE_SHEETS_CREDENTIALS}")
    print(f"api client installed: {'ya' if config.module_available('googleapiclient') else 'TIDAK'}")

    email = ""
    project = ""
    if config.GOOGLE_SHEETS_CREDENTIALS.exists():
        try:
            key_data = _json.loads(
                config.GOOGLE_SHEETS_CREDENTIALS.read_text(encoding="utf-8")
            )
            email = str(key_data.get("client_email", ""))
            project = str(key_data.get("project_id", ""))
        except (OSError, _json.JSONDecodeError):
            pass
    print(f"service account     : {email or '(tidak terbaca)'}")
    print(f"project             : {project or '-'}")
    if email:
        print(f"\n👉 Share spreadsheet & folder Drive ke: {email} (Viewer)")

    problems = 0

    # ── Sheets ─────────────────────────────────────────────────────────────
    print("\n--- Google Sheets ---")
    if config.SHEET_SOURCE not in {"gsheets", "google", "google-sheets", "sheets"}:
        print("ℹ️  SHEET_SOURCE bukan gsheets — set SHEET_SOURCE=gsheets untuk memakai sheet live.")
    elif not config.GOOGLE_SPREADSHEET_ID:
        print("❌ GOOGLE_SPREADSHEET_ID kosong. Tempel URL/id spreadsheet di agent/.env.")
        print(f"   Share sheet-nya dulu ke {email or 'service account'} (Viewer).")
        try:
            from .sheets_client import find_spreadsheets

            visible = find_spreadsheets(config.GOOGLE_SHEETS_CREDENTIALS)
        except Exception as exc:
            print(f"   (tidak bisa cek Drive: {exc})")
        else:
            if visible:
                print(f"\n   Spreadsheet yang bisa diakses service account ({len(visible)}):")
                for item in visible[:10]:
                    print(f"     • {item['name'][:44]:46} {item['id']}")
                print("\n   Salin salah satu id di atas ke GOOGLE_SPREADSHEET_ID.")
            else:
                print(
                    "\n   Belum ada spreadsheet yang bisa diakses. Buka sheet → Share →\n"
                    f"   tambahkan {email or 'service account'} sebagai Viewer, lalu ulangi."
                )
        problems += 1
    else:
        from .sheets_client import (
            GoogleDriveWorkbookSource,
            resolve_spreadsheet_source,
            spreadsheet_metadata,
        )

        source = None
        try:
            meta = spreadsheet_metadata(
                config.GOOGLE_SPREADSHEET_ID,
                credentials_path=config.GOOGLE_SHEETS_CREDENTIALS,
            )
            kind = (
                "Google Sheet native"
                if meta.get("mimeType") == "application/vnd.google-apps.spreadsheet"
                else "file .xlsx di Drive"
            )
            print(f"file                : {meta.get('name')} ({kind})")
            source = resolve_spreadsheet_source(
                config.GOOGLE_SPREADSHEET_ID,
                credentials_path=config.GOOGLE_SHEETS_CREDENTIALS,
            )
            names = source.sheet_names()
        except SheetAccessError as exc:
            print(f"❌ {exc}")
            problems += 1
            names = []
        except SheetRateLimitError as exc:
            print(f"❌ rate limit: {exc}")
            problems += 1
            names = []
        except SheetSourceError as exc:
            print(f"❌ {exc}")
            problems += 1
            names = []
        except Exception as exc:  # network / auth
            print(f"❌ {type(exc).__name__}: {exc}")
            problems += 1
            names = []
        else:
            print(f"✅ terhubung — {len(names)} tab")
            if isinstance(source, GoogleDriveWorkbookSource):
                print("   (dibaca sebagai .xlsx dari Drive; Sheets API tidak dipakai)")
            for name in names:
                print(f"   • {name}")

            # Only task tabs are mandatory; registry tabs (Bukti Tambahan,
            # Evidence Vault) are optional in a given workbook.
            expected = set()
            for task in validator_rulebase().get("tugas_pp", []) + validator_rulebase().get("tugas_buzzer", []):
                if task.get("sheet"):
                    expected.add(task["sheet"].strip().lower())
            actual = {name.strip().lower() for name in names}
            missing = sorted(expected - actual)
            if missing:
                print("\n⚠️  Tab yang diharapkan rulebase tapi tidak ada:")
                for name in missing:
                    print(f"   - {name}")
            if args.rows:
                print("\nRow counts:")
                for name in names[: args.rows]:
                    try:
                        print(f"   {name:32} {len(source.rows(name)):>4} rows")
                    except Exception as exc:
                        print(f"   {name:32} ❌ {exc}")
        finally:
            if source is not None:
                source.close()

    # ── Drive ──────────────────────────────────────────────────────────────
    print("\n--- Google Drive ---")
    folder_input = args.folder
    if not folder_input:
        for task in validator_rulebase().get("tugas_pp", []):
            if task.get("drive_link"):
                folder_input = task["drive_link"]
                break
    folder_id = extract_drive_folder_id(folder_input)

    if not folder_id:
        print("ℹ️  Tidak ada folder untuk dicek. Pakai --folder <id/url>.")
    elif not config.module_available("googleapiclient"):
        print("❌ google-api-python-client belum terinstall.")
        problems += 1
    else:
        from .drive_client import build_drive_backend

        backend = build_drive_backend("google")
        if backend.name != "google":
            print(f"❌ {getattr(backend, 'reason', 'Google Drive tidak siap')}")
            problems += 1
        else:
            try:
                files = backend.list_folder(folder_id)
            except Exception as exc:
                print(f"❌ folder {folder_id}: {exc}")
                print(f"   Share folder ke {email or 'service account'} (Viewer).")
                problems += 1
            else:
                print(f"✅ folder {folder_id}: {len(files)} file")
                for item in files[:5]:
                    print(f"   • {item.get('name')} ({item.get('mimeType', '').split('.')[-1]})")

    print()
    if problems:
        print(f"{problems} masalah ditemukan. Perbaiki lalu jalankan ulang.")
        return 1
    print("Semua siap.")
    return 0


def validator_rulebase() -> dict:
    """Small helper so google-status can read task metadata."""
    return _rulebase()


def cmd_cleanup(args: argparse.Namespace) -> int:
    manager = ImageManager()
    before = manager.stats()
    report = manager.cleanup(force=args.force)
    after = manager.stats()
    print(
        f"Cleanup: {report['removed']} file dihapus, "
        f"{report['freed'] / 1024:.0f} KB dibebaskan"
    )
    print(f"Sebelum: {before['files']} file ({before['size_human']})")
    print(f"Sesudah: {after['files']} file ({after['size_human']})")
    return 0


def cmd_bukti(args: argparse.Namespace) -> int:
    handler = BuktiTambahanHandler()
    result = handler.handle(
        caption=args.caption, link=args.link or "", pj=args.pj or "cli", actor="cli"
    )
    print(result.message)
    return 0 if result.ok else 1


def cmd_config(args: argparse.Namespace) -> int:
    print(f"Project root : {config.PROJECT_ROOT}")
    print(f"Rulebase     : {config.RULEBASE_PATH}")
    print(f"Sheet source : {config.SHEET_SOURCE}")
    if config.SHEET_SOURCE in {"gsheets", "google", "google-sheets", "sheets"}:
        sheet_id = config.GOOGLE_SPREADSHEET_ID
        print(f"Spreadsheet  : {sheet_id[:60] + '…' if len(sheet_id) > 60 else sheet_id or '(kosong)'}")
    else:
        print(f"Dataset      : {config.DUMMY_DATA_PATH}")
    print(f"Output       : {config.OUTPUT_DIR}")
    print(f"Vision       : {config.VISION_PROVIDER}")
    print(f"Drive        : {config.DRIVE_PROVIDER}")
    print(f"Discord token: {'SET' if config.DISCORD_BOT_TOKEN else 'NOT SET'}")
    problems = config.validate_config(require_discord=args.discord)
    if problems:
        print()
        print("Issues:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("OK")
    return 0


# ─── parser ─────────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.main",
        description="Mamazon Spade Office Agent — CLI",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("list-sheets", help="daftar sheet + statistik multi-link")
    p.set_defaults(func=cmd_list_sheets)

    p = sub.add_parser("lint", help="laporkan isu data di rulebase.json")
    p.set_defaults(func=cmd_lint)

    p = sub.add_parser("validate", help="validasi satu sheet")
    p.add_argument("--sheet", required=True, help="nama sheet, boleh disingkat")
    p.add_argument("--force", action="store_true", help="re-validasi walau sudah VERIFIED")
    p.add_argument("--limit", type=int, default=None, help="batasi jumlah baris")
    p.add_argument("--dry-run", action="store_true", help="parse saja, tanpa Vision")
    p.add_argument("--json", action="store_true", help="cetak hasil sebagai JSON")
    p.add_argument("--quiet", action="store_true", help="tanpa progress/rincian")
    p.add_argument("--actor", default="cli", help="nama aktor untuk audit log")
    p.set_defaults(func=cmd_validate)

    p = sub.add_parser("angel-card", help="lihat registry / cek satu nama")
    p.add_argument("--check", default=None, help="cek exemption untuk satu nama")
    p.set_defaults(func=cmd_angel_card)

    p = sub.add_parser("denda", help="lihat denda per panitia / agregat")
    p.add_argument("--nama", default=None)
    p.add_argument("--all", action="store_true", help="ikutkan yang RESOLVED")
    p.set_defaults(func=cmd_denda)

    p = sub.add_parser("ketentuan", help="tampilkan ketentuan satu task")
    p.add_argument("--task", required=True, help="mis. PP-03 atau BZ-06")
    p.set_defaults(func=cmd_ketentuan)

    p = sub.add_parser("scan-blast", help="parse blast message ke blast_context.json")
    p.add_argument("--text-file", default=None)
    p.add_argument("--demo", action="store_true")
    p.set_defaults(func=cmd_scan_blast)

    p = sub.add_parser("demo", help="demo end-to-end (blast + 3 sheet + rekap)")
    p.set_defaults(func=cmd_demo)

    p = sub.add_parser("vision-status", help="diagnosa backend Vision (openrouter/tencent/mock)")
    p.add_argument("--models", type=int, default=0, help="tampilkan N model dari /v1/models")
    p.add_argument("--test-image", action="store_true", help="kirim gambar uji ke model")
    p.set_defaults(func=cmd_vision_status)

    p = sub.add_parser("google-status", help="diagnosa service account, Sheets & Drive")
    p.add_argument("--folder", default=None, help="id/URL folder Drive yang dicek")
    p.add_argument("--rows", type=int, default=0, help="tampilkan jumlah baris N tab pertama")
    p.set_defaults(func=cmd_google_status)

    p = sub.add_parser("cleanup", help="hapus temp image lebih dari TTL")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_cleanup)

    p = sub.add_parser("bukti", help="simulasikan upload bukti tambahan")
    p.add_argument("--caption", required=True)
    p.add_argument("--link", default="")
    p.add_argument("--pj", default="cli")
    p.set_defaults(func=cmd_bukti)

    p = sub.add_parser("config", help="cek konfigurasi & env")
    p.add_argument("--discord", action="store_true", help="wajibkan token + channel id")
    p.set_defaults(func=cmd_config)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except FileNotFoundError as exc:
        print(f"❌ {exc}")
        return 1
    except Exception as exc:
        from .sheets_client import SheetSourceError

        if isinstance(exc, SheetSourceError):
            print(f"❌ Gagal membaca spreadsheet: {exc}")
            print("   Diagnosa: python -m src.main google-status")
            return 1
        raise
    except KeyboardInterrupt:
        print("\nDibatalkan.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
