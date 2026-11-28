import json
from html import escape

import httpx
import pytest
from pydantic import SecretStr

from app import reverse_search
from app.config import settings


MATCH = {'title': 'Landscape', 'url': 'https://example.org/landscape.jpg', 'thumbnail': 'https://example.org/thumb.jpg'}


@pytest.mark.parametrize(('response', 'status'), [
    ('normal', 'found'),
    ('empty', 'no_results'),
    ('captcha', 'captcha'),
    ('captcha_html', 'captcha'),
])
def test_yandex_supports_upload_and_recognizes_response_types(monkeypatch, tmp_path, response, status):
    file = tmp_path / 'image.jpg'
    file.write_bytes(b'local-image-bytes')
    data = {'url': MATCH['url'], 'title': MATCH['title'], 'thumb': {'url': '//example.org/thumb.jpg'},
            'domain': 'example.org', 'description': '', 'originalImage': {'width': 320, 'height': 200}}
    state = {'initialState': {'cbirSites': {'sites': [data] if response == 'normal' else []}}}

    def yandex(request):
        assert request.method == 'POST'
        assert request.url.host == 'yandex.ru'
        assert request.url.params['cbir_page'] == 'sites'
        assert b'local-image-bytes' in request.content
        if response == 'captcha':
            return httpx.Response(302, headers={'location': '/showcaptcha'})
        if response == 'captcha_html':
            return httpx.Response(403, text='<html><form action="/checkcaptcha"></form></html>')
        return httpx.Response(200, text=f'<html><div class="Root" id="ImagesApp-1" data-state="{escape(json.dumps(state))}"></div></html>')

    async_client = httpx.AsyncClient
    monkeypatch.setattr(reverse_search.httpx, 'AsyncClient',
                        lambda **options: async_client(transport=httpx.MockTransport(yandex), **options))
    monkeypatch.setattr(reverse_search, '_lens',
                        lambda _: pytest.fail('fallback ran after results') if response == 'normal' else [])
    result = reverse_search._lookup(file)
    assert result.attempts[0]['status'] == status
    assert result.matches == ([MATCH] if response == 'normal' else [])


def test_fallback_chain_is_ordered_and_bright_data_is_opt_in(monkeypatch, tmp_path):
    path = tmp_path / 'preview.jpg'
    path.write_bytes(b'preview')
    calls = []

    def blocked(_image):
        calls.append('yandex')
        raise reverse_search.SearchError('captcha', 'капча')

    def empty(_image):
        calls.append('lens')
        return []

    def found(_image):
        calls.append('bright_data')
        return [MATCH]

    monkeypatch.setattr(reverse_search, '_yandex', blocked)
    monkeypatch.setattr(reverse_search, '_lens', empty)
    monkeypatch.setattr(reverse_search, '_bright_data', found)
    assert reverse_search.search_image(path) == []
    assert calls == ['yandex', 'lens']
    calls.clear()
    monkeypatch.setattr(settings, 'bright_data_proxy_user', 'example')
    monkeypatch.setattr(settings, 'bright_data_proxy_password', SecretStr('test'))
    result = reverse_search._lookup(path)
    assert result.provider == 'bright_data'
    assert result.matches == [MATCH]
    assert calls == ['yandex', 'lens', 'bright_data']
    assert [attempt['status'] for attempt in result.attempts] == ['captcha', 'no_results', 'found']


def test_lens_worker_returns_standard_match_rows(monkeypatch, tmp_path):
    interpreter = tmp_path / 'python'
    interpreter.touch()
    photo = tmp_path / 'preview.jpg'

    def worker(command, **options):
        assert command == [str(interpreter), '-I', str(reverse_search.LENS_WORKER), str(photo)]
        assert options['timeout'] == 45
        output = json.dumps({'status': 'found', 'matches': [MATCH]})
        return reverse_search.subprocess.CompletedProcess(command, 0, stdout=output)

    monkeypatch.setattr(reverse_search, '_lens_python', lambda: interpreter)
    monkeypatch.setattr(reverse_search.subprocess, 'run', worker)
    assert reverse_search._lens(photo) == [MATCH]


def test_bright_data_reuses_session_for_local_upload_and_result(monkeypatch, tmp_path):
    photo = tmp_path / 'preview.jpg'
    photo.write_bytes(b'preview-image')
    seen = []

    def provider(request):
        seen.append(request.headers['x-brd-session'])
        if request.method == 'POST':
            assert str(request.url) == 'https://lens.google.com/v3/upload'
            assert b'preview-image' in request.content
            return httpx.Response(303, headers={'location': 'https://www.google.com/search?vsrid=123'})
        assert request.url.params['brd_lens'] == 'exact_matches'
        assert request.headers['x-unblock-data-format'] == 'parsed_light'
        return httpx.Response(200, json={'exact_matches': [MATCH]})

    client = httpx.Client
    monkeypatch.setattr(reverse_search.httpx, 'Client',
                        lambda **kwargs: client(transport=httpx.MockTransport(provider),
                                                verify=kwargs['verify'], follow_redirects=False))
    monkeypatch.setattr(reverse_search, '_bright_settings', lambda: ('example', 'password'))
    assert reverse_search._bright_data(photo) == [MATCH]
    assert len(seen) == 2 and seen[0] == seen[1]
    with pytest.raises(reverse_search.SearchError):
        reverse_search._lens_result_url('https://unrelated.example.org/search')
