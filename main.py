import os
from urllib.parse import urljoin
from typing import Optional

import httpx
import requests
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_FILE = os.path.join(BASE_DIR, "metabase-ai-agent.html")

REMOTE_METABASE_BASE = os.getenv("REMOTE_METABASE_BASE", "https://anandtannak-metabase.hf.space")
METABASE_ADMIN_EMAIL = os.getenv("METABASE_ADMIN_EMAIL", "tannaanand992@gmail.com")
METABASE_ADMIN_PASSWORD = os.getenv("METABASE_ADMIN_PASSWORD", "tanna_anand_1")
GROQ_API_KEY = os.getenv("GROQ_API_KEY", "")  # set this in your .env or environment
LOCAL_PORT = int(os.getenv("PORT", "8000"))

app = FastAPI()
_metabase_cookie_header: Optional[str] = None

if os.path.exists(os.path.join(BASE_DIR, "static")):
    app.mount("/static", StaticFiles(directory=os.path.join(BASE_DIR, "static")), name="static")


def _should_strip_header(name: str) -> bool:
    return name.lower() in {
        "content-security-policy", "content-security-policy-report-only",
        "x-frame-options", "frame-options", "cross-origin-opener-policy",
        "cross-origin-embedder-policy", "content-encoding",
        "content-length", "transfer-encoding", "connection",
    }


def _rewrite_location(location: str) -> str:
    if location.startswith(REMOTE_METABASE_BASE):
        return location.replace(REMOTE_METABASE_BASE, "/metabase", 1)
    return location


def _build_cookie_header(cookie_jar) -> str:
    return "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookie_jar)


def _get_metabase_cookie_header() -> str:
    global _metabase_cookie_header
    if _metabase_cookie_header:
        return _metabase_cookie_header
    response = requests.post(
        f"{REMOTE_METABASE_BASE.rstrip('/')}/api/session",
        json={"username": METABASE_ADMIN_EMAIL, "password": METABASE_ADMIN_PASSWORD},
        timeout=30,
    )
    response.raise_for_status()
    _metabase_cookie_header = _build_cookie_header(response.cookies)
    return _metabase_cookie_header


def _refresh_metabase_cookie_header() -> str:
    global _metabase_cookie_header
    _metabase_cookie_header = None
    return _get_metabase_cookie_header()


async def _metabase_json(client: httpx.AsyncClient, path: str, params: Optional[dict] = None):
    async def _request(cookie_header: str):
        return await client.get(
            f"{REMOTE_METABASE_BASE.rstrip('/')}/{path.lstrip('/')}",
            params=params,
            headers={
                "origin": REMOTE_METABASE_BASE,
                "referer": REMOTE_METABASE_BASE + "/",
                "cookie": cookie_header,
            },
        )

    response = await _request(_get_metabase_cookie_header())
    if response.status_code == 401:
        response = await _request(_refresh_metabase_cookie_header())
    response.raise_for_status()
    return response.json()


@app.get("/", response_class=HTMLResponse)
async def root() -> HTMLResponse:
    with open(HTML_FILE, "r", encoding="utf-8") as f:
        html = f.read()

    # Rewrite Metabase URL to use local proxy
    html = html.replace("https://anandtannak-metabase.hf.space/", "/metabase/")
    html = html.replace("https://anandtannak-metabase.hf.space", "/metabase")

    # ── KEY INJECTION ──
    # Inject Groq key from environment so you never hardcode it in the HTML
    if GROQ_API_KEY:
        html = html.replace(
            "const HARDCODED_KEY = '';",
            f"const HARDCODED_KEY = '{GROQ_API_KEY}';"
        )

    return HTMLResponse(html)


