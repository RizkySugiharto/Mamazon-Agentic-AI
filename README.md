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

## Agentic RAG Architecture & Orchestration

The `#ai-chat` conversational interface is built as a stateful, cyclical **Agentic Retrieval-Augmented Generation (RAG)** pipeline orchestrated via [LangGraph](https://github.com/langchain-ai/langgraph), [LangChain](https://github.com/langchain-ai/langchain), and an embedded, serverless [`zvec`](https://github.com/alibaba/zvec) vector store.

### Orchestration Graph

```
[User Input (Text / Voice)] ──▶ [Whisper STT (9Router)]
                                     │
┌────────────────────────────────────┴────────────────────────────────────┐
│                    LangGraph Stateful Execution Loop                    │
│                                                                         │
│   START ──▶ [ guard ] ────── blocked? ──────▶ [ respond ] ──▶ END       │
│                │                                   ▲                    │
│             passed                                 │                    │
│                ▼                                   │                    │
│          [ retrieve ]                              │                    │
│                │                                   │                    │
│        (zvec embeddings)                           │                    │
│                ▼                                   │                    │
│          ┌─▶ [ agent ] ──── final answer ──────────┘                    │
│          │     ▲   │                                                    │
│          │     │   └── tool call ──┐                                    │
│          │     └───────────────────┴───▶ [ tools ] (role-gated sandbox) │
└──────────┼──────────────────────────────────────────────────────────────┘
           ▼
[Checkpointer] ──▶ SqliteCheckpointSaver (`agent/output/persistent_memory.sqlite`)
```

### Graph Nodes & State Lifecycle

Execution state (`AgentState`) travels through specialized, isolated nodes:

1. **`guard` (Boundary & Scope Control)**:
   - Evaluates incoming prompts against hard boundaries (developer-only rule modifications, out-of-scope discussions, direct mutating commands).
   - Injects natural guidance dynamically without consuming model inference tokens when refusing invalid attempts.
2. **`retrieve` (Context Augmentation via Local RAG)**:
   - Performs dense semantic vector search against the local `zvec` collection.
   - Extracts relevant rules, check specs (C1–C10 / B1–B7), and task requirements above the score floor (`CHAT_AGENT_MIN_SCORE = 0.45`).
3. **`agent` (ReAct Reasoning Engine)**:
   - Interacts with the active 9Router model via dynamic tool binding (`bind_tools`).
   - Autonomously determines whether to synthesize an immediate response or request domain tool execution.
4. **`tools` (Role-Gated Tool Sandbox)**:
   - Dispatches read-only and analytical tools with caller role verification (`@BPH`, `@koor-spade`, `@spade`, `@panitia`).
   - Returns structured `ToolMessage` outputs back to the agent reasoning cycle.
   - Bounded by `CHAT_AGENT_MAX_STEPS` (default: 6) to eliminate infinite tool loops.
5. **`respond` (Grounding & Action Staging)**:
   - Validates outputs against hallucination filters (`_assert_grounded_output`).
   - For mutating actions (`validate`, `blast`, `add-angel`, etc.), formats an interactive **Discord confirmation button view** (`pending_action`) instead of executing side effects unconfirmed.

### Knowledge Retrieval Engine (`zvec` + FastEmbed)

- **Zero-Daemon Vector Storage**: Embedded `zvec` engine operates in-process with C++ performance. No external database servers or Docker containers required.
- **Local ONNX Embeddings**: Vectorized via `fastembed` using `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2` (384-dimensional dense vectors with L2 normalization for exact cosine similarity).
- **Corpus & Automated Hot-Reload**: Ingests Markdown sources (`agent/data/rules/agent_rules.md`, `pp_tasks.md`, `buzzer_tasks.md`). Evaluates a SHA-256 fingerprint on boot and automatically triggers hot re-indexing on file modification (`KB_REBUILD_ON_CHANGE=true`).
- **Score Gating**: Dense similarity scores are calibrated (on-topic rules ~0.70–0.80, off-topic noise ~0.37) with a hard cutoff to prevent out-of-distribution hallucinations.

Knowledge base commands:

```bash
uv run python -m src.main kb-build     # build/rebuild local vector index from markdown rules
uv run python -m src.main kb-status    # report vector index document counts and freshness
uv run python -m src.main kb-search "<query>"  # test hybrid vector retrieval with score ranking
```

### Multi-Tier Memory & Multi-Modal Processing

- **Session Checkpointing**: Backed by `SqliteCheckpointSaver` (`persistent_memory.sqlite`), isolating conversation threads by guild, channel, and member ID with multi-turn context retention.
- **Explicit Long-Term Memory**: Per-member memory store (`agent/output/chat_memory.json`) managed via explicit user instructions (`ingat <fakta>`, `catat <fakta>`, `recall`, `lupa semua`).
- **Multi-Modal Voice Ingestion**: Discord audio attachments (`.ogg`, `.mp3`, `.m4a`, `.wav`) are intercepted, downloaded to temporary storage, and transcribed via 9Router Groq Whisper (`whisper-large-v3-turbo`) before injection into the graph state.

### Bounded Tool Sandbox

Chat agents operate under strict non-destructive constraints (no arbitrary code execution, no shell access, no direct file writes):

| Tool | Capability | Access Control |
| --- | --- | --- |
| `hitung` | Safe arithmetic evaluator using AST whitelisting (no `eval`) | All members |
| `cari aturan` | Exact and semantic rule lookup across the committee rulebase | All members |
| `ingat` / `recall` / `forget` | User-scoped explicit fact persistence | Calling member only |
| `denda panitia` | Fine totals and per-task infractions | Roster-bound panitia (self only) / Privileged (all) |
| `ketentuan tugas` | Task specifications, deadlines, hashtags, tags, and media links | All members |
| `angel card` | Angel Card registry lookup and exemption checks | All members |
| `audit log` | Tail of system validation events and administrative decisions | Privileged roles |
| `cari web` | External web search (requires `WEB_SEARCH_URL`) | Privileged roles |
| `pending_action` | Action planner with interactive Discord button confirmation | Role-gated confirmation |

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
