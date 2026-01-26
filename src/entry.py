# src/entry.py
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, HttpUrl
from workers import WorkerEntrypoint, fetch
import json
import asyncio
import asgi

# --- App Definition ---
app = FastAPI(title="PIJ Scanner", version="2.0.0")

# CORS for Production (Adjust allow_origins for strict security)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

# --- Models ---
class ScanRequest(BaseModel):
    url: str # Using str to allow manual validation or partial URLs

# --- Helpers ---
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

async def cf_fetch(path: str, method: str, env, body: dict = None, cache_ttl: int = 0):
    """
    Robust fetcher for Cloudflare APIs with error handling.
    """
    url = f"{CLOUDFLARE_API_BASE}/accounts/{env.CLOUDFLARE_ACCOUNT_ID}/{path}"
    headers = {
        "Authorization": f"Bearer {env.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json"
    }

    try:
        # Note: In a real worker, you might use the Cache API here for 'GET' requests
        # if cache_ttl > 0. For now, we fetch direct.
        res = await fetch(
            url, 
            method=method, 
            headers=headers, 
            body=json.dumps(body) if body else None
        )
        
        if res.status_code >= 400:
            error_data = await res.text()
            print(f"[API Error] {path}: {res.status_code} - {error_data}")
            # Map 404 specifically for pending scans
            if res.status_code == 404 and "result" in path:
                 raise HTTPException(status_code=404, detail="Scan not found or pending")
            raise HTTPException(status_code=502, detail="Upstream API Error")
            
        return await res.json()
    except Exception as e:
        if isinstance(e, HTTPException): raise e
        print(f"[System Error] {e}")
        raise HTTPException(status_code=500, detail="Internal Service Error")

# --- Endpoints ---

@app.get("/api/health")
async def health():
    return {"status": "operational", "system": "PIJ-Scanner v2"}

@app.post("/api/scan")
async def submit_scan(payload: ScanRequest, request: Request):
    """
    Initialize a URL scan.
    Cloudflare URL Scanner - Scan Submission
    """
    env = request.scope["env"]
    
    # 1. Submit to Cloudflare Scanner
    try:
        # Force public scan for visibility, customize screenshots
        data = {
            "url": payload.url,
            "visibility": "public",
            "screenshots": {"resolutions": ["desktop", "mobile"]}
        }
        result = await cf_fetch("urlscanner/v2/scan", "POST", env, data)
        return {"id": result["result"]["uuid"], "url": payload.url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/scan/{scan_id}")
async def get_scan_report(scan_id: str, request: Request):
    """
    Retrieve scan results and perform parallel enrichment with Intel APIs.
    Cloudflare URL Scanner - Result
    Cloudflare Intel - ASN/IP/Domain
    """
    env = request.scope["env"]
    
    # 1. Get Base Scan Result
    try:
        base_data = await cf_fetch(f"urlscanner/v2/result/{scan_id}", "GET", env)
    except HTTPException as e:
        if e.status_code == 404:
            return JSONResponse({"status": "pending"}, status_code=202)
        raise e

    scan_result = base_data["result"]
    page_data = scan_result.get("page", {})
    
    # 2. Intelligence Enrichment (Parallel)
    # We gather IP and ASN data to build a "Threat Profile"
    enrichment_tasks = []
    keys = []

    if asn := page_data.get("asn"):
        keys.append("asn")
        enrichment_tasks.append(cf_fetch(f"intel/asn/{asn}", "GET", env))
    
    # Note: Domain enrichment often requires higher plans, handled gracefully
    if domain := page_data.get("domain"):
        keys.append("domain")
        enrichment_tasks.append(cf_fetch(f"intel/domain?domain={domain}", "GET", env))

    enrichment_data = {}
    if enrichment_tasks:
        results = await asyncio.gather(*enrichment_tasks, return_exceptions=True)
        for i, res in enumerate(results):
            if isinstance(res, dict) and "result" in res:
                enrichment_data[keys[i]] = res["result"]
            else:
                enrichment_data[keys[i]] = None

    # 3. Construct "Professional" Response
    return {
        "status": "complete",
        "meta": {
            "id": scan_result["task"]["uuid"],
            "time": scan_result["task"]["time"],
            "url": scan_result["task"]["url"]
        },
        "verdict": {
            "malicious": scan_result["verdicts"]["overall"]["malicious"],
            "categories": scan_result["verdicts"]["overall"].get("categories", []),
            "score": scan_result["verdicts"]["overall"].get("score", 0)
        },
        "network": {
            "asn": page_data.get("asn"),
            "asn_org": page_data.get("asnname"),
            "server": page_data.get("server"),
            "ip": page_data.get("ip"),
            "country": page_data.get("country"),
            "intel": enrichment_data
        },
        "tech": scan_result.get("features", {}).get("technologies", []),
        "requests": scan_result.get("data", {}).get("requests", [])[:50] # Limit payload size
    }

# --- Worker Adapter ---
class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(app, request.js_object, self.env)
