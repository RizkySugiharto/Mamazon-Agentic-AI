# Mamazon Spade Agent — Planning Document

## Tencent Cloud Hackathon 2026 — Challenge B: The Office Agent

|   |   |   |
| - | - | - |

|   |   |   |
| - | - | - |

|   |   |   |
| - | - | - |

|   |   |   |
| - | - | - |

> **Versi**: 2.1 (Discord + Ketentuan Tambahan + Image Management) | **Tanggal**: 5 November 2026 | **Status**: Pre-build — arsitektur finalized

|   |   |   |
| - | - | - |

|   |   |   |
| - | - | - |

> |   |   |
> | - | - |

|   |   |
| - | - |

|   |   |
| - | - |

|   |   |
| - | - |

> **Tim**: kak wldnz + rekan | **Deadline**: 7 November 2026  
> **Pivot**: Interface berubah dari WorkBuddy chat → Discord bot. WorkBuddy tetap sebagai LLM backend.  
> **Workflow detail**: Lihat `Mamazon_Spade_Agent_Workflow.md`

---

## Daftar Isi

1. [Konteks Hackathon](#1-konteks-hackathon)
2. [Organisasi Mamazon & Problem](#2-organisasi-mamazon--problem)
3. [Solusi: AI Agent untuk Validasi Submission](#3-solusi-ai-agent-untuk-validasi-submission)
4. [Arsitektur & Stack Teknologi](#4-arsitektur--stack-teknologi)
5. [Rulebase: Aturan Validasi](#5-rulebase-aturan-validasi)
6. [Flow Validasi Instagram (Evidence-Based)](#6-flow-validasi-instagram-evidence-based)
7. [Bukti Tambahan & Evidence Vault](#7-bukti-tambahan--evidence-vault)
8. [Handling Edge Cases](#8-handling-edge-cases)
9. [Output Artifacts](#9-output-artifacts)
10. [API & Pricing](#10-api--pricing)
11. [Roadmap & Timeline](#11-roadmap--timeline)
12. [Deliverables Hackathon](#12-deliverables-hackathon)
13. [Glosarium](#13-glosarium)

---

## 1. Konteks Hackathon

### 1.1 Tentang Lomba

- **Nama**: Hackathon "AI CAN DO IT" Tencent Cloud Indonesia 2026
- **Supported by**: Sumopod & AICLUB.ID
- **Challenge**: Challenge B — The Office Agent
- **Prize**: Juara 1 USD 1,200 + 3,000 WorkBuddy credits. 9 finalis ke Demo Day 27 Nov.
- **Deadline submission**: 7 November 2026 (diundur dari 1 Nov)
- **Finalist announcement**: 10 November 2026

### 1.2 5 Prinsip Accountability (Yang Di-judge)

| Prinsip         | Arti                                  | Implementasi di Agent                                                    |
| --------------- | ------------------------------------- | ------------------------------------------------------------------------ |
| **Sources**     | Agent harus cite sumber keputusan     | Audit log: setiap decision ada rule_id + evidence_link                   |
| **Permissions** | Agent hanya akses data yang diizinkan | Google Drive (service account, di-share). TIDAK scrape Instagram         |
| **Approval**    | Human-in-the-loop untuk action publik | Blast draft → koor approve via Discord buttons → baru kirim ke #denda    |
| **Limits**      | Agent tahu batasnya                   | Max 2 clarifications. Tidak paksakan diri kalau ambigu. Tidak halusinasi |
| **Record**      | Semua keputusan tercatat              | audit_log.jsonl + verification_report.csv + violations_log.csv           |

### 1.3 Submission Items

- Title + blurb (<10 kata)
- Project description
- Accountability sheet (NEW)
- Test results + screenshot
- Architecture diagram
- Demo video 5-8 menit
- Product sharing
- Source code
- Cover image (opsional)
- Project link (bonus)

---

## 2. Organisasi Mamazon & Problem

### 2.1 Tentang Mamazon

Mamazon adalah organisasi informatika kampus UMN (disamarkan). Mamazon menjual jasa Paid Promote (PP) dan Buzzer. Divisi SPADE (Fresh Money) bertugas sebagai PJ admin semua divisi.

**Timeline Mamazon:**

- September: Penerimaan anggota baru & interview + rapat pertama
- Oktober: Mulai kerja, cari sponsor, blast tugas PP/Buzzer
- November akhir: Workshop, seminar, pameran, dan lomba

### 2.2 Workflow Saat Ini (Manual)

```
SPADE blast tugas → Anggota submit bukti via Google Form 
→ Masuk spreadsheet per tugas → PJ per divisi cek manual satu-satu
```

### 2.3 Problem Utama

| # | Problem                             | Impact                                                                                                                                                                                                  |
| - | ----------------------------------- | ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| 1 | **Multi-link Google Drive**         | Google Form meng-JOIN multiple file uploads dengan koma. Parser naif memperlakukan sebagai 1 URL panjang. Preview mati, cuma 1 gambar terbaca. **74-81% submission di PP sheets mengandung multi-link** |
| 2 | **Validasi denda error-prone**      | Salah tag = divisi "digoreng" / komplain. PJ cek manual lelah, terlewat                                                                                                                                 |
| 3 | **Angel Card tidak di-spreadsheet** | Data pemilik Angel Card (pembebas PP & Buzzer) terpisah. Sumber error validasi                                                                                                                          |
| 4 | **Bukti gambar tidak muncul**       | Drive link masih "Restricted" → preview mati. PJ harus buka Drive manual                                                                                                                                |
| 5 | **Panitia licik**                   | Fix postingan setelah di-flag → pura-pura tidak pernah salah                                                                                                                                            |
| 6 | **Story 24h expired**               | Cek story sering terlambat, story sudah hilang sebelum dicek                                                                                                                                            |

### 2.4 Data Statistik (Sheet Asli)

| Sheet            | Total link | Multi (koma) | % Multi |
| ---------------- | ---------- | ------------ | ------- |
| PP Bundle 26 Okt | 90         | 73           | 81%     |
| PP - 5 Okt       | 91         | 67           | 74%     |
| PP 29 Okt        | 42         | 34           | 81%     |
| Buzzer IG 09 Okt | 85         | 36           | 42%     |
| PP Feed 26 Okt   | 76         | 8            | 11%     |
| buzzer-6 Okt     | 64         | 0            | 0%      |

---

## 3. Solusi: AI Agent untuk Validasi Submission

### 3.1 Konsep

Agent Python yang:

1. Membaca spreadsheet submission (Google Sheets API / local xlsx)
2. Mem-parse link Google Drive (split koma, extract file_id)
3. Meng-download gambar bukti dari Drive
4. Memproses gambar dengan Vision API (Tencent Hunyuan)
5. Membandingkan dengan rulebase (C1-C10 untuk PP, B1-B7 untuk Buzzer)
6. Cross-check Angel Card Registry
7. Generate output: CSV + blast draft + audit log
8. Kirim blast draft ke koor via Discord (dengan approval gate buttons \[Approve\]\[Hold\]\[Reject])

### 3.2 Yang Agent LAKUKAN

- Parse comma-separated Drive links (root cause problem #1)
- Download dan preview gambar bukti dari Drive
- Vision API: baca caption, hashtag, tag, carousel order, aspect ratio, followers, timestamp dari screenshot
- Compare bukti dengan rulebase (18 aturan PP/Sponsor + Angel Card + Buzzer)
- Detect duplicate/resubmit submission
- Flag violation + hitung denda otomatis
- Cross-reference IG username di spreadsheet vs bukti tambahan
- Catat semua keputusan di audit log

### 3.3 Yang Agent TIDAK LAKUKAN

- **TIDAK scrape Instagram** (melanggar ToS + prinsip Permissions)
- **TIDAK auto-send blast** (butuh approval koor dulu)
- **TIDAK menebak kalau ambigu** (tanya klarifikasi, max 2x, lalu stop)
- **TIDAK halusinasi** (kalau tidak yakin, delegate ke koor)
- **TIDAK akses data di luar scope** (hanya Drive yang di-share + spreadsheet)

---

## 4. Arsitektur & Stack Teknologi

### 4.1 Stack (Revised — Discord Bot Pivot)

| Komponen           | Teknologi                                                     | Biaya                               |
| ------------------ | ------------------------------------------------------------- | ----------------------------------- |
| **Interface**      | Discord Bot (discord.py) — buttons, commands, roles, channels | Gratis                              |
| **Backend LLM**    | WorkBuddy AI (connected via Discord Remote Channels)          | 1,000 credits/person                |
| **Bahasa**         | Python 3.10+                                                  | Gratis                              |
| **Spreadsheet**    | openpyxl (local) / Google Sheets API (production)             | Gratis                              |
| **Drive download** | Google Drive API (service account)                            | Gratis                              |
| **Vision + LLM**   | Tencent Cloud Hunyuan (Vision 1.5 Instruct, Hunyuan-TurboS)   | 1M tokens gratis (1 tahun)          |
| **Blast Reader**   | blast_reader.py — auto-read #blast-tugas, extract ketentuan   | Gratis (Discord API)                |
| **Image Manager**  | image_manager.py — download + cleanup + hash dedup + TTL 24h  | Gratis (local storage)              |
| **Hosting**        | Local (dev/demo) / VPS+Docker (post-hackathon, masih rentan)  | ~Rp 50-100rb/bulan (post-hackathon) |
| **Telegram**       | TIDAK DIGUNAKAN — sudah diganti Discord sepenuhnya            | ❌ Dropped                           |
| **CLI**            | argparse (built-in, untuk testing)                            | -                                   |

### 4.2 Struktur Project (Revised)

```
agent/
  src/
    config.py            — paths, API keys (env vars), sheet-to-task mapping
    main.py              — CLI entry point (for testing)
    data_layer.py        — spreadsheet reader, link parser, Angel Card checker
    rulebase_engine.py   — rulebase loader + validation logic (C1-C10, B1-B7)
    vision_checker.py    — Vision API integration + mock/stub
    output_generator.py  — CSV + blast draft + audit log
    discord_bot.py       — Discord bot: slash commands, buttons, role-gating, embeds
    blast_reader.py      — Auto-read #blast-tugas, extract ketentuan tambahan per-blast
    image_manager.py     — Download images, hash dedup, TTL cleanup, rate-limit aware
    bukti_tambahan.py    — Bukti Tambahan handler (upload, cross-reference)
    evidence_vault.py    — Evidence Vault auto-post + recurring detection
  tests/
  config/
    service_account.json — Google service account key (dari user)
    discord_token.json   — Discord bot token
  output/
    verification_report.csv   — Hasil validasi (append per sheet batch)
    violations_log.csv        — Log akumulatif pelanggaran (stateful)
    blast_draft.md            — Draft pesan denda per sheet
    audit_log.jsonl           — Setiap keputusan agent (timestamped)
    blast_context.json        — Ketentuan tambahan dari #blast-tugas (extracted)
    processed_messages.json   — Cache message_id yang sudah di-process
  tmp/
    images/             — Downloaded bukti images (TTL 24h, auto-cleanup)
    hashes.json         — SHA-256 hash dedup cache
  requirements.txt
```



### 4.3 Mode Operasi (Revised)

| Mode                    | Cara                                                        | Kapan                 |
| ----------------------- | ----------------------------------------------------------- | --------------------- |
| **Discord Bot**         | `/validate sheet:"PP 29 Oktober"` → bot reply hasil         | Production & Demo     |
| **CLI**                 | `python main.py validate --sheet "PP 29 Oktober" --dry-run` | Development & testing |
| **WorkBuddy (backend)** | Discord bot → WorkBuddy LLM (Vision, natural language)      | Heavy AI tasks        |

### 4.4 Mock/Stub Vision API

Development dimulai dengan mock Vision (hardcoded return) untuk:

- Tidak butuh API key saat awal coding
- Testing cepat, tidak tunggu network
- Tidak habis tokens kalau salah coding
- Demo reliable

**Swap ke real API**: ganti config flag `USE_MOCK_VISION = False`. Code sama, tinggal ganti backend.

---

## 5. Rulebase: Aturan Validasi

### 5.1 Struktur Rulebase

Rulebase tersimpan di `_refs/rulebase.json` — single source of truth untuk semua aturan. Tidak berubah entah pakai dummy data atau real spreadsheet.

**Static atau Dynamic?**  
Rulebase.json adalah **STATIC** — hanya developer yang bisa mengubah. Tugas baru yang masuk tidak otomatis masuk ke rulebase.json. Ada dua alasan:

1. **Kontrol kualitas**: Aturan harus diverifikasi developer sebelum aktif. Mencegah AI halusinasi atau membuat aturan yang tidak terverifikasi.
2. **Stabilitas**: Saat validasi berjalan, rulebase tidak boleh berubah mid-run. Bot reload rulebase saat startup.

**Lalu bagaimana dengan tugas baru / ketentuan tambahan per-blast?**

Ada **dua layer ketentuan** yang di-merge saat validasi:

| Layer                  | Source                      | Sifat                        | Isi                                                                                                         |
| ---------------------- | --------------------------- | ---------------------------- | ----------------------------------------------------------------------------------------------------------- |
| **rulebase.json**      | File static, developer edit | Tetap, tidak berubah runtime | Struktur dasar: denda, checks C1-C10/B1-B7, Angel Card rules, task IDs, jadwal dasar                        |
| **blast_context.json** | #blast-tugas channel scan   | Dynamic, di-extract oleh bot | Ketentuan tambahan per-blast: caption referensi, tag spesifik, hashtag, link Drive, deadline, catatan klien |

**Saat /validate dijalankan:**  
Agent MERGE rulebase.json + blast_context.json untuk mendapatkan konteks validasi yang LENGKAP. Ini memastikan ketentuan tambahan dari penyewa jasa tetap diikuti.

**Kalau ada penyesuaian peraturan:**

1. Developer edit rulebase.json (struktur dasar)
2. Restart bot → rulebase reload
3. Atau: koor blast tugas baru di #blast-tugas → bot auto-extract ke blast_context.json (tanpa restart)

**Status peraturan saat ini:** Stabil. Perubahan future akan dilakukan oleh developer dan didokumentasikan.

### 5.2 Denda

| Jenis Pelanggaran                                  | Denda          |
| -------------------------------------------------- | -------------- |
| Tidak post / terlambat post / terlalu cepat post   | Rp 20.000      |
| Tidak kirim bukti                                  | Rp 20.000      |
| Per kesalahan posting (caption, tag, hashtag, dll) | Rp 5.000       |
| Terlambat bayar denda                              | +Rp 5.000/hari |
| Tidak mengerjakan Buzzer                           | Rp 20.000      |
| Buzzer tidak sesuai ketentuan                      | Rp 20.000      |

**Rekening**: BCA 4731763318 a.n. Bertrand Immanuel Wijaya  
**Deadline bayar**: Setiap hari Sabtu pukul 23.59 WIB  
**Kode transfer**: 01=Kas, 02=Denda Kas, 03=PP, 04=Denda PP, 05=Denda Sponsor

### 5.3 Angel Card

| Aspek              | Detail                                                                                               |
| ------------------ | ---------------------------------------------------------------------------------------------------- |
| **Harga**          | Rp 100.000                                                                                           |
| **Pembebas**       | Kewajiban PP + Buzzer                                                                                |
| **TIDAK pembebas** | Sponsor (wajib tetap post)                                                                           |
| **Berlaku untuk**  | Semua jenis PP (IG, Survey, Google Review)                                                           |
| **Syarat berlaku** | Hanya untuk PP/Buzzer yang BELUM di-blast saat pembelian                                             |
| **Masa berlaku**   | Sampai kepanitiaan selesai                                                                           |
| **Validasi**       | Agent compare Tanggal Pembelian vs Tanggal Blast. Beli ≤ Blast = exempt. Beli > Blast = TIDAK exempt |
| **Registry**       | Sheet terpisah, readonly. Write hanya BPH + Koor Spade                                               |

### 5.4 Checks PP (C1-C10)

| ID  | Nama                                     | Denda | Cara Validasi                                           |
| --- | ---------------------------------------- | ----- | ------------------------------------------------------- |
| C1  | IG aktif + ≥300 followers + no swap akun | 5rb   | Vision baca screenshot profil IG                        |
| C2  | Timing post tepat                        | 20rb  | Compare timestamp submission vs jadwal_post & deadline  |
| C3  | Urutan slide/carousel benar              | 5rb   | Vision compare urutan gambar bukti vs referensi         |
| C4  | Caption sesuai (literal/verbatim)        | 5rb   | OCR caption dari screenshot + string-match vs referensi |
| C5  | Tag/mention berfungsi & terlihat         | 5rb   | Vision deteksi tag di caption screenshot                |
| C6  | Hashtag wajib ada                        | 5rb   | Vision/OCR baca hashtag dari screenshot                 |
| C7  | Story keep 24 jam                        | 5rb   | Cek screenshot story + timestamp (lihat §6.3)           |
| C8  | Feed tidak dihapus                       | 5rb   | Cek screenshot masih live                               |
| C9  | Aspect ratio sesuai                      | 5rb   | Vision baca dimensi gambar vs referensi                 |
| C10 | Bukti terkirim & timely                  | 20rb  | Cek timestamp vs deadline                               |

### 5.5 Checks Buzzer (B1-B7)

| ID | Nama                                         | Denda | Cara Validasi                         |
| -- | -------------------------------------------- | ----- | ------------------------------------- |
| B1 | Like sesuai target (dual platform)           | 20rb  | Vision deteksi like pada screenshot   |
| B2 | Komentar natural (bukan template/emoji saja) | 20rb  | LLM judge: panjang + mengandung kata  |
| B3 | Ulasan sesuai template per divisi            | 20rb  | LLM judge: compare isi vs template    |
| B4 | Rating bintang 5                             | 20rb  | Vision deteksi rating di screenshot   |
| B5 | Vote campaign benar                          | 20rb  | Vision deteksi vote di screenshot     |
| B6 | Jawaban jujur & no-spam                      | 20rb  | Statistical check: distribusi jawaban |
| B7 | Bukti terkirim & timely                      | 20rb  | Cek timestamp vs deadline             |

### 5.6 Date Guardrail (G1)

Validasi 4 tahap:

1. Parse tanggal mulai + deadline
2. Validasi tanggal eksis & nama hari cocok
3. Validasi deadline > mulai
4. Validasi deadline ≥ jadwal_post

Jika invalid → tanya klarifikasi dengan 2-3 opsi revisi konkret → tunggu → kalau tetap invalid → **tolak dengan sopan**.

---

## 6. Flow Validasi Instagram (Evidence-Based)

### 6.1 Prinsip Utama

**Agent TIDAK scrape Instagram.** Agent membaca screenshot yang panitia upload ke Google Drive. Ini align dengan prinsip Permissions (agent hanya akses data yang diizinkan).

### 6.2 Flow Validasi

```
Panitia submit screenshot IG (upload ke Google Drive via Google Form)
        ↓
Agent download gambar dari Drive (service account, punya permission)
        ↓
Vision API baca screenshot:
  - Extract caption text
  - Detect carousel order
  - Detect aspect ratio
  - Read follower count
  - Read timestamp posting
  - Detect tags/hashtags
        ↓
Rulebase engine compare:
  - Caption vs expected (C4)
  - Tags vs required (C5)
  - Hashtags vs mandatory (C6)
  - Carousel order vs rulebase (C3)
  - Aspect ratio vs requirement (C9)
  - Timestamp vs blast schedule (C2)
  - Followers >= 300 (C1)
        ↓
Output: violation list + evidence link
```

### 6.3 C7 Story 24h — Status Tiga Tingkat

| Status           | Kondisi                                                         |
| ---------------- | --------------------------------------------------------------- |
| **PASS**         | Screenshot jelas show story live, timestamp within 24h of blast |
| **FLAG**         | Bukti tidak sufficient atau timing mencurigakan                 |
| **INCONCLUSIVE** | Agent tidak bisa determine, delegate ke koor                    |

**Detection signals:**

- Screenshot show "Uploading your story" text → FLAG "story belum live saat screenshot"
- Timestamp screenshot >24h after blast → FLAG "story mungkin sudah expired"
- Screenshot blank/no content → FLAG "tidak ada bukti story"
- All ambiguous → INCONCLUSIVE

### 6.4 Anti-Cheat Layers

| Layer                  | Cara Kerja                                                                  |
| ---------------------- | --------------------------------------------------------------------------- |
| **Metadata check**     | Baca EXIF/metadata gambar — tanggal upload, device info. Tidak match → flag |
| **Vision anomaly**     | Vision API detect: gambar edited? Text inconsistent? Pixel anomaly?         |
| **Cross-reference**    | IG username di screenshot = IG username di spreadsheet?                     |
| **violations_log.csv** | Panitia dengan riwayat violation → flag RECURRING, escalate                 |

### 6.5 Instagram Graph API (Future Enhancement)

Graph API = gratis. Tapi:

- Standard Access = hanya akun yang Mamazon kelola (tidak bisa cek akun panitia)
- App Review untuk akses akun panitia = butuh 1-4 minggu
- **Untuk hackathon: evidence-based validation tetap primary**
- **Post-hackathon**: bisa daftar Meta Developer untuk hashtag search + insights



## 8. Handling Edge Cases

### 8.1 Sheet Name Collision & Ambiguity

**Masalah**: "PP 27 Oktober 2026" vs "PP 27 okt 2026" vs "PP 27 okt" — sama tapi tidak sama.

**Solusi**: Fuzzy match + normalization (expand "okt" → "oktober", case insensitive, strip spaces). Jika >1 match → tetap konfirmasi. Tidak pernah menebak.

**Max 2 clarifications, lalu stop:**

1. Attempt 1: kasih opsi konkret ("Maksud Anda: 1. PP Feed 27 Okt, 2. PP Bundle 27 Okt?")
2. Attempt 2: opsi sempit ("Ketik nomor 1 atau 2")
3. Attempt 3: STOP ("Maaf, saya tidak dapat menentukan sheet. Tolong sebutkan nama lengkap.")

### 8.2 Duplicate & Resubmit

| Skenario                  | Cara Agent Handle                                  |
| ------------------------- | -------------------------------------------------- |
| Submit 2x, isi sama       | Ambil latest, skip lama, log "duplicate detected"  |
| Submit 2x, beda gambar/IG | Ambil latest, compare changes, detect account swap |
| Submit 3x+                | Ambil latest, log semua versi di audit log         |
| Submit ke sheet berbeda   | Flag "cross-sheet duplicate"                       |

**Stateful memory**: verification_report.csv sebagai persistent record.

- Sudah VERIFIED → skip (tidak re-validasi, hemat token)
- FLAGGED → re-validate kalau resubmit untuk perbaikan

### 8.3 IG Typo Handling

| Skenario                                            | Cara Agent Handle                                                         |
| --------------------------------------------------- | ------------------------------------------------------------------------- |
| Spreadsheet: `starfours_`, Bukti: `starfours_`      | Exact match → lanjut                                                      |
| Spreadsheet: `starfours`, Bukti: `starfours_`       | Fuzzy match (distance ≤ 2) → note "panitia typo, yang benar: starfours\_" |
| Spreadsheet: `starfur`, Bukti: `starfours_`         | Fuzzy fail (distance > 2) → flag "butuh konfirmasi koor"                  |
| Spreadsheet: `starfours_`, tidak ada bukti tambahan | Proses dengan username spreadsheet saja. Note "tidak diverifikasi PJ"     |

**IG typo = note, bukan error fatal.**

### 8.4 Wrong-Submit Detection

Panitia submit ke form yang salah (kerjakan tugas A, submit ke form B):

- Agent scan gambar → Vision summary → match ke tugas lain
- Flag: "panitia salah submit, bukti ini adalah tugas A"

### 8.5 Intent Guard (Pre-Validation)

Sebelum validasi dimulai, agent cek: apakah ini submission PP/Buzzer yang valid? Kalau bukan → flag wrong-submit. Ini juga antisipasi test case Tencent yang mungkin out-of-scope.

---

## 9. Output Artifacts

### 9.1 Lokasi File Output

Semua file output tersimpan di `agent/output/` dalam folder project:

```
agent/output/
  verification_report.csv   — Hasil validasi semua PP & Buzzer
  violations_log.csv        — Log akumulatif pelanggaran (stateful)
  blast_draft.md            — Draft pesan denda per sheet
  audit_log.jsonl           — Setiap keputusan agent
  blast_context.json        — Ketentuan tambahan dari #blast-tugas
  processed_messages.json   — Cache message_id yang sudah di-process

agent/tmp/
  images/                   — Downloaded bukti images (TTL 24h)
  hashes.json               — SHA-256 hash dedup cache
```

### 9.2 Empat File Output Utama

| File                      | Format     | Isi                                                                                              |
| ------------------------- | ---------- | ------------------------------------------------------------------------------------------------ |
| `verification_report.csv` | CSV        | Satu baris per (panitia, tugas): status, daftar pelanggaran, total denda, link bukti, confidence |
| `violations_log.csv`      | CSV        | Log akumulatif pelanggaran per panitia across waktu (stateful)                                   |
| `blast_draft.md`          | Markdown   | Draft pesan per divisi, siap dikirim via Discord setelah approval koor                           |
| `audit_log.jsonl`         | JSON Lines | Setiap keputusan agent: timestamp, input, tool, output, rationale, confidence                    |

### 9.3 Scaling: Apakah verification_report.csv Akan Membengkak?

**Pertanyaan:** verification_report.csv berisi hasil semua PP & Buzzer. Apakah nanti bakal membengkak dan membuat agent kewalahan?

**Jawaban: Tidak, karena strategi berikut:**

| Strategi                  | Implementasi                                                                   |
| ------------------------- | ------------------------------------------------------------------------------ |
| **Append per batch**      | CSV di-append per sheet validation, buakn per panitia. 1 sheet = 1 batch       |
| **Status-based skip**     | Sudah VERIFIED → skip (tidak re-validasi, hemat token + baca CSV cepat)        |
| **Index by sheet_name**   | Agent baca CSV filter by sheet_name (tidak load seluruh file ke memory)        |
| **Archive old data**      | Setelah 30 hari atau periode selesai, CSV lama di-archive ke `output/archive/` |
| **Max estimate**          | 106 panitia × 12 tugas = max ~1,272 rows. CSV dengan 1K-2K rows = still fast   |
| **CSV reader: streaming** | Python csv.DictReader baca per baris, tidak load semua ke RAM                  |
| **violations_log.csv**    | Hanya berisi yang FLAGGED (bukan semua panitia). Jauh lebih kecil              |

**Estimasi ukuran file setelah 1 periode Mamazon (Okt-Nov):**

- verification_report.csv: ~1,272 rows × ~500 bytes/row ≈ 635 KB (sangat kecil)
- violations_log.csv: ~200 rows (hanya yang bermasalah) × ~300 bytes ≈ 60 KB
- audit_log.jsonl: ~500 entries × ~800 bytes ≈ 400 KB
- Total: <2 MB. Tidak akan membengkak untuk 1 periode.

**Kalau multi-periode (tahun depan):** Archive CSV lama, start fresh.


### 9.4 Image Storage Management

**MASALAH:** Agent download gambar bukti dari Drive. Kalau tidak di-cleanup, storage membengkak.

**SOLUSI — image_manager.py:**

| Fitur              | Implementasi                                                                |
| ------------------ | --------------------------------------------------------------------------- |
| **TTL 24h**        | Gambar di `tmp/images/` auto-delete setelah 24 jam. Cron job atau on-demand |
| **Hash dedup**     | SHA-256 hash per file. Kalau panitia upload gambar yang sama 2x → skip download |
| **Batch download** | Download per sheet (bukan per panitia). Max 100 files per batch             |
| **Rate limit aware**| Jika VPS/download dibatasi: retry + exponential backoff + cache             |
| **Compress**       | Gambar >2MB di-compress ke <500KB sebelum Vision API (hemat token)          |
| **Evidence vault** | Bukti violation disimpan permanent di Drive (bukan local). Local hanya temp |

**Flow:**
```
Download image → compute hash → check dedup → save to tmp/images/
    → Vision API process → save result → mark for deletion (TTL 24h)
    → Cron: delete files older than 24h
    → Evidence: upload ke Drive Evidence Vault (permanent, not local)
```

### 9.5 Format Blast Discord

```
📋 Hasil Validasi: PP Feed 26 Oktober 2026

✅ @starfours_/: AMAN
   - Bukti utama: caption sesuai, tag benar, hashtag lengkap
   - Bukti tambahan: story 24h terverifikasi oleh PJ
   - Angel Card: tidak dimiliki

⚠️ @raihanrizky/: DENDA Rp 15.000
   - Caption: tidak sesuai template (-5rb C4)
   - Tag: tidak mention sponsor (-5rb C5)
   - Hashtag: kurang #Mamazon2026 (-5rb C6)
   - Catatan PJ: "caption kayaknya di-edit setelah upload"

❌ @user3/: DENDA Rp 30.000
   - Story: tidak ada bukti tambahan dari PJ (-5rb C7)
   - Followers: <300 (-5rb C1)
   - Carousel: urutan salah (-5rb C3)

📊 Total: 76 submissions, 71 AMAN, 5 DENDA
```

### 9.6 Discord Blast Flow

```
Agent → draft pesan (belum kirim)
    ↓
Koor Spade → review di #blast-drafts + click [Approve] / [Hold] / [Reject]
    ↓
Koor approve → Agent post ke #denda (semua panitia bisa lihat, read-only)
    ↓
Agent → catat di violations_log.csv + verification_report.csv
```

**Agent TIDAK auto-send. Approval gate wajib via Discord buttons.**

---

## 10. API & Pricing

### 10.1 Tencent Cloud Hunyuan

| Item                        | Detail                                                                           |
| --------------------------- | -------------------------------------------------------------------------------- |
| **Hunyuan-Lite**            | Completely free, tanpa batas                                                     |
| **10 model utama** (shared) | 1 juta tokens gratis, berlaku 1 tahun                                            |
| **Termasuk**                | Vision 1.5 Instruct, Hunyuan-turbos-vision, Hunyuan-T1, Hunyuan-TurboS           |
| **Embedding**               | Tambahan 1 juta tokens gratis                                                    |
| **Cara daftar**             | cloud.tencent.com → Hunyuan console → "开通服务" → klaim tokens                      |
| **Yang kita pakai**         | Vision untuk screenshot analysis, Hunyuan-Lite untuk text/intent                 |
| **Estimasi pemakaian**      | ~106 panitia × 12 task × 500-1000 tokens = 640K-1.27M tokens. Cukup di free tier |
| **Total biaya hackathon**   | Rp 0                                                                             |


### 10.2 Google Drive API

| Item | Detail |
|------|--------|
| **Biaya** | Standard use = gratis |
| **Quota per project per minute** | 1,000,000 units |
| **Quota per project per day** | 400,000,000 units (sebelum billing) |
| **Download file** | 200 units/file |
| **Estimasi per sheet** | ~32K units (63 links × 126 downloads × 200 units) |
| **Cukup untuk** | ~12,500 sheet validasi per hari |
| **Total biaya** | Rp 0 |

**Setup**:
1. Buat project di Google Cloud Console
2. Enable Google Drive API + Google Sheets API
3. Buat Service Account
4. Download JSON key file
5. Share folder Google Drive ke email service account
6. Masukkan path JSON key ke config agent

### 10.3 Discord Bot API

| Item | Detail |
|------|--------|
| **Biaya** | Gratis, tanpa quota limit |
| **Yang kita pakai** | Slash commands, embed messages, buttons, role-gating, auto-read channel |
| **Rate limit** | 50 requests/second per bot (more than enough) |
| **Message scan** | 100 messages per API call, max 1000 per request |
| **Setup** | Discord Developer Portal → Bot → Token → invite to server |


### 10.4 Instagram Graph API (Future)

| Item | Detail |
|------|--------|
| **Biaya** | Gratis |
| **Standard Access** | Hanya akun yang Mamazon kelola |
| **App Review** | Butuh 1-4 minggu untuk akses akun panitia |
| **Untuk hackathon** | TIDAK feasible. Evidence-based tetap primary |
| **Post-hackathon** | Daftar Meta Developer untuk hashtag search + insights |

---

## 11. Roadmap & Timeline

### 11.1 Timeline (7 November Deadline)

| Fase | Hari | Aktivitas | Status |
|------|------|-----------|--------|
| Design & Spec | Day 1-2 (1-3 Nov) | Rulebase, dummy data, spec PDF | ✅ DONE |
| Build core | Day 3-4 (4-5 Nov) | data_layer ✅ + rulebase_engine + vision_checker + CLI | data_layer ✅, lainnya TODO |
| Testing | Day 5-6 (6 Nov) | Run agent di dummy data, debug edge cases | TODO |
| Demo prep | Day 7 (7 Nov) | Polish output, demo scenario, backup plan | TODO |

### 11.2 Build Priority

| Priority | Komponen | Estimasi |
|----------|----------|----------|
| **MUST** | Discord bot skeleton (discord.py, slash commands, role-gating) | 4h |
| **MUST** | `/validate` PP core (parse koma → rulebase + blast_context → Vision → C1-C10 → CSV + embed) | 6h |
| **MUST** | blast_reader.py (scan #blast-tugas, extract ketentuan, save blast_context.json) | 2h |
| **MUST** | `/blast` + approval buttons + audit log + CSV output | 3h |
| **MUST** | Date guardrail + approval + audit log | 0.5h |
| **MUST** | Deliverables hackathon (accountability sheet, diagram, demo video, source code) | 1h |
| **MUST** | `/list-active` + `/check-angel` + `/denda-list` + `/ketentuan-pp/buzzer/angel-card` | 3h |
| **SHOULD** | image_manager.py (download + cleanup + hash dedup + TTL + rate-limit) | 1h |
| **SHOULD** | Angel Card cross-check + violations_log + blast_draft | 0.5h |
| **SHOULD** | Bukti Tambahan tab reader + C7 story check | 0.5h |
| **SHOULD** | Buzzer validation + wrong-submit detection | 0.5h |
| **NICE** | #ai-chat (WorkBuddy LLM integration) | 2h |
| **NICE** | Resubmit handler + duplicate detection | 2h |
| **NICE** | `/scan-blast` command (manual trigger) | 1h |
| **NICE** | Voice note STT (poin bonus hackathon) | 3h |


### 11.3 Yang Sudah Selesai (Bisa Langsung Dipakai Developer)

File-file berikut SUDAH ADA di project dan bisa langsung dikirim ke developer:

| File | Lokasi | Status | Fungsi |
|------|--------|--------|--------|
| `config.py` | `agent/src/config.py` | ✅ Done | Paths, API keys (env vars), sheet-to-task mapping, constants |
| `data_layer.py` | `agent/src/data_layer.py` | ✅ Done & tested | Spreadsheet reader, comma-split link parser, Angel Card checker, roster loader |
| `main.py` | `agent/src/main.py` | ✅ Done & tested | CLI: `list-sheets`, `validate --dry-run`, `angel-card` |
| `rulebase.json` | `_refs/rulebase.json` | ✅ Done | 18 aturan PP/Sponsor, Angel Card rules, 6 PP tasks + 6 Buzzer tasks, 17 checks (C1-C10, B1-B7), guardrail, tiered matching, anti-cheat |
| `dummy_dataset_anonymized.xlsx` | `_refs/dummy_dataset_anonymized.xlsx` | ✅ Done | 106 panitia, 13 sheets, Angel Card Registry (17 entries) |
| `build_dummy.py` | `_refs/build_dummy.py` | ✅ Done | Script untuk rebuild dummy dataset |
| `build_rulebase.py` | `_refs/build_rulebase.py` | ✅ Done | Script untuk rebuild rulebase.json |
| `requirements.txt` | `agent/requirements.txt` | ✅ Done | Dependencies: openpyxl, google-api-python-client, requests, tencentcloud-sdk-python, pillow |
| `Mamazon_Spade_Agent_Specification_v2.pdf` | Root | ✅ Done | Spec document (12 halaman) |
| `Mamazon_Spade_Agent_Planning.md` | Root | ✅ Done | This document |
| `Mamazon_Spade_Agent_Workflow.md` | Root | ✅ Done | Workflow document (happy/unhappy flows) |

**Tested working:**
- `python main.py list-sheets` → list 13 sheets dengan task mapping
- `python main.py validate --sheet "PP 29 Oktober" --dry-run` → 42 submissions, 34 multi-link parsed correctly
- `python main.py angel-card` → 17 entries, date-based validation working

### 11.4 Yang Perlu Dikerjakan (TODO untuk Developer)

| File / Komponen | Estimasi | Dependencies |
|-----------------|----------|--------------|
| `discord_bot.py` | 4h | discord.py, config.py |
| `rulebase_engine.py` | 3h | rulebase.json, data_layer.py |
| `vision_checker.py` | 3h | Tencent SDK (atau mock) |
| `output_generator.py` | 2h | data_layer.py |
| `blast_reader.py` | 2h | discord.py, rulebase.json |
| `image_manager.py` | 1h | Google Drive API |
| `bukti_tambahan.py` | 2h | data_layer.py |
| `evidence_vault.py` | 1h | output_generator.py |
| Bukti Tambahan tab reader di data_layer | 0.5h | data_layer.py (extend) |
| Duplicate/resubmit handler | 2h | data_layer.py, output_generator.py |
| Sheet name resolver dengan fuzzy match | 2h | data_layer.py |
| Intent guard (wrong-submit detection) | 1h | vision_checker.py |
| End-to-end test: PP 29 Oktober | 1h | Semua di atas |
| Demo video 5-8 menit | 2h | Working bot |
| Architecture diagram | 1h | — |
| Accountability sheet | 1h | audit_log.jsonl |

### 11.5 Yang Perlu dari User

- [ ] Tencent Cloud account + API key (SecretId + SecretKey) — atau pakai mock dulu
- [ ] Google Cloud service account JSON key
- [ ] Discord bot token (dari Discord Developer Portal)
- [ ] Discord server ID + role IDs (setup server Mamazon)
- [ ] Konfirmasi "gas" untuk mulai build

---

## 12. Deliverables Hackathon

| Item | Format | Status |
|------|--------|--------|
| Title + blurb (<10 kata) | Text | TODO |
| Project description | Text | TODO |
| Accountability sheet | Document | TODO (dari audit_log.jsonl) |
| Test results + screenshot | Image | TODO |
| Architecture diagram | Image | TODO |
| Demo video (5-8 menit) | Video | TODO |
| Product sharing | Document | TODO |
| Source code | Code (repo) | In progress (`agent/`) |
| Cover image | Image | TODO |

---

## 13. Glosarium

| Istilah | Arti |
|---------|------|
| **PP** | Paid Promote — jasa posting Instagram berbayar |
| **Buzzer** | Tugas like/komen/vote/review untuk boost engagement |
| **Angel Card** | Kartu pembebas PP & Buzzer (Rp 100rb, tidak berlaku untuk Sponsor) |
| **SPADE** | Divisi Fresh Money — PJ admin semua divisi |
| **Blast** | Pengumuman tugas dari SPADE ke panitia |
| **PJ** | Penanggung Jawab per divisi |
| **Koor** | Koordinator (koor Spade = koordinator divisi Spade) |
| **Bukti Tambahan** | Tab spreadsheet untuk PJ upload screenshot story + cross-validation |
| **Evidence Vault** | Tab spreadsheet untuk PJ screenshot violation anti-cheat |
| **Rulebase** | JSON file berisi semua aturan validasi (single source of truth, static — developer edit only) |
| **Blast Context** | JSON file berisi ketentuan tambahan dari #blast-tugas (dynamic, auto-extracted by bot) |
| **Vision API** | API yang bikin komputer "melihat" gambar (baca text, detect object, dll) |
| **Mock/Stub** | Versi palsu API yang return hardcoded result (untuk testing tanpa API key) |
| **Dry-run** | Mode CLI yang parse data tanpa manggil API |
| **C1-C10** | 10 checks validasi PP |
| **B1-B7** | 7 checks validasi Buzzer |
| **G1** | Date Guardrail — validasi rentang tanggal & jam |
| **Image Manager** | Module untuk download, hash dedup, TTL cleanup, dan rate-limit aware image handling |
| **Blast Reader** | Module untuk auto-read #blast-tugas channel dan extract ketentuan tambahan |
| **TTL** | Time To Live — durasi sebelum file auto-delete (default 24h untuk images) |

---

> **Dokumen ini disusun berdasarkan diskusi kak wldnz & Buddie (WorkBuddy AI) pada 1-5 November 2026.**
> **Untuk pertanyaan atau klarifikasi, kontak kak wldnz.**
