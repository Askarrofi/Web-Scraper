# Universal LLM Powered Web Scraper to JSON

## Latar Belakang & Masalah
Proyek ini dikembangkan berdasarkan pengalaman magang pribadi, dimana melakukan web scraping secara manual, masalah yang sering terjadi adalah struktur halaman website yang sangat kompleks, strukturnya berubah-ubah untuk setiap web, harus mengetahui isi dari website, mencari lokasi-lokasi data item yang diinginkan, dan menyusun outputnya secara manual.
Pengembangan proyek ini bertujuan untuk mempermudah proses scraping website, bahkan pengguna nonprogrammer dapat menggunakannya dengan mudah, dimana proses manual diatas tidak perlu dilakukan dan akan dieksekusi oleh LLM yang ada di dalam program ini.

## Prinsip Desain
- **Universal:** Tidak peduli itu berita, e-commerce, atau jurnal, sistem harus bisa membacanya.
- **Minim Intervensi:** Cukup masukkan URL dan nama data yang diambil, biarkan AI yang mengurus sisanya.
- **Self-Heal:** Jika ada proses yang gagal, program akan mencari cara memperbaikinya.
- **Fail-Safe:** Jika satu proses gagal, lewati dengan anggun tanpa membuat program *crash*.

---

## Fitur Inti (Core Features)
*(Tuliskan ide-ide fiturmu di sini. Fokus pada apa yang penting saja)*
1. **Input Interaktif Sederhana:** Meminta URL dan daftar target data via terminal.
2. **AI Extractor Generator:** AI membuat kode pengeruk spesifik di latar belakang (sekali saja).
3. **Auto-Pagination:** Bisa berpindah halaman otomatis hingga data habis.
4. **Ekspor Mudah:** Langsung menyimpan hasil ke dalam format JSON yang rapi.
5. *(Tambah idemu di sini...)*

---

## Alur Logika (User & System Flow)
*(Bagaimana cara kerja alat ini dari sudut pandang pengguna)*
1. **User** menjalankan program.
2. **Sistem** meminta URL target dan item data yang ingin diambil (contoh: Judul, Harga, Lokasi).
3. **Sistem** membuka *browser* tersembunyi (Playwright).
4. **AI** memindai halaman pertama untuk mengenali pola dan tombol navigasi.
5. **AI** meracik fungsi ekstraksi data.
6. **Sistem** mengulang proses (ekstrak -> pindah halaman) sampai batas maksimal atau tombol *next* habis.
7. **Sistem** menutup *browser* dan menyimpan file `output.json`.

---

## Tech Stack
- **Core:** Python
- **Browser Automation:** Playwright
- **AI Engine:** Google Gemini API
- **HTML Parser:** BeautifulSoup4
- **Hardware:** Lenovo Legion 7i
- **Tool:** Github

---

## Catatan Tambahan & Ide Masa Depan (Backlog)
*(Taruh ide-ide liar atau fitur rumit di sini agar tidak mengotori versi awal)*
- *Mungkin nanti bisa buat versi GUI/Web*