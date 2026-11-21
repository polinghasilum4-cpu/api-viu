# api/index.py — Viu Auto API (Final + Home + Browse Fix)
import os, json, hashlib, logging, re, random, asyncio
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from curl_cffi.requests import AsyncSession, Session
import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("viu")

BASE = "https://api-gateway-global.viu.com/api/mobile"
VIU_HOME = "https://www.viu.com/ott/id/id/"
COMMON = {
    "platform_flag_label": "web", "area_id": "1000",
    "language_flag_id": "8", "ut": "0",
    "platformFlagLabel": "web", "areaId": "1000",
    "languageFlagId": "8", "countryCode": "ID", "os_flag_id": "1",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

# --- CLOUDFLARE KV SETUP ---
CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_TOKEN = os.environ.get("CF_KV_TOKEN", "")
CF_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/storage/kv/namespaces/{CF_KV_ID}"

def kv_get(key: str) -> Optional[str]:
    if not (CF_ACCOUNT and CF_KV_ID and CF_TOKEN): return None
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{CF_BASE}/values/{key}", headers={"Authorization": f"Bearer {CF_TOKEN}"})
        return r.text if r.status_code == 200 else None
    except: return None

def kv_set(key: str, value: str, ex: int = 3600) -> None:
    if not (CF_ACCOUNT and CF_KV_ID and CF_TOKEN): return
    try:
        with httpx.Client(timeout=5) as c:
            c.put(f"{CF_BASE}/values/{key}", headers={"Authorization": f"Bearer {CF_TOKEN}"},
                  content=value, params={"expiration_ttl": str(ex)})
    except: pass

# --- AUTO HARVEST DENGAN CONCURRENT PROXY ---
async def test_proxy(proxy: str, headers: dict) -> Optional[str]:
    try:
        px = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        async with AsyncSession(impersonate="chrome", proxies=px, timeout=6) as s:
            r1 = await s.get(VIU_HOME, headers=headers)
            if "not available in your country" not in r1.text:
                m = re.search(r'(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)', r1.text)
                if m: return m.group(1)
            
            guest_url = "https://api-gateway-global.viu.com/api/mobile?r=/vod/login/guest&platform_flag_label=web&area_id=1000&language_flag_id=8&ut=0&countryCode=ID"
            r2 = await s.get(guest_url, headers=headers)
            if r2.status_code == 200:
                tok = r2.json().get("data", {}).get("token")
                if tok: return tok
    except Exception:
        pass
    return None

async def auto_harvest_token_async() -> str:
    log.info("Nyari token baru pake concurrent proxy...")
    proxies_list = []
    try:
        r = httpx.get("https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text", timeout=10)
        proxies_list = r.text.splitlines()
        random.shuffle(proxies_list)
        proxies_list = proxies_list[:40]
    except:
        pass
        
    if not proxies_list:
        return ""

    headers = {"user-agent": UA, "accept-language": "id-ID,id;q=0.9"}
    tasks = [test_proxy(proxy.strip(), headers) for proxy in proxies_list]
    
    for coro in asyncio.as_completed(tasks):
        tok = await coro
        if tok:
            log.info("Sukses dapet token pake proxy!")
            return tok

    return ""

def auto_harvest_token() -> str:
    try:
        return asyncio.run(auto_harvest_token_async())
    except:
        return ""

def _bearer() -> str:
    b = kv_get("viu:bearer")
    if b: return b
    b = auto_harvest_token()
    if b:
        kv_set("viu:bearer", b, ex=43200)
    return b

def _call(route: str, params: Optional[Dict] = None) -> Any:
    q = {**COMMON, "r": route}
    if params: q.update(params)
    headers = {"accept": "application/json, text/plain, */*", "authorization": f"Bearer {_bearer()}", "user-agent": UA, "origin": "https://www.viu.com", "referer": "https://www.viu.com/"}
    
    with Session(impersonate="chrome") as s:
        r = s.get(BASE, params=q, headers=headers, timeout=15)
        
    if r.status_code in (401, 403):
        kv_set("viu:bearer", "", ex=1)
        headers["authorization"] = f"Bearer {auto_harvest_token()}"
        with Session(impersonate="chrome") as s:
            r = s.get(BASE, params=q, headers=headers, timeout=15)

    r.raise_for_status()
    data = r.json()
    if data.get("status", {}).get("code") != 0:
        raise HTTPException(502, f"viu error: {data.get('status')}")
    return data.get("data", {})

app = FastAPI(title="Viu Auto API")

@app.get("/")
def root():
    return {"status": "ok", "message": "Viu API nyala."}

@app.get("/api/health")
def health():
    try:
        _bearer()
        return {"ok": True, "status": "Token siap", "engine": "Vercel SG"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/home")
def home():
    """Nampilin daftar kategori dan film trending di homepage Viu."""
    ckey = "viu:home"
    cached = kv_get(ckey)
    if cached: return json.loads(cached)
    
    data = _call("/vod/home")
    kv_set(ckey, json.dumps(data, ensure_ascii=False), ex=1800)
    return data

@app.get("/api/series/{series_id}")
def series(series_id: str, refresh: bool = False):
    ckey = f"viu:series:{series_id}"
    if not refresh:
        cached = kv_get(ckey)
        if cached: return json.loads(cached)
    data = _call("/vod/product-list", {"series_id": series_id, "size": -1, "sort": "desc"})
    plist = sorted(data.get("product_list", []), key=lambda x: int(x.get("number", 0)))
    result = {"series_id": series_id, "episodes": plist}
    kv_set(ckey, json.dumps(result, ensure_ascii=False), ex=21600)
    return result

@app.get("/api/search")
def search(q: str, size: int = 20):
    ckey = f"viu:search:{hashlib.md5(q.encode()).hexdigest()}:{size}"
    cached = kv_get(ckey)
    if cached: return json.loads(cached)

    attempts = [
        ("/vod/search", {"q": q, "size": size}),
        ("/vod/search", {"keyword": q, "size": size}),
        ("/vod/product/search", {"q": q, "size": size}),
        ("/vod/product/search", {"keyword": q, "size": size}),
        ("/vod/search/product", {"q": q, "size": size}),
        ("/vod/product-list", {"keyword": q, "size": size}),
        ("/vod/product-list", {"search": q, "size": size}),
    ]

    last_err = None
    for route, params in attempts:
        try:
            data = _call(route, params)
            plist = data.get("product_list") or data.get("products") or []
            if plist:
                result = {"query": q, "count": len(plist), "product_list": plist}
                kv_set(ckey, json.dumps(result, ensure_ascii=False), ex=3600)
                return result
        except HTTPException as e:
            last_err = e.detail
            continue

    return {"query": q, "count": 0, "product_list": [], "note": "Search kosong."}

@app.get("/api/browse")
def browse(category: str = Query(None), page: int = 1, size: int = 20, sort: str = "desc"):
    # Viu kadang minta category_id (angka) bukan string. Kita coba beberapa opsi.
    ckey = f"viu:browse:{category}:{page}:{size}"
    cached = kv_get(ckey)
    if cached: return json.loads(cached)

    params = {"size": size, "sort": sort, "start": (page - 1) * size}
    if category:
        # Coba tebak ID dari string kategori (misal drama=1, movie=2)
        cat_map = {"drama": "1", "movie": "2", "anime": "3", "variety": "4"}
        cat_id = cat_map.get(category.lower(), category)
        params["category_id"] = cat_id

    data = _call("/vod/product-list", params)
    # Kadang Viu balikin null kalau kategori salah. Kita pastiin balikin array kosong.
    if "product_list" not in data or data["product_list"] is None:
        data["product_list"] = []
        
    if data["product_list"]:
        kv_set(ckey, json.dumps(data, ensure_ascii=False), ex=1800)
        
    return data

@app.get("/api/play/{product_id}")
def play(product_id: str, series_id: str = None):
    params = {"product_id": product_id}
    if series_id: params["series_id"] = series_id
    ckey = f"viu:play:{product_id}"
    cached = kv_get(ckey)
    if cached: return json.loads(cached)
    data = _call("/vod/product/play", params)
    kv_set(ckey, json.dumps(data, ensure_ascii=False), ex=1800)
    return data
