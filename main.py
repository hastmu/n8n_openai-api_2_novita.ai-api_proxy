import os
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.responses import StreamingResponse
from dotenv import load_dotenv

load_dotenv()

NOVITA_API_KEY = os.getenv("NOVITA_API_KEY")
UPSTREAM_BASE_URL = os.getenv("UPSTREAM_BASE_URL")

app = FastAPI(title="Novita OpenAI Proxy")

# ---------- Helpers ----------
async def get_json_body(req: Request):
    if req.method in ("POST", "PUT", "PATCH"):
        body = await req.body()
        if body:
            return await req.json()
    return None

def build_headers(request: Request):
    headers = {
        "Content-Type": "application/json",
    }

    # 1️⃣ Prefer client-provided Authorization (n8n / OpenAI SDK)
    auth = request.headers.get("authorization")
    if auth:
        headers["Authorization"] = auth
    else:
        headers["Authorization"] = f"Bearer {NOVITA_API_KEY}"

    return headers
def normalize_responses_body(body: dict) -> dict:
    input_text = ""

    if isinstance(body.get("input"), str):
        input_text = body["input"]

    elif isinstance(body.get("input"), list):
        for item in body["input"]:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    input_text += content + "\n"
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            input_text += c.get("text", "") + "\n"

    elif "messages" in body:
        for m in body["messages"]:
            if m.get("role") == "user":
                input_text += m.get("content", "") + "\n"

    normalized = {
        "model": body.get("model"),
        "input": input_text.strip(),
    }

    if body.get("stream") is True:
        normalized["stream"] = True

    if "temperature" in body:
        normalized["temperature"] = body["temperature"]

    # 🔥 CRITICAL FIX
    if "max_output_tokens" in body:
        normalized["max_tokens"] = body["max_output_tokens"]

    return normalized

async def stream_upstream(req: Request, path: str):
    json_body = await get_json_body(req)

    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            method=req.method,
            url=f"{UPSTREAM_BASE_URL}{path}",
            headers=build_headers(req),
            json=json_body,
        ) as upstream:
            async for chunk in upstream.aiter_raw():
                yield chunk


async def forward_request(req: Request, path: str):
    json_body = await get_json_body(req)

    async with httpx.AsyncClient() as client:
        resp = await client.request(
            method=req.method,
            url=f"{UPSTREAM_BASE_URL}{path}",
            headers=build_headers(req),
            json=json_body,
        )

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )

async def forward_request_custom(req: Request, path: str, body: dict):
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{UPSTREAM_BASE_URL}{path}",
            headers=build_headers(req),
            json=body,
        )

        if resp.status_code >= 400:
            print("NOVITA ERROR STATUS:", resp.status_code)
            print("NOVITA ERROR BODY:", resp.text)

        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )

async def stream_upstream_custom(req: Request, path: str, body: dict):
    async with httpx.AsyncClient(timeout=None) as client:
        async with client.stream(
            "POST",
            f"{UPSTREAM_BASE_URL}{path}",
            headers=build_headers(req),
            json=body,
        ) as upstream:
            async for chunk in upstream.aiter_raw():
                yield chunk

def responses_to_chat(body: dict) -> dict:
    messages = []

    if isinstance(body.get("input"), str):
        messages.append({"role": "user", "content": body["input"]})

    elif isinstance(body.get("input"), list):
        for item in body["input"]:
            if isinstance(item, dict):
                content = item.get("content")
                if isinstance(content, str):
                    messages.append({"role": "user", "content": content})
                elif isinstance(content, list):
                    text = ""
                    for c in content:
                        if c.get("type") == "text":
                            text += c.get("text", "")
                    messages.append({"role": "user", "content": text})

    payload = {
        "model": body["model"],
        "messages": messages,
        "temperature": body.get("temperature", 0.7),
        "stream": body.get("stream", False),
    }

    if "max_output_tokens" in body:
        payload["max_tokens"] = body["max_output_tokens"]

    return payload

async def call_chat_completions(req: Request, payload: dict):
    async with httpx.AsyncClient(timeout=None) as client:
        return await client.post(
            f"{UPSTREAM_BASE_URL}/v1/chat/completions",
            headers=build_headers(req),
            json=payload,
        )

def chat_to_responses(chat_resp: dict) -> dict:
    text = chat_resp["choices"][0]["message"]["content"]

    return {
        "id": chat_resp.get("id"),
        "object": "response",
        "output": [{
            "id": "msg-0",
            "type": "message",
            "role": "assistant",
            "content": [{
                "type": "output_text",
                "text": text
            }]
        }]
    }



# ---------- OpenAI-Compatible Endpoints ----------
@app.post("/v1/responses")
async def responses(request: Request):
    body = await request.json()
    chat_payload = responses_to_chat(body)

    resp = await call_chat_completions(request, chat_payload)

    if resp.status_code >= 400:
        return Response(
            content=resp.content,
            status_code=resp.status_code,
            media_type=resp.headers.get("content-type"),
        )

    chat_json = resp.json()
    response_json = chat_to_responses(chat_json)

    return response_json


@app.post("/v1/chat/completions")
async def chat_completions(request: Request):
    body = await request.json()

    # Streaming support (required by n8n)
    if body.get("stream") is True:
        return StreamingResponse(
            stream_upstream(request, "/v1/chat/completions"),
            media_type="text/event-stream",
        )

    return await forward_request(request, "/v1/chat/completions")


@app.post("/v1/completions")
async def completions(request: Request):
    return await forward_request(request, "/v1/completions")


@app.post("/v1/embeddings")
async def embeddings(request: Request):
    return await forward_request(request, "/v1/embeddings")


@app.get("/v1/models")
async def models(request: Request):
    return await forward_request(request, "/v1/models")

@app.api_route("/v1/{path:path}", methods=["GET", "POST"])

# ---------- Health ----------

@app.get("/health")
def health():
    return {"status": "ok"}
