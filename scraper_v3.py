"""
Web Scraper LLM to JSON — Versi Lite (Dengan AI Scout & Batas Halaman)
"""

import json
import os
import re
import time
from typing import Any, Callable, TypedDict
from urllib.parse import urljoin
from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

# ==================== KONFIGURASI ====================
API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL_NAME = "gemini-3.1-flash-lite"
MAX_RETRY = 3
SCROLL_JEDA_MS = 1500
TAGS_DIBUANG = ["script", "style", "svg", "noscript", "aside", "header", "iframe", "footer"]
client = genai.Client(api_key=API_KEY)

# ==================== CETAKAN DATA ====================
class Item(TypedDict, total=False):
    title: str
    link: str

class HasilDaftar(TypedDict):
    next_page_link: str | None
    next_page_selector: str | None
    items: list[Item]

Extractor = Callable[[str], dict[str, Any]]

# ==================== FUNGSI UTILITAS ====================
def bersihkan_html(html_mentah: str) -> str:
    soup = BeautifulSoup(html_mentah, "html.parser")
    for tag in soup(TAGS_DIBUANG):
        tag.decompose()
    return soup.body.prettify() if soup.body else soup.prettify()

def panggil_ai_json(prompt: str, skema: types.Schema) -> dict | list:
    konfig = types.GenerateContentConfig(response_mime_type="application/json", temperature=0, response_schema=skema)
    for percobaan in range(1, MAX_RETRY + 1):
        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt, config=konfig)
            teks = response.text.strip()
            if teks.startswith("```"):
                teks = teks.split("```")[1].replace("json", "", 1).strip()
            return json.loads(teks)
        except Exception as e:
            print(f"[AI Error] Gagal memanggil AI: {e}. Coba lagi ({percobaan}/{MAX_RETRY})...")
            time.sleep(3)
    return {}

# ==================== AGEN AI (SCOUT & EXTRACT) ====================
def ai_ekstrak_daftar(html_bersih: str) -> HasilDaftar:
    print("[AI 1] Mencari daftar item & tombol Next...")
    skema = types.Schema(
        type=types.Type.OBJECT,
        properties={
            "next_page_link": types.Schema(type=types.Type.STRING),
            "next_page_selector": types.Schema(type=types.Type.STRING),
            "items": types.Schema(
                type=types.Type.ARRAY,
                items=types.Schema(
                    type=types.Type.OBJECT,
                    properties={"title": types.Schema(type=types.Type.STRING), "link": types.Schema(type=types.Type.STRING)},
                    required=["link"]
                )
            )
        }, required=["items"]
    )
    prompt = f"Extract list entries (title, link) and pagination targets (next page link/selector) from this HTML.\n\n[HTML]:\n{html_bersih}"
    hasil = panggil_ai_json(prompt, skema)
    if isinstance(hasil, dict):
        return {
            "next_page_link": hasil.get("next_page_link"),
            "next_page_selector": hasil.get("next_page_selector"),
            "items": [item for item in hasil.get("items", []) if isinstance(item, dict) and item.get("link")]
        }
    return {"next_page_link": None, "next_page_selector": None, "items": []}

def ai_scout_kolom(html_bersih: str) -> list[str]:
    """Mengirim AI untuk meraba-raba data apa saja yang tersedia di halaman web."""
    print("[AI Scout] Memindai struktur web untuk mencari rekomendasi data...")
    skema = types.Schema(
        type=types.Type.OBJECT,
        properties={"recommendations": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING))},
        required=["recommendations"]
    )
    prompt = f"Identify the category names of the data fields available in this HTML (e.g. Title, Author, Price). Return JSON.\n\n[HTML]:\n{html_bersih}"
    hasil = panggil_ai_json(prompt, skema)
    if isinstance(hasil, dict):
        return hasil.get("recommendations", [])
    return []

def ai_buat_extractor(html_sample: str, kolom: list[str]) -> Extractor | None:
    print("[AI Extractor] Membuat alat pengeruk lokal untuk kolom:", kolom)
    prompt = (
        f"Write a single Python function named `extract_data(html: str) -> dict:` to extract these fields: {kolom}.\n"
        f"Use BeautifulSoup and re. Wrap each field in try-except. Return ONLY raw python code.\n\n[HTML]:\n{html_sample}"
    )
    konfig = types.GenerateContentConfig(temperature=0)
    try:
        kode = client.models.generate_content(model=MODEL_NAME, contents=prompt, config=konfig).text.strip()
        if kode.startswith("```"):
            kode = kode.split("```")[1].replace("python", "", 1).strip()
        
        namespace: dict[str, Any] = {}
        exec(kode, {"BeautifulSoup": BeautifulSoup, "re": re}, namespace)
        
        fn = namespace.get("extract_data")
        if callable(fn) and isinstance(fn(html_sample), dict):
            print("[AI Extractor] Alat pengeruk berhasil dibuat!")
            return fn
    except Exception as e:
        print(f"[AI Extractor] Gagal membuat fungsi: {e}")
    return None

