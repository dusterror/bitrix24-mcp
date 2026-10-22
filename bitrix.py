import httpx
import os
from typing import Any

PORTAL = os.environ.get("BITRIX_PORTAL", "").rstrip("/")  # e.g., attech.bitrix24.kz


def _base_url() -> str:
    if not PORTAL:
        raise RuntimeError("BITRIX_PORTAL not set in environment")
    return f"https://{PORTAL}/rest"


async def call(method: str, params: dict[str, Any], access_token: str) -> Any:
    if not access_token:
        raise RuntimeError("OAuth token missing — connect Bitrix24 via OAuth in ChatGPT")
    url = f"{_base_url()}/{method}.json"
    payload = {**params, "auth": access_token}
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            raise RuntimeError(f"Bitrix24: {data.get('error_description', data['error'])}")
        return data.get("result")


async def get_current_user(access_token: str) -> dict:
    return await call("profile", {}, access_token)


async def get_tasks(access_token: str, user_id: int | None = None, limit: int = 20) -> list:
    params: dict[str, Any] = {
        "order": {"DEADLINE": "ASC"},
        "select": ["ID", "TITLE", "STATUS", "DEADLINE", "RESPONSIBLE_ID", "DESCRIPTION"],
        "params": {"NAV_PARAMS": {"nPageSize": limit}},
    }
    if user_id:
        params["filter"] = {"RESPONSIBLE_ID": user_id}
    result = await call("tasks.task.list", params, access_token)
    return result.get("tasks", []) if isinstance(result, dict) else []


async def get_task_by_id(access_token: str, task_id: int) -> dict:
    result = await call("tasks.task.get", {"taskId": task_id}, access_token)
    return result.get("task", result) if isinstance(result, dict) else result


async def create_task(
    access_token: str,
    title: str,
    description: str = "",
    deadline: str = "",
    responsible_id: int | None = None,
) -> dict:
    fields: dict[str, Any] = {"TITLE": title}
    if description:
        fields["DESCRIPTION"] = description
    if deadline:
        fields["DEADLINE"] = deadline
    else:
        fields["DEADLINE"] = ""
    if responsible_id:
        fields["RESPONSIBLE_ID"] = responsible_id
    result = await call("tasks.task.add", {"fields": fields}, access_token)
    return result.get("task", result) if isinstance(result, dict) else result


async def get_contacts(access_token: str, limit: int = 20) -> list:
    result = await call(
        "crm.contact.list",
        {
            "order": {"DATE_CREATE": "DESC"},
            "select": ["ID", "NAME", "LAST_NAME", "PHONE", "EMAIL", "COMPANY_ID"],
            "start": 0,
        },
        access_token,
    )
    return result[:limit] if isinstance(result, list) else []


async def get_deals(access_token: str, limit: int = 20) -> list:
    result = await call(
        "crm.deal.list",
        {
            "order": {"DATE_CREATE": "DESC"},
            "select": ["ID", "TITLE", "STAGE_ID", "OPPORTUNITY", "CURRENCY_ID", "CONTACT_ID", "ASSIGNED_BY_ID"],
            "start": 0,
        },
        access_token,
    )
    return result[:limit] if isinstance(result, list) else []


async def create_deal(
    access_token: str,
    title: str,
    contact_id: int | None = None,
    amount: float = 0,
    currency: str = "KZT",
) -> dict:
    fields: dict[str, Any] = {"TITLE": title, "CURRENCY_ID": currency}
    if contact_id:
        fields["CONTACT_ID"] = contact_id
    if amount:
        fields["OPPORTUNITY"] = amount
    result = await call("crm.deal.add", {"fields": fields}, access_token)
    return {"id": result}
