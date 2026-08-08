"""
Universal LLM Powered Web Scraper to JSON
==========================================
Skrip TUNGGAL, prosedural (bukan OOP), linear dari atas ke bawah.
Mengikuti 11 langkah "Alur Logika" di README.md secara presisi.

Cara pakai:
    pip install playwright google-genai beautifulsoup4
    playwright install chromium
    export GEMINI_API_KEY="xxxxx"   # atau isi saat diminta
    python scraper.py

Prinsip KISS yang dipakai:
- Tidak ada class. Semua fungsi berdiri sendiri, satu tugas per fungsi.
- Tidak ada layer/abstraksi tambahan (repository, service, dsb).
- exec() dipakai apa adanya untuk menjalankan kode hasil generate AI,
  tanpa sandbox rumit -- sesuai instruksi "gunakan exec() secara sederhana".
- Alur utama (main()) dibaca dari atas ke bawah persis seperti urutan
  langkah 1-11 di README, supaya mudah ditelusuri.
"""

import contextlib
import os
import re
import json
import time
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from google import genai

MODEL_NAME = "gemini-3.1-flash-lite"
MAX_SCROLL_STEPS = 15
SCROLL_WAIT_SECONDS = 1.0
HTML_CHAR_LIMIT_FOR_AI = 15000

# Selector umum untuk tombol "halaman berikutnya" (heuristik sederhana,
# dicoba satu per satu sampai ada yang cocok).
NEXT_PAGE_SELECTORS = [
    "a[rel='next']",
    "a[aria-label*='next' i]",
    "a[aria-label*='berikutnya' i]",
    "button[aria-label*='next' i]",
    "a:has-text('Next')",
    "a:has-text('next')",
    "a:has-text('Berikutnya')",
    "a:has-text('»')",
    "button:has-text('Next')",
    "button:has-text('Berikutnya')",
]


# ---------------------------------------------------------------------------
# Util umum
# ---------------------------------------------------------------------------
def clean_html(html, limit_chars=HTML_CHAR_LIMIT_FOR_AI):
    """Buang script/style/svg supaya hemat token saat dikirim ke AI."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "svg", "noscript", "iframe"]):
        tag.decompose()
    return str(soup)[:limit_chars]


def strip_code_fence(text):
    """Bersihkan ```python ... ``` atau ```json ... ``` jika AI ikut menyertakannya."""
    text = text.strip()
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"```\s*$", "", text)
    return text.strip()


# ---------------------------------------------------------------------------
# Langkah 4: scroll sampai konten habis
# ---------------------------------------------------------------------------
def auto_scroll(page, max_steps=MAX_SCROLL_STEPS, wait=SCROLL_WAIT_SECONDS):
    last_height = page.evaluate("document.body.scrollHeight")
    for _ in range(max_steps):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(wait)
        new_height = page.evaluate("document.body.scrollHeight")
        if new_height == last_height:
            break
        last_height = new_height


# ---------------------------------------------------------------------------
# Langkah 5: ambil link-link konten
# ---------------------------------------------------------------------------
def collect_content_links(page, base_url):
    hrefs = page.eval_on_selector_all(
        "a[href]", "els => els.map(e => e.getAttribute('href'))"
    )
    base_domain = urlparse(base_url).netloc

    seen = set()
    links = []
    for href in hrefs:
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
            continue
        full_url = urljoin(base_url, href)
        if urlparse(full_url).netloc != base_domain:
            continue
        if full_url in seen:
            continue
        seen.add(full_url)
        links.append(full_url)
    return links


def go_to_next_page(page):
    """Coba klik tombol next dengan beberapa selector umum. True jika berhasil."""
    for selector in NEXT_PAGE_SELECTORS:
        try:
            locator = page.locator(selector)
            if locator.count() > 0 and locator.first.is_visible():
                locator.first.click(timeout=5000)
                page.wait_for_load_state("networkidle", timeout=15000)
                return True
        except Exception:
            continue
    return False


# ---------------------------------------------------------------------------
# Langkah 6: AI Scout memindai konten pertama
# ---------------------------------------------------------------------------
def ai_scan_items(client, html_snippet):
    prompt = f"""Kamu adalah AI Scout untuk web scraping.
