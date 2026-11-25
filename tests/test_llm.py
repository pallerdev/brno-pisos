import json
from datetime import date

import httpx
import pytest

from pisobot.llm import Gemini, LlmResult, merge_facts
from pisobot.parse_text import TextFacts

ANSWER = {
    "summary_es": "Habitación de 16 m² en Veveří, piso con dos estudiantes.",
    "washing_machine": True, "energy_included": None, "furnished": True,
    "rent": 6500, "fees": 1800, "street": "Kotlářská, Veveří", "available_from": "2027-02-01",
}


def gemini_response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": json.dumps(payload)}]}}]})


def make(handler, usage, limit=5, key="k", sleeps=None):
    http = httpx.Client(transport=httpx.MockTransport(handler))
    # Nunca duerme de verdad: las esperas se anotan en `sleeps`
    sleep = (sleeps if sleeps is not None else []).append
    return Gemini(http, key, "gemini-2.5-flash", limit, usage, date(2026, 11, 24), sleep=sleep)


def test_analyze_parses_json_and_counts_usage():
    calls = []

    def handler(request):
        calls.append(request)
        return gemini_response(ANSWER)

    usage = {"date": "2026-11-23", "count": 4}
    result = make(handler, usage).analyze("Pokoj 16 m2, pračka, 6500 Kč + 1800 Kč")
    assert result == LlmResult(**ANSWER)
    assert usage == {"date": "2026-11-24", "count": 1}
    assert calls[0].headers["x-goog-api-key"] == "k"
    assert "gemini-2.5-flash:generateContent" in str(calls[0].url)
    body = json.loads(calls[0].content)
    assert body["generationConfig"]["responseMimeType"] == "application/json"


def test_daily_limit_and_missing_key_skip_http():
    def handler(request):
        raise AssertionError("no debería llamar")

    assert make(handler, {"date": "2026-11-24", "count": 5}).analyze("x") is None
    assert make(handler, {"date": None, "count": 0}, key="").analyze("x") is None


def test_errors_return_none():
    assert make(lambda r: httpx.Response(429), {"date": None, "count": 0}).analyze("x") is None
    bad = httpx.Response(200, json={"candidates": [{"content": {"parts": [{"text": "no es json"}]}}]})
    assert make(lambda r: bad, {"date": None, "count": 0}).analyze("x") is None


def test_malformed_shapes_return_none():
    # JSON válido pero con otra forma: nada de esto debe escapar de analyze()
    for answer in ([], 5, {"rent": 6500}, {"summary_es": None}):
        response = gemini_response(answer)
        assert make(lambda r, resp=response: resp, {"date": None, "count": 0}).analyze("x") is None
    # Bloqueo de seguridad: Gemini responde sin candidatos
    blocked = httpx.Response(200, json={"promptFeedback": {"blockReason": "SAFETY"}})
    assert make(lambda r: blocked, {"date": None, "count": 0}).analyze("x") is None


def test_wrong_field_types_become_none():
    wrong = {
        "summary_es": "x", "washing_machine": "no", "energy_included": "ne", "furnished": 1,
        "rent": "6500 Kč", "fees": True, "street": ["a"], "available_from": "pronto",
    }
    result = make(lambda r: gemini_response(wrong), {"date": None, "count": 0}).analyze("x")
    assert result == LlmResult(summary_es="x")


def test_negative_and_invalid_values_become_none():
    answer = {"summary_es": "x", "rent": -1, "fees": 0, "available_from": "2027-02-30"}
    result = make(lambda r: gemini_response(answer), {"date": None, "count": 0}).analyze("x")
    assert result == LlmResult(summary_es="x", fees=0)


def test_first_failure_disables_gemini_for_the_run():
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(500)

    gemini = make(handler, {"date": None, "count": 0})
    assert gemini.analyze("x") is None
    assert gemini.analyze("y") is None
    assert len(calls) == 1


