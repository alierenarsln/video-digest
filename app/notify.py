import httpx


async def callback(url: str, payload: dict) -> None:
    """İş bitince n8n'i uyandır. Bildirim başarısızlığı işi başarısız saymaz."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
    except httpx.HTTPError as exc:
        print(f"[notify] callback başarısız ({url}): {exc}", flush=True)
