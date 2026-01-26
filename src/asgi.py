from workers import Response

async def fetch(app, request, env):
    """
    Production ASGI Adapter for Cloudflare Workers.
    Translates Worker Request -> ASGI Scope -> Worker Response.
    """
    
    # 1. Prepare Headers (Handle both Python dict and JS Headers object)
    headers = []
    try:
        if hasattr(request.headers, "items"):
            source = request.headers.items()
        elif hasattr(request.headers, "entries"):
            source = request.headers.entries()
        else:
            source = []
        
        for k, v in source:
            headers.append([str(k).lower().encode("latin-1"), str(v).encode("latin-1")])
    except Exception:
        pass

    # 2. Parse URL
    # We manually parse because urllib can be slow/quirky in some edge runtimes
    url_str = str(request.url)
    if "?" in url_str:
        path, query = url_str.split("?", 1)
        query = query.encode("utf-8")
    else:
        path = url_str
        query = b""
        
    # Strip scheme/host to get raw path
    # (Simplified for robustness; standard urlparse also works)
    if "://" in path:
        path = "/" + path.split("://", 1)[1].split("/", 1)[1]
    
    # 3. Build ASGI Scope
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.3"},
        "http_version": "1.1",
        "method": request.method,
        "scheme": "https",
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": query,
        "root_path": "",
        "headers": headers,
        "client": ("127.0.0.1", 0),
        "server": ("cloudflare", 443),
        "extensions": {"http.response.push": {}},
        "state": {"env": env}, # Inject Env for FastAPI access
    }

    # 4. Read Body
    try:
        body = await request.bytes()
    except Exception:
        body = b""

    # 5. ASGI Communication Channels
    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    response_store = {"status": 500, "headers": [], "body": b""}

    async def send(message):
        if message["type"] == "http.response.start":
            response_store["status"] = message["status"]
            # Convert headers back to list of tuples for compatibility
            response_store["headers"] = message.get("headers", [])
        elif message["type"] == "http.response.body":
            response_store["body"] += message.get("body", b"")

    # 6. Run App
    try:
        await app(scope, receive, send)
    except Exception as e:
        print(f"ASGI Error: {e}")
        return Response("Internal Server Error", status=500)

    # 7. Convert Headers for Cloudflare Response
    # Cloudflare expects a dict or JS Headers object, not list of byte-tuples
    final_headers = {}
    for k, v in response_store["headers"]:
        final_headers[k.decode("latin-1")] = v.decode("latin-1")

    return Response(
        response_store["body"],
        status=response_store["status"],
        headers=final_headers
    )
