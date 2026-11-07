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

uv run python -m src.main config          # wiring check (incl. unified AI provider)
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
without any AI-provider or Google credentials.

## Connecting the real services

```bash
cp agent/.env.example agent/.env
```

Fill in what you have; each adapter falls back independently:

| Variable | Effect when set |
| --- | --- |
| `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_*_CHANNEL_ID` | bot goes live (all 17 slash commands) |
| `AI_PROVIDER` (+ `OPENROUTER_MODEL` / `OPENCODE_MODEL` / `OPENCODE_API_MODEL`) | selects the one provider + model used for both Vision and chat (`openrouter`, `opencode`, or `opencode-api`) |
| `agent/config/service_account.json` | `DRIVE_PROVIDER=auto` switches to real Drive downloads |

Set `AI_PROVIDER=openrouter|opencode|opencode-api` (or `mock` for offline
tests only) and force the Drive backend with `DRIVE_PROVIDER=mock|google`. Then:

```bash
cd agent && uv run python -m src.discord_bot
```

Without a token the bot exits with a checklist instead of crashing.

### Unified AI provider (Vision + chat)

One setting, `AI_PROVIDER`, chooses the model used for **both** evidence
screenshot reading (Vision) and `#ai-chat` conversation. The same provider and
the same exact model serve both paths — there is no separate chat model.
Supported production values are `openrouter | opencode | opencode-api`;
`mock` is an internal/offline-test-only stub, not a production provider. The
Tencent / Hunyuan and Antigravity / `agy` provider options were **removed** —
setting any of them now fails loudly instead of falling back silently. The
three real backends keep **distinct** model variables: `OPENROUTER_MODEL`,
`OPENCODE_MODEL` and `OPENCODE_API_MODEL` are not interchangeable. Provider
failures now surface a **sanitized reason** in the chat fallback and the audit
log — never a key, token or raw provider payload.

