# Mamazon Spade Agent — Workflow Detail (Happy & Unhappy Flow)

## Tencent Cloud Hackathon 2026 — Challenge B: The Office Agent

|   |   |
| - | - |

|   |   |
| - | - |

|   |   |
| - | - |

> **Versi**: 2.1 (Discord Bot + Ketentuan Tambahan + Image Management) | **Tanggal**: 5 November 2026  
> **Tim**: kak wldnz + rekan | **Deadline**: 7 November 2026  
> **Status**: Pre-build — arsitektur finalized, ready untuk coding

---

## Daftar Isi

1. [Arsitektur Baru: Discord + WorkBuddy](#1-arsitektur-baru-discord--workbuddy)
2. [Role & Permission Matrix](#2-role--permission-matrix)
3. [Channel Structure](#3-channel-structure)
4. [Command List](#4-command-list)
5. [Workflow: Validasi PP + Ketentuan Tambahan Per-Blast](#5-workflow-validasi-pp--ketentuan-tambahan-per-blast)
6. [Workflow: Validasi Buzzer](#6-workflow-validasi-buzzer)
7. [Workflow: Blast Denda + Approval Gate](#7-workflow-blast-denda--approval-gate)
8. [Workflow: List Active & Missed Tasks](#8-workflow-list-active--missed-tasks)
9. [Workflow: Bukti Tambahan](#9-workflow-bukti-tambahan)
10. [Workflow: Evidence Vault (Auto by Bot)](#10-workflow-evidence-vault-auto-by-bot)
11. [Workflow: Audit Log](#11-workflow-audit-log)
12. [Workflow: Angel Card Management](#12-workflow-angel-card-management)
13. [Workflow: AI Chat (#ai-chat)](#13-workflow-ai-chat-ai-chat)
14. [Workflow: Resubmit Handling](#14-workflow-resubmit-handling)
15. [Mapping ke 5 Prinsip Hackathon](#15-mapping-ke-5-prinsip-hackathon)
16. [Mapping ke Aturan Organisasi Mamazon](#16-mapping-ke-aturan-organisasi-mamazon)
17. [Edge Cases Summary](#17-edge-cases-summary)

---

## 1. Arsitektur Baru: Discord + WorkBuddy

```
┌─────────────────────────────────────────────────────────────┐
│                   DISCORD SERVER (Mamazon)                   │
│                                                              │
│  Channels:                                                   │
│  #pp-validation  #buzzer-validation  #blast-drafts           │
│  #blast-tugas (koor blast, bot auto-read ketentuan)         │
│  #bukti-tambahan #evidence  #audit-log  #ai-chat             │
│  #denda (read-only @panitia, edit BPH+koor)                 │
│  #ketentuan (read-only @panitia, auto dari rulebase)        │
│                                                              │
│  Roles:                                                      │
│  @BPH (admin)  @koor-spade  @spade  @panitia+divisi          │
│                                                              │
│  Slash Commands:                                             │
│  /validate  /check-angel  /add-angel  /remove-angel          │
│  /blast  /list-active  /resubmit-status  /audit-log          │
│  /denda-list  /hold-blast  /release-blast                    │
│  /ketentuan-pp  /ketentuan-buzzer  /ketentuan-angel-card     │
│  /scan-blast                                                 │
│                                                              │
│  Interactive:                                                │
│  [Approve] [Reject] [Hold] buttons on blast drafts           │
│  Embed messages (color-coded: green=aman, red=denda)         │
│  Progress indicators for long-running tasks                  │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
┌─────────────────────────────────────────────────────────────┐
│              PYTHON BOT (discord.py)                         │
│                                                              │
│  • Terima command dari Discord                               │
│  • Check role permission (role-gating)                       │
│  • Lightweight response (instant): list, status, ketentuan   │
│  • Heavy task → delegate ke Python agent core                │
│  • Auto-read #blast-tugas untuk extract ketentuan tambahan   │
│  • Format output ke Discord embed                            │
│  • Log semua command ke #audit-log                           │
│  • Image cleanup: auto-delete temp files after processing    │
│                                                              │
│  Hosting: Local (dev) / VPS+Docker (prod, post-hackathon)    │
└────────────────────────┬────────────────────────────────────┘
                         │
              ┌──────────┴──────────┐
              │                     │
              ▼                     ▼
┌──────────────────────┐  ┌──────────────────────────────┐
│   WORKBUDDY AI        │  │   PYTHON AGENT (core)        │
│   (optional backend)  │  │                              │
│                       │  │  data_layer.py               │
│  • LLM heavy-lifting  │  │  rulebase_engine.py          │
│  • Vision API proxy   │  │  vision_checker.py           │
│  • Natural language   │  │  output_generator.py         │
│    conversation       │  │  blast_reader.py             │
│  • Intent detection   │  │  image_manager.py            │
│                       │  │                              │
│  Status: connected    │  │  Baca spreadsheet (xlsx/API) │
│  ke Discord via       │  │  Parse Drive links (koma)    │
│  Remote Channels      │  │  Apply C1-C10/B1-B7          │
│                       │  │    + ketentuan tambahan      │
│                       │  │  Vision API (mock/real)      │
│                       │  │  Generate CSV + blast + log  │
│                       │  │  Image temp: TTL 24h, dedup  │
└──────────────────────┘  └──────────────────────────────┘
```

### Cara Menghubungkan WorkBuddy ke Discord Bot

WorkBuddy sudah terhubung ke Discord via Remote Channels (Assistant Settings). Tapi untuk arsitektur kita:

1. **WorkBuddy = LLM backend**: Bot Python kirim request ke WorkBuddy saat butuh LLM (natural language, vision analysis via Hunyuan)
2. **Python Bot = primary interface**: Discord user interact dengan Python bot, bukan langsung dengan WorkBuddy
3. **WorkBuddy tetap running di desktop**: saat demo, pastikan WorkBuddy online untuk heavy tasks
4. **Kalau WorkBuddy offline**: bot tetap jalan untuk deterministic tasks (rulebase, spreadsheet parsing, CSV). Yang tidak bisa: Vision API + natural language chat

---

## 2. Role & Permission Matrix

### Role Hierarchy (Discord)

| Role                  | Level         | Akses Server                                                   | Akses Bot Commands                                                                                                                                                   |
| --------------------- | ------------- | -------------------------------------------------------------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **@BPH**              | Administrator | Full server control                                            | Semua command                                                                                                                                                        |
| **@koor-spade**       | Manager       | Manage channels, roles (selain BPH)                            | Semua command kecuali hapus channel                                                                                                                                  |
| **@spade**            | Validator     | Akses channel validasi                                         | `/validate`, `/check-angel`, `/list-active`, `/resubmit-status`, `/denda-list`, `/ketentuan-pp`, `/ketentuan-buzzer`, `/ketentuan-angel-card`, submit bukti tambahan |
| **@panitia** + divisi | End user      | Lihat #denda (read-only), #ketentuan (read-only), #blast-tugas | `/denda-list` (lihat denda sendiri), `/ketentuan-pp`, `/ketentuan-buzzer`, `/ketentuan-angel-card`                                                                   |

### Permission per Command

| Command                   | @BPH | @koor-spade | @spade | @panitia               |
| ------------------------- | ---- | ----------- | ------ | ---------------------- |
| `/validate`               | ✅    | ✅           | ✅      | ❌                      |
| `/check-angel`            | ✅    | ✅           | ✅      | ❌                      |
| `/add-angel`              | ✅    | ✅           | ❌      | ❌                      |
| `/remove-angel`           | ✅    | ✅           | ❌      | ❌                      |
| `/blast`                  | ✅    | ✅           | ❌      | ❌                      |
| `/hold-blast`             | ✅    | ✅           | ❌      | ❌                      |
| `/release-blast`          | ✅    | ✅           | ❌      | ❌                      |
| `/list-active`            | ✅    | ✅           | ✅      | ❌                      |
| `/resubmit-status`        | ✅    | ✅           | ✅      | ❌                      |
| `/audit-log`              | ✅    | ✅           | ❌      | ❌                      |
| `/denda-list`             | ✅    | ✅           | ✅      | ✅ (hanya diri sendiri) |
| `/ketentuan-pp`           | ✅    | ✅           | ✅      | ✅                      |
| `/ketentuan-buzzer`       | ✅    | ✅           | ✅      | ✅                      |
| `/ketentuan-angel-card`   | ✅    | ✅           | ✅      | ✅                      |
| `/scan-blast`             | ✅    | ✅           | ❌      | ❌                      |
| Submit ke #bukti-tambahan | ✅    | ✅           | ✅      | ❌                      |
| Akses #ai-chat            | ✅    | ✅           | ✅      | ❌                      |
| Akses #audit-log (read)   | ✅    | ✅           | ❌      | ❌                      |
| Akses #evidence (read)    | ✅    | ✅           | ✅      | ❌                      |
| Akses #denda (read)       | ✅    | ✅           | ✅      | ✅                      |
| Edit #denda               | ✅    | ✅           | ❌      | ❌                      |
| Akses #ketentuan (read)   | ✅    | ✅           | ✅      | ✅                      |
| Akses #blast-tugas (read) | ✅    | ✅           | ✅      | ✅                      |

### Role-Gating Logic (Bot Side)

```python
async def check_permission(interaction, required_roles):
    user_roles = [r.name for r in interaction.user.roles]
    for required in required_roles:
        if required in user_roles:
            return True
    await interaction.response.send_message(
        "Maaf, Anda tidak memiliki izin untuk menggunakan command ini.",
        ephemeral=True
    )
    return False

# Usage:
@app_commands.command(name="validate")
async def validate(interaction, sheet: str):
    if not await check_permission(interaction, ["BPH", "koor-spade", "spade"]):
        return
    # ... proceed
```


**Prinsip Hackathon: Permissions** — "Stays within permissions. It only sees and uses what the user allows it to."

---

## 3. Channel Structure

| Channel              | Akses Role                            | Fungsi                                                                                  |
| -------------------- | ------------------------------------- | --------------------------------------------------------------------------------------- |
| `#pp-validation`     | spade, koor, BPH                      | Hasil validasi PP (embed per panitia)                                                   |
| `#buzzer-validation` | spade, koor, BPH                      | Hasil validasi Buzzer                                                                   |
| `#blast-drafts`      | koor, BPH                             | Draft blast denda + tombol Approve/Hold/Reject                                          |
| `#blast-tugas`       | panitia, spade, koor, BPH             | Blast tugas PP/Buzzer dari koor. **Bot auto-read** untuk extract ketentuan              |
| `#bukti-tambahan`    | spade                                 | PJ upload screenshot story + cross-validation                                           |
| `#evidence`          | spade, koor, BPH                      | Bot auto-post violation evidence (anti-cheat)                                           |
| `#audit-log`         | koor, BPH                             | History semua command + keputusan agent                                                 |
| `#ai-chat`           | spade, koor, BPH                      | Percakapan dengan AI agent (terkait SPADE management only)                              |
| `#denda`             | panitia (read-only), koor, BPH        | Semua panitia lihat denda. **Read-only untuk @panitia**. Edit hanya @BPH + @koor-spade  |
| `#ketentuan`         | panitia (read-only), spade, koor, BPH | Daftar ketentuan PP, Buzzer, Angel Card. Bot-managed, auto-generated dari rulebase.json |

### Channel Detail: #blast-tugas

Channel ini adalah tempat koor-spade blast pengumuman tugas PP/Buzzer baru ke panitia.

**Kenapa bot baca channel ini?**  
Setiap blast tugas dari penyewa jasa (klien) punya **ketentuan tambahan yang spesifik per blast** — caption custom, tag khusus, deadline berbeda, media dari folder Drive tertentu, dll. Bot harus baca blast message ini untuk:

1. **Extract ketentuan tambahan** (caption referensi, tag wajib, hashtag, deadline, link Drive)
2. **Auto-update task context** untuk validasi (tidak hanya relies on rulebase.json static)
3. **Match submission sheet** ke blast yang tepat

**Resource concern — bagaimana kalau ratusan chat?**

| Strategi             | Implementasi                                                                                     |
| -------------------- | ------------------------------------------------------------------------------------------------ |
| **Only scan recent** | Bot hanya scan message dalam rentang 7 hari terakhir (configurable)                              |
| **Pagination**       | Discord API pagination (100 messages per batch), batas max 1000 messages per scan                |
| **Keyword filter**   | Bot hanya process message yang mengandung keyword: "PP", "Buzzer", "blast", "tugas", "deadline"  |
| **Cache processed**  | Bot simpan message_id yang sudah di-process ke `processed_messages.json`. Skip yang sudah dibaca |
| **Reaction marker**  | Bot react ✅ ke message yang sudah di-extract. Skip message yang sudah punya react bot            |
| **Manual trigger**   | `/scan-blast` command untuk force-scan channel. Tidak auto-scan real-time                        |

**Batas aman:** 100-200 blast messages per scan = still fast (<5 detik). Kalau sudah ribuan, koor bisa purge old messages (Discord auto-purge setting).

### Channel Detail: #denda

| Aspek           | Detail                                                          |
| --------------- | --------------------------------------------------------------- |
| **Siapa lihat** | @panitia (read-only), @spade (read-only), @koor-spade, @BPH     |
| **Siapa edit**  | Hanya @BPH + @koor-spade (bot-managed, panitia tidak bisa edit) |
| **Isi channel** | Blast denda per sheet, auto-posted setelah approval             |
| **History**     | Semua blast denda tersimpan, panitia bisa scroll lihat riwayat  |
| **Search**      | Panitia bisa search nama mereka sendiri di channel              |

---

## 4. Command List

### Slash Commands

| Command                 | Parameters                                 | Role       | Response Time   | Description                                    |
| ----------------------- | ------------------------------------------ | ---------- | --------------- | ---------------------------------------------- |
| `/validate`             | `sheet: str`                               | spade+     | 2-5 min (heavy) | Validasi sheet PP/Buzzer                       |
| `/check-angel`          | —                                          | spade+     | instant         | Lihat Angel Card registry                      |
| `/add-angel`            | `nama: str`, `tanggal: str`, `divisi: str` | koor+      | instant         | Tambah Angel Card                              |
| `/remove-angel`         | `nama: str`                                | koor+      | instant         | Hapus Angel Card                               |
| `/blast`                | `sheet: str`                               | koor+      | 30s-1min        | Generate blast draft + buttons                 |
| `/hold-blast`           | `blast_id: str`                            | koor+      | instant         | Tahan blast yang sudah approved                |
| `/release-blast`        | `blast_id: str`                            | koor+      | instant         | Release blast yang di-hold                     |
| `/list-active`          | —                                          | spade+     | instant         | PP/Buzzer aktif vs terlewat                    |
| `/resubmit-status`      | `nama: str`                                | spade+     | instant         | Cek status resubmit panitia                    |
| `/audit-log`            | `filter: optional`                         | koor+      | instant         | Lihat history (filter: tanggal/user/nama/IG)   |
| `/denda-list`           | `nama: optional`                           | semua role | instant         | Panitia: denda sendiri. Spade+: semua denda    |
| `/ketentuan-pp`         | `task_id: optional`                        | semua role | instant         | Lihat ketentuan PP (umum atau per tugas)       |
| `/ketentuan-buzzer`     | `task_id: optional`                        | semua role | instant         | Lihat ketentuan Buzzer (umum atau per tugas)   |
| `/ketentuan-angel-card` | —                                          | semua role | instant         | Lihat ketentuan Angel Card                     |
| `/scan-blast`           | `days: optional`                           | koor+      | 5-10s           | Scan #blast-tugas untuk extract ketentuan baru |

---

## 5. Workflow: Validasi PP + Ketentuan Tambahan Per-Blast

### 5.A Konsep Ketentuan Tambahan dari Penyewa Jasa

**INI PENTING:** Setiap blast tugas PP dari klien/penyewa jasa punya **ketentuan tambahan yang spesifik** dan **tidak semua sama**. Agent WAJIB baca ketentuan tambahan ini, tidak hanya rely pada rulebase.json yang static.

**Contoh perbedaan ketentuan per blast:**

| Blast          | Caption                          | Tag Wajib                      | Hashtag                         | Media                  | Deadline     |
| -------------- | -------------------------------- | ------------------------------ | ------------------------------- | ---------------------- | ------------ |
| PP-01 (5 Okt)  | Di folder Drive                  | @insantransport, @horizon.umn  | #PPHorizon, #CaseOfTheUnseen    | Slide 1-4 dari G Drive | 9 Okt 07:00  |
| PP-03 (28 Okt) | **Custom verbatim** (jokiproyek) | @horizon_umn                   | + #jasacoding, #jasainformatika | Gambar yang DIBERIKAN  | 29 Okt 15:00 |
| PP-06 (9 Okt)  | Sesuai folder                    | @marketvirtualll, @horizon.umn | #PPHorizon, #CaseOfTheUnseen    | Gambar pada G Drive    | 10 Okt 20:00 |
| PP-07 (9 Okt)  | Dari gambar referensi            | @ditazulu, @horizon.umn        | #PPHorizon, #CaseOfTheUnseen    | Gambar yang disediakan | 9 Okt 22:00  |

**Sumber ketentuan tambahan:**

1. **rulebase.json** — struktur dasar (task ID, jadwal, deadline, tags, hashtags)
2. **#blast-tugas channel** — blast message dari koor, berisi link Drive, caption referensi, catatan khusus
3. **Drive folder** — media referensi (gambar, video, caption text file)

### 5.B Workflow: Blast Tugas Reader (/scan-blast)

```
Koor blast tugas di #blast-tugas:
    "📋 BLAST TUGAS: PP Feed 28 Oktober 2026
     Caption: [link Drive/caption.txt]
     Media: [link Drive folder]
     Tags: @horizon_umn
     Hashtag: #PPHorizon #PPHorizon_divisi #CaseOfTheUnseen
            #Openpaidpromote #paidpromotemahasiswamurah
            #jasacoding #jasainformatika #jasaIT #jasawebsite
     Deadline: Kamis 29 Oktober 15:00 WIB
     Form: [link Google Form]"
         │
         ▼
[1] Bot detect keyword "BLAST TUGAS" di #blast-tugas
         │
         ▼
[2] Bot extract:
    - Task type: PP (bukan Buzzer)
    - Caption referensi: download dari Drive link
    - Tags: parse dari message
    - Hashtags: parse dari message
    - Deadline: parse datetime
    - Form link: untuk match submission sheet
    - Drive link: untuk media referensi
         │
         ▼
[3] Bot simpan ke blast_context.json:
    {
      "task_id": "auto-detected atau null",
      "blast_message_id": "discord_msg_id",
      "blast_date": "2026-10-28",
      "caption_referensi": "downloaded text",
      "tags": ["@horizon_umn"],
      "hashtags": ["#PPHorizon", ...],
      "deadline": "2026-10-29T15:00",
      "drive_folder": "link",
      "form_link": "link",
      "extracted_at": "2026-10-28T10:00"
    }
         │
         ▼
[4] Bot react ✅ ke blast message (marker: sudah di-process)
         │
         ▼
[5] Bot post di #audit-log:
    "[2026-10-28 10:00] Bot auto-extracted blast tugas
     Message ID: discord_msg_id
     Task type: PP
     Tags: 1, Hashtags: 9
     Caption: downloaded (234 chars)
     Deadline: 29 Okt 15:00"
         │
         ▼
DONE. Ketentuan tambahan tersimpan. Saat /validate, agent
      akan merge rulebase.json + blast_context.json untuk
      validasi yang LENGKAP sesuai ketentuan blast ini.
```

### 5.C Workflow: /ketentuan-pp, /ketentuan-buzzer, /ketentuan-angel-card

### HAPPY FLOW: /ketentuan-pp

```
User (panitia): /ketentuan-pp
         │
         ▼
[1] Bot check role → semua role bisa → YES
         │
         ▼
[2] Bot load rulebase.json → extract tugas_pp + checks_pp + denda
         │
         ▼
[3] Bot post embed di #ketentuan (atau DM):
    ┌──────────────────────────────────────────┐
    │ 📋 Ketentuan Paid Promote (PP)           │
    ├──────────────────────────────────────────┤
    │ Denda:                                   │
    │ • Tidak post/terlambat: Rp 20.000       │
    │ • Per kesalahan posting: Rp 5.000        │
    │ • Tidak kirim bukti: Rp 20.000           │
    │                                          │
    │ 10 Checks:                               │
    │ C1: IG aktif + ≥300 followers            │
    │ C2: Timing post tepat                    │
    │ C3: Urutan carousel benar                │
    │ C4: Caption sesuai (WAJIB verbatim)      │
    │ C5: Tag/mention berfungsi                │
    │ C6: Hashtag di komentar                  │
    │ C7: Story keep 24 jam                    │
    │ C8: Feed tidak dihapus                   │
    │ C9: Aspect ratio sesuai                  │
    │ C10: Bukti terkirim & timely             │
    │                                          │
    │ Tugas aktif: PP-01 s/d PP-07             │
    │ Gunakan /ketentuan-pp task_id:PP-03      │
    │ untuk detail per tugas                   │
    └──────────────────────────────────────────┘
```


### HAPPY FLOW: /ketentuan-pp task_id:PP-03

```
User: /ketentuan-pp task_id:PP-03
         │
         ▼
[2] Bot load rulebase.json → find PP-03 + blast_context (kalau ada)
         │
         ▼
[3] Bot post embed:
    ┌──────────────────────────────────────────┐
    │ 📋 Ketentuan PP-03 (Feed 28 Okt)         │
    ├──────────────────────────────────────────┤
    │ Platform: Feed only                      │
    │ Jadwal post: Rabu, 28 Oktober 2026       │
    │ Deadline: Kamis, 29 Okt 15:00 WIB        │
    │                                          │
    │ Caption (VERBATIM - wajib copy paste):   │
    │ "Pusing dengan project akhir/TA..."     │
    │ (234 chars, full text di embed)          │
    │ ⚠️ Termasuk typo resmi TIDAK BOLEH     │
    │    diubah                                 │
    │                                          │
    │ Tags wajib: @horizon_umn                  │
    │ Hashtag: #PPHorizon, #CaseOfTheUnseen,   │
    │   #Openpaidpromote, #jasacoding,         │
    │   #jasainformatika, #jasaIT, ...         │
    │                                          │
    │ Media: Sesuai gambar yang DIBERIKAN      │
    │ Feed tidak boleh dihapus sampai:         │
    │   Rabu, 4 November 2026 23:59            │
    │                                          │
    │ 📎 Ketentuan tambahan dari blast:        │
    │ Caption custom dari klien - WAJIB literal│
    └──────────────────────────────────────────┘
```

### UNHAPPY FLOW: Task ID Tidak Ditemukan

```
User: /ketentuan-pp task_id:PP-99
         │
         ▼
[2] Bot load rulebase → PP-99 tidak ada
         │
         ▼
[3] Bot reply:
    "❌ Task ID 'PP-99' tidak ditemukan.
    Tugas PP yang tersedia: PP-01, PP-02, PP-03, PP-05, PP-06, PP-07"
```

### 5.D Workflow: Validasi PP (Happy Flow)

```
User (spade): /validate sheet:"PP Feed 26 Oktober"
         │
         ▼
[1] Bot check role → YES (spade/koor/BPH)
         │
         ▼
[2] Bot resolve sheet name → match ke task:
    - Sheet "PP Feed 26 Oktober" → task_id PP-03
    - Load rulebase.json: PP-03 config
    - Load blast_context.json: ketentuan tambahan dari #blast-tugas
    - MERGE: rulebase + blast_context = complete validation context
         │
         ▼
[3] Bot post progress embed:
    "⏳ Memvalidasi PP Feed 26 Oktober...
    76 submissions | Task: PP-03
    Ketentuan: rulebase + blast_context (1 blast message)"
         │
         ▼
[4] Python agent execute:
    a. data_layer: baca spreadsheet, parse Drive links (koma split)
    b. Angel Card cross-check (date-based: beli ≤ blast = exempt)
    c. For each submission:
       - Download bukti screenshot dari Drive
       - Vision API: baca caption, tag, hashtag, carousel, followers
       - Apply checks C1-C10 dari MERGED context:
         · C4: compare caption vs blast_context.caption_referensi (VERBATIM)
         · C5: compare tags vs blast_context.tags
         · C6: compare hashtags vs blast_context.hashtags
         · C2: compare timestamp vs blast_context.deadline
         · C3: compare carousel vs Drive folder referensi
         · etc.
       - Flag violations with rule_id + evidence_link
    d. Generate results per panitia
         │
         ▼
[5] Bot post results embed di #pp-validation:
    ┌──────────────────────────────────────────┐
    │ 📋 Hasil Validasi: PP Feed 26 Okt        │
    │ 76 submissions | 71 AMAN | 5 DENDA       │
    │ Ketentuan: rulebase + blast #msg_id       │
    ├──────────────────────────────────────────┤
    │ ✅ @starfours_/: AMAN                    │
    │    C1-C10: all PASS                      │
    │    Bukti: [Drive link]                   │
    ├──────────────────────────────────────────┤
    │ ❌ @raihanrizky/: DENDA Rp 15.000        │
    │    C4: caption tidak verbatim (-5rb)     │
    │    C5: tag @horizon_umn tidak ada (-5rb) │
    │    C6: hashtag kurang #jasacoding (-5rb) │
    │    Evidence: [Drive link]                │
    │    Referensi: blast_context caption       │
    └──────────────────────────────────────────┘
         │
         ▼
[6] Bot write output:
    - verification_report.csv: append results
    - violations_log.csv: append flagged
    - audit_log.jsonl: log decision
    - #evidence: auto-post violations
         │
         ▼
DONE. Hasil validasi lengkap dengan ketentuan tambahan dari blast.
```

### UNHAPPY FLOW 1: Sheet Tidak Ditemukan

```
[2] Bot resolve sheet → "PP 27 Oktober" tidak ada di workbook
         │
         ▼
Bot reply:
    "❌ Sheet 'PP 27 Oktober' tidak ditemukan.
    Sheet tersedia:
    • PP Feed 26 Oktober
    • PP Bundle 26 Oktober
    • PP 29 Oktober
    • ...
    Gunakan /list-sheets untuk daftar lengkap."
```

### UNHAPPY FLOW 2: Sheet Ambigu (>1 Match)

```
[2] Bot resolve: "PP 26 Oktober" → match 2 sheets
    "PP Feed 26 Oktober" + "PP Bundle 26 Oktober"
         │
         ▼
Bot reply:
    "⚠️ Ditemukan 2 sheet yang cocok:
    1. PP Feed 26 Oktober (76 submissions, PP-03)
    2. PP Bundle 26 Oktober (90 submissions, PP-05)
    Ketik nomor atau sebutkan nama lengkap."
```

### UNHAPPY FLOW 3: Tidak Ada Blast Context

```
[2] Bot load blast_context → tidak ada entry untuk PP-03
         │
         ▼
Bot reply (warning, tetap proceed):
    "⚠️ PERINGATAN: Ketentuan tambahan dari #blast-tugas belum
    di-extract untuk task PP-03. Validasi akan menggunakan
    rulebase.json saja.

    Beberapa check mungkin tidak lengkap (caption referensi,
    media folder). Disarankan: jalankan /scan-blast terlebih
    dahulu, lalu re-validate.

    Lanjut validasi dengan rulebase only? [Yes] [Cancel]"
```

### UNHAPPY FLOW 4: Role Tidak Cukup

```
User (@panitia): /validate sheet:"PP Feed 26 Oktober"
         │
         ▼
[1] Bot check role → @panitia tidak ada di ["spade", "koor-spade", "BPH"]
         │
         ▼
Bot reply (ephemeral):
    "🔒 Command /validate hanya untuk @spade, @koor-spade, atau @BPH."
```

### UNHAPPY FLOW 5: Drive Link Private/Restricted

```
[4c] Agent download bukti → ERROR: 403 Forbidden
         │
         ▼
Bot flag:
    "⚠️ @user8/ — BUKTI TIDAK DIVERIFIKASI
    Link Drive: restricted/private (403)
    Tidak bisa download bukti. Panitia harus ubah ke public view-only.

    Status: PENDING_MANUAL_REVIEW
    Denda: pending (tidak bisa determine tanpa bukti)"
```

---

## 6. Workflow: Validasi Buzzer

### HAPPY FLOW

```
User: /validate sheet:"Buzzer IG 09 Oktober"
         │
         ▼
[1] Bot check role → YES
         │
         ▼
[2] Bot resolve sheet → ketemu 1
         │
         ▼
[3] Bot post progress embed
         │
         ▼
[4] Python agent execute:
    a. data_layer: baca spreadsheet buzzer
    b. Angel Card cross-check
    c. For each submission:
       - Download bukti screenshot
       - Vision API: deteksi like, komentar, rating, vote
       - Apply checks: B1-B7
       - BZ-06 specific: cek komentar natural (LLM judge)
       - BZ-02/BZ-04: cek ulasan sesuai template per divisi
    d. Generate results
         │
         ▼
[5] Bot post results embed di #buzzer-validation:
    ┌──────────────────────────────────────┐
    │ 📋 Hasil Validasi: Buzzer IG 09 Okt │
    │ 85 submissions | 78 AMAN | 7 DENDA  │
    ├──────────────────────────────────────┤
    │ ✅ @starfours_/: AMAN               │
    │    Like: ✅ Komen natural: ✅       │
    ├──────────────────────────────────────┤
    │ ❌ @user5/: DENDA Rp 20.000         │
    │    B1: tidak like postingan 2       │
    │    B2: komentar hanya emoji 😂      │
    └──────────────────────────────────────┘
         │
         ▼
[6] Audit log + evidence (same as PP flow)
```

### UNHAPPY FLOW: Buzzer Submission Tidak Sesuai Divisi

```
[4c] Vision API: screenshot menunjukkan komentar di postingan_1
    tapi panitia divisi "Dupin (Visual)" seharusnya komen di postingan_2
         │
         ▼
[5] Bot flag:
    "⚠️ @user8/ — DENDA Rp 20.000
     B1: like di postingan salah
     Divisi Dupin (Visual) → wajib like postingan_2
     Tapi screenshot menunjukkan like di postingan_1"
         │
         ▼
DONE. Flagged dengan alasan spesifik.
```

---

## 7. Workflow: Blast Denda + Approval Gate

### HAPPY FLOW

```
User (koor-spade): /blast sheet:"PP Feed 26 Oktober 2026"
         │
         ▼
[1] Bot check role → koor/BPH? → YES
         │
         ▼
[2] Bot load violations dari verification_report.csv
    → 5 panitia dengan total denda Rp 95.000
         │
         ▼
[3] Bot generate blast draft (Markdown):
    ┌──────────────────────────────────────────┐
    │ 📢 BLAST DENDA — PP Feed 26 Oktober 2026 │
    │                                          │
    │ Berikut adalah daftar denda untuk        │
    │ PP Feed 26 Oktober 2026:                 │
    │                                          │
    │ 1. @raihanrizky/ — Rp 15.000            │
    │    C4: caption tidak sesuai              │
    │    C5: tag tidak berfungsi               │
    │    C6: hashtag kurang                    │
    │    Transfer: BCA 4731763318              │
    │    Kode: 04 (Denda PP)                   │
    │    Deadline: Sabtu 23.59 WIB             │
    │                                          │
    │ 2. @user3/ — Rp 30.000                  │
    │    C1: followers <300                    │
    │    C3: carousel urutan salah             │
    │    C7: story tidak ada bukti             │
    │    Transfer: BCA 4731763318              │
    │    Kode: 04 (Denda PP)                   │
    │    Deadline: Sabtu 23.59 WIB             │
    │                                          │
    │ ...                                      │
    │                                          │
    │ Total: 5 panitia, Rp 95.000              │
    └──────────────────────────────────────────┘
         │
         ▼
[4] Bot post draft di #blast-drafts dengan buttons:
    ┌──────────────────────────────────────────┐
    │ 📢 BLAST DRAFT — PP Feed 26 Okt         │
    │ 5 panitia | Rp 95.000 total             │
    │                                          │
    │ [Preview full draft]                     │
    │                                          │
    │  [✅ Approve & Send]  [⏸️ Hold]  [❌ Reject] │
    └──────────────────────────────────────────┘
         │
         ▼
[5] Koor click [✅ Approve & Send]
         │
         ▼
[6] Bot execute blast:
    a. Post blast message ke #denda (semua panitia bisa lihat, read-only)
    b. Update verification_report.csv: status → BLASTED
    c. Update violations_log.csv: add tanggal_blast
    d. Post konfirmasi di #blast-drafts:
       "✅ Blast sent! 5 panitia notified. Total Rp 95.000."
    e. Post di #audit-log:
       "[2026-11-05 08:30:00] @koor_spade approved blast
        Sheet: PP Feed 26 Oktober 2026
        5 panitia, Rp 95.000 total
        Blast ID: blast_20261105_083000"
         │
         ▼
DONE. Denda terkirim, tercatat di audit log + CSV.
```



### UNHAPPY FLOW 1: Koor Click [Hold]

```
[5] Koor click [⏸️ Hold]
         │
         ▼
[6] Bot:
    a. Simpan blast draft (status: HELD)
    b. Reply: "⏸️ Blast ditahan. Anda bisa release kapan saja
       dengan /release-blast blast_id:blast_20261105_083000"
    c. Post di #audit-log:
       "[2026-11-05 08:30:00] @koor_spade held blast
        Sheet: PP Feed 26 Oktober 2026
        Reason: (optional, koor bisa input)"
         │
         ▼
DONE. Blast disimpan tapi tidak dikirim. Bisa di-release nanti.
```

### UNHAPPY FLOW 2: Koor Click [Reject]

```
[5] Koor click [❌ Reject]
         │
         ▼
[6] Bot:
    a. Hapus draft (status: REJECTED)
    b. Reply: "❌ Blast dibatalkan. Draft tidak dikirim."
    c. Post di #audit-log:
       "[2026-11-05 08:30:00] @koor_spade rejected blast
        Sheet: PP Feed 26 Oktober 2026"
    d. verification_report.csv: status tetap FLAGGED (tidak BLASTED)
         │
         ▼
DONE. Tidak ada blast. Denda tetap tercatat tapi tidak dikirim ke panitia.
```

### UNHAPPY FLOW 3: Spade Coba Blast (Role Tidak Cukup)

```
User (role: @spade): /blast sheet:"PP Feed 26 Oktober 2026"
         │
         ▼
[1] Bot check role → @spade tidak ada di ["koor-spade", "BPH"]
         │
         ▼
[2] Bot reply (ephemeral):
    "🔒 Command /blast hanya untuk @koor-spade atau @BPH.
    Jika Anda ingin request blast, mohon hubungi koor-spade."
         │
         ▼
DONE. Log tercatat di audit-log (attempt denied).
```

### UNHAPPY FLOW 4: Tidak Ada Denda (Semua Aman)

```
[2] Bot load violations → 0 panitia dengan denda
         │
         ▼
[3] Bot reply:
    "✅ Tidak ada denda untuk PP Feed 26 Oktober 2026.
    Semua 76 panitia AMAN. Tidak perlu blast."
         │
         ▼
DONE. Tidak ada draft generated.
```

---

## 8. Workflow: List Active & Missed Tasks

### HAPPY FLOW

```
User (spade): /list-active
         │
         ▼
[1] Bot check role → YES
         │
         ▼
[2] Bot load rulebase.json → compare dengan current date (5 Nov 2026)
         │
         ▼
[3] Bot post embed:
    ┌──────────────────────────────────────────┐
    │ 📋 Status Tugas PP & Buzzer              │
    │ Per: 5 November 2026                     │
    ├──────────────────────────────────────────┤
    │ 🟢 AKTIF (deadline belum lewat):        │
    │ • PP-02 Feed — deadline: 31 Okt 10:00   │
    │   (sudah lewat? cek: ya, lewat 5 hari)  │
    │ → update: OVERDUE                        │
    │                                          │
    │ ❌ TERLEWAT (overdue):                   │
    │ • PP-02 — deadline 31 Okt 10:00 WIB     │
    │   Selisih: 5 hari terlewat               │
    │ • BZ-02 — deadline 2 Nov 21:00 WIB      │
    │   Selisih: 3 hari terlewat               │
    │                                          │
    │ ✅ SELESAI (sudah divalidasi):           │
    │ • PP-01 — 5 Okt (verified 42 submissions)│
    │ • PP-03 — 26 Okt (verified 76 submissions)│
    │ • BZ-03 — 26 Okt (verified 85 submissions)│
    │                                          │
    │ ⏳ PENDING (belum divalidasi):           │
    │ • PP-02 — 30 Okt (42 submissions)       │
    │ • BZ-06 — 9 Okt (85 submissions)         │
    └──────────────────────────────────────────┘
```

### UNHAPPY FLOW: Tidak Ada Tugas Aktif

```
[3] Bot reply:
    "ℹ️ Tidak ada tugas PP/Buzzer yang aktif saat ini.
    Semua tugas sudah divalidasi atau belum ada blast baru."
```

---

## 9. Workflow: Bukti Tambahan

### HAPPY FLOW

```
PJ (spade): Upload screenshot story ke #bukti-tambahan
    + caption: "PP Feed 26 Oktober 2026 | @starfours_ | Story 24h masih live"
         │
         ▼
[1] Bot detect image upload di #bukti-tambahan
         │
         ▼
[2] Bot extract info from message:
    - Image: download to temp
    - Caption text: parse submission name + IG username + description
    - Parse: sheet="PP Feed 26 Oktober 2026", ig="@starfours_", desc="Story 24h masih live"
         │
         ▼
[3] Bot validate format:
    - Submission name exists in rulebase? → YES
    - IG username format valid? → YES
    - Image attached? → YES
         │
         ▼
[4] Bot reply:
    "✅ Bukti tambahan diterima!
    
    📋 Submission: PP Feed 26 Oktober 2026
    👤 IG: @starfours_
    📝 Deskripsi: Story 24h masih live
    📎 File: screenshot_story.png
    
    Bukti ini akan digunakan saat validasi C7 (Story 24h).
    Audit ID: bt_20261105_090000"
         │
         ▼
[5] Bot log ke #audit-log:
    "[2026-11-05 09:00:00] @spade_member uploaded bukti tambahan
     Submission: PP Feed 26 Oktober 2026
     IG: @starfours_
     File: screenshot_story.png"
         │
         ▼
DONE. Bukti tersimpan. Saat /validate dijalankan, bukti ini di-cross-reference.
```

### UNHAPPY FLOW 1: Format Salah (Tidak Ada Submission Name)

```
PJ: Upload image + caption "story budi masih ada"
         │
         ▼
[2] Bot extract: no submission name detected
         │
         ▼
[3] Bot reply:
    "⚠️ Format bukti tambahan tidak lengkap.
    
    Mohon sertakan:
    1. Nama submission (contoh: 'PP Feed 26 Oktober 2026')
    2. Username IG (contoh: '@starfours_')
    3. Deskripsi singkat
    
    Format: [Submission] | [IG] | [Deskripsi]
    Contoh: PP Feed 26 Oktober 2026 | @starfours_ | Story 24h masih live"
         │
         ▼
DONE. Bukti tidak tersimpan. PJ harus re-upload dengan format benar.
```

### UNHAPPY FLOW 2: Submission Name Tidak Ada di Rulebase

```
PJ: Upload + caption "PP 15 November | @user1/ | Story"
         │
         ▼
[3] Bot validate: "PP 15 November" tidak ada di rulebase
         │
         ▼
[4] Bot reply:
    "❌ Submission 'PP 15 November' tidak ditemukan dalam daftar tugas.
    
    Tugas yang tersedia:
    • PP Feed 26 Oktober 2026
    • PP Bundle 26 Oktober 2026
    • PP 29 Oktober
    • ...
    
    Mohon perbaiki nama submission dan upload ulang."
```

---

## 10. Workflow: Evidence Vault (Auto by Bot)

### HAPPY FLOW

```
Bot detect violation saat /validate
         │
         ▼
[1] Agent catat violation di violations_log.csv:
    {
      "tanggal": "2026-11-05",
      "nama": "raihanrizky",
      "divisi": "Conan (Acara)",
      "id_tugas": "PP-03",
      "jenis_pelanggaran": "C4: caption tidak sesuai",
      "denda": 5000,
      "status": "FLAGGED",
      "evidence_link": "drive.google.com/open?id=xxx",
      "catatan": "Caption di-edit setelah upload"
    }
         │
         ▼
[2] Bot auto-post di #evidence:
    ┌──────────────────────────────────────────┐
    │ 🔍 EVIDENCE LOG                          │
    ├──────────────────────────────────────────┤
    │ Panitia: @raihanrizky/                   │
    │ Divisi: Conan (Acara)                    │
    │ Tugas: PP Feed 26 Oktober 2026           │
    │ Violation: C4 — caption tidak sesuai     │
    │ Denda: Rp 5.000                          │
    │ Bukti: [link Drive]                      │
    │ Timestamp: 2026-11-05 07:15:00          │
    │                                          │
    │ ⚠️ RIWAYAT: 2x violation sebelumnya     │
    │    (C6 pada PP-01, C5 pada BZ-03)       │
    │    Status: RECURRING                     │
    └──────────────────────────────────────────┘
         │
         ▼
[3] Jika RECURRING (3+ violations):
    Bot mention @koor-spade:
    "@koor-spade ⚠️ @raihanrizky/ telah melanggar 3x.
    Disarankan untuk tindakan lebih lanjut."
         │
         ▼
DONE. Evidence terarsip. Anti-cheat record tersimpan.
```

### UNHAPPY FLOW: Panitia Fix Posting Lalu Resubmit

```
Panitia resubmit dengan bukti yang diperbaiki
         │
         ▼
[1] Agent detect resubmit (nama+task sudah di CSV dengan status FLAGGED)
         │
         ▼
[2] Agent re-validate:
    - C4 sekarang PASS (caption diperbaiki)
    - Tapi bukti lama masih di Evidence Vault
         │
         ▼
[3] Bot update #evidence:
    "📋 UPDATE: @raihanrizky/ — PP Feed 26 Okt
     C4: RESOLVED (resubmit verified)
     Previous violation: archived
     New status: VERIFIED
     
     Evidence before: [link lama]
     Evidence after: [link baru]"
         │
         ▼
[4] violations_log.csv update:
    status: FLAGGED → RESOLVED
    catatan: "resubmit verified, C4 fixed"
         │
         ▼
DONE. Evidence vault tetap simpan bukti lama untuk record.
```

---

## 11. Workflow: Audit Log

### HAPPY FLOW

```
User (koor): /audit-log
         │
         ▼
[1] Bot check role → koor/BPH? → YES
         │
         ▼
[2] Bot load audit_log.jsonl → last 20 entries
         │
         ▼
[3] Bot post embed:
    ┌──────────────────────────────────────────┐
    │ 📜 Audit Log (20 terakhir)              │
    ├──────────────────────────────────────────┤
    │ [07:15] @spade: /validate PP Feed 26 Okt│
    │   Result: 76 checked, 5 flagged         │
    │                                         │
    │ [08:30] @koor: /blast PP Feed 26 Okt    │
    │   Result: approved, 5 panitia notified  │
    │                                         │
    │ [09:00] @spade: bukti tambahan upload   │
    │   Target: @starfours_ PP Feed 26 Okt    │
    │                                         │
    │ [09:15] @koor: /add-angel               │
    │   Target: Budi Santoso, tgl 3 Okt       │
    └──────────────────────────────────────────┘
```

### HAPPY FLOW: With Filter

```
User: /audit-log filter:"tanggal:2026-11-05 user:@spade_member"
         │
         ▼
[2] Bot filter audit_log.jsonl:
    - tanggal = 2026-11-05
    - user = @spade_member
         │
         ▼
[3] Bot post filtered results:
    "📜 Audit Log — Filter: 5 Nov 2026, user @spade_member
    3 entries found..."
```

### UNHAPPY FLOW: Filter Tidak Match

```
[2] Bot filter → 0 results
         │
         ▼
[3] Bot reply:
    "ℹ️ Tidak ada audit log entry yang match dengan filter.
    Filter: tanggal=2026-11-10 (tidak ada activity di tanggal ini)"
```

---

## 12. Workflow: Angel Card Management

### HAPPY FLOW: Add Angel Card

```
User (koor): /add-angel nama:"Budi Santoso" tanggal:"2026-10-03" divisi:"Kindaichi (Keamanan)"
         │
         ▼
[1] Bot check role → koor/BPH? → YES
         │
         ▼
[2] Bot validate input:
    - Nama: exists in roster? → YES
    - Tanggal: valid date format? → YES (2026-10-03)
    - Divisi: valid divisi? → YES
         │
         ▼
[3] Bot add ke Angel Card Registry:
    "Budi Santoso | Kindaichi (Keamanan) | ACTIVE | 2026-10-03"
         │
         ▼
[4] Bot reply:
    "✅ Angel Card ditambahkan!
    
    👤 Nama: Budi Santoso
    🏷️ Divisi: Kindaichi (Keamanan)
    📅 Tanggal Pembelian: 3 Oktober 2026
    🎫 Status: ACTIVE
    
    Berlaku untuk: PP & Buzzer yang BELUM di-blast saat 3 Okt.
    Tidak berlaku untuk: Sponsor."
         │
         ▼
[5] Audit log: catat perubahan
         │
         ▼
DONE. Angel Card aktif. Saat /validate, panitia ini SKIP dari list denda
      jika tanggal_beli <= tanggal_blast.
```



### UNHAPPY FLOW 1: Nama Tidak Ada di Roster

```
[2] Bot validate: "Budi Santoso" tidak ada di roster panitia
         │
         ▼
[3] Bot reply:
    "❌ Nama 'Budi Santoso' tidak ditemukan di roster panitia.
    
    Apakah maksud Anda:
    1. Budi Santoso Wijaya (Kindaichi)
    2. Budi Pratama (Conan)
    
    Ketik nomor atau perbaiki nama."
```

### UNHAPPY FLOW 2: Tanggal Invalid

```
User: /add-angel nama:"Budi" tanggal:"2026-13-45" divisi:"Keamanan"
         │
         ▼
[2] Bot validate: tanggal "2026-13-45" → invalid (bulan 13 tidak ada)
         │
         ▼
[3] Bot reply:
    "❌ Tanggal '2026-13-45' tidak valid.
    Format yang benar: YYYY-MM-DD (contoh: 2026-10-03)"
```

### HAPPY FLOW: Remove Angel Card

```
User (koor): /remove-angel nama:"Budi Santoso"
         │
         ▼
[1] Bot check role → YES
         │
         ▼
[2] Bot find "Budi Santoso" in Angel Card Registry → ketemu
         │
         ▼
[3] Bot confirm:
    "⚠️ Konfirmasi: Hapus Angel Card untuk Budi Santoso?
    [Confirm] [Cancel]"
         │
         ▼
[4] Koor click [Confirm]
         │
         ▼
[5] Bot remove + reply:
    "✅ Angel Card untuk Budi Santoso telah dihapus.
    Panitia ini akan tunduk pada semua PP/Buzzer submission."
         │
         ▼
[6] Audit log: catat penghapusan
```

---

## 13. Workflow: AI Chat (#ai-chat)

### HAPPY FLOW

```
User (spade): "Berapa denda kalo caption salah?"
         │
         ▼
[1] Bot check: message in #ai-chat? → YES
    Bot check role: spade/koor/BPH? → YES
         │
         ▼
[2] Bot detect topic: SPADE management related? → YES (denda = related)
         │
         ▼
[3] Bot route ke WorkBuddy LLM:
    Context: rulebase.json (C4 denda = Rp 5.000)
    Query: "Berapa denda kalo caption salah?"
         │
         ▼
[4] Bot reply:
    "Denda untuk caption yang tidak sesuai ketentuan adalah **Rp 5.000**
    per kesalahan posting (C4).
    
    Sumber: Peraturan PP & Sponsorship poin 3.
    
    Catatan: Caption custom dari perusahaan WAJIB literal,
    termasuk typo resmi pun harus dipakai."
```

### HAPPY FLOW: Complex Query

```
User (koor): "Siapa saja yang belum submit PP 29 Oktober?"
         │
         ▼
[1-2] Bot check → YES
         │
         ▼
[3] Bot route ke Python agent (tidak butuh LLM):
    Query roster → compare dengan submission sheet "PP 29 Oktober"
    → 106 panitia total - 42 submitted = 64 belum submit
         │
         ▼
[4] Bot reply:
    "📋 PP 29 Oktober — Status Submission
    Total panitia: 106
    Sudah submit: 42
    Belum submit: 64
    
    Beberapa yang belum submit:
    • @user10/ (Conan)
    • @user22/ (Marple)
    • @user31/ (Fletch)
    • ... (64 total)
    
    Deadline: Jumat 9 Oktober 2026 07:00 WIB (sudah lewat)
    ⚠️ Panitia yang tidak submit = denda Rp 20.000 (C10)"
```

### UNHAPPY FLOW 1: Topic di Luar Scope

```
User (spade): "Bagaimana cara masak nasi goreng?"
         │
         ▼
[1-2] Bot check → YES (role OK, channel OK)
         │
         ▼
[3] Bot detect topic: SPADE management related? → NO (masak = tidak related)
         │
         ▼
[4] Bot reply:
    "Maaf, saya hanya dapat membantu pertanyaan terkait
    management SPADE — PP, Buzzer, denda, Angel Card,
    validasi, blast, dan aturan organisasi.
    
    Untuk pertanyaan di luar scope tersebut, mohon
    gunakan channel lain atau search engine."
```

### UNHAPPY FLOW 2: User Minta Perubahan via Chat

```
User (koor): "Tolong tambahin denda baru Rp 10.000 untuk panitia yang telat reply WA"
         │
         ▼
[3] Bot detect: permintaan perubahan rulebase via chat
         │
         ▼
[4] Bot reply:
    "⚠️ Perubahan aturan tidak dapat dilakukan via chat.
    
    Untuk menambah/mengubah aturan:
    1. Edit rulebase.json secara langsung (developer)
    2. Atau gunakan command khusus (jika tersedia)
    
    Saran: diskusikan perubahan dengan BPH,
    lalu update rulebase.json dan restart bot.
    
    Alasan: mencegah AI halusinasi atau membuat
    aturan yang tidak terverifikasi."
```

### UNHAPPY FLOW 3: WorkBuddy Offline

```
[3] Bot route ke WorkBuddy → connection timeout (30s+)
         │
         ▼
[4] Bot reply:
    "⏳ WorkBuddy AI sedang offline atau lambat merespons.
    
    Untuk pertanyaan sederhana, saya bisa coba jawab
    dari rulebase lokal. Pertanyaan kompleks (natural
    language, Vision) butuh WorkBuddy online.
    
    Status: WorkBuddy tidak terdeteksi.
    Coba lagi dalam beberapa menit."
```

---

## 14. Workflow: Resubmit Handling

### HAPPY FLOW

```
Panitia resubmit via Google Form (tidak via Discord)
    → submission baru masuk spreadsheet dengan nama+NIM yang sama
         │
         ▼
[1] /validate dijalankan → agent baca spreadsheet
         │
         ▼
[2] Agent detect duplicate:
    - (nama, nim) sudah ada di verification_report.csv
    - Status sebelumnya: FLAGGED
    → Ini RESUBMIT, bukan submission baru
         │
         ▼
[3] Agent re-validate dengan bukti baru:
    - Download gambar baru
    - Vision API analyze
    - Apply checks C1-C10
         │
         ▼
[4a] Jika semua checks PASS:
    - Update status: FLAGGED → VERIFIED
    - Audit log: "RESUBMIT_VALIDATED, previous C4 violation RESOLVED"
    - Evidence vault: update dengan bukti baru
    - Bot post di #pp-validation:
      "✅ UPDATE: @raihanrizky/ — PP Feed 26 Okt
       Status: VERIFIED (sebelumnya FLAGGED)
       C4: RESOLVED (caption diperbaiki)
       Denda: Rp 0 (dibatalkan)"
         │
         ▼
[4b] Jika masih ada violation:
    - Update status: FLAGGED (tetap)
    - Audit log: "RESUBMIT_VALIDATED, C4 resolved but C6 still fails"
    - Bot post:
      "⚠️ UPDATE: @raihanrizky/ — PP Feed 26 Okt
       Status: masih FLAGGED
       C4: RESOLVED ✅
       C6: masih fail (hashtag kurang) ❌
       Denda: Rp 5.000 (dari Rp 10.000 sebelumnya)"
```

### UNHAPPY FLOW: Panitia Submit 3x+ (Spam)

```
[2] Agent detect: (nama, nim) submit 5x
    - Timestamps: 07:00, 07:15, 07:30, 07:45, 08:00
    → Ambil latest (08:00), log semua versi
         │
         ▼
[3] Bot post di #audit-log:
    "⚠️ DUPLICATE: @raihanrizky/ submit 5x untuk PP Feed 26 Okt
     Timestamps: 07:00, 07:15, 07:30, 07:45, 08:00
     Using: latest (08:00)
     Changes between versions:
     - v1→v2: gambar diganti
     - v2→v3: IG username diganti (possible account swap!)
     - v3→v4: caption diperbaiki
     - v4→v5: carousel order diperbaiki
     
     ⚠️ FLAG: IG username diganti di v2→v3 (possible C1 violation)"
         │
         ▼
[4] Agent re-validate dengan latest, TAPI flag account swap untuk koor review
```

---

## 15. Mapping ke 5 Prinsip Hackathon

| Prinsip         | Implementasi di Discord Bot                                                                                                             | Evidence di Workflow                                                                        |
| --------------- | --------------------------------------------------------------------------------------------------------------------------------------- | ------------------------------------------------------------------------------------------- |
| **Sources**     | Setiap output embed menyebut rule_id (C1-C10, B1-B7) + evidence link (Drive)                                                            | Workflow 5 step 5: "C4: caption tidak sesuai (-5rb)" + link bukti                           |
| **Permissions** | Role-gating: bot check role sebelum execute command. Panitia tidak bisa /validate                                                       | Workflow 5 unhappy flow 4: role tidak cukup → ditolak                                       |
| **Approval**    | Blast draft → [Approve] [Hold] [Reject] buttons → koor klik sebelum send                                                                | Workflow 7 step 4-6: approval gate dengan buttons                                           |
| **Limits**      | Max 2 clarifications untuk ambiguous sheet. Tidak halusinasi. #ai-chat tolang topic outside scope. WorkBuddy offline → graceful degrade | Workflow 5 unhappy flow 2: max 2 clarifications. Workflow 13 unhappy flow 1: topic ditolang |
| **Record**      | #audit-log channel (auto-post setiap command). audit_log.jsonl (persistent). violations_log.csv (stateful)                              | Workflow 11: audit log dengan filter. Workflow 10: evidence vault auto-post                 |

---

## 16. Mapping ke Aturan Organisasi Mamazon

| Aturan Mamazon                                 | Implementasi di Bot                               | Workflow Reference                                      |
| ---------------------------------------------- | ------------------------------------------------- | ------------------------------------------------------- |
| Denda tidak post = Rp 20.000                   | C2/C10 check → bot flag Rp 20.000                 | Workflow 5: C2 timing + C10 bukti                       |
| Per kesalahan post = Rp 5.000                  | C1,C3-C9 → each Rp 5.000                          | Workflow 5: per-violation breakdown                     |
| Angel Card exempt PP & Buzzer (bukan Sponsor)  | /add-angel + cross-check saat /validate           | Workflow 12: add angel. Workflow 5 step 4b: cross-check |
| Angel Card: beli ≤ blast = exempt              | Agent compare tanggal_beli vs tanggal_blast       | Rulebase.json: angel_card.syarat_berlaku                |
| Deadline bayar denda: Sabtu 23.59 WIB          | Blast draft include deadline + kode transfer      | Workflow 7 step 3: "Deadline: Sabtu 23.59 WIB"          |
| Kode transfer: 04=Denda PP, 05=Denda Sponsor   | Blast draft include kode transfer                 | Workflow 7 step 3: "Kode: 04 (Denda PP)"                |
| Story wajib keep 24h                           | C7 check dengan 3 status (PASS/FLAG/INCONCLUSIVE) | Workflow 5 step 4d: C7 cross-reference Bukti Tambahan   |
| Caption custom WAJIB literal                   | C4: string-match fuzzy vs caption referensi       | Rulebase.json: C4 catatan "wajib verbatim"              |
| Tag WAJIB bisa ditap (bukan teks biasa)        | C5: Vision deteksi tag aktif                      | Rulebase.json: C5 syarat                                |
| Hashtag di komentar (bukan caption)            | C6: Vision/OCR baca kolom komentar                | Rulebase.json: C6 syarat                                |
| Carousel urutan benar                          | C3: Vision compare urutan vs referensi            | Rulebase.json: C3 catatan                               |
| Feed tidak boleh dihapus                       | C8: cek screenshot masih live                     | Rulebase.json: C8                                       |
| Bukti wajib dikirim via form                   | C10: cek timestamp submission vs deadline         | Rulebase.json: C10                                      |
| Buzzer: like kedua platform (BZ-01)            | B1: Vision deteksi like di IG + TikTok            | Rulebase.json: BZ-01 ketentuan                          |
| Buzzer: komentar natural (BZ-06)               | B2: LLM judge panjang + bukan pure emoji          | Rulebase.json: BZ-06 ketentuan                          |
| Buzzer: ulasan sesuai template per divisi      | B3: LLM judge compare vs template                 | Rulebase.json: BZ-02/BZ-04 pembagian_per_divisi         |
| Buzzer: rating bintang 5 (BZ-04)               | B4: Vision deteksi rating                         | Rulebase.json: BZ-04 ketentuan                          |
| Buzzer: vote campaign benar (BZ-03)            | B5: Vision deteksi vote                           | Rulebase.json: BZ-03 ketentuan                          |
| Anti-cheat: panitia fix posting lalu pura-pura | Evidence Vault + violations_log RECURRING flag    | Workflow 10: auto-post evidence                         |
| Blast tidak boleh tanpa approval koor          | [Approve] [Hold] [Reject] buttons                 | Workflow 7: approval gate                               |



---

## 17. Edge Cases Summary

| Edge Case                         | Trigger                         | Bot Response                                          | Workflow        |
| --------------------------------- | ------------------------------- | ----------------------------------------------------- | --------------- |
| Sheet tidak ditemukan             | Nama sheet tidak ada            | List sheet tersedia + minta klarifikasi               | 5 unhappy 1     |
| Sheet ambigu (>1 match)           | 2+ sheet mirip                  | Osi nomor + minta pilih                               | 5 unhappy 2     |
| Tahun tidak valid                 | Tahun > 2026 atau < 2026        | Tolak + sebutkan range data                           | 5 unhappy 3     |
| Role tidak cukup                  | Panitia coba /validate          | Tolak + sebutkan role yang dibutuhkan                 | 5 unhappy 4     |
| Drive link private                | Link Restricted                 | Flag "tidak bisa diverifikasi" + saran ubah ke public | 5 unhappy 5     |
| Tidak ada denda                   | Semua panitia aman              | "Tidak perlu blast"                                   | 7 unhappy 4     |
| Bukti tambahan format salah       | Tidak ada submission name       | Tolak + kasih format yang benar                       | 9 unhappy 1     |
| Bukti tambahan submission invalid | Submission tidak di rulebase    | Tolak + list tugas tersedia                           | 9 unhappy 2     |
| Panitia resubmit                  | Nama+NIM sama, status FLAGGED   | Re-validate + compare + update status                 | 14 happy        |
| Panitia spam submit (3x+)         | 3+ submission sama              | Ambil latest + log semua + flag account swap          | 14 unhappy      |
| #ai-chat topic outside scope      | Pertanyaan tidak related SPADE  | Tolak + sebutkan scope                                | 13 unhappy 1    |
| #ai-chat minta ubah rulebase      | Permintaan ubah aturan via chat | Tolak + arahkan ke command/developer                  | 13 unhappy 2    |
| WorkBuddy offline                 | LLM tidak respond               | Graceful degrade + jawab dari rulebase lokal          | 13 unhappy 3    |
| Blast di-hold                     | Koor click [Hold]               | Simpan draft + bisa release nanti                     | 7 unhappy 1     |
| Blast di-reject                   | Koor click [Reject]             | Hapus draft + log + status tetap FLAGGED              | 7 unhappy 2     |
| Angel Card nama tidak di roster   | Nama tidak match                | Opsi nama mirip + minta konfirmasi                    | 12 unhappy 1    |
| Angel Card tanggal invalid        | Format tanggal salah            | Tolak + kasih format yang benar                       | 12 unhappy 2    |
| IG typo (spreadsheet vs bukti)    | Username tidak exact match      | Fuzzy match ≤2 → note. >2 → flag koor                 | 5 step 4d       |
| Buzzer salah divisi               | Komentar di postingan salah     | Flag dengan alasan divisi-specific                    | 6 unhappy       |
| Recurring violation (3x+)         | Panitia langgar berulang        | Evidence vault flag RECURRING + mention koor          | 10 happy step 3 |
| Blast context belum di-extract    | /scan-blast belum dijalankan    | Warning: validasi rulebase only, suggest /scan-blast  | 5.C unhappy 3   |
| #blast-tugas terlalu banyak chat  | Ratusan/ribuan message          | Only scan recent 7 hari + keyword filter + cache      | 3 blast-tugas   |
| Image storage membengkak          | Download ribuan gambar bukti    | TTL 24h + hash dedup + cleanup otomatis               | image_manager   |
| VPS download rate dibatasi        | Rate limit dari VPS provider    | Batch download + retry + cache hash                   | image_manager   |
| Rulebase berubah saat validasi    | Developer edit rulebase.json    | Bot reload saat start. Warning kalau berubah mid-run  | —               |

---

## Catatan untuk Eksekusi (Developer)

### Stack Teknis

| Komponen     | Teknologi                                                                           | Status                       |
| ------------ | ----------------------------------------------------------------------------------- | ---------------------------- |
| Discord Bot  | discord.py 2.x                                                                      | TODO                         |
| Python Agent | Python 3.10+ (data_layer, rulebase_engine, vision_checker, output_generator, blast_reader, image_manager) | data_layer ✅, lainnya TODO |
| WorkBuddy    | Remote Channels (Discord connected)                                                 | ✅ Connected                  |
| Vision API   | Tencent Hunyuan (mock first, swap real)                                             | TODO (mock)                  |
| Spreadsheet  | openpyxl (local) / Google Sheets API (production)                                   | ✅ openpyxl                   |
| Google Drive | Service account                                                                     | TODO (user provide JSON key) |
| Hosting      | Local (dev/demo) / VPS+Docker (post-hackathon, masih rentan — hold dulu)            | Local OK, VPS hold           |
| Telegram     | TIDAK DIGUNAKAN — sudah diganti Discord sepenuhnya                                  | ❌ Dropped                   |
| Database     | Spreadsheet (single source of truth) + CSV (stateful) + blast_context.json          | ✅                            |

### Build Priority (Revised untuk Discord Bot)

| Priority   | Komponen                                                                          | Estimasi |
| ---------- | --------------------------------------------------------------------------------- | -------- |
| **MUST**   | Discord bot skeleton (discord.py, slash commands, role-gating)                    | 4h       |
| **MUST**   | `/validate` PP core (parse koma → rulebase + blast_context → mock Vision → C1-C10 → embed) | 6h |
| **MUST**   | `/blast` + approval buttons + audit log + CSV output                              | 3h       |
| **MUST**   | `/list-active` + `/check-angel` + `/denda-list` + `/ketentuan-pp/buzzer/angel-card` | 3h   |
| **MUST**   | blast_reader.py (scan #blast-tugas, extract ketentuan, save blast_context.json)  | 2h       |
| **SHOULD** | image_manager.py (download + cleanup + hash dedup + TTL)                         | 1h       |
| **SHOULD** | `/add-angel` + `/remove-angel`                                                    | 1h       |
| **SHOULD** | #bukti-tambahan upload handler + cross-reference                                  | 2h       |
| **SHOULD** | Evidence vault auto-post + #audit-log auto-post                                   | 1h       |
| **SHOULD** | Buzzer validation (B1-B7)                                                         | 3h       |
| **SHOULD** | Sheet name resolver (fuzzy match + confirmation)                                  | 2h       |
| **NICE**   | #ai-chat (WorkBuddy LLM integration)                                              | 2h       |
| **NICE**   | Resubmit handler + duplicate detection                                            | 2h       |
| **NICE**   | `/hold-blast` + `/release-blast`                                                  | 1h       |
| **NICE**   | `/audit-log` dengan filter                                                        | 1h       |
| **NICE**   | `/scan-blast` command (manual trigger scan #blast-tugas)                          | 1h       |
| **NICE**   | Voice note STT (poin bonus hackathon)                                             | 3h       |

### Yang Perlu dari User

- [ ] Discord bot token (dari Discord Developer Portal)
- [ ] Discord server ID + role IDs (setup server Mamazon)
- [ ] Google Cloud service account JSON key
- [ ] Tencent Cloud API key (SecretId + SecretKey) — atau pakai mock dulu
- [ ] Konfirmasi "gas" untuk mulai build

---

> **Dokumen ini disusun berdasarkan diskusi kak wldnz & Buddie pada 1-5 November 2026.**  
> **Align dengan: Peraturan PP/Sponsorship Mamazon, Peraturan Angel Card, blast files, dan 5 Prinsip Accountability Tencent Cloud Hackathon 2026.**  
> **Untuk eksekusi oleh rekan tim kak wldnz.**
