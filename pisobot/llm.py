"""Lectura de anuncios con Gemini (capa gratuita). Si falla o se agota la cuota, el bot sigue solo con reglas."""
from __future__ import annotations

import dataclasses
import json
import sys
from dataclasses import dataclass
from datetime import date

import httpx

from pisobot.parse_text import TextFacts

GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

PROMPT = """Lees anuncios de alquiler de Brno escritos en checo o inglés.
Responde solo con JSON:
- summary_es: resumen en español de 2-3 frases (qué es, zona, condiciones importantes).
- washing_machine: true si hay lavadora en el piso (aunque sea compartida), false si dice que no hay, null si no lo menciona.
- energy_included: true si el precio incluye luz/gas, false si van aparte, null si no se sabe.
- furnished: true si está amueblado, false si no, null si no se sabe.
- rent: alquiler mensual en CZK (entero) o null.
- fees: gastos/poplatky mensuales en CZK (entero) o null.
- street: calle y barrio de Brno si aparecen, o null.
- available_from: fecha de entrada AAAA-MM-DD o null.

Anuncio:
"""

_NULLABLE = {"nullable": True}
SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "summary_es": {"type": "STRING"},
        "washing_machine": {"type": "BOOLEAN", **_NULLABLE},
        "energy_included": {"type": "BOOLEAN", **_NULLABLE},
        "furnished": {"type": "BOOLEAN", **_NULLABLE},
        "rent": {"type": "INTEGER", **_NULLABLE},
        "fees": {"type": "INTEGER", **_NULLABLE},
        "street": {"type": "STRING", **_NULLABLE},
        "available_from": {"type": "STRING", **_NULLABLE},
    },
    "required": ["summary_es"],
}


@dataclass
class LlmResult:
    summary_es: str
    washing_machine: bool | None = None
    energy_included: bool | None = None
    furnished: bool | None = None
    rent: int | None = None
    fees: int | None = None
    street: str | None = None
    available_from: str | None = None


def _to_result(data) -> LlmResult | None:
    """La salida de la IA no es fiable: cada campo se valida por tipo y lo que no encaje queda en None."""
    if not isinstance(data, dict) or not isinstance(data.get("summary_es"), str):
        return None
    fields = {"summary_es": data["summary_es"]}
    # Un "true" en texto o un 1 no deben convertirse en verdicto: solo bool de verdad
    for name in ("washing_machine", "energy_included", "furnished"):
        value = data.get(name)
        fields[name] = value if isinstance(value, bool) else None
    # bool es subclase de int en Python, por eso se excluye a mano
    for name in ("rent", "fees"):
        value = data.get(name)
        ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
        fields[name] = value if ok else None
    for name in ("street", "available_from"):
        value = data.get(name)
        fields[name] = value if isinstance(value, str) else None
    if fields["available_from"] is not None:
        try:
            date.fromisoformat(fields["available_from"])
        except ValueError:
            fields["available_from"] = None
    return LlmResult(**fields)


class Gemini:
    def __init__(self, http: httpx.Client, api_key: str, model: str, daily_limit: int, usage: dict, today: date):
        self.http = http
        self.api_key = api_key
        self.model = model
        self.daily_limit = daily_limit
        self.usage = usage
        self.today = today.isoformat()
        # Tras el primer fallo no se vuelve a llamar en esta ejecución: una API caída o una clave mala se repetiría en cada anuncio
        self.failed = False

    def available(self) -> bool:
        if self.failed or not self.api_key:
            return False
        count = self.usage.get("count", 0) if self.usage.get("date") == self.today else 0
        return count < self.daily_limit

    def analyze(self, text: str) -> LlmResult | None:
        if not self.available():
            return None
        if self.usage.get("date") != self.today:
            self.usage["date"], self.usage["count"] = self.today, 0
        # Se cuenta antes de llamar: un intento fallido también gasta cuota
        self.usage["count"] = self.usage.get("count", 0) + 1
        body = {
            "contents": [{"parts": [{"text": PROMPT + text[:6000]}]}],
            "generationConfig": {"responseMimeType": "application/json", "responseSchema": SCHEMA, "temperature": 0},
        }
        try:
            response = self.http.post(
                GEMINI_URL.format(model=self.model), headers={"x-goog-api-key": self.api_key}, json=body,
                timeout=30, follow_redirects=False,
            )
            response.raise_for_status()
            raw = response.json()["candidates"][0]["content"]["parts"][0]["text"]
            data = json.loads(raw)
        except httpx.HTTPStatusError as exc:
            return self._fail(f"HTTP {exc.response.status_code}")
        except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError) as exc:
            return self._fail(type(exc).__name__)
        result = _to_result(data)
        if result is None:
            return self._fail("respuesta no válida")
        return result

    def _fail(self, reason: str) -> None:
        # Solo código de estado o nombre de excepción: nunca clave, URL ni cuerpo de respuesta (los logs de Actions son públicos)
        self.failed = True
        print(f"gemini: {reason}", file=sys.stderr)
        return None


def merge_facts(rules: TextFacts, llm: LlmResult | None) -> TextFacts:
    """Las coincidencias explícitas de las reglas mandan; la IA solo rellena lo desconocido."""
    if llm is None:
        return rules
    merged = dataclasses.replace(rules)
    for name in ("washing_machine", "energy_included", "furnished"):
        if getattr(merged, name) is None:
            setattr(merged, name, getattr(llm, name))
    return merged
