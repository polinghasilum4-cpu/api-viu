# scripts/harvest_viu.py
import os, sys, logging
import httpx
from patchright.sync_api import sync_playwright

log = logging.getLogger("harvest")
logging.basicConfig(level=logging.INFO)

def harvest() -> str:
    bearer = None
    with sync_playwright() as p:
        b = p.chromium.launch(headless=True, channel="chrome",
            args=["--disable-blink-features=AutomationControlled"])
        ctx = b.new_context(locale="id-ID",
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/131.0.0.0 Safari/537.36")
        page = ctx.new_page()

        def on_req(req):
            nonlocal bearer
            if bearer:
                return
            if "api-gateway" in req.url or "/api/mobile" in req.url:
                auth = req.headers.get("authorization", "")
                if auth.startswith("Bearer eyJ"):
                    bearer = auth[7:]

        page.on("request", on_req)
        page.goto("https://www.viu.com/ott/id/id/",
                  wait_until="networkidle", timeout=45_000)
        if not bearer:
            page.evaluate("""
                fetch('/api/mobile?r=/vod/home&platform_flag_label=web&area_id=1000&language_flag_id=8&ut=0&countryCode=ID', {headers:{accept:'application/json'}}).catch(()=>{})
            """)
            page.wait_for_timeout(3000)
        b.close()
    if not bearer:
        raise RuntimeError("bearer kosong")
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
