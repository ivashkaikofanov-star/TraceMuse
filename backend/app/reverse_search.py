"""Submit image previews to the configured reverse image search engines."""

import asyncio
import json
import mimetypes
import os
import re
import ssl
import subprocess
from html import unescape
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from uuid import uuid4

import httpx
from lxml import etree, html
from PicImageSearch import Yandex
from PicImageSearch.exceptions import ParsingError

from .config import BACKEND_DIR, settings
from .evidence import _plain, _read
from .license_evidence import host_matches
from .public_web import public_url

YANDEX_HOSTS = {'yandex.ru', 'www.yandex.ru', 'yandex.com', 'www.yandex.com', 'ya.ru'}
LENS_HOSTS = {'lens.google.com', 'www.google.com', 'google.com'}
NAMES = {'yandex': 'Яндекс', 'lens': 'Google Lens', 'bright_data': 'Bright Data'}
LENS_DIR = BACKEND_DIR / '.venv-lens'
LENS_WORKER = BACKEND_DIR / 'lens_worker.py'
MAX_RESULTS = 40
LICENSE_SITES = ('commons.wikimedia.org', 'freepik.com', 'unsplash.com', 'pexels.com', 'pixabay.com')
IGNORED_SITES = ('storage.googleapis.com', 'vk.ru', 'mavink.com', 'sprashivalka.com', 'pinimg.com',
                 'yandex.ru', 'yandex.com', 'yandex.net', 'ya.ru')


class SearchError(ValueError):
    def __init__(self, status: str, message: str):
        super().__init__(message)
        self.status = status


@dataclass
class SearchResult:
    status: str
    matches: list[dict]
    attempts: list[dict]
    provider: str = ''


def _url(value):
    if isinstance(value, str) and value.startswith('//'):
        value = 'https:' + value
    return public_url(value)


def _match(title, page, thumbnail=None):
    if not (url := _url(page)):
        return None
    return {'title': _plain(title, 180) or urlsplit(url).hostname, 'url': url,
            'thumbnail': _url(thumbnail) or ''}


def _unique(matches):
    unique = {}
    for row in matches:
        if row:
            unique.setdefault(row['url'], row)
    return list(unique.values())[:MAX_RESULTS]


def license_links(matches):
    """Drop proxy and repost URLs, then prioritize original stock cards."""
    ranked = []
    for row in matches:
        if not (url := _url(row.get('url'))):
            continue
        parsed = urlsplit(url)
        host = parsed.hostname
        if (any(host_matches(host, domain) for domain in IGNORED_SITES)
                or re.search(r'(?:^|\.)pinterest\.[a-z]+(?:\.[a-z]{2})?$', host, re.I)):
            continue
        priority = next((i for i, domain in enumerate(LICENSE_SITES) if host_matches(host, domain)), len(LICENSE_SITES))
        if priority < len(LICENSE_SITES) and (parsed.path == '/' or re.search(r'/(?:search|discover)(?:/|$)', parsed.path)):
            continue
        card = bool(re.search(r'/(?:wiki/File:|photos?/[^/?]+|(?:free|premium)-(?:photo|vector|psd)/[^/?]+|illustrations/[^/?]+)',
                              parsed.path, re.I))
        ranked.append((priority, not card, row | {'url': url}))
    trusted = [item for item in ranked if item[0] < len(LICENSE_SITES)]
    if trusted:
        trusted.sort(key=lambda item: item[:2])
        return _unique([row for _, _, row in trusted])
    return _unique([row for _, _, row in ranked])[:3]


def search_replacements(client, deadline, term):
    """Search Yandex Images for photo-card leads. Licensing is checked on each card."""
    data = _read(client, 'https://yandex.ru/images/search', {'text': term + ' site:pexels.com'}, deadline, 2_000_000)
    try:
        document = html.fromstring(data)
    except (ValueError, etree.Error):
        return []
    matches = []
    for node in document.xpath('//*[@data-state]')[:8]:
        try:
            rows = json.loads(node.get('data-state')).get('initialState', {}).get('serpList', {}).get('items', {}).get('entities', {})
        except (ValueError, AttributeError, RecursionError):
            continue
        if not isinstance(rows, dict):
            continue
        for row in list(rows.values())[:16]:
            if not isinstance(row, dict) or not isinstance(snippet := row.get('snippet'), dict):
                continue
            match = _match(snippet.get('title') or row.get('alt'), snippet.get('url'), row.get('image'))
            if match and host_matches(urlsplit(match['url']).hostname, 'pexels.com'):
                media = row.get('origUrl')
                match['image_url'] = _url(unescape(media)) if isinstance(media, str) else None
                matches.append(match)
    return _unique(matches)[:6]


