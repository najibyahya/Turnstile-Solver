# Turnstile CAPTCHA Solver

> Terinspirasi dari [SGAHSCAJASCJ/Turnstile-Solver](https://github.com/SGAHSCAJASCJ/Turnstile-Solver)

Solusi pemecahan CAPTCHA Cloudflare Turnstile berkinerja tinggi yang dibangun dengan **FastAPI** dan teknologi browser asinkron (**Camoufox**), menyediakan layanan RESTful API yang siap dipakai.

---

## ✨ Fitur Tambahan

| Fitur | Keterangan |
|---|---|
| 📦 Auto Install | Dependensi & browser Camoufox diinstall otomatis saat pertama kali jalan |
| ⚙️ Konfigurasi via `config.json` | Tidak perlu edit kode, semua setting dari satu file |
| 🖥️ Cek Sistem | Menampilkan info CPU & RAM + rekomendasi kesesuaian config sebelum running |
| 🔌 Cek Port | Deteksi port yang sudah terpakai dan minta ganti otomatis |
| 🔁 Proxy Rotation | Dukungan proxy per-thread dengan rotasi round-robin |
| 🐛 Mode Debug | Aktifkan/matikan log detail via config |
| 🪟 Windows / RDP | Patch asyncio untuk kompatibilitas penuh di Windows & Ubuntu |

---

## 📊 Metrik Performa

| Metrik | Nilai | Keterangan |
|---|---|---|
| Kapasitas Konkurensi | 500+ req/menit | Pool halaman asinkron |
| Rata-rata Waktu Respons | 1,8 – 3 detik | Rata-rata per captcha |
| Tingkat Keberhasilan | 99%+ | Dalam kondisi normal |
| Penggunaan Memori | ~300 MB/halaman | Per instance browser |

---

## 🚀 Mulai Cepat

### Persyaratan
- Python 3.8+
- Windows / Linux / macOS / RDP
- RAM 2 GB+
- Koneksi internet stabil

### Instalasi & Menjalankan
```bash
git clone https://github.com/najibyahya/Turnstile-Solver
cd Turnstile-Solver
python api_server.py
```

> Script akan otomatis menginstall semua dependensi yang dibutuhkan (`fastapi`, `uvicorn`, `camoufox`, `loguru`, `psutil`) dan mengunduh browser Camoufox jika belum ada.

---

## ⚙️ Konfigurasi (`config.json`)

Edit file `config.json` sesuai kebutuhan **sebelum** menjalankan script, atau ubah langsung via prompt interaktif saat script berjalan.

```json
{
    "headless":      true,
    "thread":        2,
    "page_count":    1,
    "proxy_support": false,
    "proxy_file":    "proxies.txt",
    "host":          "0.0.0.0",
    "port":          8000,
    "debug":         false
}
```

| Parameter | Tipe | Default | Keterangan |
|---|---|---|---|
| `headless` | bool | `true` | Browser berjalan tanpa tampilan (mode server) |
| `thread` | int | `2` | Jumlah instance browser — jangan melebihi jumlah core CPU |
| `page_count` | int | `1` | Jumlah halaman per instance browser |
| `proxy_support` | bool | `false` | Aktifkan penggunaan proxy |
| `proxy_file` | string | `proxies.txt` | Path file daftar proxy |
| `host` | string | `0.0.0.0` | Host binding server |
| `port` | int | `8000` | Port server |
| `debug` | bool | `false` | Tampilkan log DEBUG detail |

---

## 🌐 Penggunaan Proxy

### 1. Aktifkan di `config.json`
```json
{
    "proxy_support": true,
    "proxy_file": "proxies.txt"
}
```

### 2. Isi `proxies.txt` (satu proxy per baris)
```
# Tanpa autentikasi
http://203.0.113.10:3128
socks5://203.0.113.20:1080

# Dengan autentikasi
http://username:password@203.0.113.30:8080
socks5://username:password@203.0.113.40:1080
```

Proxy akan dirotasi secara **round-robin** — setiap thread browser mendapat proxy yang berbeda.

---

## 📖 Dokumentasi API

### ➡️ Kirim Tugas CAPTCHA
```http
GET /turnstile?url=https://example.com&sitekey=0x4AAAAAAA...
```

| Parameter | Wajib | Keterangan |
|---|---|---|
| `url` | ✅ | URL halaman tempat Turnstile berada |
| `sitekey` | ✅ | Sitekey Turnstile dari halaman tersebut |
| `action` | ❌ | Nilai `data-action` (opsional) |
| `cdata` | ❌ | Nilai `data-cdata` (opsional) |

**Respons `202 Accepted`:**
```json
{
  "task_id": "550e8400-e29b-41d4-a716-446655440000",
  "status": "accepted"
}
```

---

### ➡️ Ambil Hasil
```http
GET /result?id=<task_id>
```

**Respons sukses `200 OK`:**
```json
{
  "status": "success",
  "elapsed_time": 2.431,
  "value": "0.AbCdEf..."
}
```

**Kode status HTTP:**
| Kode | Kondisi |
|---|---|
| `200` | Token berhasil didapat |
| `202` | Masih diproses, coba lagi |
| `404` | `task_id` tidak valid atau sudah expired |
| `408` | Timeout (> 5 menit) |
| `422` | Captcha gagal diselesaikan |
| `429` | Server penuh, coba lagi nanti |

---

## ❗ Referensi Error

### Error saat startup

| Error | Artinya | Solusi |
|---|---|---|
| `ModuleNotFoundError: No module named 'xxx'` | Dependensi belum terinstall | Jalankan ulang script, atau `pip install xxx --break-system-packages` |
| `CalledProcessError: pip install returned non-zero` | pip diblokir sistem (Ubuntu 22.04+) | `pip install xxx --break-system-packages` atau pakai virtualenv |
| `OSError: [Errno 98] Address already in use` | Port sudah dipakai proses lain | Ganti port di `config.json` atau matikan proses yang memakai port tersebut |

### Error saat runtime (log)

| Log | Artinya | Solusi |
|---|---|---|
| `Percobaan captcha X gagal: Timeout 400ms exceeded` | Captcha belum muncul / lambat load | Normal jika tidak terlalu sering. Aktifkan `debug: false` untuk sembunyikan |
| `Pool halaman berhasil diinisialisasi, berisi 0 halaman` | Browser gagal membuat halaman | Cek RAM tersedia, coba kurangi `thread` atau `page_count` |
| `proxy_support aktif tapi file 'proxies.txt' tidak ditemukan` | File proxy tidak ada | Buat file `proxies.txt` dengan daftar proxy |
| `Format proxy tidak valid: ...` | Format proxy di file salah | Gunakan format `protocol://host:port` atau `protocol://user:pass@host:port` |
| `Server telah mencapai kapasitas maksimum` | Semua slot browser sedang terpakai | Naikkan `thread` atau `page_count`, atau tunggu request selesai |

### Kode HTTP response API

| Kode | Artinya |
|---|---|
| `202` | Tugas diterima / masih diproses — poll ulang beberapa saat |
| `400` | Parameter `url` atau `sitekey` tidak disertakan |
| `404` | `task_id` tidak valid atau sudah expired |
| `408` | Tugas timeout (> 5 menit) |
| `422` | Captcha gagal diselesaikan setelah 30 percobaan |
| `429` | Server penuh — semua slot browser sedang terpakai |
| `500` | Error tak terduga di server |

---

## 🔧 Tips Penyetelan Performa

- **`thread`**: Sesuaikan dengan jumlah core CPU. Contoh: 8-core → maksimal `thread: 8`
- **`page_count`**: Mulai dari `1`. Naikkan hanya jika RAM mencukupi (±300 MB per halaman)
- **`debug: false`**: Matikan untuk output bersih di production
- **Gunakan proxy** untuk meningkatkan tingkat keberhasilan di sitekey yang ketat

---

## 📄 Lisensi

MIT License — Lihat file [LICENSE](LICENSE) untuk detail.

---

## 🔗 Kredit & Referensi

- 🧑‍💻 **Owner Asli**: [SGAHSCAJASCJ](https://github.com/SGAHSCAJASCJ/Turnstile-Solver) — Fondasi solver ini
- 🦊 [Camoufox](https://github.com/daijro/camoufox) — Browser anti-deteksi berbasis Firefox
- ⚡ [FastAPI](https://fastapi.tiangolo.com/) — Framework API modern
- ☁️ [Cloudflare Turnstile](https://developers.cloudflare.com/turnstile/) — Dokumentasi resmi Turnstile

---

<div align="center">

**⚡ Performa Tinggi &nbsp;|&nbsp; 🚀 Mudah Dipakai &nbsp;|&nbsp; 🛡️ Stabil & Andal &nbsp;|&nbsp; 🌐 Proxy Ready**

</div>
