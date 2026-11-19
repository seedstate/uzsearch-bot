from __future__ import annotations

import aiohttp

API_URL = "https://pay.crypt.bot/api"


class CryptoPayError(RuntimeError):
    pass


async def _request(token: str, method: str, data: dict | None = None) -> dict:
    headers = {"Crypto-Pay-API-Token": token}
    timeout = aiohttp.ClientTimeout(total=20)
    async with aiohttp.ClientSession(headers=headers, timeout=timeout) as session:
        async with session.post(f"{API_URL}/{method}", json=data or {}) as response:
            payload = await response.json(content_type=None)
    if not payload.get("ok"):
        raise CryptoPayError(str(payload.get("error") or "Crypto Pay API error"))
    return payload["result"]


async def test_token(token: str) -> dict:
    return await _request(token, "getMe")


async def create_invoice(
    token: str, user_id: int, days: int, amount: str, tier: str = "premium"
) -> dict:
    if tier not in {"premium", "ultra"}:
        raise ValueError("Unknown subscription tier")
    return await _request(
        token,
        "createInvoice",
        {
            "currency_type": "fiat",
            "fiat": "USD",
            "accepted_assets": "USDT,TON",
            "amount": amount,
            "description": f"Username Search {tier.title()} — {days} дней",
            "payload": f"{tier}:{user_id}:{days}",
            "allow_comments": False,
            "allow_anonymous": False,
            "expires_in": 3600,
        },
    )


async def get_invoice(token: str, invoice_id: int) -> dict | None:
    result = await _request(token, "getInvoices", {"invoice_ids": str(invoice_id)})
    items = result.get("items", []) if isinstance(result, dict) else result
    return items[0] if items else None