| `AI_PROVIDER` | Backend | Model setting |
| --- | --- | --- |
| `openrouter` | [OpenRouter](https://openrouter.ai) OpenAI-compatible endpoint (stdlib-only, no SDK) | `OPENROUTER_MODEL` |
| `opencode` | local OpenCode CLI, headless `opencode run` | `OPENCODE_MODEL` |
| `opencode-api` | OpenCode Console inference HTTP API (OpenAI-compatible endpoint) | `OPENCODE_API_MODEL` |
| `mock` | deterministic offline stub | — (internal/offline-test only, **not** a production provider) |

Get a model before starting: for the local CLI run `opencode models` and copy
one exact `provider/model` slug; for `opencode-api` list models with
`GET https://opencode.ai/inference/v1/models` and create an API key in the
OpenCode Console. For `opencode-api`, use the **bare id** from that listing
(e.g. `nemotron-3.5-super-vl-free`); an `opencode/<id>` value is also accepted
and is normalized to the bare id before the direct API call. Neither path
guesses a default model, and the model must be vision-capable for screenshot
reading.

Legacy names (`VISION_PROVIDER`, `AI_AGENT_PROVIDER`, `CHAT_PROVIDER`,
`CHAT_MODEL`) are ignored as separate selectors; they survive only as
backward-compatible mirrors/aliases of `AI_PROVIDER` and never select a
different provider or model. Do not set them — use `AI_PROVIDER` and the
provider's own `*_MODEL` variable instead. Restart the bot after changing `.env`.

`CHAT_ENABLED=false` only disables chat rewriting in `#ai-chat` (answers fall
back to deterministic grounded facts); it does **not** disable Vision/evidence
reading. `CHAT_TIMEOUT` and `CHAT_MAX_TOKENS` likewise only affect chat.

#### OpenRouter

[OpenRouter](https://openrouter.ai) exposes 450+ models behind one
OpenAI-compatible endpoint. The adapter is stdlib-only — no extra Python
dependency, no SDK to install.

```dotenv
# 1. create an API key: https://openrouter.ai/keys
# 2. put these in agent/.env
AI_PROVIDER=openrouter
OPENROUTER_API_KEY=sk-or-v1-...
OPENROUTER_MODEL=google/gemma-4-26b-a4b-it:free   # example only — keep the model you configured
```

`OPENROUTER_MODEL` is used for both Vision and chat, so it **must** accept image
input for screenshot reading. The id above is an example that preserves the
configured free model; the agent keeps exactly the model you set and never
silently substitutes a different or paid one. Model ids are `provider/model`
(e.g. `google/gemini-2.0-flash-001`, `openai/gpt-4o-mini`,
`anthropic/claude-3.5-sonnet`). Cheap vision models are usually enough — the
task is reading text from a screenshot, not reasoning about it.

```bash
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
AI_PROVIDER=openrouter OPENROUTER_BASE_URL=http://127.0.0.1:20129/api/v1 \
OPENROUTER_API_KEY=test-key OPENROUTER_MODEL=google/gemini-2.0-flash-001 \
DRIVE_PROVIDER=mock uv run python -m src.main validate --sheet "PP 29 Oktober" --limit 3
```

#### Local OpenCode CLI

Use a locally installed OpenCode CLI. There is **no guessed default model**: run
`opencode models` and copy one exact `provider/model` slug into
`OPENCODE_MODEL`. That model serves both Vision and chat, so for screenshots it
**must be vision-capable** — evidence images are attached to the prompt with
repeated `--file <image>` flags.

```dotenv
AI_PROVIDER=opencode
OPENCODE_CLI=opencode                 # Windows: absolute ...\opencode.CMD path
OPENCODE_MODEL=provider/model          # exact slug from `opencode models`
OPENCODE_TIMEOUT=120
OPENCODE_AGENT=plan
OPENCODE_CWD=                          # blank = agent/tmp/opencode_ai (isolated, no .env)
OPENCODE_MAX_IMAGES=8
OPENCODE_MAX_PROMPT_CHARS=12000
```

The bot invokes the documented headless form (one `--file` per image):

```bash
opencode run --model <OPENCODE_MODEL> --agent plan --file <image> "<prompt>"
```

Invocation safeguards:

- resolves the executable to an **absolute path** via `shutil.which` — on
  Windows this yields the real `opencode.CMD`, fixing `WinError 2` /
  `FileNotFoundError` when a bare `opencode` name cannot be spawned;
- built as an **argument list** with `shell=False` — no shell, no string
  interpolation of untrusted content, and **no `--auto`** flag;
- `OPENCODE_CWD` blank or relative is resolved to an isolated **absolute**
  directory under `agent/tmp` (`agent/tmp/opencode_ai` by default); it must not
  contain a `.env`, so the local CLI cannot read project secrets;
- read-only `plan` agent, with no permission bypass such as
  `--dangerously-skip-permissions`;
- all untrusted chat content (user messages, history, grounded facts) is
  JSON-encoded as data, never concatenated into instructions;
- the configured provider/model is **never silently switched**;
- on a missing CLI, unauthenticated session, timeout, malformed output or an
  ungrounded answer, chat falls back **visibly** to the deterministic grounded
  facts rather than another provider.

#### OpenCode API (HTTP, distinct from the local CLI)

`opencode-api` calls the official OpenAI-compatible HTTP endpoint. It is
**HTTP/API-based and different from the local `opencode` CLI** above: no binary
is run on this machine, no local `opencode models` lookup happens, and it uses
its own `OPENCODE_API_*` variables plus a Bearer API key.

```dotenv
AI_PROVIDER=opencode-api
OPENCODE_API_BASE_URL=https://opencode.ai/inference/openai/v1
OPENCODE_API_KEY=                  # API key from the OpenCode Console
OPENCODE_API_MODEL=                # bare id from GET /inference/v1/models; no guessed default
OPENCODE_API_USER_AGENT=MamazonSpadeAgent/1.0 (+OpenCode-API)
OPENCODE_API_SESSION_ID=           # optional x-opencode-session header
OPENCODE_API_TIMEOUT=90
OPENCODE_API_MAX_IMAGES=8
OPENCODE_API_MAX_TOKENS=3000
```

Fixed request behaviour:

- `OPENCODE_API_BASE_URL` accepts either the **base** URL or a pasted **full**
  `.../chat/completions` URL; the code strips the suffix back to a single base
  before appending it, so both forms work.
- `OPENCODE_API_MODEL` accepts either a **bare id** or `opencode/<id>`; the
  `opencode/` prefix is **normalized to the bare id** for the direct API (see
  `config.effective_opencode_api_model`). Copy ids from
  `GET https://opencode.ai/inference/v1/models`. No default is guessed.
- Every request sends a custom `OPENCODE_API_USER_AGENT` plus `Accept`, and the
  optional `OPENCODE_API_SESSION_ID` as `x-opencode-session`. This is required
  because Python's default `urllib` User-Agent is rejected by Cloudflare with
  **403 error 1010**.
- The configured model is **never silently switched** to a paid one; on a
  missing key, timeout, malformed response or ungrounded answer, chat falls
  back visibly to deterministic grounded facts (with a sanitized reason) rather
  than another provider or model.

**Free models vs. the external API.** Official docs say free-tier models whose
id ends in `-free` (e.g. `nemotron-3.5-super-vl-free`) work **only inside
OpenCode**; external API harnesses get **403 / 429 `FreeUsageLimitError`**. For
`opencode-api`, use a **paid or Go model** — point `OPENCODE_API_BASE_URL` at
`https://opencode.ai/inference/go/openai/v1` — or use the free model through
`AI_PROVIDER=opencode` (local CLI). The agent never silently upgrades a free
model to a paid one; `ai-status` reports a warning instead.

Vision sends evidence screenshots as base64 `image_url` data-URL parts, so the
configured model **must be vision-capable**. The same endpoint, headers and
exact model serve **both** Vision and chat.

#### Check the active setting

```bash
cd agent
uv run python -m src.main ai-status
uv run python -m src.main config
```

`ai-status` works for all three providers (`openrouter`, `opencode`,
`opencode-api`): it reports the unified provider, exact model, the local CLI
path or API base URL, and any configuration issues without sending a prompt.
It also emits **non-blocking warnings** (for example a `-free` model on the
external API, a normalized `opencode/<id>` model, or a missing CLI path).
Warnings are advisory: `ai-status` still reports **OK** when nothing is
blocking, so a warning alone does not mean the provider is broken.
`vision-status` diagnoses the active Vision backend, and `config` prints the
wiring check, including the active `AI_PROVIDER` and its model. For
`opencode-api`, model discovery uses `GET https://opencode.ai/inference/v1/models`
as described above. `chat-status` remains a backward-compatible alias for
`ai-status`.

#### Troubleshooting

| Symptom | Cause | Fix |
| --- | --- | --- |
| `403` Cloudflare **error 1010** on `opencode-api` | Python's default `urllib` User-Agent is blocked | A custom `OPENCODE_API_USER_AGENT` is now sent on every request — verify its value (default `MamazonSpadeAgent/1.0 (+OpenCode-API)`). `Accept` and optional `x-opencode-session` are also set. |
| `429` / `FreeUsageLimitError` on `opencode-api` | a free `-free` model is blocked for external API harnesses (official docs: free models work only inside OpenCode) | Use a **paid or Go model** (`https://opencode.ai/inference/go/openai/v1`) for `AI_PROVIDER=opencode-api`, or run the free model through `AI_PROVIDER=opencode` (local CLI). The agent never switches to a paid model silently. |
| `model not found` on `opencode-api` | wrong id format | Use the **bare id** from `GET https://opencode.ai/inference/v1/models`; an `opencode/<id>` prefix is normalized to the bare id automatically. |
| CLI `WinError 2` / `FileNotFoundError` on Windows | a bare `opencode` name cannot be spawned | Set `OPENCODE_CLI` to the **absolute** `.../opencode.CMD` path (e.g. `C:/Users/<you>/AppData/Roaming/npm/opencode.CMD`); the resolver uses `shutil.which` to the real executable. |
| `OPENCODE_CWD` contains a `.env` (or is relative) | the local CLI could read project secrets | Leave `OPENCODE_CWD` **blank** (defaults to isolated `agent/tmp/opencode_ai`) or set an absolute isolated directory. Blank/relative values are resolved to an absolute path under `agent/tmp`. |
| `ai-status` shows warnings but still `OK` | warnings are non-blocking advisories (free model, normalized id, missing CLI path) | Read the warning text; the provider is still usable. Fix only what the warning names. |

All provider failures (auth, quota, timeout, malformed output) appear in the
chat fallback and the audit log as **sanitized reasons** — no keys, tokens or
raw provider payloads are ever exposed.

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
uv run python -m src.main ai-status                   # unified provider + non-blocking warnings (OK if only warnings)
uv run python -m src.main config                     # wiring check, incl. active AI field
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

`/help` shows the active AI field (provider + exact model + status/warnings);
from the CLI, `ai-status` / `chat-status` report the same, including
non-blocking warnings. Chat failures now include a sanitized reason in the
fallback answer and the audit log.

Passive listeners: `#blast-tugas` is parsed automatically (✅ reaction + reply),
`#bukti-tambahan` accepts `Submission | @ig | Deskripsi` uploads, `#ai-chat`
supports natural Indonesian conversation, greetings and lightweight emotional
acknowledgment, plus the bounded agent tools described in **Agent tools and
confirmed execution** below. It uses the active unified `AI_PROVIDER` model
(OpenRouter, local OpenCode CLI, or OpenCode API), grounded in read-only rulebase
and current organization data. Set `CHAT_ENABLED=false` for offline deterministic
answers (this only disables chat rewriting, not Vision). On timeout, quota
failure or empty model output it visibly falls back to grounded answers and now
shows a **sanitized reason** (no secrets). Recent context is kept per guild,
channel and member (12 turns, 30-minute inactivity expiry, at most 100 sessions),
not persisted or shared between members. Only validator roles retain chat access.
Chat never executes arbitrary code; any mutating/expensive action must be
confirmed through an explicit Discord button with the same role gate and audit
entry (see below).

### New interactive commands

- `/help`: private, role-aware command guide and clear action boundaries. It
  also shows the active **AI field** — the unified `AI_PROVIDER`, its exact
  model and current status/warnings — so a member can see which backend is
  answering.
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

## Agent tools and confirmed execution

`#ai-chat` exposes a small, bounded toolkit — **no LangChain and no arbitrary
code execution**. Everything is enabled by `CHAT_TOOLS_ENABLED` (default `true`)
and obeys the same role gates as the equivalent slash commands.

Explicit tool commands (Indonesian):

| Command | What it does |
| --- | --- |
| `ingat <fakta>` / `catat <fakta>` | store an explicit long-term memory fact for the calling member |
| `lihat ingatan` / `recall` | read back stored facts |
| `lupa semua` / `forget` | clear the member's stored facts |
| `hitung <ekspresi>` | safe arithmetic only (a parsed calculator, not `eval`) |
| `cari aturan <query>` | search the local rulebase |
| `cari web <query>` | optional web search, **only** when `WEB_SEARCH_URL` is configured |
| `status ai` | report the active provider/model and non-blocking warnings |

Short-term conversation memory already exists (12 turns, per guild/channel/
member). Long-term memory is explicit and opt-in per member, persisted to
`agent/output/chat_memory.json` (`CHAT_MEMORY_PATH`), capped by
`CHAT_MEMORY_MAX_ITEMS` (default 20) and toggled by `CHAT_MEMORY_ENABLED`. Web
search additionally needs `WEB_SEARCH_URL` and optionally `WEB_SEARCH_API_KEY`
(sent as a Bearer token); `WEB_SEARCH_TIMEOUT` and `WEB_SEARCH_MAX_RESULTS`
bound it. Without `WEB_SEARCH_URL`, `cari web` replies that search is not
configured and never falls back to a live provider.

Read-only data access in chat: with the **same role gates** as the slash
commands, chat can surface denda members, the Angel Card registry, Google Sheets
metadata/headers/sample rows, task overview, audit log, resubmit status and
`ketentuan`. `DISCORD_MEMBER_MAP` still gates private panitia fine data by
immutable Discord user ID — an unbound panitia is denied rather than trusted by
nickname.

Mutating / expensive commands (`validate`, `blast`, `add-angel`, `remove-angel`,
`scan-blast`, `hold-blast`, `release-blast`) are **never** run just because a
user typed them. Chat only prepares an explicit **Discord confirmation button**;
the action runs after the button is pressed, under the same role gate and with
an audit-log entry. `blast` still **only creates a draft** — sending to `#denda`
always requires koor approval through the existing approval button. There is no
arbitrary code execution anywhere in the chat path.

## Pipeline

```
sheet query ─▶ resolve sheet (structure-aware fuzzy)
            ─▶ resolve task  (rulebase.sheet → config map → fuzzy)
            ─▶ merge rulebase.json + blast_context.json
            ─▶ Angel Card check (beli ≤ blast → EXEMPT)
            ─▶ dedupe resubmits (latest timestamp wins)
            ─▶ download evidence (hash dedup, compress, 403 → review)
            ─▶ VisionFacts (mock | OpenRouter | OpenCode CLI | OpenCode API)
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
| `vision_checker.py` | `MockVision` (deterministic); `OpenRouterVision`, `OpenCodeVision` (local CLI) and the `opencode-api` HTTP adapter — all selected by the unified `AI_PROVIDER`; `UnavailableVision` |
| `drive_client.py` | `MockDrive` (synthetic images, 403 simulation) and `GoogleDrive` |
| `image_manager.py` | download, SHA-256 dedup, compression, TTL cleanup |
| `output_generator.py` | CSV upserts, blast draft, audit log, blast state machine |
| `blast_reader.py` | `#blast-tugas` → `blast_context.json` |
| `bukti_tambahan.py` | PJ upload parsing and validation |
| `evidence_vault.py` | violation entries, recurring escalation |
| `validator.py` | orchestrator + reporting helpers |
| `discord_bot.py` | commands, embeds, buttons, role gating |
| `chat_agent.py` | grounded read-only facts + unified OpenRouter / OpenCode CLI / OpenCode API conversation, scope guard and bounded user history |

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