def _http_status(code):
    if code == 429:
        raise SearchError('rate_limited', 'Сервис ограничил частоту запросов.')
    if code >= 400:
        raise SearchError('unavailable', f'Сервис ответил HTTP {code}.')


def _captcha(url):
    return bool(re.search(r'/(?:showcaptcha|captcha|checkcaptcha|sorry)(?:/|[?&#]|$)', str(url), re.I))


async def _yandex_request(request):
    if _captcha(request.url):
        raise SearchError('captcha', 'Яндекс запросил капчу.')
    if request.url.scheme != 'https' or request.url.host not in YANDEX_HOSTS:
        raise SearchError('unavailable', 'Яндекс перенаправил запрос на другой сайт.')


async def _yandex_response(response):
    if _captcha(response.url) or _captcha(response.headers.get('location', '')):
        raise SearchError('captcha', 'Яндекс запросил капчу.')
    if response.status_code == 429:
        _http_status(response.status_code)
    if response.is_redirect:
        return
    length = response.headers.get('content-length', '')
    if length.isdecimal() and len(length) < 12 and int(length) > 6_000_000:
        raise SearchError('unavailable', 'Ответ Яндекса слишком большой.')
    body = await response.aread()
    if re.search(rb'(?i)<(?:title|form)[^>]*>(?:\s*captcha)|\bSmartCaptcha-(?:Form|Checkbox)\b|'
                 rb'<form[^>]+(?:action="[^"]*/(?:checkcaptcha|showcaptcha)|(?:id|class)="[^"]*captcha)', body):
        raise SearchError('captcha', 'Яндекс запросил капчу.')
    _http_status(response.status_code)


async def _search_yandex(image_path):
    async with asyncio.timeout(16):
        async with httpx.AsyncClient(timeout=httpx.Timeout(12, connect=4), follow_redirects=True,
                                     max_redirects=4, event_hooks={'request': [_yandex_request],
                                                                    'response': [_yandex_response]}) as client:
            try:
                result = await Yandex(client=client, base_url='https://yandex.ru').search(file=image_path)
            except ParsingError as exc:
                if 'Failed to extract search results' in str(exc):
                    return []
                raise SearchError('unavailable', 'Яндекс вернул страницу в неожиданном формате.') from None
    return _unique([_match(row.title, row.url, row.thumbnail) for row in result.raw[:MAX_RESULTS]])


def _yandex(image_path):
    return asyncio.run(_search_yandex(image_path))


def _lens_python():
    return LENS_DIR / ('Scripts/python.exe' if os.name == 'nt' else 'bin/python')


def _lens(image_path):
    python = _lens_python()
    if not python.is_file():
        raise SearchError('not_configured', 'Резерв Google Lens ещё не установлен.')
    result = subprocess.run([str(python), '-I', str(LENS_WORKER), str(image_path)], capture_output=True,
                            timeout=45, encoding='utf-8', errors='replace', check=False)
    if result.returncode or not result.stdout.strip():
        raise SearchError('unavailable', 'Процесс Google Lens завершился с ошибкой.')
    payload = json.loads(result.stdout.splitlines()[-1])
    status = payload['status']
    if status == 'certificate_error':
        raise SearchError('unavailable', 'Браузер не смог проверить сертификат Google Lens.')
    if status in {'captcha', 'rate_limited', 'unavailable', 'not_configured'}:
        messages = {'captcha': 'Google Lens запросил капчу.',
                    'rate_limited': 'Google Lens ограничил частоту запросов.',
                    'unavailable': 'Google Lens не вернул доступную выдачу.',
                    'not_configured': 'Для Google Lens требуется установить Chromium.'}
        raise SearchError(status, messages[status])
    return _unique([_match(row.get('title'), row.get('url'), row.get('thumbnail'))
                    for row in payload['matches'][:MAX_RESULTS] if isinstance(row, dict)])


def _bright_candidates(data):
    if not isinstance(data, dict):
        return []
    blocks = [data]
    for name in ('data', 'results', 'lens'):
        if isinstance(block := data.get(name), dict):
            blocks.append(block)
    matches = []
    for block in blocks:
        for name in ('exact_matches', 'visual_matches', 'organic', 'items', 'results'):
            rows = block.get(name)
            if not isinstance(rows, list):
                continue
            for row in rows[:MAX_RESULTS]:
                if not isinstance(row, dict):
                    continue
                image = row.get('thumbnail') or row.get('image')
                if isinstance(image, dict):
                    image = image.get('url') or image.get('src')
                matches.append(_match(row.get('title') or row.get('name'),
                                      row.get('link') or row.get('url'), image))
    return _unique(matches)