# ==================== PLAYWRIGHT (NAVIGASI) ====================
def gulir_sampai_mentok(page):
    tinggi_awal = 0
    for _ in range(10):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        try:
            page.wait_for_load_state("networkidle", timeout=SCROLL_JEDA_MS)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(SCROLL_JEDA_MS)
        
        tinggi_baru = page.evaluate("document.body.scrollHeight")
        if tinggi_baru == tinggi_awal:
            break
        tinggi_awal = tinggi_baru

def pindah_halaman(page, link_ai, selector_ai) -> bool:
    try:
        if link_ai and link_ai.startswith("http"):
            page.goto(link_ai, wait_until="domcontentloaded", timeout=15000)
            return True
        if selector_ai and page.locator(selector_ai).count() > 0:
            page.locator(selector_ai).first.click()
            page.wait_for_timeout(3000)
            return True
    except Exception:
        pass
    print("[INFO] Tombol Next tidak ditemukan. Selesai.")
    return False

# ==================== FUNGSI UTAMA ====================
def main():
    print("=== UNIVERSAL WEB SCRAPER (LITE + SCOUT) ===")
    url_target = input("Masukkan URL website: ").strip()
    if not url_target: return
    
    # KEMBALINYA FITUR 1: Input Batas Halaman
    max_in = input("Batas maksimal halaman (Angka / Kosongkan jika tanpa batas): ").strip()
    max_halaman = int(max_in) if max_in.isdigit() else None
    
    hasil_akhir = []
    
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
        context = browser.new_context()
        page_utama = context.new_page()
        
        try:
            page_utama.goto(url_target, wait_until="domcontentloaded")
            halaman_ke = 1
            extractor_lokal = None
            kolom_target = []
            
            while True:
                # KEMBALINYA FITUR 1: Eksekusi Batas Halaman (Logika Short-Circuit 'and')
                if max_halaman and halaman_ke > max_halaman:
                    print(f"\n[INFO] Batas halaman ({max_halaman}) tercapai.")
                    break

                print(f"\n--- MEMBACA HALAMAN {halaman_ke} ---")
                gulir_sampai_mentok(page_utama)
                html_utama = bersihkan_html(page_utama.content())
                
                daftar = ai_ekstrak_daftar(html_utama)
                if not daftar["items"]:
                    break
                
                # KEMBALINYA FITUR 2: AI Scout & Pemilihan Kolom
                if extractor_lokal is None:
                    print("\n[Sistem] Mengirim Agen Pengintai (Scout) ke item pertama...")
                    page_scout = context.new_page()
                    page_scout.goto(urljoin(page_utama.url, daftar["items"][0]["link"]))
                    html_scout = bersihkan_html(page_scout.content())
                    
                    rekomendasi = ai_scout_kolom(html_scout)
                    print("\n" + "=" * 50)
                    if rekomendasi:
                        print("AI SCOUT MENEMUKAN DATA BERIKUT DI WEBSITE INI:")
                        for i, rek in enumerate(rekomendasi, 1):
                            print(f"   [{i}] {rek}")
                        print("=" * 50 + "\nKetik NOMOR atau NAMA DATA bebas (pisahkan dengan koma).")
                        print("Kosongkan lalu tekan Enter untuk mengambil semua rekomendasi di atas.")
                    else:
                        print("AI SCOUT GAGAL MENDETEKSI DATA OTOMATIS.")
                        print("=" * 50 + "\nKetik MANUAL nama data yang ingin kamu ambil.")

                    pilihan = input("> ").strip()
                    if not pilihan:
                        kolom_target = rekomendasi or ["Judul", "Isi"]
                    else:
                        for p in pilihan.split(","):
                            p = p.strip()
                            if p.isdigit() and rekomendasi and 0 <= int(p) - 1 < len(rekomendasi):
                                kolom_target.append(rekomendasi[int(p) - 1])
                            elif p:
                                kolom_target.append(p)
                    
                    extractor_lokal = ai_buat_extractor(html_scout, kolom_target)
                    page_scout.close()
                
                # Proses Item
                for idx, item in enumerate(daftar["items"], 1):
                    print(f"[{idx}] Memproses: {item.get('title')}")
                    if not extractor_lokal or not item.get("link"): continue
                    
                    page_item = context.new_page()
                    try:
                        page_item.goto(urljoin(page_utama.url, item["link"]), timeout=15000)
                        data_ekstrak = extractor_lokal(bersihkan_html(page_item.content()))
                        if data_ekstrak:
                            data_ekstrak["Main_Link"] = item["link"]
                            hasil_akhir.append(data_ekstrak)
                    except Exception as e:
                        print(f"  -> Gagal memuat item: {e}")
                    finally:
                        page_item.close()
                
                if not pindah_halaman(page_utama, daftar["next_page_link"], daftar["next_page_selector"]):
                    break
                halaman_ke += 1
                
        finally:
            with open("output_lite.json", "w", encoding="utf-8") as f:
                json.dump(hasil_akhir, f, indent=4, ensure_ascii=False)
            print("\n=== SELESAI! Data tersimpan di output_lite.json ===")
            browser.close()

if __name__ == "__main__":
    main()