Berikut potongan HTML dari sebuah halaman konten:
---
{html_snippet}
---
Tugas: identifikasi item-item data yang kemungkinan ingin diambil user
(misal: judul, harga, tanggal, penulis, deskripsi, kategori, dst).
Jawab HANYA dengan daftar nama item dipisah koma, tanpa penjelasan apapun.
Contoh format jawaban: judul, harga, tanggal_terbit, penulis"""
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    text = strip_code_fence(response.text)
    items = [i.strip() for i in text.split(",") if i.strip()]
    return items


# ---------------------------------------------------------------------------
# Langkah 8: parsing pilihan user (digit rekomendasi ATAU nama sendiri)
# ---------------------------------------------------------------------------
def parse_user_choice(raw_input_text, recommended_items):
    chosen = []
    for part in [p.strip() for p in raw_input_text.split(",") if p.strip()]:
        if part.isdigit():
            idx = int(part) - 1
            if 0 <= idx < len(recommended_items):
                chosen.append(recommended_items[idx])
        else:
            chosen.append(part)

    # dedupe sambil pertahankan urutan
    seen = set()
    result = []
    for item in chosen:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


# ---------------------------------------------------------------------------
# Langkah 9: AI meracik fungsi ekstraksi Python murni
# ---------------------------------------------------------------------------
def ai_generate_extractor(client, html_snippet, target_items):
    items_str = ", ".join(target_items)
    prompt = f"""Kamu adalah generator kode Python ahli web scraping.
Berikut potongan HTML dari sebuah halaman konten:
---
{html_snippet}
---
Buatkan SATU fungsi Python murni bernama extract_data(html) yang:
- Menerima satu parameter `html` (string HTML mentah dari halaman sejenis).
- Menggunakan BeautifulSoup untuk parsing (BeautifulSoup sudah tersedia,
  JANGAN menulis baris import apapun).
- Mengembalikan dictionary dengan key PERSIS berikut: {items_str}
- Jika suatu data tidak ditemukan di html, isi valuenya dengan None,
  jangan sampai raise error.
- Bungkus seluruh isi fungsi dengan try/except agar tidak crash.
- HANYA balas kode python murni mulai dari 'def extract_data(html):'
  sampai selesai. Jangan ada penjelasan atau markdown code fence."""
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    return strip_code_fence(response.text)


# ---------------------------------------------------------------------------
# Self-heal: fallback AI untuk satu halaman yang gagal diekstrak lokal
# ---------------------------------------------------------------------------
def ai_fix_extraction(client, html_snippet, target_items):
    items_str = ", ".join(target_items)
    prompt = f"""Fungsi ekstraksi otomatis gagal pada halaman ini.
