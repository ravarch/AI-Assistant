# src/entry.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from workers import WorkerEntrypoint, Response, fetch
import json
import asyncio

# Initialize FastAPI
app = FastAPI()

# Configuration
CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

async def cf_api_fetch(path: str, method: str, env, body: dict = None):
    """Helper to make authenticated requests to Cloudflare API."""
    url = f"{CLOUDFLARE_API_BASE}/accounts/{env.CLOUDFLARE_ACCOUNT_ID}/{path}"
    headers = {
        "Authorization": f"Bearer {env.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # workers.fetch expects a dict for headers if not using Headers object
    response = await fetch(
        url, 
        method=method, 
        headers=headers, 
        body=json.dumps(body) if body else None
    )
    
    if response.status_code >= 400:
        error_text = await response.text()
        print(f"Cloudflare API Error ({path}): {error_text}")
        raise HTTPException(status_code=response.status_code, detail=f"Cloudflare API Error: {error_text}")
        
    return await response.json()

@app.post("/api/scan")
async def start_scan(request: Request):
    """Submit a URL for scanning."""
    body = await request.json()
    target_url = body.get("url")
    
    if not target_url:
        raise HTTPException(status_code=400, detail="URL is required")

    # Call Cloudflare URL Scanner API
    # Submit URL to scan endpoint
    result = await cf_api_fetch("urlscanner/v2/scan", "POST", request.scope["env"], {"url": target_url})
    
    return {"scan_id": result["result"]["uuid"]}

@app.get("/api/scan/{scan_id}")
async def get_scan_result(scan_id: str, request: Request):
    """Poll for scan results and enrich with Intel data."""
    env = request.scope["env"]
    
    # Get URL Scan Result
    # Note: If scan is running, this might return 404 or incomplete data. 
    # Frontend should handle polling logic.
    try:
        scan_data = await cf_api_fetch(f"urlscanner/v2/result/{scan_id}", "GET", env)
    except HTTPException as e:
        if e.status_code == 404:
            return JSONResponse({"status": "pending"}, status_code=202)
        raise e

    result = scan_data["result"]
    page = result.get("page", {})
    
    # Intel Enrichment (Parallel Fetching)
    enrichment = {}
    
    # Prepare enrichment tasks
    tasks = []
    
    # Intel ASN
    if asn := page.get("asn"):
         tasks.append(cf_api_fetch(f"intel/asn/{asn}", "GET", env))
    else:
        tasks.append(asyncio.sleep(0)) # No-op

    # Intel IP
    if ip := page.get("ip"):
        tasks.append(cf_api_fetch(f"intel/ip/{ip}", "GET", env))
    else:
        tasks.append(asyncio.sleep(0)) # No-op
        
    # Execute enrichment
    # Note: In a real production app, use asyncio.gather. 
    # For simple Python workers, sequential is safer if async support is limited, 
    # but asyncio is supported.
    try:
        intel_results = await asyncio.gather(*tasks, return_exceptions=True)
        
        if isinstance(intel_results[0], dict):
            enrichment["asn"] = intel_results[0].get("result")
        if isinstance(intel_results[1], dict):
            enrichment["ip"] = intel_results[1].get("result")
            
    except Exception as e:
        print(f"Enrichment failed: {e}")

    # Construct final response
    return {
        "status": "complete",
        "scan": result,
        "enrichment": enrichment
    }

# Worker Entrypoint Adapter for FastAPI
import asgi

class Default(WorkerEntrypoint):
    async def fetch(self, request):
        # Inject env into request scope for access in endpoints
        return await asgi.fetch(app, request.js_object, self.env)
