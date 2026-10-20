import httpx
import os
from typing import Any

_webhook_url: str | None = None


def get_webhook_url() -> str:
    global _webhook_url
    if not _webhook_url:
        _webhook_url = os.environ.get("BITRIX_WEBHOOK_URL", "").rstrip("/")
        if not _webhook_url:
            raise RuntimeError("BITRIX_WEBHOOK_URL not set in environment")
    return _webhook_url


async def call(method: str, params: dict[str, Any] | None = None) -> Any:
    url = f"{get_webhook_url()}/{method}"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=params or {})
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Bitrix24 error: {data.get('error_description', data['error'])}")
        return data.get("result")


async def get_current_user() -> dict:
    return await call("profile")


async def get_tasks(user_id: int | None = None, limit: int = 20) -> list:
    params: dict[str, Any] = {"order": {"DEADLINE": "ASC"}, "select": ["ID", "TITLE", "STATUS", "DEADLINE", "RESPONSIBLE_ID", "DESCRIPTION"]}
    if user_id:
        params["filter"] = {"RESPONSIBLE_ID": user_id}
    params["params"] = {"NAV_PARAMS": {"nPageSize": limit}}
    result = await call("tasks.task.list", params)
    return result.get("tasks", []) if isinstance(result, dict) else []


async def create_task(title: str, description: str = "", deadline: str = "", responsible_id: int | None = None) -> dict:
    fields: dict[str, Any] = {"TITLE": title}
    if description:
        fields["DESCRIPTION"] = description
    if deadline:
        fields["DEADLINE"] = deadline
    if responsible_id:
        fields["RESPONSIBLE_ID"] = responsible_id
    result = await call("tasks.task.add", {"fields": fields})
    return result.get("task", result) if isinstance(result, dict) else result


async def get_contacts(limit: int = 20) -> list:
    result = await call("crm.contact.list", {
        "order": {"DATE_CREATE": "DESC"},
        "select": ["ID", "NAME", "LAST_NAME", "PHONE", "EMAIL", "COMPANY_ID"],
        "start": 0,
    })
    return result[:limit] if isinstance(result, list) else []


async def get_deals(limit: int = 20) -> list:
    result = await call("crm.deal.list", {
        "order": {"DATE_CREATE": "DESC"},
        "select": ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "CURRENCY_ID", "CONTACT_ID", "ASSIGNED_BY_ID"],
        "start": 0,
    })
    return result[:limit] if isinstance(result, list) else []


async def create_deal(title: str, contact_id: int | None = None, amount: float = 0, currency: str = "KZT") -> dict:
    fields: dict[str, Any] = {"TITLE": title, "CURRENCY_ID": currency}
    if contact_id:
        fields["CONTACT_ID"] = contact_id
    if amount:
        fields["OPPORTUNITY"] = amount
    result = await call("crm.deal.add", {"fields": fields})
    return {"id": result}
