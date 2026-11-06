# Mamazon Spade Agent — Developer Handoff Guide

## Untuk Developer (Rekan Tim kak wldnz)

|   |   |
| - | - |

> **Tanggal**: 5 November 2026 | **Deadline**: 7 November 2026  
> **Stack**: Python 3.10+ / discord.py / Tencent Hunyuan / Google Drive API

---

## 1. File yang SUDAH ADA (Bisa Langsung Dipakai)

File berikut sudah dibuat dan tested. **Tidak perlu dibuat ulang**, tinggal pakai dan extend.

### Source Code

| File               | Lokasi                    | Fungsi                                                                         | Status          |
| ------------------ | ------------------------- | ------------------------------------------------------------------------------ | --------------- |
| `config.py`        | `agent/src/config.py`     | Paths, API keys (env vars), sheet-to-task mapping, constants                   | ✅ Done, updated |
| `data_layer.py`    | `agent/src/data_layer.py` | Spreadsheet reader, comma-split link parser, Angel Card checker, roster loader | ✅ Done & tested |
| `main.py`          | `agent/src/main.py`       | CLI: `list-sheets`, `validate --dry-run`, `angel-card`                         | ✅ Done & tested |
| `requirements.txt` | `agent/requirements.txt`  | Dependencies (discord.py added, telegram removed)                              | ✅ Done          |

### Data & Config

| File                            | Lokasi                                | Fungsi                                                                                                                                                   |
| ------------------------------- | ------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------- |
| `rulebase.json`                 | `_refs/rulebase.json`                 | 18 aturan PP/Sponsor, Angel Card, 6 PP + 6 Buzzer tasks, 17 checks (C1-C10, B1-B7), guardrail, tiered matching, anti-cheat, custom_requirements per task |
| `dummy_dataset_anonymized.xlsx` | `_refs/dummy_dataset_anonymized.xlsx` | 106 panitia, 13 sheets, Angel Card Registry (17 entries). Names anonymized.                                                                              |
| `build_dummy.py`                | `_refs/build_dummy.py`                | Script rebuild dummy dataset                                                                                                                             |
| `build_rulebase.py`             | `_refs/build_rulebase.py`             | Script rebuild rulebase.json                                                                                                                             |

### Dokumentasi

| File                                       | Lokasi | Fungsi                                                                                      |
| ------------------------------------------ | ------ | ------------------------------------------------------------------------------------------- |
| `Mamazon_Spade_Agent_Planning.md`          | Root   | Planning document (arsitektur, stack, rulebase, edge cases, output artifacts, roadmap)      |
| `Mamazon_Spade_Agent_Workflow.md`          | Root   | Workflow document (happy/unhappy flows untuk semua command, role matrix, channel structure) |
| `Mamazon_Spade_Agent_Specification_v2.pdf` | Root   | Spec document (12 halaman, untuk baca cepat)                                                |

---

## 2. Cara Test yang Sudah Ada

```bash
# Install dependencies
cd agent
pip install -r requirements.txt

# List semua sheet di dummy dataset
python src/main.py list-sheets

# Dry-run validasi (no API calls, just parse data)
python src/main.py validate --sheet "PP 29 Oktober" --dry-run

# Lihat Angel Card registry
python src/main.py angel-card
```

**Expected output dry-run PP 29 Oktober:**

- 42 submissions
- 34 multi-link (parsed correctly — comma split working)
- 0 no-link

---

## 3. File yang Perlu Dibuat (TODO)

Urutan prioritas:

| # | File                  | Estimasi | Dependencies                 | Deskripsi                                                                             |
| - | --------------------- | -------- | ---------------------------- | ------------------------------------------------------------------------------------- |
| 1 | `discord_bot.py`      | 4h       | discord.py, config.py        | Bot skeleton: slash commands, role-gating, embeds, buttons                            |
| 2 | `rulebase_engine.py`  | 3h       | rulebase.json, data_layer.py | Load rulebase + blast_context, run C1-C10/B1-B7 checks                                |
| 3 | `vision_checker.py`   | 3h       | Tencent SDK                  | Vision API integration. **Mulai dengan mock/stub**, swap ke real via config flag      |
| 4 | `output_generator.py` | 2h       | data_layer.py                | Generate verification_report.csv, violations_log.csv, blast_draft.md, audit_log.jsonl |
| 5 | `blast_reader.py`     | 2h       | discord.py, rulebase.json    | Auto-read #blast-tugas channel, extract ketentuan tambahan, save blast_context.json   |
| 6 | `image_manager.py`    | 1h       | Google Drive API             | Download images, hash dedup, TTL cleanup (24h), rate-limit aware, compress >2MB       |
| 7 | `bukti_tambahan.py`   | 2h       | data_layer.py                | Handle PJ upload screenshot, cross-reference dengan submission                        |
| 8 | `evidence_vault.py`   | 1h       | output_generator.py          | Auto-post violations ke #evidence, detect recurring (3x+ → mention koor)              |

---

## 4. Discord Server Setup

### Channels yang Perlu Dibuat

```
#pp-validation       — Hasil validasi PP
#buzzer-validation   — Hasil validasi Buzzer
#blast-drafts        — Draft blast denda + buttons [Approve][Hold][Reject]
#blast-tugas         — Koor blast tugas baru (bot auto-read)
#bukti-tambahan      — PJ upload screenshot story
#evidence            — Bot auto-post violation evidence
#audit-log           — History semua command
#ai-chat             — Percakapan dengan AI agent
#denda               — Blast denda (read-only untuk @panitia, edit BPH+koor)
#ketentuan           — Daftar ketentuan (read-only untuk @panitia)
```

### Roles yang Perlu Dibuat

