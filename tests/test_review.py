import json

import httpx
import pytest

from pisobot.review import API, DiscordBot, split_url


@pytest.mark.parametrize(
    ('content', 'expected'),
    [
        (
            'mira https://www.sreality.cz/detail/a/b/c/d/1 este',
            ('https://www.sreality.cz/detail/a/b/c/d/1', 'mira este'),
        ),
        ('<https://reality.bazos.cz/inzerat/1/x.php>', ('https://reality.bazos.cz/inzerat/1/x.php', '')),
        ('Pronajmu pokoj, 7000 Kč', (None, 'Pronajmu pokoj, 7000 Kč')),
        ('<mensaje> <https://example.com/>', ('https://example.com/', '<mensaje>')),
        ('https://example.com/oferta.php. Pračka', ('https://example.com/oferta.php', 'Pračka')),
    ],
)
def test_split_url(content, expected):
    assert split_url(content) == expected


def test_new_messages_sorted_with_auth():
    def handler(request):
        assert str(request.url.copy_with(query=None)) == f'{API}/channels/555/messages'
        assert request.url.params['after'] == '10'
        assert request.headers['Authorization'] == 'Bot tok'
        return httpx.Response(200, json=[{'id': '30'}, {'id': '12'}, {'id': '25'}])

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert [m['id'] for m in DiscordBot(http, 'tok').new_messages('555', '10')] == ['12', '25', '30']


def test_new_messages_fetches_all_pages_after_the_saved_cursor():
    cursors = []

    def handler(request):
        after = int(request.url.params['after'])
        limit = int(request.url.params['limit'])
        cursors.append(after)
        ids = range(after + 1, min(after + limit + 1, 120))
        return httpx.Response(200, json=[{'id': str(msg_id)} for msg_id in reversed(ids)])

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        messages = DiscordBot(http, 'tok').new_messages('555', '9')

    assert [int(m['id']) for m in messages] == list(range(10, 120))
    assert len(cursors) > 1 and cursors == sorted(set(cursors))


def test_new_messages_without_a_cursor_only_reads_recent_history():
    def handler(request):
        assert 'after' not in request.url.params and 'before' not in request.url.params
        return httpx.Response(200, json=[{'id': str(i)} for i in range(110, 60, -1)])

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        messages = DiscordBot(http, 'tok').new_messages('555', None)

    assert [int(m['id']) for m in messages] == list(range(61, 111))


@pytest.mark.parametrize('payload', [{'error': 'token-secreto'}, [{'id': 'token-secreto'}], [{'content': 'a'}]])
def test_new_messages_rejects_malformed_payload_without_repeating_it(payload):
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    with httpx.Client(transport=transport) as http, pytest.raises(ValueError, match='Discord') as error:
        DiscordBot(http, 'tok').new_messages('555', '10')

    assert 'token-secreto' not in str(error.value)


def test_new_messages_reports_http_failure_without_returning_a_partial_page():
    def handler(request):
        if request.url.params['after'] == '9':
            limit = int(request.url.params['limit'])
            return httpx.Response(200, json=[{'id': str(i)} for i in range(10, 10 + limit)])
        return httpx.Response(429, json={'retry_after': 30})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http, pytest.raises(httpx.HTTPStatusError) as error:
        DiscordBot(http, 'tok').new_messages('555', '9')

    assert error.value.response.status_code == 429


def test_reply_references_message_and_disables_mentions():
    bodies = []

    def handler(request):
        assert request.method == 'POST'
        assert request.headers['Authorization'] == 'Bot tok'
        bodies.append(json.loads(request.content))
        return httpx.Response(200, json={})

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        bot = DiscordBot(http, 'tok')
        assert bot.reply('555', '12', 'nota', [{'title': 't'}])
        assert bot.post('555', 'aviso')

    assert bodies[0]['message_reference'] == {'message_id': '12', 'fail_if_not_exists': False}
    assert bodies[0]['embeds'] == [{'title': 't'}]
    assert bodies[0]['content'] == 'nota'
    assert bodies[1]['content'] == 'aviso'
    assert all(body['allowed_mentions'] == {'parse': []} for body in bodies)


@pytest.mark.parametrize('status', [302, 429, 500])
def test_discord_send_reports_failure_and_does_not_follow_redirects(status):
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(status, headers={'Location': 'https://example.com/other'})

    with httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True) as http:
        assert DiscordBot(http, 'tok').post('555', 'aviso') is False

    assert len(requests) == 1


def test_reply_reports_transport_failure_without_exposing_the_error(capsys):
    def handler(request):
        raise httpx.ReadTimeout('token-secreto', request=request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as http:
        assert DiscordBot(http, 'tok').reply('555', '12', None, [{'title': 't'}]) is False

    assert 'token-secreto' not in capsys.readouterr().err


def test_invalid_channel_fails_without_an_authenticated_request():
    def unexpected_request(request):
        pytest.fail('No debe haber peticiones con un id de canal no válido')

    with httpx.Client(transport=httpx.MockTransport(unexpected_request)) as http:
        bot = DiscordBot(http, 'tok')
        with pytest.raises(ValueError, match='id del canal'):
            bot.new_messages('nombre-de-canal', None)
        assert bot.post('nombre-de-canal', 'aviso') is False
