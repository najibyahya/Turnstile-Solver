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

# Kompatibilitas Windows
if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())


class ClearanceAPIServer:
    """
    Solver gabungan:
      GET /turnstile  → Cloudflare Turnstile token
      GET /clearance  → cf_clearance cookie (bypass Cloudflare WAF)
    """

    HTML_TEMPLATE = """
    <!DOCTYPE html>
    <html lang="en">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Boterdrop Solver</title>
        <script src="https://challenges.cloudflare.com/turnstile/v0/api.js?onload=onloadTurnstileCallback" async="" defer=""></script>
    </head>
    <body>
        <!-- cf turnstile -->
        <p id="ip-display"></p>
    </body>
    </html>
    """

    def __init__(self, headless: bool, thread: int, page_count: int,
                 proxy_support: bool, proxy_file: str = "proxies.txt"):
        self.app = FastAPI()
        self.headless = headless
        self.thread_count = thread
        self.page_count = page_count
        self.proxy_support = proxy_support
        self.proxy_file = proxy_file
        self.page_pool = asyncio.Queue()
        self.browser_args = [
            "--no-sandbox",
            "--disable-setuid-sandbox",
        ]
        self.camoufox = None
        self.browser = None
        self.results = {}
        self.contexts = []  # Track semua context agar bisa di-restart dengan aman
        self.proxies = []
        self._proxy_index = 0
        self.max_task_num = self.thread_count * self.page_count

        self.app.add_event_handler("startup", self._startup)
        self.app.add_event_handler("shutdown", self._shutdown)
        self.app.get("/turnstile")(self.process_turnstile)
        self.app.get("/clearance")(self.process_clearance)
        self.app.get("/result")(self.get_result)

    # ──────────────────────────────────────────────
    #  PROXY
    # ──────────────────────────────────────────────

    def _load_proxies(self):
        if not self.proxy_support:
            return
        if not os.path.isfile(self.proxy_file):
            logger.warning(f"proxy_support aktif tapi file '{self.proxy_file}' tidak ditemukan.")
            return
        with open(self.proxy_file) as f:
            lines = [ln.strip() for ln in f if ln.strip() and not ln.startswith("#")]
        self.proxies = lines
        logger.info(f"Memuat {len(self.proxies)} proxy dari '{self.proxy_file}'")

    def _next_proxy(self):
        if not self.proxies:
            return None
        proxy = self.proxies[self._proxy_index % len(self.proxies)]
        self._proxy_index += 1
        return proxy

    async def _create_context_with_proxy(self, proxy: str = None):
        if not proxy:
            return await self.browser.new_context()
        from urllib.parse import urlparse
        parsed = urlparse(proxy)
        if not parsed.scheme or not parsed.hostname:
            logger.warning(f"Format proxy tidak valid: {proxy}")
            return await self.browser.new_context()
        server = f"{parsed.scheme}://{parsed.hostname}:{parsed.port}"
        if parsed.username and parsed.password:
            return await self.browser.new_context(
                proxy={"server": server, "username": parsed.username, "password": parsed.password}
            )
        return await self.browser.new_context(proxy={"server": server})

    # ──────────────────────────────────────────────
    #  BROWSER LIFECYCLE
    # ──────────────────────────────────────────────

    async def _startup(self):
        logger.info("Inisialisasi browser...")
        try:
            await self._initialize_browser()
        except Exception as e:
            logger.error(f"Inisialisasi gagal: {e}")
            raise

    async def _shutdown(self):
        logger.info("Menutup browser...")
        try:
            await self.browser.close()
        except Exception as e:
            logger.warning(f"Error saat menutup browser: {e}")
        logger.success("Browser berhasil ditutup")

    async def _initialize_browser(self):
        self._load_proxies()
        self.camoufox = AsyncCamoufox(
            headless=self.headless,
            exclude_addons=[DefaultAddons.UBO],
            args=self.browser_args
        )
        self.browser = await self.camoufox.start()
        await self._build_page_pool()
        logger.success(f"Pool siap: {self.page_pool.qsize()} halaman")
        asyncio.create_task(self._cleanup_results())
        asyncio.create_task(self._periodic_cleanup())

    async def _build_page_pool(self):
        """Buat/rebuild semua context dan page ke dalam pool."""
        self.contexts = []
        self._proxy_index = 0
        for _ in range(self.thread_count):
            proxy = self._next_proxy() if self.proxy_support else None
            context = await self._create_context_with_proxy(proxy)
            self.contexts.append(context)
            for _ in range(self.page_count):
                page = await context.new_page()
                await self.page_pool.put((page, context))

    async def _cleanup_results(self):
        """Bersihkan semua hasil (success & error) yang sudah lebih dari 10 menit dan belum diambil."""
        while True:
            await asyncio.sleep(300)  # Cek setiap 5 menit
            now = time.time()
            expired = [
                tid for tid, res in list(self.results.items())
                if isinstance(res, dict)
                and now - res.get("start_time", now) > 300  # Hapus jika > 5 menit
            ]
            if expired:
                for tid in expired:
                    self.results.pop(tid, None)
                logger.info(f"[Cleanup] Menghapus {len(expired)} hasil kedaluwarsa dari memori")

    async def _periodic_cleanup(self, interval_minutes: int = 10):
        """
        Periodic cleanup yang benar-benar membebaskan RAM:
        - Jika semua page idle → full context restart (close & recreate)
        - Jika ada page yang sedang dipakai → light cleanup saja, coba lagi siklus berikutnya
        """
        while True:
            await asyncio.sleep(interval_minutes * 60)
            logger.info("[Cleanup] Mencoba drain pool untuk restart context...")

            # Drain semua page dari pool secara non-blocking
            collected = []
            try:
                while True:
                    item = self.page_pool.get_nowait()
                    collected.append(item)
            except asyncio.QueueEmpty:
                pass

            if len(collected) < self.max_task_num:
                # Ada task yang sedang berjalan → hanya light cleanup pada page yang idle
                busy = self.max_task_num - len(collected)
                logger.info(f"[Cleanup] {busy} page sedang dipakai, light cleanup pada {len(collected)} page idle...")
                for page, context in collected:
                    try:
                        await page.unroute_all()
                    except Exception:
                        pass
                    try:
                        await context.clear_cookies()
                    except Exception:
                        pass
                    try:
                        await page.goto("about:blank")
                    except Exception:
                        pass
                    await self.page_pool.put((page, context))
                logger.info("[Cleanup] Light cleanup selesai, full restart ditunda ke siklus berikutnya")
                continue

            # Semua page idle → Full context restart untuk bebaskan RAM
            logger.info("[Cleanup] Semua page idle, memulai full context restart...")

            # Tutup semua page
            for page, _ in collected:
                try:
                    await page.close()
                except Exception:
                    pass

            # Tutup semua context (ini yang bebaskan RAM Firefox)
            for context in self.contexts:
                try:
                    await context.close()
                except Exception:
                    pass

            # Recreate semua context dan page
            await self._build_page_pool()
            logger.success(f"[Cleanup] Full context restart selesai, {self.page_pool.qsize()} halaman siap")

    # ──────────────────────────────────────────────
    #  TURNSTILE SOLVER
    # ──────────────────────────────────────────────

    async def _save_debug_on_fail(self, page, task_id: str, round_num: int, url: str):
        """Simpan screenshot + HTML + info halaman ke debug_logs/ saat gagal."""
        try:
            debug_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_logs")
            os.makedirs(debug_dir, exist_ok=True)
            ts = time.strftime("%Y%m%d_%H%M%S")
            prefix = os.path.join(debug_dir, f"{ts}_{task_id[:8]}_r{round_num}")

            # Screenshot
            try:
                await page.screenshot(path=f"{prefix}.png", full_page=True)
            except Exception as e:
                logger.debug(f"[Debug] Gagal screenshot: {e}")

            # Info halaman
            info = {}
            try:
                info["title"] = await page.title()
            except Exception:
                info["title"] = "N/A"
            try:
                info["url"] = page.url
            except Exception:
                info["url"] = "N/A"
            try:
                info["turnstile_widget_exists"] = await page.locator("//div[@class='cf-turnstile']").count() > 0
            except Exception:
                info["turnstile_widget_exists"] = "N/A"
            try:
                info["cf_response_value"] = await page.input_value("[name=cf-turnstile-response]", timeout=500)
            except Exception:
                info["cf_response_value"] = "not found"
            try:
                info["js_errors"] = await page.evaluate(
                    "() => window.__debugErrors || []"
                )
            except Exception:
                info["js_errors"] = []
            try:
                info["html_snippet"] = await page.inner_html("body")
            except Exception:
                info["html_snippet"] = "N/A"

            info["task_id"] = task_id
            info["target_url"] = url
            info["round"] = round_num
            info["timestamp"] = ts

            with open(f"{prefix}.json", "w", encoding="utf-8") as f:
                json.dump(info, f, indent=2, ensure_ascii=False)

            logger.info(f"[Debug] Disimpan: {prefix}.png + .json")
        except Exception as e:
            logger.warning(f"[Debug] Gagal simpan debug: {e}")

    async def _solve_turnstile(self, task_id: str, url: str, sitekey: str,
                                action: str = None, cdata: str = None):
        start_time = time.time()
        page, context = await self.page_pool.get()
        try:
            url_with_slash = url if url.endswith("/") else url + "/"
            turnstile_div = (
                f'<div class="cf-turnstile" style="background:white;" data-sitekey="{sitekey}"'
                + (f' data-action="{action}"' if action else "")
                + (f' data-cdata="{cdata}"' if cdata else "")
                + "></div>"
            )
            page_data = self.HTML_TEMPLATE.replace("<!-- cf turnstile -->", turnstile_div)

            MAX_ROUNDS = 2
            for round_num in range(1, MAX_ROUNDS + 1):
                if round_num > 1:
                    logger.info(f"[Turnstile] Putaran {round_num}: reload halaman — {task_id}")
                    if self.proxy_support and self.proxies:
                        proxy = self._next_proxy()
                        try:
                            new_context = await self._create_context_with_proxy(proxy)
                            new_page = await new_context.new_page()
                            try:
                                await page.close()
                            except Exception:
                                pass
                            try:
                                await context.close()
                            except Exception:
                                pass
                            page, context = new_page, new_context
                            logger.info(f"[Turnstile] Proxy diganti: {proxy} — {task_id}")
                        except Exception as e:
                            logger.warning(f"[Turnstile] Gagal ganti proxy: {e}, tetap pakai context lama")

                # Pasang error listener JS untuk debug
                try:
                    await page.evaluate("() => { window.__debugErrors = []; window.onerror = (m,s,l,c,e) => { window.__debugErrors.push({msg:m,src:s,line:l,col:c}); }; }")
                except Exception:
                    pass

                try:
                    await page.unroute_all()
                except Exception:
                    pass
                await page.route(url_with_slash, lambda route: route.fulfill(body=page_data, status=200))
                await page.goto(url_with_slash)
                await page.eval_on_selector("//div[@class='cf-turnstile']", "el => el.style.width = '70px'")

                solved = False
                for attempt in range(80):  # 80 × 0.3s = ~24 detik timeout
                    try:
                        value = await page.input_value("[name=cf-turnstile-response]", timeout=400)
                        if value == "":
                            await page.locator("//div[@class='cf-turnstile']").click(timeout=400)
                            await asyncio.sleep(0.3)
                        else:
                            elapsed = round(time.time() - start_time, 3)
                            self.results[task_id] = {"status": "success", "elapsed_time": elapsed, "value": value}
                            logger.info(f"[Turnstile] Sukses (putaran {round_num}) — {task_id} ({elapsed}s)")
                            solved = True
                            return
                    except Exception as e:
                        logger.debug(f"[Turnstile] Putaran {round_num} percobaan {attempt + 1} gagal: {e}")

                if not solved:
                    logger.warning(f"[Turnstile] Putaran {round_num} gagal 30x — {task_id}, {'retry...' if round_num < MAX_ROUNDS else 'menyerah'}")
                    # Simpan debug info di setiap putaran yang gagal
                    await self._save_debug_on_fail(page, task_id, round_num, url_with_slash)

            elapsed = round(time.time() - start_time, 3)
            self.results[task_id] = {"status": "error", "elapsed_time": elapsed, "value": "captcha_fail"}
            logger.warning(f"[Turnstile] Gagal setelah {MAX_ROUNDS} putaran — {task_id}")
        except Exception as e:
            elapsed = round(time.time() - start_time, 3)
            self.results[task_id] = {"status": "error", "elapsed_time": elapsed, "value": "captcha_fail"}
            logger.error(f"[Turnstile] Exception — {task_id}: {e}")
        finally:
            try:
                await page.unroute_all()
            except Exception:
                pass
            await self.page_pool.put((page, context))


    # ──────────────────────────────────────────────
    #  CF_CLEARANCE SOLVER
    # ──────────────────────────────────────────────

    async def _solve_clearance(self, task_id: str, url: str, timeout: int = 30):
        """
        Navigasikan browser ke URL target, tunggu Cloudflare challenge selesai,
        lalu ambil cookie cf_clearance dan User-Agent.
        """
        start_time = time.time()
        page, context = await self.page_pool.get()
        try:
            user_agent = await page.evaluate("navigator.userAgent")

            logger.info(f"[Clearance] Navigasi ke {url} — {task_id}")
            await page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)

            # Tunggu Cloudflare challenge selesai
            # Indikator: judul halaman bukan "Just a moment..." dan cf_clearance ada di cookies
            deadline = time.time() + timeout
            cf_clearance = None
            while time.time() < deadline:
                title = await page.title()
                cookies = await context.cookies()
                cf_cookie = next((c for c in cookies if c["name"] == "cf_clearance"), None)

                if cf_cookie and "just a moment" not in title.lower():
                    cf_clearance = cf_cookie["value"]
                    break

                # Jika ada tantangan interaktif, coba tunggu saja
                await asyncio.sleep(1)

            elapsed = round(time.time() - start_time, 3)

            if cf_clearance:
                # Kumpulkan semua cookies dari domain
                all_cookies = await context.cookies()
                cookie_header = "; ".join(f"{c['name']}={c['value']}" for c in all_cookies)
                self.results[task_id] = {
                    "status": "success",
                    "elapsed_time": elapsed,
                    "cf_clearance": cf_clearance,
                    "user_agent": user_agent,
                    "cookies": cookie_header,
                }
                logger.success(f"[Clearance] Sukses — {task_id} ({elapsed}s)")
            else:
                title = await page.title()
                self.results[task_id] = {
                    "status": "error",
                    "elapsed_time": elapsed,
                    "value": "clearance_fail",
                    "message": f"cf_clearance tidak ditemukan setelah {timeout}s. Judul halaman: '{title}'",
                }
                logger.warning(f"[Clearance] Gagal — {task_id}")

        except Exception as e:
            elapsed = round(time.time() - start_time, 3)
            self.results[task_id] = {"status": "error", "elapsed_time": elapsed, "value": str(e)}
            logger.error(f"[Clearance] Exception — {task_id}: {e}")
        finally:
            # Reset halaman & bersihkan cookies agar tidak membawa state lama
            try:
                await context.clear_cookies()
            except Exception:
                pass
            try:
                await page.goto("about:blank")
            except Exception:
                pass
            await self.page_pool.put((page, context))

    # ──────────────────────────────────────────────
    #  ENDPOINTS
    # ──────────────────────────────────────────────

    async def process_turnstile(self, url: str = Query(...), sitekey: str = Query(...),
                                 action: str = Query(None), cdata: str = Query(None)):
        if not url or not sitekey:
            raise HTTPException(status_code=400, detail={"status": "error", "error": "Parameter 'url' dan 'sitekey' wajib diisi"})

        if self.page_pool.qsize() == 0:
            return JSONResponse(content={"status": "error", "error": "Server penuh, coba lagi nanti"}, status_code=429)

        task_id = str(uuid.uuid4())
        self.results[task_id] = {"status": "process", "message": "solving turnstile", "start_time": time.time()}
        try:
            asyncio.create_task(self._solve_turnstile(task_id, url, sitekey, action, cdata))
            return JSONResponse(content={"task_id": task_id, "status": "accepted"}, status_code=202)
        except Exception as e:
            self.results.pop(task_id, None)
            return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

    async def process_clearance(
        self,
        url: str = Query(..., description="URL target yang dilindungi Cloudflare"),
        timeout: int = Query(30, description="Waktu tunggu maksimal dalam detik (default: 30)"),
    ):
        """
        Endpoint untuk mendapatkan cf_clearance cookie.

        Response sukses berisi:
        - cf_clearance : nilai cookie cf_clearance
        - user_agent   : User-Agent yang HARUS dipakai bersama cookie ini
        - cookies      : seluruh cookie domain dalam format header (opsional, untuk kemudahan)
        """
        if not url:
            raise HTTPException(status_code=400, detail={"status": "error", "error": "Parameter 'url' wajib diisi"})

        if self.page_pool.qsize() == 0:
            return JSONResponse(content={"status": "error", "error": "Server penuh, coba lagi nanti"}, status_code=429)

        task_id = str(uuid.uuid4())
        self.results[task_id] = {"status": "process", "message": "solving clearance", "start_time": time.time()}
        try:
            asyncio.create_task(self._solve_clearance(task_id, url, timeout))
            return JSONResponse(content={"task_id": task_id, "status": "accepted"}, status_code=202)
        except Exception as e:
            self.results.pop(task_id, None)
            return JSONResponse(content={"status": "error", "message": str(e)}, status_code=500)

    async def get_result(self, task_id: str = Query(..., alias="id")):
        if not task_id:
            return JSONResponse(content={"status": "error", "message": "Parameter id wajib diisi"}, status_code=400)
        if task_id not in self.results:
            return JSONResponse(content={"status": "error", "message": "task_id tidak valid atau sudah expired"}, status_code=404)

        result = self.results[task_id]

        if result.get("status") == "process":
            start_time = result.get("start_time", time.time())
            if time.time() - start_time > 300:
                self.results[task_id] = {
                    "status": "error",
                    "elapsed_time": round(time.time() - start_time, 3),
                    "value": "timeout",
                    "message": "Tugas timeout"
                }
                result = self.results[task_id]
            else:
                return JSONResponse(content=result, status_code=202)

        result = self.results.pop(task_id)
        if result.get("status") == "success":
            status_code = 200
        elif result.get("value") == "timeout":
            status_code = 408
        else:
            status_code = 422
        return JSONResponse(content=result, status_code=status_code)