def test_single_503_is_retried_once_after_two_seconds():
    calls, sleeps = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(503) if len(calls) == 1 else gemini_response(ANSWER)

    usage = {"date": "2026-11-24", "count": 0}
    gemini = make(handler, usage, sleeps=sleeps)
    assert gemini.analyze("x") == LlmResult(**ANSWER)
    assert len(calls) == 2
    assert sleeps == [2]
    assert calls[1].content == calls[0].content
    # Cada petición que sale cuenta para el tope diario, también la repetida
    assert usage["count"] == 2
    # La IA sigue disponible para el resto de la ejecución
    assert gemini.failed is False
    assert gemini.analyze("y") == LlmResult(**ANSWER)
    assert len(calls) == 3


def test_second_503_disables_gemini_after_a_single_retry(capsys):
    calls, sleeps = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(503, text="clave-secreta overloaded")

    gemini = make(handler, {"date": None, "count": 0}, key="clave-secreta", sleeps=sleeps)
    assert gemini.analyze("x") is None
    assert len(calls) == 2
    assert sleeps == [2]
    assert gemini.failed is True
    assert gemini.analyze("y") is None
    assert len(calls) == 2
    err = capsys.readouterr().err
    assert err == "gemini: HTTP 503\n"
    assert "clave-secreta" not in err


@pytest.mark.parametrize("status", [400, 403, 429, 500, 502, 504])
def test_only_503_is_retried(status):
    calls, sleeps = [], []

    def handler(request):
        calls.append(request)
        return httpx.Response(status)

    gemini = make(handler, {"date": None, "count": 0}, sleeps=sleeps)
    assert gemini.analyze("x") is None
    assert len(calls) == 1
    assert sleeps == []
    assert gemini.failed is True


def test_timeout_is_not_retried():
    calls, sleeps = [], []

    def handler(request):
        calls.append(request)
        raise httpx.ReadTimeout("timed out")

    gemini = make(handler, {"date": None, "count": 0}, sleeps=sleeps)
    assert gemini.analyze("x") is None
    assert len(calls) == 1
    assert sleeps == []
    assert gemini.failed is True


def test_failure_after_the_retry_reports_the_last_status(capsys):
    statuses = iter([503, 500])
    calls = []

    def handler(request):
        calls.append(request)
        return httpx.Response(next(statuses))

    gemini = make(handler, {"date": None, "count": 0})
    assert gemini.analyze("x") is None
    assert len(calls) == 2
    assert capsys.readouterr().err == "gemini: HTTP 500\n"
    assert gemini.failed is True


def test_failure_prints_one_line_without_secrets(capsys):
    key = "clave-secreta-123"

    def handler(request):
        return httpx.Response(403, text=f"echo {key}")

    make(handler, {"date": None, "count": 0}, key=key).analyze("x")
    err = capsys.readouterr().err
    assert err == "gemini: HTTP 403\n"
    assert key not in err


def test_timeout_is_reported_by_exception_name(capsys):
    def handler(request):
        raise httpx.ReadTimeout("timed out")

    make(handler, {"date": None, "count": 0}).analyze("x")
    assert capsys.readouterr().err == "gemini: ReadTimeout\n"


def test_failed_call_still_counts_usage():
    usage = {"date": "2026-11-24", "count": 2}
    make(lambda r: httpx.Response(500), usage).analyze("x")
    assert usage == {"date": "2026-11-24", "count": 3}


def test_api_key_only_travels_in_header():
    calls = []

    def handler(request):
        calls.append(request)
        return gemini_response(ANSWER)

    make(handler, {"date": None, "count": 0}, key="clave-secreta").analyze("x")
    assert "clave-secreta" not in str(calls[0].url)
    assert calls[0].headers["x-goog-api-key"] == "clave-secreta"
    assert calls[0].extensions["timeout"]["read"] == 30


def test_merge_facts_only_fills_unknowns():
    rules = TextFacts(washing_machine=False, energy_included=None, furnished=None, walkthrough_room=True)
    merged = merge_facts(rules, LlmResult(**ANSWER))
    assert merged.washing_machine is False  # la regla explícita manda
    assert merged.furnished is True
    assert merged.energy_included is None
    assert merged.walkthrough_room is True
    assert merge_facts(rules, None) == rules
