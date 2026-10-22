import os
import uuid
import json
import httpx
from dotenv import load_dotenv
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse
from starlette.routing import Route, Mount
from starlette.middleware.authentication import AuthenticationMiddleware
from mcp.server.mcpserver import MCPServer
from mcp.server.auth.provider import AccessToken
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.middleware.auth_context import get_access_token, AuthContextMiddleware
import bitrix

load_dotenv()

PORTAL = os.environ.get("BITRIX_PORTAL", "").rstrip("/")
CLIENT_ID = os.environ.get("BITRIX_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("BITRIX_CLIENT_SECRET", "")
UPSTASH_URL = os.environ.get("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.environ.get("UPSTASH_REDIS_REST_TOKEN", "")
SERVER_URL = os.environ.get("SERVER_URL", "https://bitrix24-mcp-1.onrender.com")

REDIRECT_URI = f"{SERVER_URL}/callback"


# ── Upstash Redis (REST API, no extra library) ────────────────────────────────

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


# ── Bitrix24 OAuth helpers ────────────────────────────────────────────────────

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


async def _refresh_token(refresh_token: str) -> dict:
    async with httpx.AsyncClient(timeout=15) as client:
        r = await client.post(
            "https://oauth.bitrix.info/oauth/token/",
            params={
                "grant_type": "refresh_token",
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
        )
    return r.json()


# ── Session management ────────────────────────────────────────────────────────

async def _get_session(session_id: str) -> dict | None:
    raw = await redis_get(f"session:{session_id}")
    if not raw:
        return None
    return json.loads(raw)


async def _save_session(session_id: str, data: dict) -> None:
    await redis_set(f"session:{session_id}", json.dumps(data))


async def _get_valid_access_token(session_id: str) -> str | None:
    session = await _get_session(session_id)
    if not session:
        return None

    access_token = session.get("access_token", "")
    refresh = session.get("refresh_token", "")

    # Check if current access_token still works
    try:
        async with httpx.AsyncClient(timeout=8) as client:
            r = await client.get(
                f"https://{PORTAL}/rest/profile.json",
                params={"auth": access_token},
            )
        data = r.json()
        if "result" in data and "error" not in data:
            return access_token
    except Exception:
        pass

    # Refresh using refresh_token
    if not refresh:
        return None
    try:
        tokens = await _refresh_token(refresh)
        new_access = tokens.get("access_token")
        if not new_access:
            return None
        session["access_token"] = new_access
        session["refresh_token"] = tokens.get("refresh_token", refresh)
        await _save_session(session_id, session)
        return new_access
    except Exception:
        return None


# ── MCP token verifier ────────────────────────────────────────────────────────

class SessionTokenVerifier:
    async def verify_token(self, token: str) -> AccessToken | None:
        access_token = await _get_valid_access_token(token)
        if not access_token:
            return None
        session = await _get_session(token)
        user_id = session.get("user_id", "unknown") if session else "unknown"
        return AccessToken(
            token=access_token,
            client_id=user_id,
            scopes=["crm", "task"],
            subject=user_id,
        )


# ── MCP tools ─────────────────────────────────────────────────────────────────

mcp = MCPServer("bitrix24")


def _token() -> str:
    at = get_access_token()
    if not at:
        raise RuntimeError("Не авторизован.")
    return at.token


@mcp.tool()
async def whoami() -> str:
    """Показать текущего пользователя Bitrix24"""
    try:
        user = await bitrix.get_current_user(_token())
        return (
            f"ID: {user.get('ID')}\n"
            f"Имя: {user.get('NAME')} {user.get('LAST_NAME')}\n"
            f"Email: {user.get('EMAIL')}\n"
            f"Должность: {user.get('WORK_POSITION') or '—'}"
        )
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
async def get_my_tasks(limit: int = 20) -> str:
    """Получить мои задачи в Bitrix24"""
    try:
        token = _token()
        user = await bitrix.get_current_user(token)
        user_id = user.get("ID") if isinstance(user, dict) else None
        tasks = await bitrix.get_tasks(token, user_id=user_id, limit=limit)
        if not tasks:
            return "Задач не найдено."
        status_map = {"1": "Новая", "2": "Ожидает", "3": "В работе", "4": "Завершена", "5": "Отложена"}
        lines = [f"Задачи ({len(tasks)}):"]
        for t in tasks:
            status = status_map.get(str(t.get("status", "")), str(t.get("status", "")))
            lines.append(f"• [{t['id']}] {t['title']} | {status} | Дедлайн: {t.get('deadline') or '—'}")
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
async def get_task(task_id: int) -> str:
    """Получить задачу по ID"""
    try:
        task = await bitrix.get_task_by_id(_token(), task_id)
        if not task:
            return f"Задача {task_id} не найдена."
        status_map = {"1": "Новая", "2": "Ожидает", "3": "В работе", "4": "Завершена", "5": "Отложена"}
        status = status_map.get(str(task.get("status", "")), str(task.get("status", "")))
        return (
            f"ID: {task.get('id')}\n"
            f"Название: {task.get('title')}\n"
            f"Статус: {status}\n"
            f"Дедлайн: {task.get('deadline') or '—'}\n"
            f"Описание: {task.get('description') or '—'}"
        )
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
async def create_task(title: str, description: str = "", deadline: str = "") -> str:
    """Создать задачу в Bitrix24. deadline в формате YYYY-MM-DD (необязательно)"""
    try:
        result = await bitrix.create_task(
            _token(), title=title, description=description, deadline=deadline
        )
        task_id = result.get("id") if isinstance(result, dict) else result
        return f"Задача создана! ID: {task_id}, Название: {title}"
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
async def get_contacts(limit: int = 20) -> str:
    """Получить список контактов CRM Bitrix24"""
    try:
        contacts = await bitrix.get_contacts(_token(), limit=limit)
        if not contacts:
            return "Контакты не найдены."
        lines = [f"Контакты ({len(contacts)}):"]
        for c in contacts:
            name = f"{c.get('NAME', '')} {c.get('LAST_NAME', '')}".strip()
            phones = ", ".join(p["VALUE"] for p in (c.get("PHONE") or []) if "VALUE" in p)
            lines.append(f"• [{c['ID']}] {name} | {phones or '—'}")
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
async def get_deals(limit: int = 20) -> str:
    """Получить список сделок CRM Bitrix24"""
    try:
        deals = await bitrix.get_deals(_token(), limit=limit)
        if not deals:
            return "Сделки не найдены."
        lines = [f"Сделки ({len(deals)}):"]
        for d in deals:
            lines.append(
                f"• [{d['ID']}] {d['TITLE']} | {d.get('STAGE_ID', '—')} "
                f"| {d.get('OPPORTUNITY', 0)} {d.get('CURRENCY_ID', '')}"
            )
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
async def create_deal(title: str, amount: float = 0, currency: str = "KZT") -> str:
    """Создать сделку в CRM Bitrix24"""
    try:
        result = await bitrix.create_deal(_token(), title=title, amount=amount, currency=currency)
        return f"Сделка создана! ID: {result.get('id')}, Название: {title}"
    except Exception as e:
        return f"Ошибка: {e}"


# ── OAuth web pages ───────────────────────────────────────────────────────────

async def connect_page(request: Request) -> HTMLResponse:
    html = """<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Подключение Bitrix24 к Claude</title>
  <style>
    body{font-family:system-ui,sans-serif;max-width:480px;margin:80px auto;padding:0 20px;color:#1a1a1a}
    h1{font-size:1.5rem;margin-bottom:8px}
    p{color:#555;line-height:1.5}
    .btn{display:inline-block;padding:13px 28px;background:#2563eb;color:#fff;border-radius:8px;
         text-decoration:none;font-size:1rem;font-weight:500;margin-top:16px}
    .btn:hover{background:#1d4ed8}
  </style>
</head>
<body>
  <h1>Bitrix24 → Claude Desktop</h1>
  <p>Нажми кнопку, войди своим аккаунтом Bitrix24.<br>
  Получишь персональный токен — вставь один раз в конфиг Claude Desktop.<br>
  <strong>Токен не истекает.</strong> Больше ничего делать не нужно.</p>
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
        return HTMLResponse(f"<h1>Ошибка авторизации: {err}</h1>", status_code=400)

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
        user_id = "unknown"
        name = "Пользователь"

    session_id = str(uuid.uuid4())
    await _save_session(session_id, {
        "access_token": access_token,
        "refresh_token": refresh,
        "user_id": user_id,
        "name": name,
    })

    config_block = f'''"mcpServers": {{
  "bitrix24": {{
    "command": "npx",
    "args": [
      "-y",
      "mcp-remote",
      "{SERVER_URL}/sse",
      "--header",
      "Authorization:Bearer {session_id}"
    ]
  }}
}},'''

    html = f"""<!DOCTYPE html>
<html lang="ru">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Готово!</title>
  <style>
    body{{font-family:system-ui,sans-serif;max-width:600px;margin:60px auto;padding:0 20px;color:#1a1a1a}}
    h1{{color:#16a34a}}
    .box{{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:14px 16px;
          font-family:monospace;font-size:0.8rem;white-space:pre;overflow-x:auto;margin:8px 0}}
    .step{{margin:20px 0}}
    h3{{margin:0 0 6px;font-size:0.95rem}}
    .copy-btn{{margin-top:8px;padding:7px 16px;background:#2563eb;color:#fff;border:none;
               border-radius:6px;cursor:pointer;font-size:0.85rem}}
    .copy-btn:active{{background:#1d4ed8}}
    .note{{background:#fefce8;border:1px solid #fde047;border-radius:6px;padding:10px 14px;
           font-size:0.88rem;margin-top:24px}}
    code{{background:#e5e7eb;padding:2px 5px;border-radius:4px;font-size:0.85em}}
  </style>
</head>
<body>
  <h1>✓ Авторизован: {name}</h1>

  <div class="step">
    <h3>Шаг 1 — Закрой Claude Desktop полностью</h3>
    <p>Mac: <kbd>Cmd+Q</kbd> &nbsp; Windows: правая кнопка на иконке → Закрыть</p>
  </div>

  <div class="step">
    <h3>Шаг 2 — Открой файл настроек</h3>
    <p>Mac: <code>~/Library/Application Support/Claude/claude_desktop_config.json</code><br>
    Windows: <code>%APPDATA%\\Claude\\claude_desktop_config.json</code></p>
  </div>

  <div class="step">
    <h3>Шаг 3 — Найди строку <code>"mcpServers": {{}},</code> и замени на это:</h3>
    <div class="box" id="cfg">{config_block}</div>
    <button class="copy-btn" onclick="navigator.clipboard.writeText(document.getElementById('cfg').textContent).then(()=>this.textContent='Скопировано!')">Скопировать</button>
  </div>

  <div class="step">
    <h3>Шаг 4 — Сохрани файл и открой Claude Desktop</h3>
    <p>Напиши в чате: <em>«покажи мои задачи в Bitrix24»</em></p>
  </div>

  <div class="note">
    ⚡ Этот токен <strong>не истекает</strong> — сервер обновляет Bitrix24 автоматически.<br>
    Больше ничего не нужно менять.
  </div>
</body>
</html>"""
    return HTMLResponse(html)


# ── App assembly ──────────────────────────────────────────────────────────────

def build_app():
    mcp_app = mcp.sse_app()
    mcp_app = AuthContextMiddleware(mcp_app)
    mcp_app = AuthenticationMiddleware(
        mcp_app,
        backend=BearerAuthBackend(token_verifier=SessionTokenVerifier()),
    )

    return Starlette(
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
    uvicorn.run(build_app(), host="0.0.0.0", port=port, log_level="info")
