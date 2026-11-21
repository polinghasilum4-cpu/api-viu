# api/index.py — Viu Auto API (Proxy Rotator Edition)
import os, json, hashlib, logging, re, random
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query
from curl_cffi import requests as creq
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

# --- AUTO HARVEST DENGAN PROXY ROTATOR ---
def auto_harvest_token() -> str:
    log.info("Nyari token baru pake rotasi proxy...")
    
    # Ambil daftar proxy gratis dari GitHub
    proxies_list = []
    try:
        r = httpx.get("https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt", timeout=10)
        proxies_list = r.text.splitlines()
        random.shuffle(proxies_list) # Acak biar dapet IP beda-beda
        proxies_list = proxies_list[:30] # Coba 30 proxy pertama
    except:
        pass
        
    headers = {"user-agent": UA, "accept-language": "id-ID,id;q=0.9"}
    
    for proxy in proxies_list:
        proxy = proxy.strip()
        if not proxy: continue
        log.info(f"Coba proxy: {proxy}")
        try:
            px = {"http": f"http://{proxy}", "https": f"http://{proxy}"}
            with creq.Session(impersonate="chrome", proxies=px) as s:
                # Tembak halaman depan Viu
                r1 = s.get(VIU_HOME, headers=headers, timeout=8)
                if "not available in your country" not in r1.text:
                    m = re.search(r'(eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+)', r1.text)
                    if m:
                        log.info(f"Sukses dapet token pake proxy {proxy}!")
                        return m.group(1)
                
                # Tembak API Guest
                guest_url = "https://api-gateway-global.viu.com/api/mobile?r=/vod/login/guest&platform_flag_label=web&area_id=1000&language_flag_id=8&ut=0&countryCode=ID"
                r2 = s.get(guest_url, headers=headers, timeout=8)
                if r2.status_code == 200:
                    tok = r2.json().get("data", {}).get("token")
                    if tok:
                        log.info(f"Sukses dapet token pake proxy {proxy}!")
                        return tok
        except Exception:
            continue

    # Kalau semua proxy mati, pake token asli dari lo (siapa tau masih idup)
    return "eyJhbGciOiJBMTI4S1ciLCJlbmMiOiJBMTI4Q0JDLUhTMjU2In0.Hv2g6GGzyq-g35coFi7tGkUpXhtMZD5c2pnozynjhD6w7kUqIlFQoA.1ZF-h1ORbdpl6__sxza2Iw.N3YrDC25A1n25LaOAiLj3AycPUxM9ryiCHfInFRw8objq39NOCxfsC67gw0_q4rEPM8YYuVakwe1n55S8xuPcqQSx5f0z-7h1ZrFS_ABQKUDxnwHmw4L3qF1Nsmi_66CAzAwulCzs3o0ro2uR1Dc2fMM5yrfUDbe_davrV9ekRVwYkEZ0So2KqQb8ehd8mpmPPDaccpnw9x9U5PS_MImNma8710Ib6QW68vrLXRDjnQ.QCaYCMW4Cnk8hSSM0eUZMQ"

def _bearer() -> str:
    b = kv_get("viu:bearer")
    if b: return b
    b = auto_harvest_token()
    if b:
        kv_set("viu:bearer", b, ex=43200) # Simpen 12 jam
    return b

def _call(route: str, params: Optional[Dict] = None) -> Any:
    q = {**COMMON, "r": route}
    if params: q.update(params)
    headers = {"accept": "application/json, text/plain, */*", "authorization": f"Bearer {_bearer()}", "user-agent": UA, "origin": "https://www.viu.com", "referer": "https://www.viu.com/"}
    
    with creq.Session(impersonate="chrome") as s:
        r = s.get(BASE, params=q, headers=headers, timeout=15)
        
    if r.status_code in (401, 403):
        kv_set("viu:bearer", "", ex=1)
        headers["authorization"] = f"Bearer {auto_harvest_token()}"
        with creq.Session(impersonate="chrome") as s:
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
    data = _call("/vod/product-list", {"q": q, "size": size, "sort": "desc"})
    kv_set(ckey, json.dumps(data, ensure_ascii=False), ex=3600)
    return data

@app.get("/api/browse")
def browse(category: str = Query(None), page: int = 1, size: int = 20, sort: str = "desc", filter_id: int = None):
    params = {"size": size, "sort": sort, "start": (page - 1) * size}
    if category: params["category"] = category
    if filter_id: params["filter_id"] = filter_id
    h = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
    ckey = f"viu:browse:{h}"
    cached = kv_get(ckey)
    if cached: return json.loads(cached)
    data = _call("/vod/product-list", params)
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
