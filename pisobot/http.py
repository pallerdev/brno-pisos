import httpx

def make_client(user_agent: str) -> httpx.Client:
    return httpx.Client(
        headers={"User-Agent": user_agent, "Accept-Language": "cs,en;q=0.8"},
        timeout=30,
        follow_redirects=True,
    )
