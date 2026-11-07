"""LangChain tools exposed to the chat agent.

Every tool here is read-only or bounded. The role gate is not re-implemented:
the tools delegate to `chat_agent.AgentToolkit`, which already enforces
`config.PERMISSIONS` and the private-fine roster binding. That keeps one
source of truth for authorization — the agent cannot widen its own access.

Mutating commands (validate, blast, add-angel, ...) are deliberately absent.
They stay in the deterministic planner, which returns a `pending_action` that
the Discord layer renders as a confirmation button.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Sequence

from langchain_core.tools import tool

logger = logging.getLogger(__name__)


def build_tools(toolkit: Any) -> list[Any]:
    """Create the tool list bound to one `AgentToolkit` instance."""
    if toolkit is None:
        return []

    @tool
    def search_knowledge(query: str) -> str:
        """Cari_aturan_C1-C10_B1-B7_ketentuan_tugas_denda_dan_command.

        Gunakan ini saat pengguna menanyakan aturan, nilai denda, syarat
        tugas, atau perintah apa saja yang tersedia.

        Args:
            query: pertanyaan pengguna dalam bahasa Indonesia.
        """
        from . import knowledge

        store = knowledge.ensure_store()
        if store is None:
            return "Basis pengetahuan sedang tidak tersedia."
        results = knowledge.search(query, store=store)
        if not results:
            return "Tidak ada pengetahuan yang cocok di basis data."
        lines = []
        for doc, score in results:
            lines.append(f"[{doc.metadata.get('id', '?')}] {doc.page_content}")
        return "\n".join(lines)

    @tool
    def get_task_requirements(task_id: str) -> str:
        """Ambil_ketentuan_lengkap_untuk_sebuah_tugas_PP-03_atau_BZ-06.

        Mengembalikan platform, jadwal, deadline, dan semua Poin check yang
        berlaku untuk tugas tersebut.

        Args:
            task_id: kode tugas, contoh PP-03 atau BZ-06.
        """
        return toolkit._ketentuan(task_id).text

    @tool
    def get_fines(nama: str = "") -> str:
        """Ambil_rekap_denda_dan_daftar_anggota_yang_terkena_denda.

        Gunakan nama kosong (nama="") untuk mengambil daftar seluruh anggota
        panitia yang terkena denda beserta divisi dan total dendanya.
        Isi nama bila hanya ingin rincian denda satu panitia spesifik.

        Args:
            nama: nama panitia spesifik, atau kosongkan untuk melihat semua anggota yang kena denda.
        """
        return toolkit._denda(nama).text

    @tool
    def get_sheet_detail(nama_sheet: str = "") -> str:
        """Lihat_daftar_sheet_atau_detail_satu_sheet.

        Args:
            nama_sheet: nama sheet; kosongkan untuk daftar semua sheet.
        """
        return toolkit._sheets(nama_sheet or None).text

    @tool
    def get_task_overview() -> str:
        """Lihat_status_tugas_aktif_beserta_deadline-nya."""
        return toolkit._active().text

    @tool
    def get_angel_cards() -> str:
        """Lihat_daftar_panitia_yang_memiliki_Angel_Card_aktif.

        Mengembalikan daftar panitia dengan Angel Card, divisi, dan tanggal pembelian.
        Gunakan data ini untuk menjawab pertanyaan soal Angel Card, termasuk jika pengguna
        hanya meminta nama saja atau format khusus.
        """
        return toolkit._angels().text

    @tool
    def get_resubmit_status(nama: str) -> str:
        """Cek_status_resubmit_seorang_panitia.

        Args:
            nama: nama panitia yang dicek.
        """
        return toolkit._resubmit(nama).text

    @tool
    def get_audit_log() -> str:
        """Lihat_10_entri_audit_log_terakhir."""
        return toolkit._audit().text

    @tool
    def calculate(expression: str) -> str:
        """Hitung_ekspresi_matematika_sederhana_(+,-,*,/,//,%).

        Args:
            expression: ekspresi, contoh "2 + 3 * 4".
        """
        from ..chat_agent import _calc

        try:
            return _calc(expression)
        except ValueError as exc:
            return f"Tidak bisa menghitung: {exc}"

    @tool
    def remember_fact(fact: str) -> str:
        """Simpan_fakta_preferensi_atau_konteks_pribadi_pengguna.

        Jangan pernah menyimpan password, token, atau API key.

        Args:
            fact: fakta singkat yang perlu diingat.
        """
        return toolkit._store(fact).text

    @tool
    def recall_facts() -> str:
        """Lihat_fakta_yang_tersimpan_tentang_pengguna."""
        return toolkit._recall().text

    @tool
    def forget_facts() -> str:
        """Hapus_semua_fakta_yang_tersimpan_tentang_pengguna."""
        return toolkit._forget().text

    @tool
    def get_member_history(nama: str) -> str:
        """Lihat riwayat validasi historis dan denda seorang anggota panitia dari memori persisten."""
        from ..memory_store import get_memory_store
        store = get_memory_store()
        records = store.get_member_history(nama)
        if not records:
            return f"Belum ada catatan riwayat validasi untuk '{nama}' di memori persisten."
        lines = [f"Riwayat validasi untuk **{nama}** ({len(records)} catatan):"]
        for r in records[:5]:
            lines.append(
                f"• Sheet: **{r.get('sheet_name', '-')}** | Task: `{r.get('task_id', '-')}` | "
                f"Status: **{r.get('status', '-')}** | Denda: Rp{r.get('total_denda', 0):,}"
            )
            for v in r.get("violations", []):
                lines.append(f"  › Pelanggaran `{v.get('check_id')}`: {v.get('detail')} (-Rp{v.get('denda', 0):,})")
        return "\n".join(lines)

    @tool
    def get_validation_batches_history(limit: int = 5) -> str:
        """Lihat riwayat proses validasi sheet yang pernah dijalankan bot sebelumnya."""
        from ..memory_store import get_memory_store
        store = get_memory_store()
        batches = store.get_validation_batches(limit=limit)
        if not batches:
            return "Belum ada riwayat batch validasi tersimpan di memori persisten."
        lines = ["Riwayat batch validasi tersimpan:"]
        for b in batches:
            lines.append(
                f"• **{b.get('sheet_name')}** ({b.get('task_id') or '-'}): "
                f"{b.get('total', 0)} diperiksa, {b.get('flagged_count', 0)} denda, "
                f"total denda: Rp{b.get('total_denda', 0):,} (oleh {b.get('actor') or 'sistem'})"
            )
        return "\n".join(lines)

    @tool
    def store_cross_thread_fact(key: str, value: str) -> str:
        """Simpan fakta penting cross-thread yang tetap bertahan saat bot restart/mati."""
        from ..memory_store import get_memory_store
        store = get_memory_store()
        user_key = toolkit._user_key()
        store.set_fact(user_key, key, value)
        return f"Fakta '{key}' berhasil disimpan ke memori jangka panjang."

    @tool
    def recall_cross_thread_facts() -> str:
        """Lihat semua fakta memori jangka panjang pengguna lintas thread."""
        from ..memory_store import get_memory_store
        store = get_memory_store()
        user_key = toolkit._user_key()
        facts = store.get_facts(user_key)
        if not facts:
            return "Belum ada fakta cross-thread tersimpan untuk akun kamu."
        lines = ["Fakta memori jangka panjang kamu:"]
        for k, v in facts.items():
            lines.append(f"• **{k}**: {v}")
        return "\n".join(lines)

    @tool
    def duckduckgo_web_search(query: str) -> str:
        """Cari informasi publik di web menggunakan DuckDuckGo Search."""
        from ..web_scraper import duckduckgo_search
        results = duckduckgo_search(query, max_results=5)
        if not results:
            return f"Tidak ditemukan hasil pencarian web untuk '{query}'."
        lines = [f"Hasil pencarian web untuk **{query}**:"]
        for r in results:
            lines.append(f"• **{r.get('title')}**\n  URL: {r.get('href')}\n  {r.get('body')}")
        return "\n".join(lines)

    @tool
    def scrape_web_page(url: str) -> str:
        """Buka dan baca konten teks dari sebuah halaman web publik."""
        from ..web_scraper import scrape_page
        data = scrape_page(url)
        if "error" in data:
            return f"Gagal membaca URL: {data['error']}"
        return f"**Judul:** {data.get('title')}\n**Deskripsi:** {data.get('description')}\n**Konten:**\n{data.get('content')}"

    @tool
    def inspect_instagram_post_url(url: str) -> str:
        """Periksa postingan Instagram secara langsung (author, caption, dan preview image)."""
        from ..web_scraper import extract_instagram_post
        scraped = extract_instagram_post(url)
        if not scraped.success:
            return f"Gagal membaca postingan Instagram: {scraped.error}"
        return (
            f"**Post Instagram:** {scraped.url}\n"
            f"• **Author:** @{scraped.author or '-'}\n"
            f"• **Caption:** {scraped.caption or '(kosong)'}\n"
            f"• **Preview Image:** {'Tersedia & terunduh' if scraped.local_image_path else 'Tidak ditemukan'}"
        )

    @tool
    def mcp_read_sheet_range(range_a1: str, spreadsheet_id: str = "") -> str:
        """Baca data cell dari Google Sheets secara realtime menggunakan freema/mcp-gsheets MCP.

        Args:
            range_a1: Notasi rentang cell A1 (contoh: 'Bukti Tambahan!A1:D10' atau 'PP Bundle 26 Oktober!A1:Z5').
            spreadsheet_id: ID spreadsheet (opsional; default memakai spreadsheet aktif).
        """
        from ..mcp_client import get_mcp_client
        client = get_mcp_client()
        return client.read_range(spreadsheet_id, range_a1)

    @tool
    def mcp_get_spreadsheet_metadata(spreadsheet_id: str = "") -> str:
        """Dapatkan struktur dan daftar sheet dari Google Sheets secara realtime via freema/mcp-gsheets MCP.

        Args:
            spreadsheet_id: ID spreadsheet (opsional; default memakai spreadsheet aktif).
        """
        from ..mcp_client import get_mcp_client
        client = get_mcp_client()
        return client.get_metadata(spreadsheet_id)

    @tool
    def mcp_write_sheet_range(range_a1: str, values_json: str, spreadsheet_id: str = "") -> str:
        """Tulis atau perbarui data cell ke Google Sheets secara realtime via freema/mcp-gsheets MCP.

        Args:
            range_a1: Notasi rentang cell A1 target (contoh: 'Sheet1!A2:B2').
            values_json: Nilai cell dalam format JSON array 2D (contoh: '[[\"val1\", \"val2\"]]').
            spreadsheet_id: ID spreadsheet (opsional; default memakai spreadsheet aktif).
        """
        from ..chat_agent import member_can
        if not member_can(toolkit.member, "validate"):
            return "Akses ditolak: Hanya koordinator atau peran berwenang yang dapat menulis ke spreadsheet."
        import json
        try:
            values = json.loads(values_json)
            if not isinstance(values, list):
                return "Format values_json harus berupa JSON array 2 dimensi, misal: [[\"a\", \"b\"]]"
        except Exception as exc:
            return f"Format JSON tidak valid: {exc}"

        from ..mcp_client import get_mcp_client
        client = get_mcp_client()
        return client.update_range(spreadsheet_id, range_a1, values)

    @tool
    def provider_status() -> str:
        """Lihat_status_provider_AI_yang_aktif."""
        return toolkit.provider_status().text

    @tool
    def plan_validate_sheet(sheet: str, divisi: str = "", force: bool = False) -> str:
        """Siapkan_rencana_validasi_sheet_Google_Sheets_(PP_atau_Buzzer).

        Gunakan ini saat pengguna meminta validasi sheet. Validasi memerlukan
        konfirmasi tombol oleh pengguna sebelum dieksekusi.

        Args:
            sheet: nama sheet yang divalidasi. Jika ambigu, tanyakan konfirmasi terlebih dahulu.
            divisi: filter divisi tertentu, atau kosongkan untuk semua divisi.
            force: atur True jika ingin memaksa re-validasi baris VERIFIED.
        """
        return toolkit.plan_command("validate", {"sheet": sheet, "divisi": divisi, "force": force})

    @tool
    def plan_blast_draft(sheet: str, divisi: str = "") -> str:
        """Siapkan_rencana_pembuatan_draft_blast_denda_untuk_sheet.

        Gunakan ini saat pengguna meminta blast atau draft pengumuman denda.

        Args:
            sheet: nama sheet yang ingin dibuat draft blast-nya.
            divisi: batasi ke divisi tertentu, atau kosongkan untuk semua.
        """
        return toolkit.plan_command("blast", {"sheet": sheet, "divisi": divisi})

    @tool
    def plan_add_angel_card(nama: str, tanggal: str, divisi: str = "") -> str:
        """Siapkan_rencana_penambahan_Angel_Card_panitia.

        Args:
            nama: nama panitia penerima Angel Card.
            tanggal: tanggal pembelian format YYYY-MM-DD (contoh: 2026-10-03).
            divisi: divisi panitia (opsional).
        """
        return toolkit.plan_command("add-angel", {"nama": nama, "tanggal": tanggal, "divisi": divisi})

    @tool
    def plan_remove_angel_card(nama: str) -> str:
        """Siapkan_rencana_penghapusan_Angel_Card_panitia.

        Args:
            nama: nama panitia yang Angel Card-nya dihapus.
        """
        return toolkit.plan_command("remove-angel", {"nama": nama})

    @tool
    def plan_scan_blast(days: int = 7) -> str:
        """Siapkan_rencana_pemindaian_pesan_tugas_di_channel_blast_tugas.

        Args:
            days: jumlah hari ke belakang yang ingin dipindai (default 7).
        """
        return toolkit.plan_command("scan-blast", {"days": days})

    @tool
    def plan_hold_blast(blast_id: str, reason: str = "") -> str:
        """Siapkan_rencana_penahanan_draft_blast_denda.

        Args:
            blast_id: ID blast draft yang ingin ditahan.
            reason: alasan penahanan blast.
        """
        return toolkit.plan_command("hold-blast", {"blast_id": blast_id, "reason": reason})

    @tool
    def plan_release_blast(blast_id: str) -> str:
        """Siapkan_rencana_pelepasan_penahanan_draft_blast_denda.

        Args:
            blast_id: ID blast draft yang ingin dilepas penahanannya.
        """
        return toolkit.plan_command("release-blast", {"blast_id": blast_id})

    @tool
    def plan_set_roster_discord(nama: str, discord_id: str) -> str:
        """Siapkan_rencana_pengaturan_Discord_ID_panitia_di_roster.

        Args:
            nama: nama panitia sesuai roster.
            discord_id: ID Discord numerik (snowflake) atau mention.
        """
        return toolkit.plan_command("roster-set-discord", {"nama": nama, "discord_id": discord_id})

    @tool
    def plan_remove_roster_discord(nama: str) -> str:
        """Siapkan_rencana_penghapusan_Discord_ID_panitia_di_roster.

        Args:
            nama: nama panitia yang ingin dihapus Discord ID-nya.
        """
        return toolkit.plan_command("roster-remove-discord", {"nama": nama})

    return [
        search_knowledge,
        get_task_requirements,
        get_fines,
        get_sheet_detail,
        get_task_overview,
        get_angel_cards,
        get_resubmit_status,
        get_audit_log,
        calculate,
        remember_fact,
        recall_facts,
        forget_facts,
        get_member_history,
        get_validation_batches_history,
        store_cross_thread_fact,
        recall_cross_thread_facts,
        duckduckgo_web_search,
        scrape_web_page,
        inspect_instagram_post_url,
        mcp_read_sheet_range,
        mcp_get_spreadsheet_metadata,
        mcp_write_sheet_range,
        provider_status,
        plan_validate_sheet,
        plan_blast_draft,
        plan_add_angel_card,
        plan_remove_angel_card,
        plan_scan_blast,
        plan_hold_blast,
        plan_release_blast,
        plan_set_roster_discord,
        plan_remove_roster_discord,
    ]


def tool_names(tools: Sequence[Any]) -> list[str]:
    """Names of the bound tools (for logs and status output)."""
    return [str(getattr(tool, "name", "")) for tool in tools]


def make_tool_callable(
    tools: Sequence[Any], name: str
) -> Callable[..., str] | None:
    """Find a bound tool by name."""
    for tool in tools:
        if getattr(tool, "name", "") == name:
            return tool
    return None