```
@BPH         — Administrator (full access)
@koor-spade  — Manager (all commands except delete channel)
@spade       — Validator (/validate, /check-angel, /list-active, /denda-list, /ketentuan-*)
@panitia     — End user (/denda-list own, /ketentuan-*, read #denda, #ketentuan, #blast-tugas)
```

### Slash Commands

```
/validate sheet:"PP 29 Oktober"
/check-angel
/add-angel nama:"Budi" tanggal:"2026-10-03" divisi:"Kindaichi (Keamanan)"
/remove-angel nama:"Budi"
/blast sheet:"PP Feed 26 Oktober"
/hold-blast blast_id:"blast_20261105_083000"
/release-blast blast_id:"blast_20261105_083000"
/list-active
/resubmit-status nama:"raihanrizky"
/audit-log filter:"tanggal:2026-11-05"
/denda-list nama:"raihanrizky"     (panitia: own only, spade+: all)
/ketentuan-pp task_id:"PP-03"      (semua role)
/ketentuan-buzzer task_id:"BZ-06"  (semua role)
/ketentuan-angel-card              (semua role)
/scan-blast days:"7"               (koor+)
```

---

## 5. Environment Variables

```bash
# Tencent Cloud (atau pakai mock vision)
export TENCENT_SECRET_ID="your_secret_id"
export TENCENT_SECRET_KEY="your_secret_key"
export TENCENT_REGION="ap-jakarta"

# Discord Bot
export DISCORD_BOT_TOKEN="your_bot_token"
export DISCORD_GUILD_ID="your_server_id"
export DISCORD_BLAST_TUGAS_CHANNEL_ID="channel_id"
export DISCORD_DENDA_CHANNEL_ID="channel_id"
export DISCORD_BLAST_DRAFTS_CHANNEL_ID="channel_id"
export DISCORD_AUDIT_LOG_CHANNEL_ID="channel_id"
export DISCORD_PP_VALIDATION_CHANNEL_ID="channel_id"
export DISCORD_BUZZER_VALIDATION_CHANNEL_ID="channel_id"
```

---

## 6. Konsep Penting yang WAJIB Dipahami

### 6.1 Dua Layer Ketentuan (PENTING!)

Setiap blast tugas dari penyewa jasa punya **ketentuan tambahan yang spesifik per blast**. Agent WAJIB baca ini, tidak hanya rely pada rulebase.json.

| Layer                | Source                      | Sifat   | Isi                                                                           |
| -------------------- | --------------------------- | ------- | ----------------------------------------------------------------------------- |
| `rulebase.json`      | File static, developer edit | Tetap   | Denda, checks, task IDs, jadwal dasar                                         |
| `blast_context.json` | #blast-tugas channel scan   | Dynamic | Caption referensi, tag spesifik, hashtag, link Drive, deadline, catatan klien |

**Saat /validate:** Agent MERGE rulebase.json + blast_context.json.

**Contoh:** PP-03 punya caption custom dari klien jokiproyek (234 chars, verbatim). PP-07 punya caption dari gambar referensi. BZ-06 punya pembagian postingan per divisi. BZ-02 punya template ulasan per divisi.

### 6.2 Rulebase = Static

Rulebase.json **tidak berubah saat runtime**. Tugas baru tidak otomatis masuk. Kalau ada peraturan baru:

1. Developer edit rulebase.json
2. Restart bot → reload
3. Atau: koor blast di #blast-tugas → bot extract ke blast_context.json (tanpa restart)

### 6.3 Image Management

Downloaded images **TIDAK disimpan permanent**:

- TTL 24h di `tmp/images/`
- Hash dedup (SHA-256) — skip download kalau gambar sama
- Compress >2MB → <500KB sebelum Vision API
- Evidence permanent disimpan di Drive, bukan local

### 6.4 Mock Vision First

Mulai dengan mock/stub Vision API. Tidak butuh API key. Swap ke real via `USE_MOCK_VISION = False`.

### 6.5 Scaling — CSV Tidak Akan Membengkak

- Max ~1,272 rows per periode (106 panitia × 12 tugas)
- Sudah VERIFIED → skip (tidak re-validasi)
- CSV reader: streaming (tidak load semua ke RAM)
- Archive setelah 30 hari

---

## 7. Flow Singkat: Validasi PP

```
1. User: /validate sheet:"PP Feed 26 Oktober"
2. Bot check role → spade/koor/BPH? → YES
3. Bot resolve sheet → match PP-03 di rulebase
4. Bot load rulebase.json (PP-03 config) + blast_context.json (ketentuan tambahan)
5. Bot post progress embed
6. Python agent:
   a. data_layer: baca spreadsheet, parse Drive links (comma split)
   b. Angel Card cross-check (date-based: beli ≤ blast = exempt)
   c. For each submission:
      - image_manager: download bukti from Drive (hash dedup, compress)
      - vision_checker: Vision API baca caption, tag, hashtag, carousel, followers
      - rulebase_engine: apply C1-C10 dari MERGED context (rulebase + blast_context)
      - Flag violations with rule_id + evidence_link
   d. output_generator: write CSV + audit log
7. Bot post results embed di #pp-validation
8. Bot auto-post violations ke #evidence
```

---

## 8. Yang Perlu dari kak wldnz

- [ ] Discord bot token (Discord Developer Portal → Bot → Token)
- [ ] Discord server ID + role IDs
- [ ] Google Cloud service account JSON key
- [ ] Tencent Cloud API key (atau pakai mock dulu)
- [ ] Konfirmasi "gas"

---

> **Pertanyaan teknis?** Baca `Mamazon_Spade_Agent_Workflow.md` (happy/unhappy flows) dan `Mamazon_Spade_Agent_Planning.md` (arsitektur detail).  
> **File data ada di `_refs/` folder.**  
> **Test CLI dulu sebelum build Discord bot.**