@app.api_route("/metabase", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def metabase_root(request: Request):
    return await proxy_metabase(request)


@app.api_route("/metabase/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy_metabase(request: Request, path: str = ""):
    target_url = urljoin(f"{REMOTE_METABASE_BASE.rstrip('/')}/", path)
    if request.url.query:
        target_url = f"{target_url}?{request.url.query}"

    body = await request.body()
    headers = {
        k: v for k, v in request.headers.items()
        if k.lower() not in {"host","content-length","accept-encoding","origin","referer"}
    }
    headers["origin"] = REMOTE_METABASE_BASE
    headers["referer"] = REMOTE_METABASE_BASE + "/"
    headers["cookie"] = _get_metabase_cookie_header()

    async with httpx.AsyncClient(follow_redirects=False, timeout=60.0) as client:
        upstream = await client.request(request.method, target_url, content=body, headers=headers)

    if upstream.status_code == 401 and path:
        headers["cookie"] = _refresh_metabase_cookie_header()
        async with httpx.AsyncClient(follow_redirects=False, timeout=60.0) as client:
            upstream = await client.request(request.method, target_url, content=body, headers=headers)

    response_headers = {}
    set_cookie_headers = []
    for name, value in upstream.headers.items():
        if _should_strip_header(name): continue
        if name.lower() == "location": value = _rewrite_location(value)
        if name.lower() == "set-cookie": continue
        response_headers[name] = value

    for value in upstream.headers.get_list("set-cookie"):
        value = value.replace("; Secure","").replace(";Secure","")
        value = value.replace("SameSite=None","SameSite=Lax")
        value = value.replace("Domain=.hf.space","").replace("Domain=hf.space","")
        set_cookie_headers.append(value)

    media_type = upstream.headers.get("content-type","text/plain")
    content = upstream.content

    if "text/html" in media_type:
        html = content.decode("utf-8", errors="ignore")
        html = html.replace(REMOTE_METABASE_BASE, "/metabase")
        content = html.encode("utf-8")

    response = Response(
        content=content, status_code=upstream.status_code,
        headers=response_headers, media_type=media_type,
    )
    for cookie_value in set_cookie_headers:
        response.headers.append("set-cookie", cookie_value)
    return response


@app.api_route("/app/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy_app(request: Request, path: str):
    return await proxy_metabase(request, path=f"app/{path}")

@app.api_route("/api/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy_api(request: Request, path: str):
    return await proxy_metabase(request, path=f"api/{path}")

@app.api_route("/auth/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy_auth(request: Request, path: str):
    return await proxy_metabase(request, path=f"auth/{path}")

@app.api_route("/public/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy_public(request: Request, path: str):
    return await proxy_metabase(request, path=f"public/{path}")

@app.api_route("/dashboard/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy_dashboard(request: Request, path: str):
    return await proxy_metabase(request, path=f"dashboard/{path}")

@app.api_route("/question/{path:path}", methods=["GET","POST","PUT","PATCH","DELETE","OPTIONS","HEAD"])
async def proxy_question(request: Request, path: str):
    return await proxy_metabase(request, path=f"question/{path}")


@app.get("/api/analytics/context")
async def analytics_context(q: str = "", url: str = ""):
    query = q.strip()
    target_url = url.strip()

    payload = {
        "query": query,
        "url": target_url,
        "search": None,
        "card": None,
        "card_result": None,
        "dashboard": None,
        "errors": [],
    }

    async with httpx.AsyncClient(follow_redirects=False, timeout=60.0) as client:
        try:
            if query:
                payload["search"] = await _metabase_json(client, "api/search", params={"q": query})
        except Exception as exc:
            payload["errors"].append(f"search: {exc}")

        try:
            card_id = None
            dashboard_id = None
            if "/question/" in target_url:
                card_id = target_url.rstrip("/").split("/question/")[-1].split("?")[0]
            if "/dashboard/" in target_url:
                dashboard_id = target_url.rstrip("/").split("/dashboard/")[-1].split("?")[0]

            if card_id and card_id.isdigit():
                payload["card"] = await _metabase_json(client, f"api/card/{card_id}")
                try:
                    payload["card_result"] = await _metabase_json(client, f"api/card/{card_id}/query/json")
                except Exception as exc:
                    payload["errors"].append(f"card_result: {exc}")

            if dashboard_id and dashboard_id.isdigit():
                payload["dashboard"] = await _metabase_json(client, f"api/dashboard/{dashboard_id}")
        except Exception as exc:
            payload["errors"].append(f"details: {exc}")

    return JSONResponse(payload)


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=LOCAL_PORT)
