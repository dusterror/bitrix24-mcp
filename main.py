import os
import uuid
import json
import httpx
from contextvars import ContextVar
from dotenv import load_dotenv

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

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

# Token available to MCP tools via contextvar
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


# ── ASGI middleware: sets _BITRIX_TOKEN before SSE handler runs ───────────────

class TokenMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path", "").rstrip("/") == "/sse":
            headers = {k.lower(): v for k, v in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode()
            if auth.startswith("Bearer "):
                sid = auth[7:].strip()
                token = await _get_valid_token(sid)
                if not token:
                    response = PlainTextResponse("Unauthorized", status_code=401)
                    await response(scope, receive, send)
                    return
                _BITRIX_TOKEN.set(token)
            else:
                response = PlainTextResponse("Unauthorized", status_code=401)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


# ── MCP server ────────────────────────────────────────────────────────────────

mcp = MCPServer("bitrix24")

STATUS_MAP = {"1": "Новая", "2": "Ожидает", "3": "В работе", "4": "Завершена", "5": "Отложена"}


def _tok() -> str:
    t = _BITRIX_TOKEN.get()
    if not t:
        raise RuntimeError("Не авторизован")
    return t


@mcp.tool()
async def whoami() -> str:
    """Показать текущего пользователя Bitrix24"""
    u = await bitrix.get_current_user(_tok())
    return (
        f"ID: {u.get('ID')}\n"
        f"Имя: {u.get('NAME')} {u.get('LAST_NAME')}\n"
        f"Email: {u.get('EMAIL')}\n"
        f"Должность: {u.get('WORK_POSITION') or '—'}"
    )


@mcp.tool()
async def get_my_tasks(limit: int = 20) -> str:
    """Получить мои задачи в Bitrix24"""
    token = _tok()
    u = await bitrix.get_current_user(token)
    tasks = await bitrix.get_tasks(token, user_id=u.get("ID"), limit=limit)
    if not tasks:
        return "Задач не найдено."
    lines = [f"Задачи ({len(tasks)}):"]
    for t in tasks:
        s = STATUS_MAP.get(str(t.get("status", "")), "")
        lines.append(f"• [{t['id']}] {t['title']} | {s} | {t.get('deadline') or '—'}")
    return "\n".join(lines)


@mcp.tool()
async def get_task(task_id: int) -> str:
    """Получить задачу по ID"""
    t = await bitrix.get_task_by_id(_tok(), task_id)
    if not t:
        return f"Задача {task_id} не найдена."
    s = STATUS_MAP.get(str(t.get("status", "")), "")
    return (
        f"ID: {t.get('id')}\nНазвание: {t.get('title')}\n"
        f"Статус: {s}\nДедлайн: {t.get('deadline') or '—'}\n"
        f"Описание: {t.get('description') or '—'}"
    )


@mcp.tool()
async def create_task(title: str, description: str = "", deadline: str = "") -> str:
    """Создать задачу. deadline в формате YYYY-MM-DD"""
    r = await bitrix.create_task(_tok(), title=title, description=description, deadline=deadline)
    return f"Задача создана! ID: {r.get('id') if isinstance(r, dict) else r}"


@mcp.tool()
async def get_contacts(limit: int = 20) -> str:
    """Получить список контактов CRM"""
    contacts = await bitrix.get_contacts(_tok(), limit=limit)
    if not contacts:
        return "Контакты не найдены."
    lines = [f"Контакты ({len(contacts)}):"]
    for c in contacts:
        nm = f"{c.get('NAME', '')} {c.get('LAST_NAME', '')}".strip()
        ph = ", ".join(p["VALUE"] for p in (c.get("PHONE") or []) if "VALUE" in p)
        lines.append(f"• [{c['ID']}] {nm} | {ph or '—'}")
    return "\n".join(lines)


@mcp.tool()
async def get_deals(limit: int = 20) -> str:
    """Получить список сделок CRM"""
    deals = await bitrix.get_deals(_tok(), limit=limit)
    if not deals:
        return "Сделки не найдены."
    lines = [f"Сделки ({len(deals)}):"]
    for d in deals:
        lines.append(
            f"• [{d['ID']}] {d['TITLE']} | {d.get('STAGE_ID', '—')} "
            f"| {d.get('OPPORTUNITY', 0)} {d.get('CURRENCY_ID', '')}"
        )
    return "\n".join(lines)


@mcp.tool()
async def create_deal(title: str, amount: float = 0, currency: str = "KZT") -> str:
    """Создать сделку в CRM"""
    r = await bitrix.create_deal(_tok(), title=title, amount=amount, currency=currency)
    return f"Сделка создана! ID: {r.get('id')}"


# ── OAuth pages ───────────────────────────────────────────────────────────────

async def connect_page(request: Request) -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Bitrix24 → Claude</title>
<style>body{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 20px}
h1{font-size:1.5rem}p{color:#555;line-height:1.5}
.btn{display:inline-block;padding:13px 28px;background:#2563eb;color:#fff;border-radius:8px;
     text-decoration:none;font-size:1rem;font-weight:500;margin-top:16px}
.btn:hover{background:#1d4ed8}</style></head>
<body><h1>Bitrix24 → Claude Desktop</h1>
<p>Нажми кнопку, войди своим аккаунтом Bitrix24.<br>
Токен не истекает — больше ничего делать не нужно.</p>
<a href="/auth" class="btn">Войти через Bitrix24</a></body></html>"""
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
            r = await client.get(f"https://{PORTAL}/rest/profile.json", params={"auth": access_token})
        profile = r.json().get("result", {})
        user_id = str(profile.get("ID", ""))
        name = f"{profile.get('NAME', '')} {profile.get('LAST_NAME', '')}".strip()
    except Exception:
        user_id, name = "unknown", "Пользователь"

    sid = str(uuid.uuid4())
    await _save_session(sid, {"access_token": access_token, "refresh_token": refresh,
                               "user_id": user_id, "name": name})

    config_block = (
        f'"mcpServers": {{\n  "bitrix24": {{\n    "command": "npx",\n'
        f'    "args": [\n      "-y",\n      "mcp-remote",\n'
        f'      "{SERVER_URL}/sse",\n      "--header",\n'
        f'      "Authorization:Bearer {sid}"\n    ]\n  }}\n}},'
    )

    html = f"""<!DOCTYPE html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Готово!</title>
<style>body{{font-family:system-ui,sans-serif;max-width:600px;margin:60px auto;padding:0 20px}}
h1{{color:#16a34a}}.box{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;
padding:14px 16px;font-family:monospace;font-size:0.8rem;white-space:pre;overflow-x:auto;margin:8px 0}}
.step{{margin:20px 0}}h3{{margin:0 0 6px;font-size:0.95rem}}
.btn{{margin-top:8px;padding:7px 16px;background:#2563eb;color:#fff;border:none;
      border-radius:6px;cursor:pointer;font-size:0.85rem}}
.note{{background:#fefce8;border:1px solid #fde047;border-radius:6px;padding:10px 14px;
       font-size:0.88rem;margin-top:24px}}
code{{background:#e5e7eb;padding:2px 5px;border-radius:4px;font-size:0.85em}}</style></head>
<body>
<h1>✓ Авторизован: {name}</h1>
<div class="step"><h3>Шаг 1 — Закрой Claude Desktop (Cmd+Q)</h3></div>
<div class="step"><h3>Шаг 2 — Открой файл настроек</h3>
<p>Mac: <code>~/Library/Application Support/Claude/claude_desktop_config.json</code><br>
Windows: <code>%APPDATA%\\Claude\\claude_desktop_config.json</code></p></div>
<div class="step"><h3>Шаг 3 — Найди <code>"mcpServers": {{}},</code> и замени на:</h3>
<div class="box" id="cfg">{config_block}</div>
<button class="btn" onclick="navigator.clipboard.writeText(document.getElementById('cfg').textContent).then(()=>this.textContent='Скопировано!')">Скопировать</button></div>
<div class="step"><h3>Шаг 4 — Сохрани и открой Claude Desktop</h3>
<p>Напиши: <em>«покажи мои задачи в Bitrix24»</em></p></div>
<div class="note">⚡ Токен <strong>не истекает</strong> — сервер обновляет Bitrix24 автоматически.</div>
</body></html>"""
    return HTMLResponse(html)


# ── App assembly ──────────────────────────────────────────────────────────────

_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
mcp_app = mcp.sse_app(transport_security=_security)
mcp_app = TokenMiddleware(mcp_app)

app = Starlette(
    routes=[
        Route("/connect", connect_page),
        Route("/auth", auth_redirect),
        Route("/callback", oauth_callback),
        Mount("/", mcp_app),
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
