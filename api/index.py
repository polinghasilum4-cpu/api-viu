# api/index.py — Viu Auto API (Fortress Edition)
import os, json, hashlib, logging, re, random, asyncio, time, secrets, threading
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Depends, Header, Request
from curl_cffi.requests import AsyncSession, Session
import httpx
from urllib.parse import quote

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
UA = ("Mozilla/5.0 (Linux; Android 10; K) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/157.0.0.0 Mobile Safari/537.36")

# --- OPTIONAL API KEY: browser clients may call the public API without a key ---
API_KEY_NAME = "X-API-Key"
RATE_LIMIT_WINDOW = 60
RATE_LIMITS = {
    "default": 60,
    "/api/search": 20,
    "/api/series/": 30,
    "/api/play/": 12,
}
ENABLE_KV_RATE_LIMIT = os.environ.get("ENABLE_KV_RATE_LIMIT", "false").lower() == "true"
_rate_lock = threading.Lock()
_rate_local: Dict[str, tuple] = {}

def _rate_limit(request: Request) -> None:
    # Vercel forwards the visitor IP in X-Forwarded-For. Hash it before using it
    # as a KV key so raw IP addresses are not stored in Cloudflare KV.
    forwarded_for = request.headers.get("x-forwarded-for", "")
    client_ip = forwarded_for.split(",", 1)[0].strip()
    if not client_ip and request.client:
        client_ip = request.client.host
    if not client_ip:
        client_ip = "unknown"

    path = request.url.path
    route_group, limit = next(
        ((prefix, n) for prefix, n in RATE_LIMITS.items()
         if prefix != "default" and path.startswith(prefix)),
        ("default", RATE_LIMITS["default"]),
    )
    now = int(time.time())
    window = now // RATE_LIMIT_WINDOW
    identity = hashlib.sha256(client_ip.encode()).hexdigest()[:24]
    bucket = f"xs0:rate:{identity}:{route_group}:{window}"

    # Local counter gives an immediate per-instance limit. Cloudflare KV can be
    # opted into for an approximate cross-instance counter (it is not atomic).
    with _rate_lock:
        count, expires = _rate_local.get(bucket, (0, now + RATE_LIMIT_WINDOW))
        if expires <= now:
            count = 0
            expires = now + RATE_LIMIT_WINDOW
        count += 1
        _rate_local[bucket] = (count, expires)
        if len(_rate_local) > 5000:
            for old_bucket, (_, old_expiry) in list(_rate_local.items()):
                if old_expiry <= now:
                    _rate_local.pop(old_bucket, None)

    kv_count = 0
    if ENABLE_KV_RATE_LIMIT and CF_ACCOUNT and CF_KV_ID and CF_TOKEN:
        try:
            kv_count = int(kv_get(bucket) or 0) + 1
            kv_set(bucket, str(kv_count), ex=RATE_LIMIT_WINDOW + 5)
        except (TypeError, ValueError):
            kv_count = 0

    if max(count, kv_count) > limit:
        retry_after = RATE_LIMIT_WINDOW - (now % RATE_LIMIT_WINDOW)
        raise HTTPException(
            status_code=429,
            detail="Terlalu banyak permintaan. Coba lagi sebentar.",
            headers={"Retry-After": str(retry_after)},
        )

def verify_api_key(
    request: Request,
    api_key: Optional[str] = Header(default=None, alias=API_KEY_NAME),
):
    # The key remains optional for the frontend. If a client chooses to send it,
    # reject invalid values rather than silently accepting a bad credential.
    if api_key:
        expected_key = os.environ.get("MY_API_KEY", "")
        if not expected_key or not secrets.compare_digest(api_key, expected_key):
            raise HTTPException(status_code=403, detail="API Key tidak valid.")
    _rate_limit(request)
    return api_key

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

# --- PROXY ROTATOR ---
async def get_id_proxies():
    proxies = []
    try:
        r1 = httpx.get("https://api.proxyscrape.com/v4/free-proxy-list/get?request=display_proxies&protocol=http&proxy_format=ipport&format=text&country=ID", timeout=5)
        proxies.extend(r1.text.splitlines())
    except: pass
    try:
        r2 = httpx.get("https://proxylist.geonode.com/api/proxy-list?limit=50&page=1&sort_by=lastChecked&sort_type=desc&country=ID", timeout=5)
        for item in r2.json().get("data", []):
            proxies.append(f"{item['ip']}:{item['port']}")
    except: pass
    cleaned = [p.strip() for p in proxies if ":" in p]
    random.shuffle(cleaned)
    return list(set(cleaned))[:20]

# --- TOKEN HARVESTER ---
async def test_proxy(proxy: str, headers: dict) -> Optional[str]:
    try:
        px = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
        async with AsyncSession(impersonate="chrome", proxies=px, timeout=6) as s:
            r1 = await s.get(VIU_HOME, headers=headers)
            if "not available in your country" not in r1.text:
                m = re.search(r'(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)', r1.text)
                if m: return m.group(1)
            guest_url = f"{BASE}?r=/vod/login/guest&platform_flag_label=web&area_id=1000&language_flag_id=8&ut=0&countryCode=ID"
            r2 = await s.get(guest_url, headers=headers)
            if r2.status_code == 200:
                tok = r2.json().get("data", {}).get("token")
                if tok: return tok
    except Exception: pass
    return None

