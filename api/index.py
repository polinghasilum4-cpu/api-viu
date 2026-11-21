# api/index.py — Viu API, on-demand + Cloudflare KV
import os, json, hashlib, logging
from typing import Optional, Dict, Any
from fastapi import FastAPI, HTTPException, Query, Request, Header
from curl_cffi import requests as creq
import httpx

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("viu")

BASE = "https://api-gateway-global.viu.com/api/mobile"
COMMON = {
    "platform_flag_label": "web", "area_id": "1000",
    "language_flag_id": "8", "ut": "0",
    "platformFlagLabel": "web", "areaId": "1000",
    "languageFlagId": "8", "countryCode": "ID", "os_flag_id": "1",
}
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")

CF_ACCOUNT = os.environ.get("CF_ACCOUNT_ID", "")
CF_KV_ID = os.environ.get("CF_KV_NAMESPACE_ID", "")
CF_TOKEN = os.environ.get("CF_KV_TOKEN", "")
CF_BASE = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT}/storage/kv/namespaces/{CF_KV_ID}"

def kv_get(key: str) -> Optional[str]:
    if not (CF_ACCOUNT and CF_KV_ID and CF_TOKEN):
        return None
    try:
        with httpx.Client(timeout=5) as c:
            r = c.get(f"{CF_BASE}/values/{key}",
                      headers={"Authorization": f"Bearer {CF_TOKEN}"})
        if r.status_code == 404:
            return None
        r.raise_for_status()
        return r.text
    except Exception as e:
        log.warning("kv_get %s: %s", key, e)
        return None

def kv_set(key: str, value: str, ex: int = 3600) -> None:
    if not (CF_ACCOUNT and CF_KV_ID and CF_TOKEN):
        return
    try:
        with httpx.Client(timeout=5) as c:
            r = c.put(f"{CF_BASE}/values/{key}",
                      headers={"Authorization": f"Bearer {CF_TOKEN}"},
                      content=value,
                      params={"expiration_ttl": str(ex)})
        r.raise_for_status()
    except Exception as e:
        log.warning("kv_set %s: %s", key, e)

def _bearer() -> str:
    b = kv_get("viu:bearer")
    if b:
        return b
    b = os.environ.get("VIU_BEARER", "").strip()
    if not b:
        raise HTTPException(500, "bearer kosong — harvest belum jalan")
    return b

def _call(route: str, params: Optional[Dict] = None) -> Any:
    q = {**COMMON, "r": route}
    if params:
        q.update(params)
    headers = {
        "accept": "application/json, text/plain, */*",
        "authorization": f"Bearer {_bearer()}",
        "user-agent": UA,
        "origin": "https://www.viu.com",
        "referer": "https://www.viu.com/",
    }
    with creq.Session(impersonate="chrome") as s:
        r = s.get(BASE, params=q, headers=headers, timeout=15)
    if r.status_code in (401, 403):
        raise HTTPException(401, "bearer expired — tunggu harvest berikutnya")
    r.raise_for_status()
    data = r.json()
    if data.get("status", {}).get("code") != 0:
        raise HTTPException(502, f"viu error: {data.get('status')}")
    return data.get("data", {})

app = FastAPI(title="Viu API")

@app.get("/api/health")
def health():
    b = kv_get("viu:bearer") or os.environ.get("VIU_BEARER", "")
    return {"ok": bool(b), "bearer_set": bool(b),
            "kv_configured": bool(CF_ACCOUNT and CF_KV_ID and CF_TOKEN)}

@app.post("/api/_internal/bearer")
async def update_bearer(request: Request, x_harvest_secret: str = Header(None)):
    expected = os.environ.get("HARVEST_SECRET", "")
    if not expected or not x_harvest_secret:
        raise HTTPException(401, "secret wajib")
    if not hashlib.compare_digest(x_harvest_secret, expected):
        raise HTTPException(401, "secret salah")
    body = await request.json()
    token = (body.get("bearer") or "").strip()
    if not token.startswith("eyJ"):
        raise HTTPException(400, "format bearer salah")
    kv_set("viu:bearer", token, ex=25 * 3600)
    log.info("bearer baru, len=%d", len(token))
    return {"ok": True, "len": len(token)}

@app.get("/api/series/{series_id}")
def series(series_id: str, refresh: bool = False):
    ckey = f"viu:series:{series_id}"
    if not refresh:
        cached = kv_get(ckey)
        if cached:
            return json.loads(cached)
    data = _call("/vod/product-list", {
        "series_id": series_id, "size": -1, "sort": "desc",
    })
    plist = sorted(data.get("product_list", []),
                   key=lambda x: int(x.get("number", 0)))
    result = {"series_id": series_id, "episodes": plist}
    kv_set(ckey, json.dumps(result, ensure_ascii=False), ex=21600)
    return result

@app.get("/api/episodes/{series_id}")
def episodes(series_id: str):
    return series(series_id)["episodes"]

@app.get("/api/search")
def search(q: str, size: int = 20):
    ckey = f"viu:search:{hashlib.md5(q.encode()).hexdigest()}:{size}"
    cached = kv_get(ckey)
    if cached:
        return json.loads(cached)
    data = _call("/vod/product-list", {"q": q, "size": size, "sort": "desc"})
    kv_set(ckey, json.dumps(data, ensure_ascii=False), ex=3600)
    return data

@app.get("/api/browse")
def browse(category: str = Query(None), page: int = 1,
           size: int = 20, sort: str = "desc", filter_id: int = None):
    params = {"size": size, "sort": sort, "start": (page - 1) * size}
    if category:
        params["category"] = category
    if filter_id:
        params["filter_id"] = filter_id
    h = hashlib.md5(json.dumps(params, sort_keys=True).encode()).hexdigest()
    ckey = f"viu:browse:{h}"
    cached = kv_get(ckey)
    if cached:
        return json.loads(cached)
    data = _call("/vod/product-list", params)
    kv_set(ckey, json.dumps(data, ensure_ascii=False), ex=1800)
    return data

@app.get("/api/home")
def home():
    cached = kv_get("viu:home")
    if cached:
        return json.loads(cached)
    data = _call("/vod/home")
    kv_set("viu:home", json.dumps(data, ensure_ascii=False), ex=1800)
    return data

@app.get("/api/play/{product_id}")
def play(product_id: str, series_id: str = None):
    params = {"product_id": product_id}
    if series_id:
        params["series_id"] = series_id
    ckey = f"viu:play:{product_id}"
    cached = kv_get(ckey)
    if cached:
        return json.loads(cached)
    data = _call("/vod/product/play", params)
    kv_set(ckey, json.dumps(data, ensure_ascii=False), ex=1800)
    return data
