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
_TRAILING_PUNCTUATION = ".,;:!?"
_CLOSERS = {")": "(", "]": "["}


def _trim_url(url: str) -> tuple[str, list[str]]:
    """Quita la puntuación final y los cierres que el enlace no abre; devuelve también esos cierres."""
    closers: list[str] = []
    while True:
        trimmed = url.rstrip(_TRAILING_PUNCTUATION)
        last = trimmed[-1:]
        if last in _CLOSERS and trimmed.count(last) > trimmed.count(_CLOSERS[last]):
            closers.append(last)
            trimmed = trimmed[:-1]
        if trimmed == url:
            return url, closers
        url = trimmed


def split_url(content: str) -> tuple[str | None, str]:
    match = _URL.search(content)
    if not match:
        return None, content.strip()
    # Discord rodea con <...> los enlaces sin vista previa.
    wrapped, bare = match.groups()
    before = content[:match.start()]
    if bare is None:
        url = wrapped
    else:
        url, closers = _trim_url(bare)
        # "(https://…)": el cierre se va del enlace y también su paréntesis de apertura del texto
        for closer in closers:
            if before.endswith(_CLOSERS[closer]):
                before = before[:-1]
    text = f"{before} {content[match.end():]}"
    return url, " ".join(text.split())


class ReplyRejected(Exception):
    """Discord rechaza la respuesta (4xx salvo 429): reintentarla daría siempre el mismo error."""

    def __init__(self, status: int):
        super().__init__(f"HTTP {status}")
        self.status = status


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

    def _send(self, channel_id: str, body: dict) -> int | None:
        """Código HTTP de la respuesta; None si no hubo respuesta (red, id de canal inválido)."""
        body["allowed_mentions"] = {"parse": []}
        try:
            response = self.http.post(
                _messages_url(channel_id), json=body, headers=self.headers, timeout=20, follow_redirects=False
            )
        except (httpx.HTTPError, ValueError):
            return None
        return response.status_code

    def reply(self, channel_id: str, message_id: str, content: str | None, embeds: list[dict]) -> bool:
        """True si Discord lo acepta; False si conviene reintentar. Lanza ReplyRejected si no tiene remedio."""
        body = {
            "embeds": embeds[:10],
            "message_reference": {"message_id": message_id, "fail_if_not_exists": False},
        }
        if content:
            body["content"] = content[:_CONTENT_LIMIT]
        status = self._send(channel_id, body)
        if status is not None and 400 <= status < 500 and status != 429:
            raise ReplyRejected(status)
        return status is not None and httpx.codes.is_success(status)

    def post(self, channel_id: str, content: str) -> bool:
        status = self._send(channel_id, {"content": content[:_CONTENT_LIMIT]})
        return status is not None and httpx.codes.is_success(status)
