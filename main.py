import os
import uuid
import json
import httpx
from contextvars import ContextVar
from dotenv import load_dotenv

from mcp import types
from mcp.server import Server
from mcp.server.sse import SseServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from mcp.server.models import InitializationOptions
from mcp.server.lowlevel import NotificationOptions

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from starlette.routing import Route, Mount

import bitrix

load_dotenv()

PORTAL = os.environ.get("BITRIX_PORTAL", "").rstrip("/")
CLIENT_ID = os.environ.get("BITRIX_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("BITRIX_CLIENT_SECRET", "")
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
SERVER_URL = os.environ.get("SERVER_URL", "https://bitrix24-mcp-1.onrender.com")

REDIRECT_URI = f"{SERVER_URL}/callback"

_BITRIX_TOKEN: ContextVar[str] = ContextVar("bitrix_token", default="")


# ── Upstash Redis ─────────────────────────────────────────────────────────────

async def _redis(command: list) -> object:
    async with httpx.AsyncClient(timeout=10) as client:
        r = await client.post(
            UPSTASH_URL,
            json=command,
            headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        )
    return r.json().get("result")


async def redis_get(key: str) -> str | None:
    return await _redis(["GET", key])


async def redis_set(key: str, value: str) -> None:
    await _redis(["SET", key, value])


# ── Bitrix24 OAuth ────────────────────────────────────────────────────────────

def _auth_url() -> str:
    return (
        f"https://{PORTAL}/oauth/authorize/"
        f"?client_id={CLIENT_ID}"
        f"&response_type=code"
        f"&redirect_uri={REDIRECT_URI}"
    )


async def _exchange_code(code: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://oauth.bitrix.info/oauth/token/",
            params={
                "grant_type": "authorization_code",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "redirect_uri": REDIRECT_URI,
                "code": code,
            },
        )
    return r.json()


async def _refresh_token(refresh: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://oauth.bitrix.info/oauth/token/",
            params={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": refresh,
            },
        )
    return r.json()


# ── Session helpers ───────────────────────────────────────────────────────────

async def _get_session(sid: str) -> dict | None:
    raw = await redis_get(f"session:{sid}")
    return json.loads(raw) if raw else None


async def _save_session(sid: str, data: dict) -> None:
    await redis_set(f"session:{sid}", json.dumps(data))


async def _get_valid_token(sid: str) -> str | None:
    session = await _get_session(sid)
    if not session:
        return None
    access = session.get("access_token", "")
    refresh = session.get("refresh_token", "")

    # Try existing access token
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"https://{PORTAL}/rest/profile.json",
                params={"auth": access},
            )
        data = r.json()
        if "result" in data and "error" not in data:
            return access
    except Exception:
        pass

    # Refresh
    if not refresh:
        return None
    try:
        tokens = await _refresh_token(refresh)
        new_access = tokens.get("access_token")
        if not new_access:
            return None
        session["access_token"] = new_access
        session["refresh_token"] = tokens.get("refresh_token", refresh)
        await _save_session(sid, session)
        return new_access
    except Exception:
        return None


# ── MCP server (low-level, no auth middleware) ────────────────────────────────

server = Server("bitrix24")


def _tok() -> str:
    t = _BITRIX_TOKEN.get()
    if not t:
        raise RuntimeError("Не авторизован")
    return t


@server.list_tools()
async def list_tools() -> list[types.Tool]:
    return [
        types.Tool(
            name="whoami",
            description="Показать текущего пользователя Bitrix24",
            inputSchema={"type": "object", "properties": {}},
        ),
        types.Tool(
            name="get_my_tasks",
            description="Получить мои задачи в Bitrix24",
            inputSchema={
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 20, "description": "Количество задач"}
                },
            },
        ),
        types.Tool(
            name="get_task",
            description="Получить задачу по ID",
            inputSchema={
                "type": "object",
                "properties": {"task_id": {"type": "integer"}},
                "required": ["task_id"],
            },
        ),
        types.Tool(
            name="create_task",
            description="Создать задачу. deadline в формате YYYY-MM-DD",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "description": {"type": "string", "default": ""},
                    "deadline": {"type": "string", "default": ""},
                },
                "required": ["title"],
            },
        ),
        types.Tool(
            name="get_contacts",
            description="Получить список контактов CRM",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
        ),
        types.Tool(
            name="get_deals",
            description="Получить список сделок CRM",
            inputSchema={
                "type": "object",
                "properties": {"limit": {"type": "integer", "default": 20}},
            },
        ),
        types.Tool(
            name="create_deal",
            description="Создать сделку в CRM",
            inputSchema={
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "amount": {"type": "number", "default": 0},
                    "currency": {"type": "string", "default": "KZT"},
                },
                "required": ["title"],
            },
        ),
    ]


