import os
from dotenv import load_dotenv
load_dotenv()

from mcp.server.mcpserver import MCPServer
import bitrix

mcp = MCPServer("bitrix24")


@mcp.tool()
async def get_my_tasks(limit: int = 20) -> str:
    """Получить мои задачи в Bitrix24"""
    try:
        user = await bitrix.get_current_user()
        user_id = user.get("ID") if isinstance(user, dict) else None
        tasks = await bitrix.get_tasks(user_id=user_id, limit=limit)
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
async def create_task(title: str, description: str = "", deadline: str = "") -> str:
    """Создать задачу в Bitrix24. deadline в формате YYYY-MM-DD"""
    try:
        result = await bitrix.create_task(title=title, description=description, deadline=deadline)
        task_id = result.get("id") if isinstance(result, dict) else result
        return f"Задача создана! ID: {task_id}, Название: {title}"
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
async def get_contacts(limit: int = 20) -> str:
    """Получить список контактов CRM Bitrix24"""
    try:
        contacts = await bitrix.get_contacts(limit=limit)
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
        deals = await bitrix.get_deals(limit=limit)
        if not deals:
            return "Сделки не найдены."
        lines = [f"Сделки ({len(deals)}):"]
        for d in deals:
            lines.append(f"• [{d['ID']}] {d['TITLE']} | {d.get('STAGE_ID', '—')} | {d.get('OPPORTUNITY', 0)} {d.get('CURRENCY_ID', '')}")
        return "\n".join(lines)
    except Exception as e:
        return f"Ошибка: {e}"


@mcp.tool()
async def create_deal(title: str, amount: float = 0, currency: str = "KZT") -> str:
    """Создать сделку в CRM Bitrix24"""
    try:
        result = await bitrix.create_deal(title=title, amount=amount, currency=currency)
        return f"Сделка создана! ID: {result.get('id')}, Название: {title}"
    except Exception as e:
        return f"Ошибка: {e}"


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8765))
    mcp.run(transport="sse", host="0.0.0.0", port=port)
