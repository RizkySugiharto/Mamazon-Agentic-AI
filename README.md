# Mamazon Spade Office Agent

Discord bot + Python validation core for Mamazon's Paid Promote (PP) and Buzzer
submissions. It reads the submission spreadsheet, splits the comma-joined Google
Drive links that break manual review, reads the evidence screenshots, applies the
17 rulebase checks (C1–C10 / B1–B7) merged with each blast's extra requirements,
and drafts the fine blast — which only reaches `#denda` after a koor clicks
**Approve**.

Built for Tencent Cloud Hackathon 2026 — Challenge B, from the specs in
`planning/`.

---

## Quick start

The project is managed with [uv](https://docs.astral.sh/uv/): no global pip, no
manually activated virtualenv, no dependency drift.

```bash
# one-time install (see https://docs.astral.sh/uv/getting-started/installation/)
cd agent

uv sync                      # creates agent/.venv + uv.lock from pyproject.toml

uv run python -m src.main config          # wiring check
uv run python -m src.main lint            # rulebase data issues
uv run python -m src.main google-status   # service account, Sheets, Drive
uv run python -m src.main demo            # offline demo (mock vision/drive)
uv run pytest                             # offline regression suite
```

`uv run` always uses `agent/.venv` and the interpreter pinned in
`.python-version` (3.13), so your global Python is never touched.

Common operations:

```bash
uv add <package>              # add a dependency (updates pyproject + uv.lock)
uv remove <package>           # remove one
uv sync --extra tencent       # optional Tencent Hunyuan SDK
uv run --with rich python …   # one-off tool without changing the project
uv lock --upgrade             # refresh the lockfile
```

`requirements.txt` is kept for compatibility, but `pyproject.toml` + `uv.lock`
are the source of truth.

## Offline demo (no keys needed)

```bash
cd agent
uv run python ../_refs/build_dummy.py   # regenerate the fixture workbook
uv run python -m src.main demo          # blast extract + 3 sheets + recap
```

The demo runs entirely on the mock Vision backend and mock Drive, so it works
without OpenRouter or Google credentials.

## Connecting the real services

```bash
cp agent/.env.example agent/.env
```

Fill in what you have; each adapter falls back independently:

| Variable | Effect when set |
| --- | --- |
| `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_*_CHANNEL_ID` | bot goes live (all 17 slash commands) |
| `OPENROUTER_MODEL` | `VISION_PROVIDER=auto` switches to OpenRouter |
| `TENCENT_SECRET_ID`, `TENCENT_SECRET_KEY` | `VISION_PROVIDER=auto` switches to Hunyuan Vision |
| `agent/config/service_account.json` | `DRIVE_PROVIDER=auto` switches to real Drive downloads |

Force a backend explicitly with `VISION_PROVIDER=mock|openrouter|tencent` and
`DRIVE_PROVIDER=mock|google`. Then:

```bash
cd agent && uv run python -m src.discord_bot
```

Without a token the bot exits with a checklist instead of crashing.

### Vision via OpenRouter (recommended)

[OpenRouter](https://openrouter.ai) exposes 450+ models behind one
OpenAI-compatible endpoint, so the agent never needs a Tencent account. The
adapter is stdlib-only — no extra Python dependency, no SDK to install.

```bash
# 1. create an API key
#    https://openrouter.ai/keys   (credits: https://openrouter.ai/credits)

# 2. put it in agent/.env
VISION_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=google/gemini-2.0-flash-001   # must accept image input

# 3. find a vision model + check it works before touching real data
cd agent
uv run python -m src.main vision-status --models 20
uv run python -m src.main vision-status --test-image     # sends one real image
uv run python -m src.main validate --sheet "PP 29 Oktober" --limit 3 --force
```

`vision-status --models` filters the catalogue to models whose
`architecture.input_modalities` contains `image`, and prints the prompt price
per 1M tokens, so you never pick a text-only model or an expensive one by
accident. The listing works **without** a key because OpenRouter's `/models`
endpoint is public.

Model ids are `provider/model` (e.g. `openai/gpt-4o-mini`,
`anthropic/claude-3.5-sonnet`, `google/gemini-2.0-flash-001`). Cheap vision
models are usually enough — the task is reading text from a screenshot, not
reasoning about it.

| Symptom | Meaning | Fix |
| --- | --- | --- |
| `401 Unauthorized` | key missing/wrong | create one at https://openrouter.ai/keys |
| `402 Payment Required` | credits exhausted | top up, or switch to a cheaper model |
| `429` | rate limit | wait, or pick a less busy model |
| `bukan model vision` | text-only model id | `vision-status --models 20` |
| `400 ... tidak mendukung image` | model rejected the image part | pick a multimodal model |
| `Tidak bisa konek` | network/proxy | `curl https://openrouter.ai/api/v1/models` |

If OpenRouter fails mid-run, each affected submission is marked `vision_error`
→ `INCONCLUSIVE` (needs review) and is excluded from the blast. It never
silently falls back to mock, because a mock verdict would look like real
evidence against a real panitia.

**Testing without spending credits:** `uv run python -m tools.fake_openrouter` starts a
local stand-in on port 20129 that speaks the same protocol and answers with the
expected caption/tags, so the whole pipeline can be exercised offline:

```bash
uv run python -m tools.fake_openrouter --port 20129          # add --violate to trigger C6
VISION_PROVIDER=openrouter OPENROUTER_BASE_URL=http://127.0.0.1:20129/api/v1 \
OPENROUTER_API_KEY=test-key OPENROUTER_MODEL=google/gemini-2.0-flash-001 \
DRIVE_PROVIDER=mock uv run python -m src.main validate --sheet "PP 29 Oktober" --limit 3
```

### Vision via Tencent Cloud (alternative)

```bash
VISION_PROVIDER=tencent
TENCENT_SECRET_ID=...
TENCENT_SECRET_KEY=...
TENCENT_REGION=ap-jakarta
```

Same `VisionFacts` contract, same checks — only the transport differs.

### Google Sheets (live) + Google Drive (evidence)

Both use the same service-account key (`agent/config/service_account.json`).
Verified against project `mamazon-app`: Drive folder listing and file download
work; Sheets needs the spreadsheet shared with the service account.

```bash
# 1. enable APIs in the GCP project: Google Sheets API + Google Drive API
# 2. share the spreadsheet with the service account as Viewer
#    mamazon-discord-bot@mamazon-app.iam.gserviceaccount.com
# 3. agent/.env
SHEET_SOURCE=gsheets
GOOGLE_SPREADSHEET_ID=<id or full URL>
DRIVE_PROVIDER=google

# 4. diagnose (prints the service account email and every reachable tab)
cd agent
uv run python -m src.main google-status --rows 5

# 5. read the live sheet
uv run python -m src.main list-sheets
uv run python -m src.main validate --sheet "PP 29 Oktober" --limit 3
```

`google-status` is the command to run whenever something is "not found":

- prints the service-account email to share with, and the project id;
- lists every tab it can see, plus the row count per tab (`--rows N`);
- reports tabs that the rulebase expects but the sheet does not have;
- checks a Drive folder (`--folder <id|url>`, defaults to the first task folder);
- when `GOOGLE_SPREADSHEET_ID` is empty, **lists the spreadsheets the service
  account can already open** — so after sharing, re-running tells you the id.

How the reader behaves:

| Concern | Behaviour |
| --- | --- |
| Dates | `UNFORMATTED_VALUE` + serial-number conversion (1899-12-30 epoch), so locale never matters |
| Numbers | `446173.0` → `"446173"` — no `.0`, no scientific notation on NIMs |
| Tabs | header row auto-detected in the first 5 rows; `Roster`, `Angel Card Registry`, `Bukti Tambahan`, `Evidence Vault` are treated as registries, not submissions |
| Freshness | request live tab names, title, submissions and registries on every read; no parsed Sheets cache |
| 403 | `SheetAccessError` telling you exactly which email to share with |
| 404 | `SheetAccessError` pointing at `GOOGLE_SPREADSHEET_ID` |
| Quota (429 / quota-related 403) | retry, then use the last successful in-memory snapshot for that resource with a timestamped warning; without a snapshot, fail loudly; next read tries live again |
| 5xx / access errors | retry server errors, then fail; never substitute stale snapshots for permissions or server failures |

Drive evidence download is the same path the validator uses: `files.get_media`
with `drive.readonly`, 403/404 mapped to "share this file", 429/5xx retried, and
the bytes flow into `image_manager` (SHA-256 dedup → compress → Vision).

## Running the bot

```bash
cd agent
uv run python -m src.discord_bot
```

Expected startup output:

```
[bot] starting… (Ctrl+C to stop)
[bot] 17 slash commands synced
[bot] logged in as Mamazon#6015 — 2 guild(s): Rytos Studio, Mamazon
```

Stop with `Ctrl+C`. Commands appear in the guild within seconds; if they don't,
check `DISCORD_GUILD_ID` (a wrong id syncs globally, which can take an hour) and
that the bot was invited with the `applications.commands` scope.

Channel permissions the bot needs:

| Channel | Permissions |
| --- | --- |
| `#pp-validation`, `#buzzer-validation` | View, Send, Embed Links, Attach Files |
| `#blast-drafts` | View, Send, Embed Links, Read Message History |
| `#blast-tugas` | View, **Read Message History** (`/scan-blast`), Add Reactions |
| `#bukti-tambahan` | View, Read Message History, Attach Files |
| `#evidence` | View, Send, Embed Links |
| `#audit-log` | View, Send |
| `#ai-chat` | View, Read Message History, Send |
| `#denda`, `#ketentuan` | View, Send |

Verified on the live server: the bot can post to `#audit-log` (HTTP 200).
Reading message history there returned 403, so grant **Read Message History**
wherever the bot must scan (`#blast-tugas`) or reply to uploads
(`#bukti-tambahan`).

`/validate` on a whole sheet can take minutes with a slow model. The progress
embed updates every 2.5s, and if the 15-minute interaction token expires the
result is posted to the channel instead of the interaction reply.

## CLI

```
uv run python -m src.main list-sheets                 # sheets, row counts, multi-link stats, task mapping
uv run python -m src.main lint                        # rulebase data issues (ERROR/WARN/INFO)
uv run python -m src.main validate --sheet "PP 29 Oktober"
uv run python -m src.main validate --sheet "PP Feed 26 Oktober" --dry-run
uv run python -m src.main validate --sheet "PP 26 Oktober"        # → ambiguous, lists candidates
uv run python -m src.main angel-card --check "Budi Santoso"
uv run python -m src.main denda --nama "Budi Santoso"
uv run python -m src.main ketentuan --task PP-03
uv run python -m src.main scan-blast --demo
uv run python -m src.main bukti --caption "PP 29 Oktober | @budi_ | Story 24h masih live"
uv run python -m src.main vision-status --models 20 --test-image   # diagnose the Vision backend
uv run python -m src.main cleanup                     # 24h TTL sweep of tmp/images
```

`validate` exits `2` for an unknown or ambiguous sheet — never guesses.

## Discord setup

Channels (ids go in `.env`):

```
#pp-validation  #buzzer-validation  #blast-drafts  #blast-tugas  #bukti-tambahan
#evidence  #audit-log  #ai-chat  #denda  #ketentuan
```

Roles: `@BPH` (admin, bypasses checks), `@koor-spade`, `@spade`, `@panitia`.
The matrix in `src/config.py:PERMISSIONS` is the single source of truth and
mirrors `planning/Mamazon_Spade_Agent_Workflow.md` §2.

Commands: `/help` `/validate` `/blast` `/hold-blast` `/release-blast` `/list-active`
`/check-angel` `/add-angel` `/remove-angel` `/denda-list` `/resubmit-status`
`/audit-log` `/ketentuan-pp` `/ketentuan-buzzer` `/ketentuan-angel-card`
`/scan-blast` `/list-sheets`.

Passive listeners: `#blast-tugas` is parsed automatically (✅ reaction + reply),
`#bukti-tambahan` accepts `Submission | @ig | Deskripsi` uploads, `#ai-chat`
supports natural Indonesian conversation, greetings and lightweight emotional acknowledgment.
It uses the configured OpenRouter model and key (`CHAT_MODEL` defaults to
`OPENROUTER_MODEL`, including `google/gemma-4-26b-a4b-it:free`), grounded in
read-only rulebase and current organization data. Set `CHAT_ENABLED=false` for
offline deterministic answers. On timeout, quota failure or empty model output,
it visibly falls back to grounded answers. Recent context is kept per guild,
channel and member (12 turns, 30-minute inactivity expiry, at most 100 sessions),
not persisted or shared between members. Only validator roles retain chat access.
Chat cannot execute validations, change rules/fines or send blasts.

### New interactive commands

- `/help`: private, role-aware command guide and clear action boundaries.
- `/denda-list nama:...`: every active fine is paginated, with sheet/blast task
  name, rule explanation, observed failure, amount, per-task subtotal and running
  member accumulation. Without `nama`, privileged users see all members; panitia
  still see only themselves using koor-maintained `DISCORD_MEMBER_MAP` JSON
  (`{"discord-user-id":"Exact Roster Name"}` in `.env`). Unbound panitia are
  denied instead of trusting an impersonatable nickname. Totals exclude resolved/withdrawn records and are
  **recorded fines, not a payment-balance confirmation**.
- `/ketentuan-buzzer task_id:BZ-02` and `/ketentuan-pp task_id:PP-03`: Indonesian
  weekdays, explained checks, short guidance, and link buttons to `#blast-tugas`
  and the original task message. The exact-message button appears only after
  the message has been ingested (`/scan-blast` for older messages); no guessed IDs.
- `/validate sheet:... divisi:...`: filter by the exact division in that sheet
  (autocomplete helps; omit for all). Rows without division use roster data.
  Unknown divisions are rejected. A canonical task lock blocks overlapping runs,
  even with aliases, `force:true`, or a different division. The same task cannot
  produce a blast while its validation is running.
- The progress message has **Batalkan validasi**. Only its initiator or a
  koor/BPH with validation permission may cancel. An active API request must
  finish before the worker exits; partial results are discarded and only a
  cancellation audit is written. Downloads already made may remain in the image
  cache. Cancellation stops being available when persistence begins.
- `/blast sheet:... divisi:...`: optionally restrict a coordinator's draft to
  one division; approval still gates sending to `#denda`. Approval rejects stale
  drafts after revalidation, is serialized to prevent duplicate button sends,
  and marks SENT only after delivery succeeds. Successful chunks are recorded
  so retrying a partially delivered blast resumes instead of replaying them.

Validation reports, fine-log mutations and audit appends are serialized across
workers. Pending/manual-review/error results cannot create new fines; previously
flagged unsent charges become withdrawn on pending/exempt revalidation. Already
blasted charges remain for coordinator review rather than being erased.

## Pipeline

```
sheet query ─▶ resolve sheet (structure-aware fuzzy)
            ─▶ resolve task  (rulebase.sheet → config map → fuzzy)
            ─▶ merge rulebase.json + blast_context.json
            ─▶ Angel Card check (beli ≤ blast → EXEMPT)
            ─▶ dedupe resubmits (latest timestamp wins)
            ─▶ download evidence (hash dedup, compress, 403 → review)
            ─▶ VisionFacts (mock | Hunyuan)
            ─▶ checks C1–C10 / B1–B7
            ─▶ verification_report.csv · violations_log.csv · audit_log.jsonl · blast_draft.md
            ─▶ Discord: summary + paginated detail + #evidence
            ─▶ /blast → [Approve][Hold][Reject] → #denda
```

| Module | Responsibility |
| --- | --- |
| `config.py` | env, paths, adapter flags, permission matrix |
| `models.py` | `Submission`, `VisionFacts`, `CheckResult`, `ValidationBatch`, `BlastRecord` |
| `utils.py` | Indonesian dates, link splitting, fuzzy matching, hashing |
| `sheets_client.py` | `XlsxSource` / `GoogleSheetsSource`, serial dates, discovery |
| `data_layer.py` | sheet reader (either source), sheet resolver, roster, Angel registry, dedupe |
| `rulebase_engine.py` | merge + 17 checks + Angel exemption + `lint_rulebase` |
| `vision_checker.py` | `MockVision` (deterministic), `OpenRouterVision` (OpenAI-compatible), `TencentVision` |
| `drive_client.py` | `MockDrive` (synthetic images, 403 simulation) and `GoogleDrive` |
| `image_manager.py` | download, SHA-256 dedup, compression, TTL cleanup |
| `output_generator.py` | CSV upserts, blast draft, audit log, blast state machine |
| `blast_reader.py` | `#blast-tugas` → `blast_context.json` |
| `bukti_tambahan.py` | PJ upload parsing and validation |
| `evidence_vault.py` | violation entries, recurring escalation |
| `validator.py` | orchestrator + reporting helpers |
| `discord_bot.py` | commands, embeds, buttons, role gating |
| `chat_agent.py` | grounded read-only facts + OpenRouter conversation, scope guard and bounded user history |

## Outputs

`agent/output/`

| File | Semantics |
| --- | --- |
| `verification_report.csv` | one row per (sheet, nama, nim); **upserted**, so a resubmit flips FLAGGED → VERIFIED without duplicating |
| `violations_log.csv` | one row per (nama, task, check); FLAGGED → RESOLVED / BLASTED |
| `blast_draft.md` | draft grouped per divisi with transfer code and deadline |
| `audit_log.jsonl` | append-only decisions: actor, action, rule_ids, confidence |
| `blast_context.json` | per-task extra requirements extracted from `#blast-tugas` |
| `blasts/<id>.json` | approval state machine (DRAFT → HELD/APPROVED → SENT / REJECTED) |

## Decisions worth knowing

1. **`rulebase.json` is never patched at runtime.** `uv run python -m src.main lint`
   reports the known defects instead: `PP-01` has `deadline` (9 Oct) *before*
   `jadwal_post` (29 Oct), and `PP - 5 Oktober` / `buzzer-6 Oktober` are claimed
   by different tasks than `config.SHEET_TO_TASK` says. Because of this, C2/C10
   return **FLAG with Rp 0** for PP-01 instead of fining 42 people Rp 20.000.
   Fix the data when you are ready; the bot will pick it up on restart.
2. **A schedule without a clock time means the whole day.** `"Rabu, 28 Oktober
   2026"` parses to 00:00, not 23:59 — otherwise every morning post looks
   "too early".
3. **Unverifiable ≠ violation.** Missing caption reference, unreadable story,
   unknown followers → `INCONCLUSIVE` (needs review), never a fine.
4. **C2 and C10 can both fire** on a late submission (late post + late proof),
   matching the rulebook literally: 2 × Rp 20.000.
5. **Angel Card edits go to an overlay** (`output/angel_card_overrides.jsonl`),
   because the registry sheet is read-only by policy. Every add/remove is
   attributable.
6. **`#denda` is never written automatically.** The approval gate is a button,
   and the state survives a restart.

## Tests

```bash
cd agent && uv run python -m pytest tests/ -q      # 210 tests
```

Covers link splitting (the 74–81% multi-link root cause), date-only schedules,
day-name mismatches, blast date guardrail (G1), all 17 checks, Angel Card
boundary (`beli == blast`), resubmit idempotency, blast state machine, blast
parsing, chat scope guard, Discord message chunking, and the rulebase lint.

## Still to do

- Prompt tuning against real screenshots: `OpenRouterVision` is wired and
  unit-tested; run `vision-status --test-image` to confirm the chosen model can
  actually read a screenshot before validating a whole sheet.
- Google Sheets is implemented; it needs the spreadsheet shared with the service
  account (`google-status` lists what is reachable).
- Anti-cheat metadata (EXIF) check from the planning doc §6.4.
- Voice-note STT bonus, architecture diagram, demo video.