@server.call_tool()
async def call_tool(name: str, arguments: dict | None) -> list[types.TextContent]:
    args = arguments or {}
    try:
        token = _tok()
    except RuntimeError as e:
        return [types.TextContent(type="text", text=str(e))]

    status_map = {"1": "Новая", "2": "Ожидает", "3": "В работе", "4": "Завершена", "5": "Отложена"}

    try:
        if name == "whoami":
            u = await bitrix.get_current_user(token)
            text = (
                f"ID: {u.get('ID')}\n"
                f"Имя: {u.get('NAME')} {u.get('LAST_NAME')}\n"
                f"Email: {u.get('EMAIL')}\n"
                f"Должность: {u.get('WORK_POSITION') or '—'}"
            )

        elif name == "get_my_tasks":
            u = await bitrix.get_current_user(token)
            tasks = await bitrix.get_tasks(token, user_id=u.get("ID"), limit=args.get("limit", 20))
            if not tasks:
                text = "Задач не найдено."
            else:
                lines = [f"Задачи ({len(tasks)}):"]
                for t in tasks:
                    s = status_map.get(str(t.get("status", "")), "")
                    lines.append(f"• [{t['id']}] {t['title']} | {s} | {t.get('deadline') or '—'}")
                text = "\n".join(lines)

        elif name == "get_task":
            t = await bitrix.get_task_by_id(token, args["task_id"])
            if not t:
                text = f"Задача {args['task_id']} не найдена."
            else:
                s = status_map.get(str(t.get("status", "")), "")
                text = (
                    f"ID: {t.get('id')}\n"
                    f"Название: {t.get('title')}\n"
                    f"Статус: {s}\n"
                    f"Дедлайн: {t.get('deadline') or '—'}\n"
                    f"Описание: {t.get('description') or '—'}"
                )

        elif name == "create_task":
            r = await bitrix.create_task(
                token,
                title=args["title"],
                description=args.get("description", ""),
                deadline=args.get("deadline", ""),
            )
            text = f"Задача создана! ID: {r.get('id') if isinstance(r, dict) else r}"

        elif name == "get_contacts":
            contacts = await bitrix.get_contacts(token, limit=args.get("limit", 20))
            if not contacts:
                text = "Контакты не найдены."
            else:
                lines = [f"Контакты ({len(contacts)}):"]
                for c in contacts:
                    nm = f"{c.get('NAME', '')} {c.get('LAST_NAME', '')}".strip()
                    ph = ", ".join(p["VALUE"] for p in (c.get("PHONE") or []) if "VALUE" in p)
                    lines.append(f"• [{c['ID']}] {nm} | {ph or '—'}")
                text = "\n".join(lines)

        elif name == "get_deals":
            deals = await bitrix.get_deals(token, limit=args.get("limit", 20))
            if not deals:
                text = "Сделки не найдены."
            else:
                lines = [f"Сделки ({len(deals)}):"]
                for d in deals:
                    lines.append(
                        f"• [{d['ID']}] {d['TITLE']} | {d.get('STAGE_ID', '—')} "
                        f"| {d.get('OPPORTUNITY', 0)} {d.get('CURRENCY_ID', '')}"
                    )
                text = "\n".join(lines)

        elif name == "create_deal":
            r = await bitrix.create_deal(
                token,
                title=args["title"],
                amount=args.get("amount", 0),
                currency=args.get("currency", "KZT"),
            )
            text = f"Сделка создана! ID: {r.get('id')}"

        else:
            text = f"Неизвестный инструмент: {name}"

    except Exception as e:
        text = f"Ошибка: {e}"

    return [types.TextContent(type="text", text=text)]


# ── SSE transport ─────────────────────────────────────────────────────────────