def create_app(headless, thread, page_count, proxy_support, proxy_file="proxies.txt") -> FastAPI:
    server = ClearanceAPIServer(headless=headless, thread=thread, page_count=page_count,
                                proxy_support=proxy_support, proxy_file=proxy_file)
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
  ___) | (_) | |\ V /  __/ |   Clearance v1.0.0
 |____/ \___/|_| \_/ \___|_|
"""
    print("\033[95m" + banner + "\033[0m")
    print("  \033[90m github.com/najibyahya/Turnstile-Solver\033[0m")
    print()


# ──────────────────────────────────────────────
#  AUTO INSTALL
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
            for extra in [[], ["--break-system-packages"]]:
                try:
                    subprocess.check_call(
                        [sys.executable, "-m", "pip", "install", pkg] + extra,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
                    )
                    installed = True
                    break
                except subprocess.CalledProcessError:
                    continue
            print(f"  {'✅' if installed else '⚠️ Gagal install'}  {pkg}")

    import shutil
    if not shutil.which("camoufox") and not _camoufox_data_exists():
        print("  🌐  Mengunduh data browser Camoufox...")
        subprocess.check_call([sys.executable, "-m", "camoufox", "fetch"])
        print("  ✅  Camoufox berhasil diunduh")
    print("═" * 52 + "\n")


def _camoufox_data_exists():
    d = os.path.join(os.path.expanduser("~"), ".camoufox")
    return os.path.isdir(d) and bool(os.listdir(d))


def _check_xvfb(headless: bool):
    if sys.platform == "win32" or headless:
        return
    import shutil, subprocess
    if os.environ.get("DISPLAY", ""):
        print("  ✅  DISPLAY terdeteksi, GUI dapat berjalan")
        return
    print("═" * 52)
    print("  ⚠️   headless=false di VPS tanpa DISPLAY terdeteksi")
    if not shutil.which("xvfb-run"):
        print("  📦  Menginstall Xvfb...")
        try:
            subprocess.check_call(["apt-get", "install", "-y", "xvfb"],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            print("  ✅  Xvfb berhasil diinstall")
        except Exception:
            print("  ❌  Gagal. Jalankan manual: sudo apt-get install -y xvfb")
    print()
    print("  🚨  Jalankan dengan:")
    print("       xvfb-run -a python3 cf_clearance_server.py")
    print()
    print("  ℹ️   Atau ubah headless=true di config.json")
    print("═" * 52)
    sys.exit(1)


# ──────────────────────────────────────────────
#  CONFIG
# ──────────────────────────────────────────────
CONFIG_PATH = "config.json"
CONFIG_DEFAULTS = {
    "headless":      True,
    "thread":        2,
    "page_count":    1,
    "proxy_support": False,
    "proxy_file":    "proxies.txt",
    "host":          "0.0.0.0",
    "port":          8001,   # port berbeda dari api_server.py agar bisa jalan bersamaan
    "debug":         False,
}


def _load_config() -> dict:
    try:
        with open(CONFIG_PATH) as f:
            return {**CONFIG_DEFAULTS, **json.load(f)}
    except FileNotFoundError:
        print(f"  ⚠️  {CONFIG_PATH} tidak ditemukan, menggunakan nilai default\n")
        return dict(CONFIG_DEFAULTS)
    except json.JSONDecodeError as e:
        print(f"  ❌  Format {CONFIG_PATH} tidak valid: {e}\n")
        return dict(CONFIG_DEFAULTS)


def _save_config(cfg: dict):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=4)
    print(f"\n  💾  Config disimpan ke {CONFIG_PATH}")


def _parse_value(key, raw, current):
    raw = raw.strip()
    if not raw:
        return current
    if isinstance(current, bool):
        return raw.lower() in ("true", "1", "y", "yes")
    if isinstance(current, int):
        try:
            return int(raw)
        except ValueError:
            return current
    return raw


def _show_config_summary(cfg: dict):
    print("═" * 52)
    print("  ⚙️   KONFIGURASI AKTIF")
    print("═" * 52)
    labels = {
        "headless":      ("Mode Headless",                    "bool"),
        "thread":        ("Jumlah instance browser",          "int"),
        "page_count":    ("Halaman per instance",             "int"),
        "proxy_support": ("Dukungan proxy",                   "bool"),
        "host":          ("Host server",                      "str"),
        "port":          ("Port server",                      "int"),
        "debug":         ("Mode Debug",                       "bool"),
    }
    for i, (key, (label, _)) in enumerate(labels.items(), 1):
        print(f"  [{i}] {label:<38} : {cfg.get(key)}")
    print("═" * 52)


def _interactive_config(cfg: dict) -> dict:
    _show_config_summary(cfg)
    print()
    ans = input("  ▶  Lanjutkan? [Enter/Y = ya  |  N = ubah] : ").strip().lower()
    if ans not in ("n", "no", "tidak"):
        return cfg

    field_order = ["headless", "thread", "page_count", "proxy_support", "host", "port", "debug"]
    labels = {
        "headless":      "Mode Headless (true/false)",
        "thread":        "Jumlah thread",
        "page_count":    "Halaman per instance",
        "proxy_support": "Dukungan proxy (true/false)",
        "host":          "Host server",
        "port":          "Port server",
        "debug":         "Mode Debug (true/false)",
    }
    print("\n  ✏️   Tekan Enter untuk mempertahankan nilai saat ini")
    print("─" * 52)
    new_cfg = dict(cfg)
    for key in field_order:
        raw = input(f"  {labels[key]} [{cfg[key]}] : ")
        new_cfg[key] = _parse_value(key, raw, cfg[key])

    print()
    _show_config_summary(new_cfg)
    if input("\n  💾  Simpan? [Y/Enter = ya  |  N = tidak] : ").strip().lower() not in ("n", "no"):
        _save_config(new_cfg)
    return new_cfg


def _check_port(cfg: dict) -> dict:
    import socket
    while True:
        port = cfg.get("port", 8001)
        host = cfg.get("host", "0.0.0.0")
        bind = "127.0.0.1" if host == "0.0.0.0" else host
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(1)
            in_use = s.connect_ex((bind, port)) == 0
        if not in_use:
            print(f"  ✅  Port {port} tersedia")
            print("═" * 52 + "\n")
            break
        print(f"  ❌  Port {port} sudah digunakan!")
        try:
            new_port = int(input(f"  🔁  Port pengganti : ").strip())
            cfg = dict(cfg)
            cfg["port"] = new_port
            if input(f"  💾  Simpan port {new_port}? [Y/Enter] : ").strip().lower() not in ("n", "no"):
                _save_config(cfg)
        except ValueError:
            print("  ⚠️  Input tidak valid")
    return cfg


def _check_system(cfg: dict):
    import psutil
    cpu_count = os.cpu_count() or 1
    cpu_usage = psutil.cpu_percent(interval=1)
    ram = psutil.virtual_memory()
    ram_total = ram.total / 1024**3
    ram_free = ram.available / 1024**3
    ram_pct = ram.percent
    thread = cfg.get("thread", 2)
    page_count = cfg.get("page_count", 1)
    total = thread * page_count
    est_ram = total * 0.3
    print("═" * 52)
    print("  💻  INFO SISTEM")
    print("═" * 52)
    print(f"  🖥️  CPU  : {cpu_count} core | terpakai {cpu_usage:.1f}%")
    print(f"  🧠  RAM  : {ram_total:.1f} GB | bebas {ram_free:.1f} GB ({ram_pct:.1f}%)")
    print(f"  📋  Config: {thread} thread × {page_count} halaman = {total} slot")
    print(f"  📊  Est. RAM browser: ±{est_ram:.1f} GB")
    print("─" * 52)
    issues = []
    if thread > cpu_count:
        issues.append(f"  ⚠️  thread ({thread}) > core CPU ({cpu_count})")
    if est_ram > ram_free * 0.85:
        issues.append(f"  ⚠️  Est. RAM ({est_ram:.1f} GB) mendekati RAM bebas ({ram_free:.1f} GB)")
    if cpu_usage > 80:
        issues.append(f"  ⚠️  CPU tinggi ({cpu_usage:.1f}%)")
    if issues:
        print("  ❌  Peringatan:")
        for i in issues:
            print(i)
    else:
        print("  ✅  Config sesuai dengan resource sistem")
    print("═" * 52 + "\n")


# ──────────────────────────────────────────────
#  ENTRY POINT
# ──────────────────────────────────────────────
if __name__ == "__main__":
    _print_banner()
    _auto_install()
    config = _load_config()
    config = _interactive_config(config)
    _check_xvfb(config.get("headless", True))

    print()
    print("═" * 52)
    print("  🔍  CEK PORT & SUMBER DAYA SISTEM")
    print("═" * 52)
    log_level = "DEBUG" if config.get("debug", False) else "INFO"
    logger.remove()
    logger.add(sys.stderr, level=log_level)
    logger.info(f"Level log: {log_level}")

    _check_system(config)
    config = _check_port(config)

    print("═" * 52)
    print("  🚀  Memulai Boterdrop Clearance Solver...")
    print("═" * 52 + "\n")

    app = create_app(
        headless=config["headless"],
        thread=config["thread"],
        page_count=config["page_count"],
        proxy_support=config["proxy_support"],
        proxy_file=config.get("proxy_file", "proxies.txt"),
    )
    uvicorn.run(app, host=config["host"], port=config["port"])
