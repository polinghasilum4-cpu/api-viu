# api/index.py — Viu Auto API (Exact Match Search UA)
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
# UA persis kayak di curl lu
UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Mobile Safari/537.36")

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
    except Exception: pass
    return None

async def auto_harvest_token_async() -> str:
    proxies_list = []
    try:
        r = httpx.get("https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text", timeout=10)
        proxies_list = r.text.splitlines()
        random.shuffle(proxies_list)
        proxies_list = proxies_list[:40]
    except: pass
    if not proxies_list: return ""
    headers = {"user-agent": UA, "accept-language": "id-ID,id;q=0.9"}
    tasks = [test_proxy(proxy.strip(), headers) for proxy in proxies_list]
    for coro in asyncio.as_completed(tasks):
        tok = await coro
        if tok: return tok
    return ""

def auto_harvest_token() -> str:
    try: return asyncio.run(auto_harvest_token_async())
    except: return ""

def _bearer() -> str:
    b = kv_get("viu:bearer")
    if b: return b
    b = auto_harvest_token()
    if b: kv_set("viu:bearer", b, ex=43200)
    return b

def _call(route: str, params: Optional[Dict] = None) -> Any:
    q = {**COMMON, "r": route}
    if params: q.update(params)
    headers = {"accept": "application/json, text/plain, */*", "authorization": f"Bearer {_bearer()}", "user-agent": UA, "origin": "https://www.viu.com", "referer": "https://www.viu.com/"}
    try:
        with Session(impersonate="chrome") as s:
            r = s.get(BASE, params=q, headers=headers, timeout=15)
            if r.status_code in (401, 403):
                kv_set("viu:bearer", "", ex=1)
                headers["authorization"] = f"Bearer {auto_harvest_token()}"
                r = s.get(BASE, params=q, headers=headers, timeout=15)
    except Exception as e:
        raise HTTPException(status_code=502, detail=str(e))

    if r.status_code != 200:
        raise HTTPException(status_code=r.status_code, detail=f"Viu HTTP {r.status_code}")
    try:
        data = r.json()
    except:
        raise HTTPException(status_code=500, detail="Bukan JSON")
    if not isinstance(data, dict):
        raise HTTPException(status_code=500, detail="Format aneh")
    if data.get("status", {}).get("code") != 0:
        raise HTTPException(502, data.get("status", {}).get("message", "Error"))
    return data.get("data", {})

app = FastAPI(title="Viu API")

@app.get("/")
def root(): return {"status": "ok"}

@app.get("/api/health")
def health():
    try:
        _bearer()
        return {"ok": True, "status": "Token siap"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/search")
def search(q: str):
    ckey = f"viu:search:{hashlib.md5(q.encode()).hexdigest()}"
    cached = kv_get(ckey)
    if cached: return json.loads(cached)

    # Paksa pake raw query string persis kayak curl lu biar pasti dapet
    token = _bearer()
    headers = {
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {token}",
        "user-agent": UA,
        "origin": "https://www.viu.com",
        "referer": "https://www.viu.com/",
    }
    # urlencode q biar spasi jadi %20 atau +
    from urllib.parse import quote
    keyword_encoded = quote(q)
    
    url = f"{BASE}?platform_flag_label=web&area_id=1000&language_flag_id=8&ut=0&platformFlagLabel=web&areaId=1000&languageFlagId=8&countryCode=ID&r=%2Fsearch%2Fprediction&keyword={keyword_encoded}"
    
    with Session(impersonate="chrome") as s:
        r = s.get(url, headers=headers, timeout=15)
        
    if r.status_code != 200:
        raise HTTPException(502, f"Viu HTTP {r.status_code}")
        
    data = r.json()
    result_data = data.get("data", {})
    
    result = {
        "query": q,
        "series": result_data.get("series", []),
        "actor": result_data.get("actor", [])
    }
    
    if result["series"] or result["actor"]:
        kv_set(ckey, json.dumps(result, ensure_ascii=False), ex=3600)
        
    return result

@app.get("/api/home")
def home():
    ckey = "viu:home"
    cached = kv_get(ckey)
    if cached: return json.loads(cached)
    try:
        data = _call("/vod/home")
        if data and isinstance(data, dict):
            kv_set(ckey, json.dumps(data, ensure_ascii=False), ex=1800)
            return data
    except Exception: pass
    try:
        with Session(impersonate="chrome") as s:
            r = s.get(VIU_HOME, headers={"user-agent": UA}, timeout=15)
            if r.status_code == 200 and "not available in your country" not in r.text:
                series_ids = list(set(re.findall(r'"series_id":\s*"(\d+)"', r.text)))
                fallback_list = [{"series_id": sid} for sid in series_ids[:20]]
                result = {"status": "success", "method": "html_scrape", "data": fallback_list}
                if fallback_list:
                    kv_set(ckey, json.dumps(result, ensure_ascii=False), ex=1800)
                    return result
    except Exception as e: pass
    return {"status": "error", "message": "Gagal ambil data home."}

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