Baca HTML berikut dan ekstrak manual data untuk item: {items_str}
---
{html_snippet}
---
Jawab HANYA dengan JSON object valid, key sesuai nama item persis,
value berupa string atau null jika tidak ditemukan.
Jangan sertakan penjelasan atau markdown code fence."""
    response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
    text = strip_code_fence(response.text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return {item: None for item in target_items}



def safe_close(obj) -> None:
    with contextlib.suppress(Exception):
        obj.close()


# ---------------------------------------------------------------------------
# MAIN: mengikuti langkah 1-11 secara berurutan
# ---------------------------------------------------------------------------
def main():
    # --- Setup AI client ---
    api_key = os.environ.get("GEMINI_API_KEY") or input("Masukkan GEMINI_API_KEY: ").strip()
    client = genai.Client(api_key=api_key)

    # Langkah 1 & 2: user menjalankan program, sistem minta URL + batas halaman
    url = input("Masukkan URL target: ").strip()
    max_pages_raw = input("Batas maksimal halaman (kosongkan = unlimited): ").strip()
    max_pages = int(max_pages_raw) if max_pages_raw else float("inf")

    all_results = []

    with sync_playwright() as p, contextlib.ExitStack() as stack:
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        stack.callback(safe_close, browser)
    
        context = browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ))
        stack.callback(safe_close, context)
    
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        # Langkah 4: scroll sampai semua konten tampil
        auto_scroll(page)

        # Langkah 5: ambil link-link konten (navigasi "next" dicari saat loop nanti)
        content_links = collect_content_links(page, url)
        if not content_links:
            print("Tidak ditemukan link konten sama sekali. Program dihentikan.")
            browser.close()
            return

        # Langkah 6: AI membuka & memindai konten pertama
        detail_page = browser.new_page()
        detail_page.goto(content_links[0], timeout=60000)
        first_html = clean_html(detail_page.content())
        detail_page.close()

        print("\n[AI] Memindai halaman pertama untuk mengenali item...")
        recommended_items = ai_scan_items(client, first_html)

        # Langkah 7: sistem menampilkan rekomendasi
        print("\nItem yang direkomendasikan AI:")
        for i, item in enumerate(recommended_items, start=1):
            print(f"  {i}. {item}")

        # Langkah 8: user memilih item (digit rekomendasi atau nama sendiri)
        raw_choice = input(
            "\nMasukkan item yang ingin diambil "
            "(nomor dipisah koma, atau nama sendiri): "
        ).strip()
        target_items = parse_user_choice(raw_choice, recommended_items)
        if not target_items:
            print("Tidak ada item yang dipilih. Program dihentikan.")
            browser.close()
            return
        print(f"Item yang akan diambil: {', '.join(target_items)}")

        # Langkah 9: AI meracik fungsi ekstraksi data
        print("\n[AI] Meracik fungsi ekstraksi data...")
        extractor_code = ai_generate_extractor(client, first_html, target_items)

        exec_namespace = {"BeautifulSoup": BeautifulSoup}
        try:
            exec(extractor_code, exec_namespace)  # sengaja sederhana, sesuai spesifikasi
            extract_data = exec_namespace["extract_data"]
        except Exception as e:
            print(f"Gagal memuat fungsi ekstraksi dari AI: {e}")
            browser.close()
            return

        # Langkah 10: ulangi (buka konten -> ekstrak data) -> pindah halaman
        detail_page = browser.new_page()
        page_number = 0

        while page_number < max_pages:
            page_number += 1
            print(f"\n--- Memproses halaman listing ke-{page_number} "
                  f"({len(content_links)} konten) ---")

            for link in content_links:
                try:
                    detail_page.goto(link, timeout=60000)
                    html = detail_page.content()
                except Exception as e:
                    print(f"  [SKIP] Gagal membuka {link}: {e}")
                    continue

                # Coba ekstraksi lokal (hasil exec) dulu
                try:
                    data = extract_data(html)
                    if not isinstance(data, dict):
                        raise ValueError("hasil extract_data bukan dictionary")
                except Exception as e:
                    # Self-heal: fallback 1x panggil AI untuk halaman ini saja
                    print(f"  [WARN] Ekstraksi lokal gagal ({e}). Fallback ke AI...")
                    try:
                        data = ai_fix_extraction(client, clean_html(html), target_items)
                    except Exception as e2:
                        print(f"  [SKIP] Fallback AI juga gagal: {e2}")
                        continue

                data["_source_url"] = link
                all_results.append(data)
                print(f"  [OK] {link}")

            # Cari & klik tombol next di halaman listing
            if not go_to_next_page(page):
                print("\nTombol 'next' tidak ditemukan. Pagination selesai.")
                break

            auto_scroll(page)
            content_links = collect_content_links(page, url)
            if not content_links:
                print("Tidak ada konten baru di halaman berikutnya. Berhenti.")
                break

        detail_page.close()

        # Langkah 11: tutup browser
        browser.close()

    # Langkah 11 (lanjutan): simpan output.json
    with open("output.json", "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2)

    print(f"\nSelesai. {len(all_results)} data tersimpan ke output.json")


if __name__ == "__main__":
    main()