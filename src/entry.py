# src/entry.py
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from workers import WorkerEntrypoint, fetch as worker_fetch
import json
import asyncio
import asgi # Import our local adapter

# --- App Configuration ---
app = FastAPI(title="PIJ Scanner", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

class ScanRequest(BaseModel):
    url: str

CLOUDFLARE_API_BASE = "https://api.cloudflare.com/client/v4"

# --- Helper Functions ---
async def cf_api_call(path: str, method: str, env, body: dict = None):
    url = f"{CLOUDFLARE_API_BASE}/accounts/{env.CLOUDFLARE_ACCOUNT_ID}/{path}"
    headers = {
        "Authorization": f"Bearer {env.CLOUDFLARE_API_TOKEN}",
        "Content-Type": "application/json"
    }
    
    # Use the worker_fetch imported from 'workers'
    response = await worker_fetch(
        url, 
        method=method, 
        headers=headers, 
        body=json.dumps(body) if body else None
    )
    
    if response.status_code >= 400:
        text = await response.text()
        print(f"CF API Error: {text}")
        if response.status_code == 404 and "result" in path:
            return None # Handle pending/missing specifically
        raise HTTPException(status_code=502, detail="Upstream Provider Error")
        
    return await response.json()

# --- Routes ---
@app.get("/api/health")
async def health():
    return {"status": "operational", "runtime": "Cloudflare Python Workers"}

@app.post("/api/scan")
async def submit_scan(req: Request, payload: ScanRequest):
    env = req.scope["state"]["env"] # Access env from ASGI scope
    
    try:
        res = await cf_api_call("urlscanner/v2/scan", "POST", env, {
            "url": payload.url,
            "visibility": "public",
            "screenshots": {"resolutions": ["desktop", "mobile"]}
        })
        return {"id": res["result"]["uuid"], "url": payload.url}
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

@app.get("/api/scan/{scan_id}")
async def get_scan(req: Request, scan_id: str):
    env = req.scope["state"]["env"]
    
    # Fetch Scan
    scan_data = await cf_api_call(f"urlscanner/v2/result/{scan_id}", "GET", env)
    
    if not scan_data:
        # 202 Accepted indicates processing
        return {"status": "pending"}
        
    result = scan_data["result"]
    page = result.get("page", {})
    
    # Parallel Enrichment (Optional/Demo)
    # In production, check if bindings exist before calling
    enrichment = {}
    
    return {
        "status": "complete",
        "verdict": result["verdict"],
        "meta": result["task"],
        "network": {
            "asn": page.get("asn"),
            "ip": page.get("ip"),
            "country": page.get("country")
        }
    }

# --- Entrypoint ---
class Default(WorkerEntrypoint):
    async def fetch(self, request):
        return await asgi.fetch(app, request, self.env)