sse_transport = SseServerTransport(
    "/messages/",
    security_settings=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


async def sse_endpoint(request: Request):
    auth = request.headers.get("authorization", "")
    session_id = auth.removeprefix("Bearer ").strip() if auth.startswith("Bearer ") else ""

    bitrix_token = await _get_valid_token(session_id) if session_id else None
    if not bitrix_token:
        return PlainTextResponse("Unauthorized", status_code=401)

    _BITRIX_TOKEN.set(bitrix_token)

    async with sse_transport.connect_sse(
        request.scope, request.receive, request._send
    ) as streams:
        await server.run(
            streams[0],
            streams[1],
            InitializationOptions(
                server_name="bitrix24",
                server_version="1.0.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


async def messages_endpoint(request: Request):
    await sse_transport.handle_post_message(
        request.scope, request.receive, request._send
    )


# ── OAuth pages ───────────────────────────────────────────────────────────────

async def connect_page(request: Request) -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Bitrix24 → Claude</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 20px;color:#1a1a1a}
    h1{font-size:1.5rem}p{color:#555;line-height:1.5}
    .btn{display:inline-block;padding:13px 28px;background:#2563eb;color:#fff;border-radius:8px;
         text-decoration:none;font-size:1rem;font-weight:500;margin-top:16px}
    .btn:hover{background:#1d4ed8}
  </style>
</head>
<body>
  <h1>Bitrix24 → Claude Desktop</h1>
  <p>Нажми кнопку, войди своим аккаунтом Bitrix24.<br>
  Получишь персональный токен — вставь один раз в конфиг Claude Desktop.<br>
  <strong>Токен не истекает.</strong></p>
  <a href="/auth" class="btn">Войти через Bitrix24</a>
</body>
</html>"""
    return HTMLResponse(html)


async def auth_redirect(request: Request) -> RedirectResponse:
    return RedirectResponse(_auth_url())


async def oauth_callback(request: Request) -> HTMLResponse:
    code = request.query_params.get("code")
    if not code:
        return HTMLResponse("<h1>Ошибка: нет кода авторизации</h1>", status_code=400)

    tokens = await _exchange_code(code)
    access_token = tokens.get("access_token")
    refresh = tokens.get("refresh_token")

    if not access_token:
        err = tokens.get("error_description", tokens.get("error", "неизвестная ошибка"))
        return HTMLResponse(f"<h1>Ошибка: {err}</h1>", status_code=400)

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.get(
                f"https://{PORTAL}/rest/profile.json",
                params={"auth": access_token},
            )
        profile = r.json().get("result", {})
        user_id = str(profile.get("ID", ""))
        name = f"{profile.get('NAME', '')} {profile.get('LAST_NAME', '')}".strip()
    except Exception:
        user_id, name = "unknown", "Пользователь"

    sid = str(uuid.uuid4())
    await _save_session(sid, {
        "access_token": access_token,
        "refresh_token": refresh,
        "user_id": user_id,
        "name": name,
    })

    config_block = (
        f'"mcpServers": {{\n'
        f'  "bitrix24": {{\n'
        f'    "command": "npx",\n'
        f'    "args": [\n'
        f'      "-y",\n'
        f'      "mcp-remote",\n'
        f'      "{SERVER_URL}/sse",\n'
        f'      "--header",\n'
        f'      "Authorization:Bearer {sid}"\n'
        f'    ]\n'
        f'  }}\n'
        f'}},'
    )

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
  <title>Готово!</title>
  <style>
    body{{font-family:system-ui,sans-serif;max-width:600px;margin:60px auto;padding:0 20px;color:#1a1a1a}}
    h1{{color:#16a34a}}.box{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
    padding:14px 16px;font-family:monospace;font-size:0.8rem;white-space:pre;overflow-x:auto;margin:8px 0}}
    .step{{margin:20px 0}}h3{{margin:0 0 6px;font-size:0.95rem}}
    .btn{{margin-top:8px;padding:7px 16px;background:#2563eb;color:#fff;border:none;
          border-radius:6px;cursor:pointer;font-size:0.85rem}}
    .note{{background:#fefce8;border:1px solid #fde047;border-radius:6px;padding:10px 14px;
           font-size:0.88rem;margin-top:24px}}
    code{{background:#e5e7eb;padding:2px 5px;border-radius:4px;font-size:0.85em}}
  </style>
</head>
<body>
  <h1>✓ Авторизован: {name}</h1>
  <div class="step">
    <h3>Шаг 1 — Закрой Claude Desktop (Cmd+Q)</h3>
  </div>
  <div class="step">
    <h3>Шаг 2 — Открой файл настроек</h3>
    <p>Mac: <code>~/Library/Application Support/Claude/claude_desktop_config.json</code><br>
    Windows: <code>%APPDATA%\\Claude\\claude_desktop_config.json</code></p>
  </div>
  <div class="step">
    <h3>Шаг 3 — Найди <code>"mcpServers": {{}},</code> и замени на:</h3>
    <div class="box" id="cfg">{config_block}</div>
    <button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('cfg').textContent).then(()=>this.textContent='Скопировано!')">Скопировать</button>
  </div>
  <div class="step">
    <h3>Шаг 4 — Сохрани и открой Claude Desktop</h3>
    <p>Напиши: <em>«покажи мои задачи в Bitrix24»</em></p>
  </div>
  <div class="note">⚡ Токен <strong>не истекает</strong> — сервер обновляет Bitrix24 автоматически.</div>
</body>
</html>"""
    return HTMLResponse(html)


# ── App ───────────────────────────────────────────────────────────────────────

app = Starlette(
    routes=[
        Route("/connect", connect_page),
        Route("/auth", auth_redirect),
        Route("/callback", oauth_callback),
        Route("/sse", sse_endpoint),
        Route("/messages/", messages_endpoint, methods=["POST"]),
        Route("/messages", messages_endpoint, methods=["POST"]),
    ]
)

if __name__ == "__main__":
    import uvicorn

    if not PORTAL:
        raise SystemExit("BITRIX_PORTAL not set")
    if not CLIENT_ID or not CLIENT_SECRET:
        raise SystemExit("BITRIX_CLIENT_ID / BITRIX_CLIENT_SECRET not set")
    if not UPSTASH_URL or not UPSTASH_TOKEN:
        raise SystemExit("UPSTASH_REDIS_REST_URL / UPSTASH_REDIS_REST_TOKEN not set")

    port = int(os.environ.get("PORT", 8765))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
