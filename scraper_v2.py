"""
Universal LLM Powered Web Scraper to JSON — v2 (KISS refactor)

Alur tetap sama seperti v1:
URL -> buka browser -> scroll -> AI baca daftar item & next-page
-> AI scout kolom (di item pertama) -> AI buat extractor lokal
-> loop (buka item -> ekstrak (lokal/self-heal/AI) -> next page) -> output.json
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import signal
import textwrap
import time
from collections.abc import Callable
from typing import Any, TypedDict
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from google import genai
from google.genai import types
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

# ============================================================== KONFIG ===

API_KEY = os.environ.get("GEMINI_API_KEY", "")
MODEL_NAME = "gemini-3.1-flash-lite"

CHUNK_SIZE = 100_000
DELAY_SCOUT = 5
DELAY_DAFTAR = 5
DELAY_BACA = 10

MAX_RETRY_AI = 5
EXTRACTOR_TIMEOUT = 5.0

FILE = "output.json"
FOLDER = "Output"
TIDAK_DITEMUKAN = "Tidak Ditemukan"

SCROLL_MAX_PUTARAN = 20
SCROLL_MAX_DETIK = 60.0
SCROLL_BATAS_STAGNAN = 3
SCROLL_JEDA_MS = 1500

TAGS_DIBUANG = ["script", "style", "svg", "noscript", "aside",
                "header", "iframe", "form", "meta", "footer"]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

NAMA_FUNGSI_EXTRACTOR = "extract_data"
IMPORT_DIIZINKAN = {"bs4", "re", "json"}
NAMA_TERLARANG = {
    "eval", "exec", "open", "input", "os", "sys", "subprocess", "socket",
    "shutil", "requests", "urllib", "pathlib", "importlib", "ctypes",
    "multiprocessing", "globals", "locals", "vars", "getattr", "setattr",
    "delattr", "breakpoint", "exit", "quit", "__import__",
}

BUILTINS_AMAN = {
    "len": len, "str": str, "int": int, "float": float, "bool": bool,
    "list": list, "dict": dict, "tuple": tuple, "set": set,
    "isinstance": isinstance, "range": range, "enumerate": enumerate,
    "min": min, "max": max, "sorted": sorted, "sum": sum, "abs": abs,
    "zip": zip, "map": map, "filter": filter, "reversed": reversed,
    "None": None, "True": True, "False": False,
    "Exception": Exception, "ValueError": ValueError,
    "AttributeError": AttributeError, "KeyError": KeyError,
    "TypeError": TypeError, "IndexError": IndexError, "StopIteration": StopIteration,
}

HEURISTIK_NEXT_SELECTORS = [
    ".rc-pagination-next", "li.next", "a.next", "button.next",
    "[aria-label*='Next' i]", "[aria-label*='Selanjutnya' i]",
    "a:has-text('Next')", "a:has-text('Selanjutnya')",
]

client = genai.Client(api_key=API_KEY)
KONFIG_JSON = types.GenerateContentConfig(response_mime_type="application/json", temperature=0)


class KodeTidakAmanError(Exception):
    pass


class Item(TypedDict, total=False):
    title: str
    link: str


class HasilDaftar(TypedDict):
    next_page_link: str | None
    next_page_selector: str | None
    items: list[Item]


Extractor = Callable[[str], dict[str, Any]]


# =================================================== KEAMANAN KODE AI ===

def cek_kode_aman(kode: str) -> None:
    try:
        pohon = ast.parse(kode, mode="exec")
    except SyntaxError as e:
        raise KodeTidakAmanError(f"Kode tidak valid secara sintaks: {e}") from e

    if not any(isinstance(n, ast.FunctionDef) and n.name == NAMA_FUNGSI_EXTRACTOR for n in pohon.body):
        raise KodeTidakAmanError(f"Fungsi `{NAMA_FUNGSI_EXTRACTOR}` tidak ditemukan di kode.")

    for node in ast.walk(pohon):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            akar = (getattr(node, "module", None) or node.names[0].name).split(".")[0]
            if akar not in IMPORT_DIIZINKAN:
                raise KodeTidakAmanError(f"Import tidak diizinkan: {akar}")
        elif isinstance(node, (ast.Name, ast.Attribute)):
            nama = node.id if isinstance(node, ast.Name) else node.attr
            if nama in NAMA_TERLARANG or (nama.startswith("__") and nama.endswith("__")):
                raise KodeTidakAmanError(f"Referensi tidak diizinkan: {nama}")


@contextlib.contextmanager
def batas_waktu(detik: float):
    if not hasattr(signal, "SIGALRM"):
        yield
        return

    def _handler(signum, frame):
        raise TimeoutError(f"Eksekusi melebihi {detik}s")

    penangan_lama = signal.signal(signal.SIGALRM, _handler)
    signal.setitimer(signal.ITIMER_REAL, detik)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, penangan_lama)

# ========================================================== UTIL HTML/JSON ===

def bersihkan_html(html_mentah: str) -> str:
    soup = BeautifulSoup(html_mentah, "html.parser")
    for tag in soup(TAGS_DIBUANG):
        tag.decompose()
    return soup.body.prettify() if soup.body else soup.prettify()


def bersihkan_teks(data_dict: dict[str, Any]) -> dict[str, Any]:
    hasil = {}
    for kunci, nilai in data_dict.items():
        if isinstance(nilai, str):
            rata = re.sub(r"[\n\r\t]+", " ", nilai)
            rata = re.sub(r"\s{2,}", " ", rata)
            hasil[kunci] = rata.strip()
        else:
            hasil[kunci] = nilai
    return hasil


def parse_json_ai(mentah: str) -> Any:
    """Bersihkan fence markdown (```json ... ```) lalu parse JSON, dengan
    fallback mengambil blok {...}/[...] pertama jika parse langsung gagal."""
    teks = mentah.strip()
    if teks.startswith("```"):
        bagian = teks.split("```")
        teks = bagian[1] if len(bagian) > 1 else teks
        for prefiks in ("python", "json"):
            if teks.startswith(prefiks):
                teks = teks[len(prefiks):]
    teks = teks.strip()

    try:
        return json.loads(teks, strict=False)
    except json.JSONDecodeError:
        cocok = re.search(r"\{.*\}|\[.*\]", teks, re.DOTALL)
        if not cocok:
            raise
        return json.loads(cocok.group(0), strict=False)


def tulis_json_atomik(data: Any, path: str) -> None:
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)
    os.replace(tmp, path)


def tutup_aman(objek) -> None:
    with contextlib.suppress(Exception):
        objek.close()

aaaaaaaaaaaa
# ================================================================ AI CALL ===

def panggil_ai(prompt: str, config: types.GenerateContentConfig = KONFIG_JSON) -> str:
    kesalahan_terakhir: Exception | None = None
    for percobaan in range(1, MAX_RETRY_AI + 1):
        try:
            response = client.models.generate_content(model=MODEL_NAME, contents=prompt, config=config)
            if not response.text:
                raise ValueError("Respons AI kosong (terblokir safety filter).")
            return response.text
        except Exception as e:
            kesalahan_terakhir = e
            pesan = str(e).lower()
            if any(k in pesan for k in ("429", "quota", "exhausted")):
                tunggu = 60.0
            elif any(k in pesan for k in ("timeout", "deadline", "unavailable", "500", "503")):
                tunggu = 2.0 * (2 ** (percobaan - 1))
            else:
                raise
            print(f"[AI] {e}. Tunggu {tunggu:.0f}s lalu ulangi ({percobaan}/{MAX_RETRY_AI})...")
            time.sleep(tunggu)

    assert kesalahan_terakhir is not None
    raise kesalahan_terakhir


def skema_string_fields(kolom: list[str]) -> types.GenerateContentConfig:
    properti = {k: types.Schema(type=types.Type.STRING) for k in kolom}
    return types.GenerateContentConfig(
        response_mime_type="application/json", temperature=0,
        response_schema=types.Schema(type=types.Type.OBJECT, properties=properti),
    )


# ================================================== AI: SCOUT & DAFTAR ITEM ===

def ai_scout_kolom(html_bersih: str) -> list[str]:
    skema = types.Schema(
        type=types.Type.OBJECT,
        properties={"recommendations": types.Schema(type=types.Type.ARRAY, items=types.Schema(type=types.Type.STRING))},
        required=["recommendations"],
    )
    konfig = types.GenerateContentConfig(response_mime_type="application/json", temperature=0, response_schema=skema)
    prompt = (
        "You are a Web Structure Analyst. Identify ONLY the category names of "
        "the data fields available in this HTML (e.g. Title, Author, Date, "
        "Price). Return JSON exactly as:\n"
        '{"recommendations": ["Field Name 1", "Field Name 2"]}\n\n'
        f"[HTML]:\n{html_bersih}"
    )
    try:
        hasil = parse_json_ai(panggil_ai(prompt, konfig))
        mentah = hasil.get("recommendations", []) if isinstance(hasil, dict) else []
        return [str(r).strip() for r in mentah if isinstance(r, (str, int, float)) and str(r).strip()]
    except Exception as e:
        print(f"[ERROR Scout]: {e}")
        return []


def ai_ekstrak_daftar(html_bersih: str) -> HasilDaftar:
    print("[AI 1] Mencari daftar item & mendeteksi tombol Next...")
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
                    required=["link"],
                ),
            ),
        },
        required=["items"],
    )
    konfig = types.GenerateContentConfig(response_mime_type="application/json", temperature=0, response_schema=skema)
    prompt = (
        "You are a web structure extraction expert. From this HTML listing page, extract:\n"
        "1. Every list entry: title and link.\n"
        "2. Pagination target: Look CAREFULLY for the 'Next Page' button or link. "
        "It often uses classes like 'next', 'pagination-next', 'rc-pagination-next', 'fa-chevron-right', "
        "or attributes like aria-label='Next'. Prefer the href attribute. If it is a JS/React button with "
        "no real href, you MUST provide a precise CSS selector. ONLY leave this blank if pagination is "
        "completely missing or disabled.\n"
        "Return strict JSON matching the schema only.\n\n"
        f"[HTML]:\n{html_bersih}"
    )
    default: HasilDaftar = {"next_page_link": None, "next_page_selector": None, "items": []}
    try:
        hasil = parse_json_ai(panggil_ai(prompt, konfig))
        if not isinstance(hasil, dict):
            raise ValueError("Respons AI 1 bukan objek JSON.")

        mentah = hasil.get("items", [])
        item_valid: list[Item] = [
            {"title": str(b.get("title") or "").strip(), "link": str(b["link"]).strip()}
            for b in (mentah if isinstance(mentah, list) else [])
            if isinstance(b, dict) and b.get("link")
        ]
        return {
            "next_page_link": hasil.get("next_page_link") or None,
            "next_page_selector": hasil.get("next_page_selector") or None,
            "items": item_valid,
        }
    except Exception as e:
        print(f"[ERROR AI 1]: {e}")
        return default


def ai_baca_konten(html_bersih: str, kolom: list[str]) -> dict[str, Any]:
    """Fallback termahal: minta AI membaca isi halaman langsung (per-chunk
    untuk halaman raksasa), dipakai hanya kalau extractor lokal gagal total."""
    hasil_gabungan: dict[str, Any] = {k: TIDAK_DITEMUKAN for k in kolom}
    konfig = skema_string_fields(kolom)
    chunks = [html_bersih[i:i + CHUNK_SIZE] for i in range(0, len(html_bersih), CHUNK_SIZE)] or [""]
    if len(chunks) > 1:
        print(f"[AI 2] Dokumen besar terdeteksi, dibagi jadi {len(chunks)} bagian...")

    dasar = (
        "You are a data extraction engine. Extract the requested fields from "
        f"this HTML fragment. If a field is not present, set its value to "
        f"'{TIDAK_DITEMUKAN}' exactly - never guess or invent a value."
    )
    for idx, chunk in enumerate(chunks, 1):
        print(f"-> Menganalisis bagian {idx} dari {len(chunks)}...")
        try:
            time.sleep(DELAY_BACA)
            data = parse_json_ai(panggil_ai(f"{dasar}\n\n[HTML FRAGMENT]:\n{chunk}", konfig))
            if not isinstance(data, dict):
                continue
            for kolom_nama, nilai in data.items():
                if kolom_nama not in hasil_gabungan:
                    continue
                nilai_str = str(nilai).strip() if nilai is not None else ""
                if nilai_str and nilai_str != TIDAK_DITEMUKAN and hasil_gabungan[kolom_nama] == TIDAK_DITEMUKAN:
                    hasil_gabungan[kolom_nama] = nilai_str
        except Exception as e:
            print(f"Gagal menganalisis bagian {idx}: {e}")

    return hasil_gabungan


# ============================================== EXTRACTOR GENERATOR (SELF-HEAL) ===

def _prompt_extractor(html_sample: str, kolom: list[str]) -> str:
    skema_json = json.dumps({k: "string" for k in kolom}, indent=2, ensure_ascii=False)
    return textwrap.dedent(f"""
        You are a Senior Web Scraping Engineer. Write a single Python function
        to extract data from the HTML structure provided.

        CRITICAL INSTRUCTIONS TO PREVENT NULL VALUES:
        1. For texts like "Abstraksi", "Abstract", "Deskripsi", or Metadata (ISBN, Date), DO NOT rely on CSS classes as they change often.
        2. USE TEXT-BASED REGEX SEARCH to find the label/title, then extract the content next to it.
           Website structures usually use Tables (<th>Label</th><td>Value</td>) or Divs.
           USE THIS EXACT APPROACH for tricky fields:
           ```python
           try:
               label_tag = soup.find(string=re.compile(r'Abstraksi|Abstract|Deskripsi', re.IGNORECASE))
               if label_tag:
                   parent = label_tag.parent
                   sibling = parent.find_next_sibling()
                   if sibling:
                       result['Abstraksi'] = sibling.get_text(separator=' ', strip=True)
                   else:
                       next_el = parent.find_next(['p', 'div', 'td', 'span'])
                       if next_el:
                           result['Abstraksi'] = next_el.get_text(separator=' ', strip=True)
           except Exception:
               pass
           ```
        3. Wrap EVERY field extraction in its own `try/except Exception: pass`.
        4. Output ONLY raw python code. No markdown fences.
        5. Define exactly: `def {NAMA_FUNGSI_EXTRACTOR}(html: str) -> dict:`
        6. Return a dict with exactly these keys: {list(kolom)}
        7. `BeautifulSoup` and `re` are already imported. Do not import anything else.

        [TARGET SCHEMA]:
        {skema_json}

        [SAMPLE HTML]:
        {html_sample}
    """).strip()


def ai_buat_extractor(html_sample: str, kolom: list[str]) -> Extractor | None:
    """Minta AI menulis fungsi ekstraksi Python, jalankan di sandbox terbatas,
    lalu smoke-test sebelum dipakai untuk item-item berikutnya (gratis)."""
    print("[Extractor Generator] Meminta AI menulis fungsi ekstraksi lokal...")
    try:
        prompt = _prompt_extractor(html_sample, kolom)
        kode_mentah = panggil_ai(prompt, types.GenerateContentConfig(temperature=0))
        kode = kode_mentah.strip()
        if kode.startswith("```"):
            bagian = kode.split("```")
            kode = bagian[1] if len(bagian) > 1 else kode
            if kode.startswith("python"):
                kode = kode[len("python"):]
        kode = kode.strip()

        cek_kode_aman(kode)

        sandbox_globals: dict[str, Any] = {
            "BeautifulSoup": BeautifulSoup, "re": re, "__builtins__": dict(BUILTINS_AMAN),
        }
        namespace: dict[str, Any] = {}
        exec(compile(kode, "<extractor_ai>", "exec"), sandbox_globals, namespace)

        fn = namespace.get(NAMA_FUNGSI_EXTRACTOR)
        if not callable(fn):
            raise KodeTidakAmanError(f"AI tidak mendefinisikan fungsi `{NAMA_FUNGSI_EXTRACTOR}`.")

        with batas_waktu(EXTRACTOR_TIMEOUT):
            hasil_tes = fn(html_sample)
        if not isinstance(hasil_tes, dict) or not all(k in hasil_tes for k in kolom):
            raise KodeTidakAmanError("Extractor tidak mengembalikan semua kolom yang diminta.")
        terisi = [v for v in hasil_tes.values() if v not in (None, "", [])]
        if len(terisi) < len(kolom) * 0.5:
            raise KodeTidakAmanError(f"Extractor hanya menemukan {len(terisi)} dari {len(kolom)} data.")

        print("[Extractor Generator] Berhasil! Item selanjutnya diekstrak GRATIS (tanpa AI).")
        return fn
    except Exception as e:
        print(f"[Extractor Generator] Gagal membuat alat pengeruk lokal ({e}).")
        return None


def coba_ekstrak_lokal(extractor: Extractor | None, html: str, kolom: list[str]) -> dict[str, Any] | None:
    if extractor is None:
        return None
    try:
        with batas_waktu(EXTRACTOR_TIMEOUT):
            hasil = extractor(html)
        if not isinstance(hasil, dict) or not all(k in hasil for k in kolom):
            return None
        if not any(v not in (None, "", []) for v in hasil.values()):
            return None
        return hasil
    except Exception:
        return None


def ekstrak_dengan_self_heal(html: str, kolom: list[str], extractor: Extractor | None) -> tuple[dict[str, Any] | None, Extractor | None]:
    hasil = coba_ekstrak_lokal(extractor, html, kolom)
    if hasil is not None:
        print("[Extractor Lokal] Diekstrak tanpa panggil AI (gratis).")
        return hasil, extractor

    for percobaan in range(1, 4):
        print(f"[Self-Healing] Extractor usang/rusak. Regenerasi... ({percobaan}/3)")
        baru = ai_buat_extractor(html, kolom)
        hasil = coba_ekstrak_lokal(baru, html, kolom)
        if hasil is not None:
            print("[Self-Healing] Extractor baru berhasil, akan dipakai seterusnya.")
            return hasil, baru
        print("[Self-Healing] Extractor baru gagal membaca teks.")

    print("[Fallback] Self-healing gagal. Menggunakan AI Pembaca Isi (berbayar)...")
    return ai_baca_konten(html, kolom), extractor


# ==================================================== PLAYWRIGHT: SCROLL & NAV ===

def gulir_sampai_mentok(page) -> None:
    mulai = time.monotonic()
    tinggi_terakhir = page.evaluate("document.body.scrollHeight")
    stagnan = 0
    putaran = 0

    while True:
        putaran += 1
        if putaran > SCROLL_MAX_PUTARAN or (time.monotonic() - mulai >= SCROLL_MAX_DETIK):
            break

        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        try:
            page.wait_for_load_state("networkidle", timeout=SCROLL_JEDA_MS)
        except PlaywrightTimeoutError:
            page.wait_for_timeout(SCROLL_JEDA_MS)

        tinggi_baru = page.evaluate("document.body.scrollHeight")
        if tinggi_baru == tinggi_terakhir:
            stagnan += 1
            if stagnan >= SCROLL_BATAS_STAGNAN:
                break
        else:
            stagnan = 0
            tinggi_terakhir = tinggi_baru


def _klik_selector(page, selector: str) -> bool:
    try:
        tombol = page.locator(selector).first
        if tombol.count() == 0:
            return False
        kelas = tombol.get_attribute("class") or ""
        if ("disabled" in kelas.lower()
                or tombol.get_attribute("aria-disabled") == "true"
                or tombol.get_attribute("disabled") is not None):
            return False
        tombol.scroll_into_view_if_needed()
        tombol.evaluate("node => node.click()")
        page.wait_for_timeout(5000)
        return True
    except Exception:
        return False


def _goto_link(page, link: str) -> bool:
    try:
        page.goto(urljoin(page.url, link), wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)
        return True
    except Exception:
        return False


def pindah_halaman(page, link_ai: str | None, selector_ai: str | None) -> bool:
    """Coba, berurutan: link dari AI -> selector dari AI -> heuristik lokal.
    Tidak ada nesting try/except bersarang; tiap strategi = satu fungsi kecil."""
    if link_ai and link_ai.startswith((".", "#")):
        print(f"[INFO] Koreksi: '{link_ai}' terlihat seperti selector, bukan link.")
        selector_ai, link_ai = link_ai, None

    if link_ai and _goto_link(page, link_ai):
        print(f"[INFO] Navigasi via link AI -> {link_ai}")
        return True

    if selector_ai and _klik_selector(page, selector_ai):
        print(f"[INFO] Navigasi via selector AI -> {selector_ai}")
        return True

    print("[INFO] Navigasi AI gagal/kosong. Memulai pemindaian heuristik lokal...")
    for sel in HEURISTIK_NEXT_SELECTORS:
        if _klik_selector(page, sel):
            print(f"[INFO] Heuristik lokal menemukan dan mengklik tombol: {sel}")
            return True

    print("[INFO] Tombol Next tidak ditemukan atau sudah mentok. Crawling selesai.")
    return False


# ========================================================= SCOUT & ITEM ===

def jalankan_scout(context, page_utama, link_item_pertama: str) -> tuple[list[str], Extractor | None]:
    print("\n[Sistem] Mengirim Agen Pengintai (Scout) ke item pertama...")
    page_scout = None
    try:
        page_scout = context.new_page()
        page_scout.goto(urljoin(page_utama.url, link_item_pertama), wait_until="domcontentloaded", timeout=20000)
        html_scout = bersihkan_html(page_scout.content())
        time.sleep(DELAY_SCOUT)

        rekomendasi = ai_scout_kolom(html_scout)
        print("\n" + "=" * 50)
        if rekomendasi:
            print("AI SCOUT FOUND THESE DATA POINTS ON THE WEBSITE:")
            for i, rek in enumerate(rekomendasi, 1):
                print(f"   [{i}] {rek}")
            print("=" * 50 + "\n\nKetik NOMOR atau NAMA DATA bebas (pisahkan dengan koma).")
        else:
            print("AI SCOUT GAGAL MENDETEKSI DATA OTOMATIS.")
            print("=" * 50 + "\n\nTolong ketik MANUAL nama data yang ingin kamu ambil dari web ini.")

        pilihan = input("> ").strip()
        kolom_terpilih: list[str] = []
        if not pilihan:
            kolom_terpilih = rekomendasi or ["Data 1", "Data 2", "Data 3"]
        else:
            for p in pilihan.split(","):
                p = p.strip()
                if p.isdigit() and rekomendasi and 0 <= int(p) - 1 < len(rekomendasi):
                    kolom_terpilih.append(rekomendasi[int(p) - 1])
                elif p:
                    kolom_terpilih.append(p)

        print(f"[INFO] AI akan mengekstrak format ini:\n"
              f"{json.dumps({k: '...' for k in kolom_terpilih}, indent=2, ensure_ascii=False)}")

        extractor = ai_buat_extractor(html_scout, kolom_terpilih)
        return kolom_terpilih, extractor
    except Exception as e:
        print(f"Scout gagal total: {e}")
        return ["Data Utama", "Isi"], None
    finally:
        if page_scout:
            tutup_aman(page_scout)


def proses_satu_item(context, page_utama, item: Item, kolom: list[str],
                      extractor: Extractor | None) -> tuple[dict[str, Any] | None, Extractor | None]:
    title = item.get("title") or "(tanpa judul)"
    link = item.get("link")
    if not link:
        return None, extractor

    url = urljoin(page_utama.url, link)
    print(f"Tab Baru -> {title[:50]}...")

    page = None
    try:
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=20000)
        page.wait_for_timeout(1000)
        html = bersihkan_html(page.content())

        isi, extractor = ekstrak_dengan_self_heal(html, kolom, extractor)
        if not isinstance(isi, dict):
            return None, extractor

        return {"Main_Link": url, **bersihkan_teks(isi)}, extractor
    except Exception as e:
        print(f"Gagal memuat item: {e}")
        return None, extractor
    finally:
        if page:
            tutup_aman(page)


# ============================================================ PENYIMPANAN ===

def simpan_per_item(hasil_halaman: list[dict[str, Any]], folder: str, urutan_awal: int) -> int:
    counter = urutan_awal
    for data in hasil_halaman:
        counter += 1
        tulis_json_atomik(data, os.path.join(folder, f"item_{counter:04d}.json"))
    print(f"[INFO] {len(hasil_halaman)} item berhasil disimpan di folder '{folder}/'.")
    return counter


# ================================================================== MAIN ===

def main() -> None:
    print("\n" + "=" * 21 + "\nUNIVERSAL WEB SCRAPER\n" + "=" * 21)

    if not API_KEY:
        print("\n[FATAL] API_KEY kosong.")
        return

    url_target = input("\nMasukkan URL website : \n> ").strip()
    if not url_target:
        return
    max_in = input("\nBatas maksimal halaman (Angka / Kosongkan jika tanpa batas): \n> ").strip()
    max_halaman = int(max_in) if max_in.isdigit() else None

    print("\nMode penyimpanan hasil:\n[1] Satu file JSON tunggal (single file)\n[2] Satu file JSON per item (separate files)")
    pilihan = input("> ").strip()
    if pilihan == "2":
        mode_simpan = "per_item"
        tujuan_simpan = f"{FOLDER}_{int(time.time())}"
        os.makedirs(tujuan_simpan, exist_ok=True)
    else:
        mode_simpan = "satu_file"
        tujuan_simpan = FILE

    print("\n" + "=" * 50 + "\nMEMULAI PROSES CRAWLING...\n" + "=" * 50 + "\n")

    waktu_mulai = time.monotonic()
    hasil_akhir: list[dict[str, Any]] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--disable-blink-features=AutomationControlled"])
            with contextlib.ExitStack() as stack:
                stack.callback(tutup_aman, browser)
                context = browser.new_context(user_agent=USER_AGENT)
                stack.callback(tutup_aman, context)

                page_utama = context.new_page()
                page_utama.goto(url_target, wait_until="domcontentloaded", timeout=30000)
                page_utama.wait_for_timeout(3000)

                halaman_ke = 1
                title_sebelumnya = ""
                kolom: list[str] = []
                extractor: Extractor | None = None
                scout = False
                total_item = 0

                while True:
                    if max_halaman and halaman_ke > max_halaman:
                        print(f"\n[INFO] Batas halaman ({max_halaman}) tercapai.")
                        break

                    print(f"\n=== FASE 1: MEMBACA HALAMAN {halaman_ke} ===")
                    gulir_sampai_mentok(page_utama)
                    html_utama = bersihkan_html(page_utama.content())
                    time.sleep(DELAY_DAFTAR)

                    daftar = ai_ekstrak_daftar(html_utama)
                    data_list = daftar["items"]
                    if not data_list:
                        print("[INFO] Tidak ada data ditemukan.")
                        break

                    title_pertama = data_list[0].get("title")
                    if title_pertama and title_pertama == title_sebelumnya:
                        print("[INFO] Data berulang, mentok di ujung.")
                        break
                    title_sebelumnya = title_pertama or title_sebelumnya

                    if not scout:
                        kolom, extractor = jalankan_scout(context, page_utama, data_list[0].get("link", ""))
                        scout = True

                    print(f"\n=== FASE 2: DEEP CRAWLING HALAMAN {halaman_ke} ===")
                    hasil_halaman = []
                    for idx, item in enumerate(data_list, start=1):
                        print(f"[{idx}] ", end="")
                        data_item, extractor = proses_satu_item(context, page_utama, item, kolom, extractor)
                        if data_item:
                            hasil_halaman.append(data_item)

                    hasil_akhir.extend(hasil_halaman)
                    if mode_simpan == "per_item":
                        total_item = simpan_per_item(hasil_halaman, tujuan_simpan, total_item)
                    else:
                        tulis_json_atomik(hasil_akhir, tujuan_simpan)

                    print(f"\n=== NAVIGASI: Mencari Jalan ke Halaman {halaman_ke + 1} ===")
                    if not pindah_halaman(page_utama, daftar["next_page_link"], daftar["next_page_selector"]):
                        break
                    halaman_ke += 1
    except Exception as e:
        print(f"\n[FATAL] Error tak terduga: {e}")
    finally:
        print(f"\n=== SELESAI! TERSIMPAN DI '{tujuan_simpan}' ===")
        waktu_total = time.monotonic() - waktu_mulai
        jam, sisa = divmod(int(waktu_total), 3600)
        menit, detik = divmod(sisa, 60)
        print(f"Waktu eksekusi: {jam} jam {menit} menit {detik} detik")


if __name__ == "__main__":
    main()
