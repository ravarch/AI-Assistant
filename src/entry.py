from workers import WorkerEntrypoint, Response
from pyodide.ffi import to_js
from urllib.parse import urlparse
import json
import base64
import datetime

class Default(WorkerEntrypoint):
    async def fetch(self, request):
        url = urlparse(request.url)
        method = request.method

        # ---------------------------------------------------------
        # API: Deep Research & Prompt Engineering
        # ---------------------------------------------------------
        if url.path == "/api/research" and method == "POST":
            try:
                body = await request.json()
                user_concept = body.get("prompt", "")
                
                # Chain-of-Thought System Prompt
                system_prompt = (
                    "You are an expert Visual Research Agent. Your goal is to prepare a deep, "
                    "detailed visual briefing for an image generation model (Flux).\n"
                    "Step 1: Analyze the user's concept for historical accuracy, artistic style, and lighting physics.\n"
                    "Step 2: Create a rich, descriptive prompt based on this analysis.\n"
                    "Output format: JSON with keys 'analysis' (brief research notes) and 'prompt' (the final image prompt)."
                )

                response = await self.env.AI.run(
                    "@cf/meta/llama-3.3-70b-instruct-fp8-fast", # Using Llama 3.3 for better reasoning
                    to_js({
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": f"Research and refine this concept: {user_concept}"}
                        ],
                        "response_format": {"type": "json_object"}
                    })
                )
                
                # Parse the JSON output from the LLM
                llm_raw = response.get("response") or "{}"
                data = json.loads(llm_raw)
                
                return Response(json.dumps(data), headers={"Content-Type": "application/json"})
            except Exception as e:
                # Fallback if LLM fails
                return Response(json.dumps({
                    "analysis": "Direct pass (LLM Error)", 
                    "prompt": user_concept
                }), headers={"Content-Type": "application/json"})

        # ---------------------------------------------------------
        # API: Multi-Model Generation
        # ---------------------------------------------------------
        elif url.path == "/api/generate" and method == "POST":
            try:
                body = await request.json()
                prompt = body.get("prompt", "")
                steps = body.get("steps", 4)
                model_key = body.get("model", "schnell")
                
                # Map shorthand to full model IDs
                models = {
                    "schnell": "@cf/black-forest-labs/flux-1-schnell",
                    "dev": "@cf/black-forest-labs/flux-2-dev",
                    "klein": "@cf/black-forest-labs/flux-2-klein-4b"
                }
                selected_model = models.get(model_key, models["schnell"])

                # Execute Inference
                result = await self.env.AI.run(
                    selected_model,
                    to_js({
                        "prompt": prompt,
                        "num_steps": steps, # Note: 'num_steps' or 'steps' depending on model, 'steps' is safer generic
                        "steps": steps 
                    })
                )
                
                # Handle Base64 Image
                b64_image = result.get("image")
                if not b64_image:
                    raise Exception("No image returned from model")

                # Store in R2
                image_bytes = base64.b64decode(b64_image)
                filename = f"flux-{model_key}-{datetime.datetime.now().timestamp()}.jpg"
                
                await self.env.BUCKET.put(filename, image_bytes, to_js({
                    "httpMetadata": {"contentType": "image/jpeg"}
                }))

                # Construct Public URL (Proxy via this worker)
                # In production, enable public access on R2 and use: https://<bucket>.r2.dev/<filename>
                public_url = f"/image/{filename}"
                
                return Response(
                    json.dumps({
                        "url": public_url,
                        "model": selected_model
                    }), 
                    headers={"Content-Type": "application/json"}
                )
            except Exception as e:
                return Response(json.dumps({"error": str(e)}), status=500, headers={"Content-Type": "application/json"})

        # ---------------------------------------------------------
        # API: Image Proxy (Public URL Handler)
        # ---------------------------------------------------------
        elif url.path.startswith("/image/"):
            key = url.path.split("/image/")[1]
            try:
                object = await self.env.BUCKET.get(key)
                if object is None:
                    return Response("Image not found", status=404)
                
                headers = {
                    "Content-Type": "image/jpeg",
                    "Cache-Control": "public, max-age=31536000"
                }
                return Response(object.body, headers=headers)
            except Exception as e:
                return Response("Error fetching image", status=500)

        # ---------------------------------------------------------
        # Static Asset Fallback
        # ---------------------------------------------------------
        try:
            return await self.env.ASSETS.fetch(request)
        except Exception:
            return Response("Not Found", status=404)
