import json
import os
import sys
import time
import uuid
import asyncio
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import JSONResponse
from loguru import logger
from camoufox import DefaultAddons
from camoufox.async_api import AsyncCamoufox
import uvicorn

# Kompatibilitas Windows: gunakan SelectorEventLoop agar asyncio.Queue & task berjalan normal
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

class TurnstileAPIServer:
    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>body's solver</title>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onloadTurnstileCallback" async="" defer=""></script>
    </head>
    <body>
        <!-- cf turnstile -->
        <p id="ip-display"></p>
    </body>
    </html>
    """

    def __init__(self, headless: bool, thread: int, page_count: int, proxy_support: bool, proxy_file: str = "proxies.txt"):
        self.app = FastAPI()
        self.headless = headless
        self.thread_count = thread
        self.page_count = page_count
        self.proxy_support = proxy_support
        self.proxy_file = proxy_file
        self.page_pool = asyncio.Queue()
        # Konfigurasi argumen startup browser
        self.browser_args = [
            "--no-sandbox",  # Nonaktifkan sandbox, diperlukan di beberapa lingkungan
            "--disable-setuid-sandbox",  # Digunakan bersama no-sandbox
        ]
        self.camoufox = None  # Instance Camoufox
        self.results = {}  # Penyimpanan hasil tugas
        self.proxies = []  # Daftar proxy (diisi saat startup jika proxy_support=True)
        self._proxy_index = 0  # Indeks rotasi proxy
        self.max_task_num = self.thread_count * self.page_count
        # Daftarkan event startup dan shutdown
        self.app.add_event_handler("startup", self._startup)
        self.app.add_event_handler("shutdown", self._shutdown)
        self.app.get("/turnstile")(self.process_turnstile)
        self.app.get("/result")(self.get_result)

    async def _cleanup_results(self):
        """Bersihkan hasil yang kedaluwarsa secara berkala"""
        while True:
            await asyncio.sleep(3600)  # Bersihkan setiap jam
            expired = [
                tid for tid, res in self.results.items()
                if isinstance(res, dict) and res.get("status") == "error"
                   and time.time() - res.get("start_time", 0) > 3600
            ]
            for tid in expired:
                self.results.pop(tid, None)
                logger.debug(f"Membersihkan tugas kedaluwarsa: {tid}")

    async def _periodic_cleanup(self, interval_minutes: int = 60):
        """Bersihkan dan bangun ulang halaman satu per satu secara berkala untuk menghindari pemblokiran tugas"""
        while True:
            await asyncio.sleep(interval_minutes * 60)
            logger.info("Mulai membersihkan cache halaman dan konteks satu per satu")

            total = self.max_task_num
            success = 0
            for _ in range(total):
                try:
                    # Coba ambil halaman dari pool (menandakan halaman sedang idle)
                    page, context= await self.page_pool.get()
                    try:
                        await page.close()
                    except:
                        pass
                    try:
                        await context.close()
                    except Exception as e:
                        logger.warning(f"Error saat membersihkan halaman: {e}")

                    context = await self._create_context_with_proxy()
                    page = await context.new_page()
                    await self.page_pool.put((page, context))
                    success += 1
                    await asyncio.sleep(1.5)  # Tunggu sebentar untuk menghindari dampak berantai
                except Exception as e:
                    logger.warning(f"Gagal membersihkan dan membangun ulang halaman: {e}")
                    continue
            logger.success(f"Pembersihan berkala selesai, total diproses {success}/{total} halaman")

    async def _startup(self) -> None:
        """Initialize the browser and page pool on startup."""
        logger.info("Mulai inisialisasi browser")
        try:
            await self._initialize_browser()
        except Exception as e:
            logger.error(f"Inisialisasi browser gagal: {str(e)}")
            raise

    async def _shutdown(self) -> None:
        """Bersihkan semua sumber daya browser saat shutdown"""
        logger.info("Mulai membersihkan sumber daya browser")
        try:
            await self.browser.close()
        except Exception as e:
            logger.warning(f"Terjadi kesalahan saat menutup browser: {e}")
        logger.success("Semua sumber daya browser telah dibersihkan")

    async def _create_context_with_proxy(self, proxy: str = None):
        """Buat konteks browser berdasarkan proxy.
        Format: protocol://host:port  atau  protocol://user:pass@host:port
        """
        if not proxy:
            return await self.browser.new_context()

        from urllib.parse import urlparse
        parsed = urlparse(proxy)

        if not parsed.scheme or not parsed.hostname:
            logger.warning(f"Format proxy tidak valid: {proxy}, menggunakan konteks tanpa proxy")
            return await self.browser.new_context()

        server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"

        if parsed.username and parsed.password:
            return await self.browser.new_context(
                proxy={
                    "server": server,
                    "username": parsed.username,
                    "password": parsed.password,
                }
            )
        return await self.browser.new_context(proxy={"server": server})

    def _load_proxies(self):
        """Muat daftar proxy dari file.
        Format: protocol://host:port  atau  protocol://user:pass@host:port
        """
        if not self.proxy_support:
            return
        if not os.path.isfile(self.proxy_file):
            logger.warning(f"proxy_support aktif tapi file '{self.proxy_file}' tidak ditemukan. Berjalan tanpa proxy.")
            return
        with open(self.proxy_file, "r") as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        self.proxies = lines
        logger.info(f"Memuat {len(self.proxies)} proxy dari '{self.proxy_file}'")

    def _next_proxy(self):
        """Kembalikan proxy berikutnya secara round-robin, atau None jika kosong."""
        if not self.proxies:
            return None
        proxy = self.proxies[self._proxy_index % len(self.proxies)]
        self._proxy_index += 1
        return proxy

    async def _initialize_browser(self):
        self._load_proxies()
        self.camoufox = AsyncCamoufox(
            headless=self.headless,
            exclude_addons=[DefaultAddons.UBO],
            args=self.browser_args
        )
        self.browser = await self.camoufox.start()

        # Buat pool halaman — setiap thread mendapat proxy berbeda (round-robin)
        for _ in range(self.thread_count):
            proxy = self._next_proxy() if self.proxy_support else None
            context = await self._create_context_with_proxy(proxy)
            for _ in range(self.page_count):
                page = await context.new_page()
                await self.page_pool.put((page, context))

        logger.success(f"Pool halaman berhasil diinisialisasi, berisi {self.page_pool.qsize()} halaman")
        asyncio.create_task(self._cleanup_results())  # Bersihkan hasil tugas
        asyncio.create_task(self._periodic_cleanup())  # Bersihkan cache halaman dan konteks secara berkala

    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str, action: str = None, cdata: str = None):
        """Selesaikan captcha Turnstile menggunakan halaman dari pool"""
        start_time = time.time()
        page, context = await self.page_pool.get()
        try:
            url_with_slash = url + "/" if not url.endswith("/") else url
            turnstile_div = (f'<div class="cf-turnstile" style="background: white;" data-sitekey="{sitekey}"' +
                             (f' data-action="{action}"' if action else '') +
                             (f' data-cdata="{cdata}"' if cdata else '') + '></div>')
            page_data = self.HTML_TEMPLATE.replace("<!-- cf turnstile -->", turnstile_div)
            await page.route(url_with_slash, lambda route: route.fulfill(body=page_data, status=200))
            await page.goto(url_with_slash)
            await page.eval_on_selector("//div[@class='cf-turnstile']", "el => el.style.width = '70px'")

            # Coba selesaikan captcha, maksimal 30 percobaan
            for attempt in range(30):
                try:
                    # Periksa nilai respons captcha
                    turnstile_check = await page.input_value("[name=cf-turnstile-response]", timeout=400)
                    if turnstile_check == "":
                        # Jika respons kosong, klik elemen captcha untuk memicu verifikasi
                        await page.locator("//div[@class='cf-turnstile']").click(timeout=400)
                        await asyncio.sleep(0.2)
                    else:
                        # Captcha berhasil diselesaikan
                        elapsed_time = round(time.time() - start_time, 3)
                        self.results[task_id] = {
                            "status": 'success',
                            "elapsed_time": elapsed_time,
                            "value": turnstile_check
                        }
                        logger.info(f"Captcha berhasil diselesaikan, Task ID: {task_id}, waktu: {elapsed_time} detik")
                        break
                except Exception as e:
                    # Percobaan tunggal gagal, lanjutkan ke percobaan berikutnya
                    logger.debug(f"Percobaan captcha {attempt + 1} gagal: {e}")

            # Jika semua percobaan gagal, tandai sebagai error
            if self.results.get(task_id) == {"status": "process", "message": 'solving captcha'}:
                elapsed_time = round(time.time() - start_time, 3)
                self.results[task_id] = {
                    "status": "error",
                    "elapsed_time": elapsed_time,
                    "value": "captcha_fail"
                }
                logger.warning(f"Captcha gagal diselesaikan, Task ID: {task_id}, waktu: {elapsed_time} detik")

        except Exception as e:
            # Tangani situasi pengecualian
            elapsed_time = round(time.time() - start_time, 3)
            self.results[task_id] = {
                "status": "error",
                "elapsed_time": elapsed_time,
                "value": "captcha_fail"
            }
            logger.error(f"Pengecualian saat memecahkan captcha, Task ID: {task_id}: {e}")
        finally:
            # Kembalikan halaman ke pool
            await self.page_pool.put((page, context))

    async def process_turnstile(self, url: str = Query(...), sitekey: str = Query(...), action: str = Query(None),
                                cdata: str = Query(None)):
        """Tangani permintaan endpoint /turnstile"""
        # Validasi parameter
        if not url or not sitekey:
            raise HTTPException(
                status_code=400,
                detail={"status": "error", "error": "Parameter 'url' dan 'sitekey' harus disertakan"}
            )

        # Periksa beban server berdasarkan ketersediaan halaman di pool
        available_pages = self.page_pool.qsize()
        if available_pages == 0:
            logger.warning(f"Beban server penuh, tidak ada halaman tersedia (kapasitas: {self.max_task_num})")
            return JSONResponse(
                content={"status": "error", "error": "Server telah mencapai kapasitas maksimum, coba lagi nanti"},
                status_code=429
            )

        # Buat task ID unik
        task_id = str(uuid.uuid4())
        logger.info(f"Menerima tugas baru, task_id: {task_id}, url: {url}, sitekey: {sitekey}")

        # Inisialisasi status tugas
        self.results[task_id] = {
            "status": "process",
            "message": 'solving captcha',
            "start_time": time.time()
        }

        try:
            # Buat tugas asinkron untuk memproses captcha
            asyncio.create_task(
                self._solve_turnstile(
                    task_id=task_id,
                    url=url,
                    sitekey=sitekey,
                    action=action,
                    cdata=cdata
                )
            )
            return JSONResponse(
                content={"task_id": task_id, "status": "accepted"},
                status_code=202
            )
        except Exception as e:
            logger.error(f"Terjadi kesalahan tak terduga saat memproses permintaan: {str(e)}")
            # Bersihkan tugas yang gagal
            self.results.pop(task_id, None)
            return JSONResponse(
                content={"status": "error", "message": f"Kesalahan internal server: {str(e)}"},
                status_code=500
            )

    async def get_result(self, task_id: str = Query(..., alias="id")):
        """Kembalikan hasil penyelesaian captcha"""
        # Validasi parameter
        if not task_id:
            return JSONResponse(
                content={"status": "error", "message": "Parameter task_id tidak ada"},
                status_code=400
            )

        # Periksa apakah tugas ada
        if task_id not in self.results:
            return JSONResponse(
                content={"status": "error", "message": "task_id tidak valid atau tugas sudah kedaluwarsa"},
                status_code=404
            )

        result = self.results[task_id]

        # Periksa apakah tugas masih diproses
        if result.get("status") == "process":
            # Periksa apakah tugas sudah timeout (lebih dari 5 menit)
            start_time = result.get("start_time", time.time())
            if time.time() - start_time > 300:  # Timeout 5 menit
                self.results[task_id] = {
                    "status": "error",
                    "elapsed_time": round(time.time() - start_time, 3),
                    "value": "timeout",
                    "message": "Tugas timeout"
                }
                result = self.results[task_id]
            else:
                # Tugas masih diproses, kembalikan status proses
                return JSONResponse(content=result, status_code=202)

        # Tugas selesai, kembalikan hasil dan bersihkan
        result = self.results.pop(task_id)

        # Tentukan kode status HTTP berdasarkan status hasil
        if result.get("status") == "success":
            status_code = 200
        elif result.get("value") == "timeout":
            status_code = 408  # Request Timeout
        elif "captcha_fail" in result.get("value", ""):
            status_code = 422  # Unprocessable Entity
        else:
            status_code = 500  # Internal Server Error

        return JSONResponse(content=result, status_code=status_code)
def create_app(headless: bool, thread: int, page_count: int, proxy_support: bool, proxy_file: str = "proxies.txt") -> FastAPI:
    server = TurnstileAPIServer(headless=headless, thread=thread, page_count=page_count, proxy_support=proxy_support, proxy_file=proxy_file)
    return server.app


# ──────────────────────────────────────────────
#  BANNER
# ──────────────────────────────────────────────
def _print_banner():
    banner = r"""
  ____        _                _
 | __ )  ___ | |_ ___ _ __ __| |_ __ ___  _ __
 |  _ \ / _ \| __/ _ \ '__/ _` | '__/ _ \| '_ \
 | |_) | (_) | ||  __/ | | (_| | | | (_) | |_) |
 |____/ \___/ \__\___|_|  \__,_|_|  \___/| .__/
  ____        _                           |_|
 / ___|  ___ | |_   _____ _ __
 \___ \ / _ \| \ \ / / _ \ '__|
  ___) | (_) | |\ V /  __/ |   Turnstile v1.0.0
 |____/ \___/|_| \_/ \___|_|
