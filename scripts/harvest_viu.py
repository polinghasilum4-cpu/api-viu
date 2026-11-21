# scripts/harvest_viu.py
import os, sys, logging
import httpx
from patchright.sync_api import sync_playwright

log = logging.getLogger("harvest")
logging.basicConfig(level=logging.INFO)

def harvest() -> str:
    bearer = None
    with sync_playwright() as p:
        # Paksa UA Windows biar nggak dicurigai
        b = p.chromium.launch(headless=True, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="en-US",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36")
        page = ctx.new_page()

        def on_req(req):
            nonlocal bearer
            if bearer:
                return
            # Cek header authorization
            auth = req.headers.get("authorization", "")
            if auth.startswith("Bearer eyJ"):
                bearer = auth[7:]
                log.info("Bearer ketangkep dari header XHR")
            
            # Kadang Viu naruh token di header custom
            x_auth = req.headers.get("x-access-token", "")
            if x_auth.startswith("eyJ"):
                bearer = x_auth
                log.info("Bearer ketangkep dari x-access-token")

        page.on("request", on_req)
        
        log.info("Buka halaman Viu...")
        page.goto("https://www.viu.com/ott/id/id/",
                  wait_until="domcontentloaded", timeout=60_000)
        
        # Kasih waktu app React-nya jalan
        page.wait_for_timeout(5000)

        if not bearer:
            log.info("Nggak nemu di header, coba cek localStorage...")
            # Kadang token disimpen di browser storage
            js_code = """
            () => {
                let tok = localStorage.getItem('access_token') || 
                          localStorage.getItem('token') || 
                          sessionStorage.getItem('token');
                return tok;
            }
            """
            tok_storage = page.evaluate(js_code)
            if tok_storage and tok_storage.startswith("eyJ"):
                bearer = tok_storage
                log.info("Bearer ketangkep dari localStorage")

        if not bearer:
            log.info("Coba trigger API manual...")
            # Pancing API-nya buat manggil
            page.evaluate("""
                fetch('https://api-gateway-global.viu.com/api/mobile?r=/vod/home&platform_flag_label=web&area_id=1000&language_flag_id=8&ut=0&countryCode=ID')
                .catch(()=>{})
            """)
            page.wait_for_timeout(4000)

        # Kalau tetep gagal, screenshot buat diagnosa
        if not bearer:
            page.screenshot(path="error_viu.png", full_page=True)
            log.error("Gagal dapet token. Screenshot disimpen di error_viu.png")
            b.close()
            raise RuntimeError("bearer kosong — cek error_viu.png")

        b.close()
    return bearer

def push_to_vercel(bearer: str) -> None:
    url = os.environ["VERCEL_URL"].rstrip("/") + "/api/_internal/bearer"
    secret = os.environ["VIU_SECRET"]
    with httpx.Client(timeout=30) as c:
        r = c.post(url, json={"bearer": bearer},
                   headers={"x-harvest-secret": secret})
    log.info("push status=%d body=%s", r.status_code, r.text[:200])
    r.raise_for_status()

if __name__ == "__main__":
    try:
        tok = harvest()
        log.info("bearer ok len=%d", len(tok))
        push_to_vercel(tok)
    except Exception as e:
        log.error("harvest gagal: %s", e)
        sys.exit(1)
