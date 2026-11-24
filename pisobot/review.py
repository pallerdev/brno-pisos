"""Lee #revisar y responde por la API REST de Discord."""
from __future__ import annotations

import re

import httpx

API = "https://discord.com/api/v10"
_URL = re.compile(r"<(https?://[^\s<>]+)>|(https?://[^\s<>]+)", re.IGNORECASE)
_SNOWFLAKE = re.compile(r"[0-9]{1,20}")
_INITIAL_LIMIT = 50
_PAGE_LIMIT = 100
_CONTENT_LIMIT = 1900


def split_url(content: str) -> tuple[str | None, str]:
    match = _URL.search(content)
    if not match:
        return None, content.strip()
    # Discord rodea con <...> los enlaces sin vista previa.
    wrapped, bare = match.groups()
    url = wrapped if bare is None else bare.rstrip(".,;:!?")
    text = f"{content[:match.start()]} {content[match.end():]}"
    return url, " ".join(text.split())


def _messages_url(channel_id: str) -> str:
    if not _SNOWFLAKE.fullmatch(channel_id):
        raise ValueError("Discord: el id del canal debe ser numérico")
    return f"{API}/channels/{channel_id}/messages"


def _message_id(message: dict) -> int:
    raw = message.get("id") if isinstance(message, dict) else None
    if not isinstance(raw, str) or not _SNOWFLAKE.fullmatch(raw):
        raise ValueError("Discord: mensaje sin id válido")
    return int(raw)


class DiscordBot:
    def __init__(self, http: httpx.Client, token: str):
        self.http = http
        self.headers = {"Authorization": f"Bot {token}"}

    def _page(self, url: str, params: dict) -> list[dict]:
        response = self.http.get(url, params=params, headers=self.headers, timeout=20, follow_redirects=False)
        response.raise_for_status()
        try:
            messages = response.json()
        except ValueError:
            raise ValueError("Discord: respuesta de mensajes no válida") from None
        if not isinstance(messages, list):
            raise ValueError("Discord: respuesta de mensajes no válida")
        return sorted(messages, key=_message_id)

    def new_messages(self, channel_id: str, after: str | None) -> list[dict]:
        url = _messages_url(channel_id)
        if after is None:
            return self._page(url, {"limit": _INITIAL_LIMIT})

        messages = []
        while True:
            page = self._page(url, {"limit": _PAGE_LIMIT, "after": after})
            # Una página solapada puede duplicar mensajes, pero nunca debe hacer retroceder el cursor.
            new = [message for message in page if _message_id(message) > int(after)]
            messages.extend(new)
            if len(page) < _PAGE_LIMIT:
                return messages
            if not new:
                raise ValueError("Discord: la paginación de mensajes no avanza")
            after = new[-1]["id"]

    def _send(self, channel_id: str, body: dict) -> bool:
        body["allowed_mentions"] = {"parse": []}
        try:
            response = self.http.post(
                _messages_url(channel_id), json=body, headers=self.headers, timeout=20, follow_redirects=False
            )
        except (httpx.HTTPError, ValueError):
            return False
        return response.is_success

    def reply(self, channel_id: str, message_id: str, content: str | None, embeds: list[dict]) -> bool:
        body = {
            "embeds": embeds[:10],
            "message_reference": {"message_id": message_id, "fail_if_not_exists": False},
        }
        if content:
            body["content"] = content[:_CONTENT_LIMIT]
        return self._send(channel_id, body)

    def post(self, channel_id: str, content: str) -> bool:
        return self._send(channel_id, {"content": content[:_CONTENT_LIMIT]})
