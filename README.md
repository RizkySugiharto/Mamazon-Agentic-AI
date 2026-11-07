# Mamazon Spade Office Agent

Discord bot + Python validation core for Mamazon's Paid Promote (PP) and Buzzer submissions. Splits comma-joined Google Drive links that break manual review, reads evidence screenshots with Vision, executes 17 rulebase checks (C1–C10 / B1–B7) merged with per-task requirements from `#blast-tugas`, and drafts fine blasts gated by coordinator approval before `#denda`. Features voice note transcription (Whisper) and an agentic chatbot (LangGraph + local `zvec` vector store + persistent SQLite memory + bounded tools).

Built for Tencent Cloud Hackathon 2026 — Challenge B.

---

## Quick start

Project managed with [uv](https://docs.astral.sh/uv/):

```bash
cd agent

uv sync                                # install dependencies from pyproject.toml + uv.lock

uv run python -m src.main config       # wiring check (paths, 9Router, Drive, Discord)
uv run python -m src.main lint         # check rule data issues
uv run python -m src.main google-status # service account, Sheets, Drive diagnostics
uv run python -m src.main ai-status    # 9Router connectivity and active model/combo
uv run python -m src.main kb-build     # build/rebuild local vector knowledge base
uv run python -m src.main demo         # offline demo run (mock vision/drive)
uv run pytest                          # run full test suite (540+ tests)
```

Common operations:

```bash
uv add <package>              # add dependency to pyproject.toml + uv.lock
uv remove <package>           # remove dependency
uv lock --upgrade             # refresh lockfile
```

---

## Offline demo (no keys required)

```bash
cd agent
uv run python ../_refs/build_dummy.py   # build fixture workbook
uv run python -m src.main demo          # blast extract + 3 sheets + recap
```

Demo runs on mock Vision backend and mock Drive without external credentials.

---

## Configuration & environment

Copy the template:

```bash
cp agent/.env.example agent/.env
```

Key environment variables:

| Variable | Description |
| --- | --- |
| `DISCORD_BOT_TOKEN`, `DISCORD_GUILD_ID`, `DISCORD_*_CHANNEL_ID` | Discord bot connection and channel routing |
| `NINEROUTER_BASE_URL` | 9Router gateway base URL (default: `http://localhost:20128/v1`) |
| `NINEROUTER_API_KEY` | 9Router Bearer API key |
| `NINEROUTER_MODEL` | Exact model ID or combo name (default: `mamazon`) |
| `NINEROUTER_WHISPER_MODEL` | Audio transcription model (default: `groq/whisper-large-v3-turbo`) |
| `VOICE_DETECTION_ENABLED` | Enable voice note / audio transcription in `#ai-chat` (default: `true`) |
| `SHEET_SOURCE` | `xlsx` (local fixture) or `gsheets` (live Google Sheets) |
| `GOOGLE_SPREADSHEET_ID` | Google Spreadsheet ID or URL |
| `DRIVE_PROVIDER` | `auto` (uses Google Drive if service account exists), `google`, or `mock` |
| `AI_OFFLINE` | Set `true` to force deterministic mock Vision/chat for offline tests |

Run the bot:

```bash
cd agent && uv run python -m src.discord_bot
```

---

## Unified AI provider: 9Router

9Router is the sole production AI backend for both Vision screenshot analysis and `#ai-chat` conversation, plus Whisper audio transcription.

| Setting | Purpose | Default |
| --- | --- | --- |
| `NINEROUTER_BASE_URL` | Gateway OpenAI-compatible endpoint | `http://localhost:20128/v1` |
| `NINEROUTER_API_KEY` | Bearer authentication key | — |
| `NINEROUTER_MODEL` | Model ID or gateway combo name | `mamazon` |
| `NINEROUTER_TIMEOUT` | Request timeout in seconds | `120` |
| `NINEROUTER_MAX_IMAGES` | Max evidence screenshots per prompt | `8` |
| `NINEROUTER_MAX_TOKENS` | Completion token limit | `3000` |
| `NINEROUTER_JSON_MODE` | Request structured JSON output | `true` |
| `NINEROUTER_REASONING_EFFORT` | Reasoning effort for reasoning models (`low`/`medium`/`high`) | — |
| `NINEROUTER_WHISPER_MODEL` | Audio transcription model for voice messages | `groq/whisper-large-v3-turbo` |
| `VOICE_DETECTION_ENABLED` | Transcribe voice notes in `#ai-chat` | `true` |

Legacy and removed provider names (`openrouter`, `opencode`, `opencode-api`, `tencent`, `antigravity`, etc.) are blocked and rejected explicitly.

### Diagnostics

```bash
cd agent
uv run python -m src.main ai-status                    # report 9Router config, model & warnings
uv run python -m src.main vision-status --models 20    # list vision-capable models/combos from gateway
uv run python -m src.main vision-status --test-image   # send smoke-test image to verify OCR
```

---

## Agentic chat & knowledge base

`#ai-chat` features an agentic workflow powered by LangChain and LangGraph:

- **Local Vector Store (`zvec`)**: Embedded vector database with `fastembed` (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`). No external database server required.
- **Knowledge Base**: Indexed from committee rules (`agent/data/rules/agent_rules.md`) and task definitions.
- **Persistent Memory**: SQLite conversation checkpointing (`agent/output/persistent_memory.sqlite`) and explicit member memory (`agent/output/chat_memory.json`).
- **Speech-to-Text**: Voice notes (`.ogg`, `.mp3`, `.m4a`, `.wav`) sent in `#ai-chat` are transcribed via Whisper and processed directly.
- **Bounded Tool Calling**: Safe AST-evaluated arithmetic (`hitung`), local rule search (`cari aturan`), member memory (`ingat`/`catat`/`recall`/`lupa`), web search (`cari web`), web scraper, and MCP client integration.
- **Confirmed Execution**: Mutating operations (`validate`, `blast`, `add-angel`, `remove-angel`, `scan-blast`, `hold-blast`, `release-blast`) generate interactive Discord confirmation buttons with strict role checks and audit logging.

Knowledge base commands:

```bash
uv run python -m src.main kb-build     # build/refresh vector index from markdown rules
uv run python -m src.main kb-status    # verify index doc counts and freshness
uv run python -m src.main kb-search "<query>"  # test hybrid vector + FTS retrieval
```

---

## Google Sheets & Drive integration

Uses GCP service account key at `agent/config/service_account.json`.

```bash
# 1. Share Google Spreadsheet & Drive folder with service account email as Viewer
# 2. Configure agent/.env:
SHEET_SOURCE=gsheets
GOOGLE_SPREADSHEET_ID=<spreadsheet_id_or_url>
DRIVE_PROVIDER=google

# 3. Diagnose access
cd agent
uv run python -m src.main google-status --rows 5
```

---

## Discord bot

The bot registers 19 slash commands:

| Command | Role Gate | Description |
| --- | --- | --- |
| `/help` | Everyone | Role-aware command guide and active AI status |
| `/validate` | `@spade`, `@koor-spade`, `@BPH` | Run submission validation on a sheet, with division filter and cancel button |
| `/blast` | `@koor-spade`, `@BPH` | Generate fine blast draft |
| `/hold-blast` | `@koor-spade`, `@BPH` | Hold pending fine blast |
| `/release-blast` | `@koor-spade`, `@BPH` | Release held blast draft |
| `/list-active` | Everyone | Show active PP and Buzzer task status |
| `/check-angel` | Everyone | View Angel Card registry or query member exemption |
| `/add-angel` | `@koor-spade`, `@BPH` | Register an Angel Card purchase |
| `/remove-angel` | `@koor-spade`, `@BPH` | Remove Angel Card entry |
| `/denda-list` | Privileged / Panitia (self) | Paginated active fines; panitia locked to own Discord ID |
| `/resubmit-status`| Everyone | Check resubmission status for a member |
| `/audit-log` | Privileged | View append-only audit trail |
| `/ketentuan-pp` | Everyone | Paid Promote task requirements with button link to `#blast-tugas` |
| `/ketentuan-buzzer` | Everyone | Buzzer task requirements with button link to `#blast-tugas` |
| `/ketentuan-angel-card` | Everyone | Angel Card rules and purchase conditions |
| `/scan-blast` | Privileged | Parse `#blast-tugas` messages into `blast_context.json` |
| `/list-sheets` | Privileged | List submission sheets and multi-link ratios |
| `/roster-set-discord` | Privileged | Map Discord user ID to exact roster name |
| `/roster-remove-discord` | Privileged | Remove Discord user ID roster binding |

Passive listeners:
- `#blast-tugas`: Ingests new task broadcasts into `blast_context.json`.
- `#bukti-tambahan`: Ingests late/additional proof uploads.
- `#ai-chat`: Handles text and voice notes with grounded Indonesian responses.

---

## CLI reference

```bash
uv run python -m src.main list-sheets                  # inspect sheets, row counts, multi-links
uv run python -m src.main lint                         # validate markdown rule definitions
uv run python -m src.main validate --sheet "PP 29 Oktober" # validate sheet
uv run python -m src.main validate --sheet "PP 26 Oktober" --dry-run # dry-run without Vision
uv run python -m src.main angel-card --check "Nama"    # check member Angel Card status
uv run python -m src.main denda --nama "Nama"          # show recorded fines
uv run python -m src.main ketentuan --task PP-03       # show merged task specifications
uv run python -m src.main scan-blast --demo            # test blast text parsing
uv run python -m src.main vision-status --models 20 --test-image # test 9Router Vision
uv run python -m src.main ai-status                    # check AI provider connectivity
uv run python -m src.main google-status --rows 5       # check Google Sheets/Drive API
uv run python -m src.main kb-build                     # rebuild local knowledge base
uv run python -m src.main kb-search "denda caption"    # query knowledge base
uv run python -m src.main cleanup                      # clean expired cached images
```

---

## Architecture & modules

```
Spreadsheet/Drive ─▶ data_layer / sheets_client / drive_client
                   ─▶ blast_reader (extracts #blast-tugas context)
                   ─▶ agent.evaluator (merges rules + context + Angel Card)
                   ─▶ image_manager (download, SHA-256 dedupe, compress)
                   ─▶ vision_checker (9Router multimodal OCR)
                   ─▶ validator (executes C1–C10 / B1–B7 checks)
                   ─▶ output_generator (reports, fine logs, audit log, blast drafts)
                   ─▶ discord_bot (UI, slash commands, approval buttons)
                   ─▶ voice_transcriber (9Router Whisper speech-to-text)
                   ─▶ agent (LangGraph + zvec vectorstore + persistent memory)
```

| Component | Responsibility |
| --- | --- |
| `src/config.py` | Environment, paths, 9Router configuration, permission matrix |
| `src/agent/evaluator.py` | 17 validation checks (C1–C10 / B1–B7), Angel Card exemption, rule linting |
| `src/agent/graph.py` | LangGraph multi-turn conversation graph and state handling |
| `src/agent/knowledge.py` | Corpus builder and hybrid search indexing |
| `src/agent/vectorstore.py` | Local `zvec` embedded vector store adapter |
| `src/agent/tools.py` | Safe calculator, rule lookup, web search, scraper, memory tools |
| `src/voice_transcriber.py`| Audio download and Whisper transcription via 9Router |
| `src/vision_checker.py` | `NineRouterVision` multimodal adapter and `MockVision` |
| `src/sheets_client.py` | Google Sheets API client and local xlsx reader |
| `src/drive_client.py` | Google Drive API evidence downloader and mock Drive |
| `src/data_layer.py` | Roster resolution, Angel registry, submission deduplication |
| `src/image_manager.py` | Image downloading, SHA-256 deduplication, caching, TTL cleanup |
| `src/output_generator.py` | CSV reports, fine logs, audit logs, and blast state machine |
| `src/validator.py` | Submission validation pipeline orchestrator |
| `src/discord_bot.py` | Discord client, slash commands, views, and button interactions |
| `src/chat_agent.py` | Grounded chat fallback and interactive confirmation dispatch |
| `src/memory_store.py` | SQLite conversation and member memory persistence |
| `src/mcp_client.py` | Model Context Protocol client integration |
| `src/web_scraper.py` | Web content retrieval for grounded research |

---

## Output artifacts

All outputs are saved to `agent/output/`:

| File | Content |
| --- | --- |
| `verification_report.csv` | Upserted submission status per row (`VERIFIED`, `FLAGGED`, `EXEMPT`) |
| `violations_log.csv` | Detailed log of individual rule violations |
| `blast_draft.md` | Formatted fine blast message grouped by division |
| `audit_log.jsonl` | Append-only decision trail with actor, action, and rule IDs |
| `blast_context.json` | Extracted task parameters from `#blast-tugas` |
| `persistent_memory.sqlite`| Checkpointed conversational state for LangGraph chat |
| `chat_memory.json` | Explicit member memory store |
| `blasts/<id>.json` | Fine blast approval state machine |

---

## Tests

Run the test suite:

```bash
cd agent
uv run pytest
```

540+ tests cover validation checks, link splitting, Angel Card exemption, blast state machine, 9Router integration, voice transcription, LangGraph agent workflows, and vector store retrieval.