async def auto_harvest_token_async() -> str:
    proxies_list = await get_id_proxies()
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

# --- LAPIS 2: EXPONENTIAL BACKOFF ---
def _call(route: str, params: Optional[Dict] = None, retries: int = 2) -> Any:
    q = {**COMMON, "r": route}
    if params: q.update(params)
    headers = {"accept": "application/json, text/plain, */*", "authorization": f"Bearer {_bearer()}", "user-agent": UA, "origin": "https://www.viu.com", "referer": "https://www.viu.com/"}
    
    for attempt in range(retries + 1):
        try:
            with Session(impersonate="chrome") as s:
                r = s.get(BASE, params=q, headers=headers, timeout=15)
                if r.status_code in (401, 403):
                    kv_set("viu:bearer", "", ex=1)
                    headers["authorization"] = f"Bearer {auto_harvest_token()}"
                    r = s.get(BASE, params=q, headers=headers, timeout=15)
                
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                
                if r.status_code != 200:
                    raise HTTPException(status_code=r.status_code, detail=f"Viu HTTP {r.status_code}")

        except Exception as e:
            if attempt == retries: raise HTTPException(status_code=502, detail=str(e))
            time.sleep(2 ** attempt)
            continue

        try:
            data = r.json()
        except:
            raise HTTPException(status_code=500, detail="Bukan JSON")
        if not isinstance(data, dict):
            raise HTTPException(status_code=500, detail="Format aneh")
        if data.get("status", {}).get("code") != 0:
            raise HTTPException(502, data.get("status", {}).get("message", "Error"))
        
        return data.get("data", {})

app = FastAPI(title="Viu API", description="Protected API")

@app.get("/")
def root(): return {"status": "ok", "message": "API aktif."}

# --- ENDPOINTS ---

@app.get("/api/health", dependencies=[Depends(verify_api_key)])
def health():
    try:
        _bearer()
        return {"ok": True, "status": "Token siap"}
    except Exception as e:
        return {"ok": False, "error": str(e)}

@app.get("/api/search", dependencies=[Depends(verify_api_key)])
def search(q: str):
    ckey = f"viu:search:{hashlib.md5(q.encode()).hexdigest()}"
    cached = kv_get(ckey)
    if cached: return json.loads(cached)

    token = _bearer()
    headers = {
        "sec-ch-ua-platform": '"Android"',
        "authorization": f"Bearer {token}",
        "user-agent": UA,
        "accept": "application/json, text/plain, */*",
        "origin": "https://www.viu.com",
        "referer": "https://www.viu.com/",
    }
    
    keyword_encoded = quote(q)
    url = f"{BASE}?platform_flag_label=web&area_id=1000&language_flag_id=8&ut=0&platformFlagLabel=web&areaId=1000&languageFlagId=8&countryCode=ID&r=%2Fsearch%2Fprediction&keyword={keyword_encoded}"

    # Spoof IP (Trik andalan)
    fake_ip = f"114.122.{random.randint(1,255)}.{random.randint(1,255)}"
    headers_spoof = {**headers, "x-forwarded-for": fake_ip, "x-real-ip": fake_ip}
    
    try:
        with Session(impersonate="chrome") as s:
            r = s.get(url, headers=headers_spoof, timeout=10)
            data = r.json()
            if data.get("server", {}).get("area", {}).get("country", {}).get("code") == "ID":
                result = {
                    "query": q, "method": "spoofed",
                    "series": data.get("data", {}).get("series", []),
                    "actor": data.get("data", {}).get("actor", [])
                }
                kv_set(ckey, json.dumps(result, ensure_ascii=False), ex=3600)
                return result
    except Exception: pass

    return {"query": q, "series": [], "actor": [], "note": "Spoof gagal, silakan coba lagi."}

@app.get("/api/home", dependencies=[Depends(verify_api_key)])
def home():
    ckey = "viu:home"
    cached = kv_get(ckey)
    if cached: return json.loads(cached)
    
    try:
        with Session(impersonate="chrome") as s:
            r = s.get(VIU_HOME, headers={"user-agent": UA}, timeout=15)
            if r.status_code == 200:
                series_ids = list(set(re.findall(r'"series_id":\s*"(\d+)"', r.text)))
                fallback_list = [{"series_id": sid} for sid in series_ids[:20]]
                result = {"status": "success", "method": "html_scrape", "data": fallback_list}
                if fallback_list:
                    kv_set(ckey, json.dumps(result, ensure_ascii=False), ex=1800)
                    return result
    except Exception: pass
    return {"status": "error", "message": "Gagal ambil data home."}

@app.get("/api/series/{series_id}", dependencies=[Depends(verify_api_key)])
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

@app.get("/api/play/{product_id}", dependencies=[Depends(verify_api_key)])
def play(product_id: str, series_id: str = None):
    params = {"product_id": product_id}
    if series_id: params["series_id"] = series_id
    ckey = f"viu:play:{product_id}"
    cached = kv_get(ckey)
    if cached: return json.loads(cached)
    data = _call("/vod/product/play", params)
    kv_set(ckey, json.dumps(data, ensure_ascii=False), ex=1800)
    return data
