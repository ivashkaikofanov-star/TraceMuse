"""Bounded, read-only retrieval of public web pages and images."""

import http.client
import ipaddress
import socket
from dataclasses import dataclass
from time import monotonic
from urllib.parse import quote, urljoin, urlsplit, urlunsplit
from urllib.request import getproxies, proxy_bypass

import httpx

from .evidence import HTTP_HEADERS, _http_url

# These public publishers and CDNs can also be reached through an operator-configured proxy.
PROXY_HOSTS = frozenset({
    'backiee.com', 'blogger.googleusercontent.com', 'i.pinimg.com', 'www.pinterest.com',
    'au.pinterest.com', 'ca.pinterest.com', 'img.freepik.com', 'img.magnific.com',
    'www.magnific.com', 'www.freepik.com', 'iqtisadiyyat.az', 'www.goodfon.ru',
    'www.goodfon.com', 'images.pexels.com', 'www.pexels.com', 'images.unsplash.com',
    'unsplash.com', 'commons.wikimedia.org', 'upload.wikimedia.org',
})
REDIRECTS = {301, 302, 303, 307, 308}
HEADERS = {**HTTP_HEADERS, 'Accept-Encoding': 'identity'}
TRANSLATION_PREFIXES = tuple(ipaddress.IPv6Network(prefix) for prefix in ('64:ff9b::/96', '64:ff9b:1::/48'))


class FetchError(ValueError):
    pass


@dataclass(frozen=True)
class PublicResponse:
    url: str
    content_type: str
    data: bytes
    link: str = ''


def public_url(value):
    parsed = _http_url(value)
    if not parsed or '@' in parsed.netloc:
        return None
    try:
        hostname = parsed.hostname.rstrip('.').encode('idna').decode('ascii')
        default = 443 if parsed.scheme == 'https' else 80
        if parsed.port not in {None, default} or '.' not in hostname or ':' in hostname:
            return None
        try:
            if not _global(ipaddress.ip_address(hostname)):
                return None
        except ValueError:
            pass  # DNS names and alternate address representations are checked at resolution.
    except (UnicodeError, ValueError):
        return None
    return urlunsplit((parsed.scheme, hostname, parsed.path or '/', parsed.query, ''))


def _global(address):
    if isinstance(address, ipaddress.IPv6Address):
        if (address.ipv4_mapped is not None or address.sixtofour is not None or address.teredo is not None
                or any(address in prefix for prefix in TRANSLATION_PREFIXES)):
            return False
    return address.is_global and not address.is_multicast and not address.is_reserved


def _addresses(hostname, port):
    resolved = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP)
    if not resolved or any(not _global(ipaddress.ip_address(entry[4][0])) for entry in resolved):
        raise FetchError('адрес не относится к публичной сети')
    return list(dict.fromkeys(entry[4][0] for entry in resolved))


def _body(chunks, deadline, limit):
    data = bytearray()
    for chunk in chunks:
        if monotonic() >= deadline:
            raise FetchError('истекло время проверки')
        if len(data) + len(chunk) > limit:
            raise FetchError('ответ превышает допустимый размер')
        data.extend(chunk)
    return bytes(data)


def _response(status, headers, chunks, deadline, limit):
    if status in REDIRECTS:
        location = headers.get('location', '')
        if not location:
            raise FetchError('переадресация без адреса')
        return location
    if not 200 <= status < 300:
        raise FetchError(f'сайт ответил HTTP {status}')
    length = headers.get('content-length', '')
    if length.isascii() and length.isdecimal() and len(length) < 12 and int(length) > limit:
        raise FetchError('ответ превышает допустимый размер')
    encoding = headers.get('content-encoding', 'identity')
    if encoding.lower() not in {'', 'identity'}:
        raise FetchError('сайт вернул неподдерживаемое кодирование')
    return (headers.get('content-type', '').split(';', 1)[0].strip().lower(),
            _body(chunks, deadline, limit), headers.get('link', ''))


def _fetch_direct(parsed, addresses, deadline, limit):
    port = 443 if parsed.scheme == 'https' else 80
    target = quote(parsed.path, safe="/%:@!$&'()*+,;=-._~")
    if parsed.query:
        target += '?' + quote(parsed.query, safe="%:@!$&'()*+,;=/?-._~")
    factory = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
    last_error = None
    for address in addresses[:3]:
        timeout = min(3.5, deadline - monotonic())
        if timeout <= 0:
            raise FetchError('истекло время проверки')
        connection = factory(parsed.hostname, port, timeout=timeout)
        # The socket connects only to the address already resolved and validated.
        # The HTTP Host header and HTTPS SNI still use the publisher's hostname.
        connection._create_connection = lambda *_args: socket.create_connection((address, port), timeout)
        try:
            connection.request('GET', target, headers=HEADERS)
            with connection.getresponse() as response:
                return _response(response.status, response.headers, iter(lambda: response.read1(32_768), b''),
                                 deadline, limit)
        except (OSError, http.client.HTTPException) as exc:
            last_error = exc
        finally:
            connection.close()
    if isinstance(last_error, TimeoutError):
        raise FetchError('сайт не ответил вовремя')
    raise FetchError('не удалось установить соединение с сайтом')


def fetch(url, deadline, limit):
    for _ in range(4):
        url = public_url(url)
        if not url:
            raise FetchError('адрес не поддерживается')
        parsed = urlsplit(url)
        timeout = min(3.5, deadline - monotonic())
        if timeout <= 0:
            raise FetchError('истекло время проверки')
        try:
            addresses = _addresses(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
        except OSError as exc:
            raise FetchError('не удалось найти сетевой адрес сайта') from exc
        proxy = getproxies()
        if parsed.hostname in PROXY_HOSTS and (proxy.get(parsed.scheme) or proxy.get('all')) and not proxy_bypass(parsed.hostname):
            try:
                with httpx.Client(headers=HEADERS, follow_redirects=False, timeout=timeout) as client:
                    with client.stream('GET', url) as response:
                        result = _response(response.status_code, response.headers, response.iter_raw(), deadline, limit)
            except httpx.TimeoutException as exc:
                raise FetchError('сайт не ответил вовремя') from exc
            except (httpx.HTTPError, httpx.InvalidURL) as exc:
                raise FetchError('не удалось установить соединение с сайтом') from exc
        else:
            result = _fetch_direct(parsed, addresses, deadline, limit)
        if isinstance(result, tuple):
            content_type, data, link = result
            return PublicResponse(url, content_type, data, link)
        url = urljoin(url, result)
    raise FetchError('слишком много переадресаций')