"""
    print("\033[96m" + banner + "\033[0m")
    print("  \033[90m github.com/najibyahya/Turnstile-Solver\033[0m")
    print()


# ──────────────────────────────────────────────
#  AUTO INSTALL DEPENDENSI
# ──────────────────────────────────────────────
def _auto_install():
    import subprocess
    PACKAGES = ["fastapi", "uvicorn", "camoufox", "loguru", "psutil"]
    print("\n" + "═" * 52)
    print("  🔧  Memeriksa dependensi yang dibutuhkan...")
    print("═" * 52)
    for pkg in PACKAGES:
        try:
            __import__(pkg.split("[")[0])
            print(f"  ✅  {pkg} sudah terpasang")
        except ImportError:
            print(f"  📦  Menginstall {pkg}...")
            installed = False
            # Coba instalasi biasa dulu
            for extra_args in [[], ["--break-system-packages"]]:
                try:
                    subprocess.check_call(
                        [sys.executable, "-m", "pip", "install", pkg] + extra_args,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    installed = True
                    break
                except subprocess.CalledProcessError:
                    continue
            if installed:
                print(f"  ✅  {pkg} berhasil diinstall")
            else:
                print(f"  ⚠️  Gagal install {pkg} via pip. Coba manual: pip install {pkg}")

    # Fetch browser camoufox jika belum ada
    import shutil
    if not shutil.which("camoufox") and not _camoufox_data_exists():
        print("  🌐  Mengunduh data browser Camoufox (sekali saja)...")
        subprocess.check_call([sys.executable, "-m", "camoufox", "fetch"])
        print("  ✅  Data browser Camoufox berhasil diunduh")
    print("═" * 52 + "\n")


def _camoufox_data_exists():
    """Cek apakah data browser camoufox sudah ada."""
    import os
    camoufox_dir = os.path.join(os.path.expanduser("~"), ".camoufox")
    return os.path.isdir(camoufox_dir) and bool(os.listdir(camoufox_dir))


# ──────────────────────────────────────────────
#  LOAD & SIMPAN KONFIGURASI
# ──────────────────────────────────────────────
CONFIG_PATH = "config.json"
CONFIG_DEFAULTS = {
    "headless":      True,
    "thread":        2,
    "page_count":    1,
    "proxy_support": False,
    "proxy_file":    "proxies.txt",
    "host":          "0.0.0.0",
    "port":          8000,
    "debug":         False,
}

def _load_config() -> dict:
    try:
        with open(CONFIG_PATH, "r") as f:
            cfg = json.load(f)
        return {**CONFIG_DEFAULTS, **cfg}
    except FileNotFoundError:
        print(f"  ⚠️  {CONFIG_PATH} tidak ditemukan, menggunakan nilai default\n")
        return dict(CONFIG_DEFAULTS)
    except json.JSONDecodeError as e:
        print(f"  ❌  Format {CONFIG_PATH} tidak valid: {e}, menggunakan nilai default\n")
        return dict(CONFIG_DEFAULTS)


def _save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)
    print(f"\n  💾  Konfigurasi disimpan ke {CONFIG_PATH}")


def _parse_value(key: str, raw: str, current):
    """Konversi input string ke tipe yang sesuai dengan nilai saat ini."""
    raw = raw.strip()
    if raw == "":
        return current
    if isinstance(current, bool):
        return raw.lower() in ("true", "1", "yes", "y")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            print(f"  ⚠️  Nilai tidak valid untuk {key}, tetap menggunakan: {current}")
            return current
    return raw


# ──────────────────────────────────────────────
#  TAMPILKAN RINGKASAN & KONFIRMASI KONFIGURASI
# ──────────────────────────────────────────────
def _show_config_summary(cfg: dict):
    print("═" * 52)
    print("  ⚙️   KONFIGURASI AKTIF")
    print("═" * 52)
    labels = {
        "headless":      ("Mode Headless (tanpa tampilan browser)", "bool"),
        "thread":        ("Jumlah instance browser (thread)",       "int"),
        "page_count":    ("Jumlah halaman per instance",            "int"),
        "proxy_support": ("Dukungan proxy",                          "bool"),
        "host":          ("Host server",                             "str"),
        "port":          ("Port server",                             "int"),
        "debug":         ("Mode Debug (tampilkan log detail)",       "bool"),
    }
    for i, (key, (label, _)) in enumerate(labels.items(), 1):
        val = cfg[key]
        print(f"  [{i}] {label:<40} : {val}")
    print("═" * 52)


def _interactive_config(cfg: dict) -> dict:
    """Tampilkan ringkasan dan beri opsi edit sebelum server dijalankan."""
    _show_config_summary(cfg)
    print()
    answer = input("  ▶  Lanjutkan dengan konfigurasi ini? [Enter/Y = ya  |  N = ubah] : ").strip().lower()

    if answer not in ("n", "no", "tidak"):
        return cfg  # Tidak ada perubahan

    # Mode edit – tampilkan tiap field, Enter untuk lewati
    print()
    print("  ✏️   Masukkan nilai baru (tekan Enter untuk mempertahankan nilai saat ini)")
    print("─" * 52)

    field_order = ["headless", "thread", "page_count", "proxy_support", "host", "port", "debug"]
    labels = {
        "headless":      "Mode Headless (true/false)",
        "thread":        "Jumlah thread / instance browser",
        "page_count":    "Jumlah halaman per instance",
        "proxy_support": "Dukungan proxy (true/false)",
        "host":          "Host server",
        "port":          "Port server",
        "debug":         "Mode Debug — tampilkan log DEBUG (true/false)",
    }

    new_cfg = dict(cfg)
    for key in field_order:
        current = cfg[key]
        raw = input(f"  {labels[key]} [{current}] : ")
        new_cfg[key] = _parse_value(key, raw, current)

    # Tampilkan ulang hasil perubahan
    print()
    print("  📋  Konfigurasi yang akan digunakan:")
    _show_config_summary(new_cfg)

    save = input("\n  💾  Simpan konfigurasi ini ke config.json? [Y/Enter = ya  |  N = tidak] : ").strip().lower()
    if save not in ("n", "no", "tidak"):
        _save_config(new_cfg)

    return new_cfg


# ──────────────────────────────────────────────
#  CEK SISTEM (CPU & RAM)
# ──────────────────────────────────────────────
def _check_system(cfg: dict):
    """Tampilkan info CPU & RAM saat ini beserta rekomendasi kesesuaian config."""
    import psutil, os

    cpu_count  = os.cpu_count() or 1
    cpu_usage  = psutil.cpu_percent(interval=1)
    ram        = psutil.virtual_memory()
    ram_total  = ram.total / (1024 ** 3)          # GB
    ram_used   = ram.used  / (1024 ** 3)          # GB
    ram_free   = ram.available / (1024 ** 3)      # GB
    ram_pct    = ram.percent

    thread     = cfg.get("thread", 2)
    page_count = cfg.get("page_count", 1)
    total_pages = thread * page_count

    # Perkiraan RAM yang dibutuhkan (~300 MB per halaman browser)
    est_ram_gb = total_pages * 0.3

    print("═" * 52)
    print("  💻  INFO SISTEM")
    print("═" * 52)
    print(f"  🖥️  CPU     : {cpu_count} core  |  terpakai {cpu_usage:.1f}%")
    print(f"  🧠  RAM     : {ram_total:.1f} GB total  |  terpakai {ram_used:.1f} GB ({ram_pct:.1f}%)  |  bebas {ram_free:.1f} GB")
    print(f"  📋  Config  : {thread} thread × {page_count} halaman = {total_pages} slot konkuren")
    print(f"  📊  Est. kebutuhan RAM browser : ±{est_ram_gb:.1f} GB")
    print("─" * 52)

    # --- Rekomendasi ---
    issues = []
    if thread > cpu_count:
        issues.append(f"  ⚠️  thread ({thread}) melebihi jumlah core CPU ({cpu_count}) → potensi lambat")
    if est_ram_gb > ram_free * 0.85:
        issues.append(f"  ⚠️  Estimasi RAM browser ({est_ram_gb:.1f} GB) mendekati/melebihi RAM bebas ({ram_free:.1f} GB)")
    if cpu_usage > 80:
        issues.append(f"  ⚠️  CPU sedang tinggi ({cpu_usage:.1f}%) → pertimbangkan kurangi thread")

    if issues:
        print("  ❌  PERINGATAN KESESUAIAN CONFIG:")
        for iss in issues:
            print(iss)
    else:
        print("  ✅  Konfigurasi tampak sesuai dengan sumber daya sistem saat ini")
    print("═" * 52 + "\n")


# ──────────────────────────────────────────────
#  CEK PORT
# ──────────────────────────────────────────────
def _check_port(cfg: dict) -> dict:
    """Cek apakah port di config sudah dipakai. Jika ya, minta user ganti."""
    import socket

    while True:
        port = cfg.get("port", 8000)
        host = cfg.get("host", "0.0.0.0")
        bind_host = "127.0.0.1" if host == "0.0.0.0" else host

        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            in_use = s.connect_ex((bind_host, port)) == 0

        if not in_use:
            print(f"  ✅  Port {port} tersedia")
            print("═" * 52 + "\n")
            break

        print(f"  ❌  Port {port} sudah digunakan oleh proses lain!")
        try:
            raw = input(f"  🔁  Masukkan port pengganti [contoh: 8080] : ").strip()
            new_port = int(raw)
            cfg = dict(cfg)
            cfg["port"] = new_port

            save = input(f"  💾  Simpan port {new_port} ke config.json? [Y/Enter = ya  |  N = tidak] : ").strip().lower()
            if save not in ("n", "no", "tidak"):
                _save_config(cfg)
        except ValueError:
            print("  ⚠️  Input tidak valid, coba lagi")
            continue

    return cfg


# ──────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == '__main__':
    _print_banner()
    _auto_install()
    config = _load_config()
    config = _interactive_config(config)

    print()
    print("═" * 52)
    print("  🔍  CEK PORT & SUMBER DAYA SISTEM")
    print("═" * 52)
    # Konfigurasi level logging
    log_level = "DEBUG" if config.get("debug", False) else "INFO"
    logger.remove()
    logger.add(sys.stderr, level=log_level)
    logger.info(f"Level log diset ke: {log_level}")

    _check_system(config)
    config = _check_port(config)

    print("═" * 52)
    print("  🚀  Memulai server Turnstile Solver...")
    print("═" * 52 + "\n")

    app = create_app(
        headless=config["headless"],
        thread=config["thread"],
        page_count=config["page_count"],
        proxy_support=config["proxy_support"],
        proxy_file=config.get("proxy_file", "proxies.txt"),
    )
    uvicorn.run(app, host=config["host"], port=config["port"])
