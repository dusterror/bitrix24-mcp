import os
import httpx
from dotenv import load_dotenv

load_dotenv()

from mcp.server.mcpserver import MCPServer
from mcp.server.auth.provider import AccessToken, TokenVerifier
from mcp.server.auth.middleware.bearer_auth import BearerAuthBackend
from mcp.server.auth.middleware.auth_context import get_access_token, AuthContextMiddleware
from starlette.middleware.authentication import AuthenticationMiddleware
import bitrix

PORTAL = os.environ.get("BITRIX_PORTAL", "").rstrip("/")


class BitrixTokenVerifier:
    """Validates Bitrix24 OAuth tokens by calling /rest/profile."""

    async def verify_token(self, token: str) -> AccessToken | None:
        if not token or not PORTAL:
            return None
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    f"https://{PORTAL}/rest/profile.json",
                    params={"auth": token},
                )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if "error" in data or "result" not in data:
                return None
            user_id = str(data["result"].get("ID", "unknown"))
            return AccessToken(
                token=token,
                client_id=user_id,
                scopes=["crm", "task"],
                subject=user_id,
            )
        except Exception:
            return None


def _token() -> str:
    at = get_access_token()
    if not at:
        raise RuntimeError("Не авторизован. Подключите Bitrix24 через OAuth в ChatGPT.")
    return at.token


mcp = MCPServer("bitrix24")


@mcp.tool()
async def whoami() -> str:
    """Показать текущего пользователя Bitrix24 (проверка авторизации)"""
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


if __name__ == "__main__":
    import uvicorn

    if not PORTAL:
        raise SystemExit("BITRIX_PORTAL not set in .env")

    port = int(os.environ.get("PORT", 8765))

    # Build ASGI app with Bearer auth middleware
    # Order: AuthenticationMiddleware (outermost) → AuthContextMiddleware → MCP app
    app = mcp.sse_app()
    app = AuthContextMiddleware(app)
    app = AuthenticationMiddleware(app, backend=BearerAuthBackend(token_verifier=BitrixTokenVerifier()))

    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