def _bright_settings():
    return (settings.bright_data_proxy_user.strip(), settings.bright_data_proxy_password.get_secret_value())


def bright_data_configured():
    return all(_bright_settings())


def _lens_result_url(location):
    url = urljoin('https://lens.google.com/v3/upload', location)
    parsed = urlsplit(url)
    normalized = _url(url)
    if not normalized or parsed.scheme != 'https' or parsed.hostname not in LENS_HOSTS:
        raise SearchError('unavailable', 'Bright Data не вернул ссылку на выдачу Google Lens.')
    if _captcha(normalized):
        raise SearchError('captcha', 'Поисковик запросил капчу через Bright Data.')
    if parsed.path == '/v3/upload':
        raise SearchError('unavailable', 'Bright Data не вернул ссылку на выдачу Google Lens.')
    args = dict(parse_qsl(parsed.query, keep_blank_values=True))
    args.update(brd_json='1', brd_lens='exact_matches')
    return urlunsplit(parsed._replace(query=urlencode(args), fragment=''))


def _bright_data(image_path):
    proxy = httpx.Proxy('http://brd.superproxy.io:44445', auth=_bright_settings())
    verify = ssl.create_default_context()
    if settings.bright_data_ca_file:
        verify.load_verify_locations(cafile=settings.bright_data_ca_file)
    session = {'x-brd-session': uuid4().hex}
    with httpx.Client(proxy=proxy, verify=verify, timeout=httpx.Timeout(15, connect=5),
                      follow_redirects=False) as client:
        mime = mimetypes.guess_type(image_path.name)[0] or 'application/octet-stream'
        with image_path.open('rb') as file:
            response = client.post('https://lens.google.com/v3/upload', headers=session,
                                   files={'encoded_image': ('image' + image_path.suffix.lower(), file, mime)})
        _http_status(response.status_code)
        if (location := response.headers.get('location')):
            with client.stream('GET', _lens_result_url(location),
                               headers={**session, 'x-unblock-data-format': 'parsed_light'}) as reply:
                _http_status(reply.status_code)
                if reply.is_redirect:
                    if _captcha(reply.headers.get('location', '')):
                        raise SearchError('captcha', 'Поисковик запросил капчу через Bright Data.')
                    raise SearchError('unavailable', 'Bright Data перенаправил запрос вместо выдачи.')
                data = bytearray()
                for chunk in reply.iter_bytes(32_768):
                    if len(data) + len(chunk) > 2_000_000:
                        raise SearchError('unavailable', 'Ответ Bright Data слишком большой.')
                    data.extend(chunk)
                return _bright_candidates(json.loads(data))
        return _bright_candidates(response.json())


def _lookup(image_path):
    attempts = []
    engines = [('yandex', _yandex), ('lens', _lens)]
    if bright_data_configured():
        engines.append(('bright_data', _bright_data))
    for provider, engine in engines:
        try:
            matches = engine(image_path)
        except SearchError as exc:
            attempts.append({'provider': provider, 'status': exc.status, 'message': str(exc)})
        except (TimeoutError, subprocess.TimeoutExpired, httpx.TimeoutException):
            attempts.append({'provider': provider, 'status': 'unavailable', 'message': 'Время ожидания истекло.'})
        except (httpx.HTTPError, OSError, ValueError, KeyError, TypeError, AttributeError, RuntimeError):
            attempts.append({'provider': provider, 'status': 'unavailable', 'message': 'Сервис не вернул доступную выдачу.'})
        else:
            status = 'found' if matches else 'no_results'
            attempts.append({'provider': provider, 'status': status})
            if matches:
                return SearchResult(status, matches, attempts, provider)
    incomplete = any(row['status'] not in {'no_results', 'not_configured'} for row in attempts)
    return SearchResult('unavailable' if incomplete else 'no_results', [], attempts)


def search_image(image_path: str | Path) -> list[dict[str, str]]:
    """Return JSON-ready rows with title, url and thumbnail for a local image."""
    path = Path(image_path).resolve(strict=True)
    if not path.is_file():
        raise IsADirectoryError(path)
    return _lookup(path).matches


def search_previews(previews: dict[str, bytes]) -> dict[str, SearchResult]:
    with TemporaryDirectory(prefix='tracemuse-reverse-') as directory, ThreadPoolExecutor(max_workers=2) as pool:
        def search(item):
            index, (sha, data) = item
            path = Path(directory) / f'{index}.jpg'
            path.write_bytes(data)
            return sha, _lookup(path)
        return dict(pool.map(search, enumerate(previews.items())))
