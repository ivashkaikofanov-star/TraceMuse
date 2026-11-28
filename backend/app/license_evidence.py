"""Extract terms for a particular image from public pages and image metadata."""

import json
import re
import shutil
import subprocess
from collections import defaultdict, deque
from pathlib import Path
from tempfile import TemporaryDirectory
from time import monotonic
from urllib.parse import urljoin, urlsplit

import extruct
from lxml import etree, html

from .config import DEFAULT_EXIFTOOL_PATH, settings
from .evidence import PlainText, _license, _plain
from .public_web import FetchError, fetch, public_url

FREEPIK_TERMS = 'https://www.freepik.com/legal/terms-of-use'
UNSPLASH_TERMS = 'https://unsplash.com/license'
PEXELS_TERMS = 'https://www.pexels.com/license/'
MAX_IMAGE = 6_000_000
MAX_HTML = 1_500_000
SOURCE_NAMES = {'json-ld': 'JSON-LD на сайте', 'microdata': 'Microdata на сайте'}
READ_ERRORS = (OSError, ValueError, SyntaxError)


def host_matches(host, domain):
    return bool(host and (host == domain or host.endswith('.' + domain)))


def field_url(value):
    if isinstance(value, dict):
        value = value.get('@id') or value.get('url')
    return value if isinstance(value, str) and len(value) <= 2048 else ''


def _name(value):
    if isinstance(value, list):
        return ', '.join(filter(None, (_name(item) for item in value[:3] if isinstance(item, (dict, str)))))[:200]
    if isinstance(value, dict):
        properties = value.get('properties')
        value = properties.get('name') if isinstance(properties, dict) else value.get('name')
    return _plain(value, 200)


def _image_urls(value, base):
    values = value if isinstance(value, list) else [value]
    urls = []
    for item in values[:12]:
        if isinstance(item, dict):
            if isinstance(item.get('properties'), dict):
                item = item['properties']
            item = item.get('contentUrl') or item.get('url') or item.get('@id')
        raw = field_url(item)
        if raw and (url := public_url(urljoin(base, raw))) and url != base:
            urls.append(url)
    return urls


def page_declarations(data, page_url):
    """Index declarations by the media URL named within each record."""
    by_image = defaultdict(list)
    try:
        records = extruct.extract(data[:MAX_HTML], base_url=page_url, syntaxes=list(SOURCE_NAMES), errors='ignore')
    except (ValueError, TypeError, UnicodeError, etree.Error, RecursionError):
        return by_image, ''
    queue = deque((syntax, value) for syntax in SOURCE_NAMES for value in records.get(syntax, [])[:60])
    original = ''
    for _ in range(220):
        if not queue:
            break
        syntax, node = queue.popleft()
        if isinstance(node, list):
            queue.extend((syntax, item) for item in node[:24])
            continue
        if not isinstance(node, dict):
            continue
        properties = node.get('properties') if syntax == 'microdata' else node
        if not isinstance(properties, dict):
            continue
        types = node.get('type') if syntax == 'microdata' else node.get('@type')
        types = types if isinstance(types, list) else [types]
        kinds = {kind.rsplit('/', 1)[-1] for kind in types if isinstance(kind, str)}
        if kinds.intersection({'ImageObject', 'CreativeWork'}):
            images = _image_urls(properties.get('contentUrl') or properties.get('image'), page_url)
            if 'ImageObject' in kinds and not images:
                images = _image_urls(properties.get('url'), page_url)
            if 'CreativeWork' not in kinds or len(set(images)) == 1:
                for url in dict.fromkeys(images):
                    by_image[url].append((SOURCE_NAMES[syntax], properties))
        original = original or field_url(properties.get('isBasedOn'))
        queue.extend((syntax, properties[key]) for key in ('@graph', 'image', 'mainEntity',
                                                           'associatedMedia', 'primaryImageOfPage')
                     if key in properties)
    return by_image, original


def _recognized(url):
    return _license({'extmetadata': {'LicenseUrl': {'value': url}}})


def _licensed_text(text):
    # Match explicit statements with a definite scope. A bare keyword cannot grant rights.
    for pattern, kind in ((r'^(?:license[d]? under\s+)?(?:creative commons\s+)?CC[ -]?0(?:\s+1\.0)?[.!]?$', 'cc0'),
                          (r'^(?:license[d]? under\s+)?CC\s+(BY(?:-NC)?(?:-SA|-ND)?)\s+([1-4]\.\d)[.!]?$', 'cc')):
        match = re.fullmatch(pattern, text.strip(), re.I)
        if match:
            url = ('https://creativecommons.org/publicdomain/zero/1.0/' if kind == 'cc0'
                   else f'https://creativecommons.org/licenses/{match[1].lower()}/{match[2]}/')
            return _recognized(url)
    return {}


def _page_result(declarations, page_url):
    findings = {}
    note = {}
    for source, row in declarations:
        raw = row.get('license')
        if isinstance(raw, list):
            if len(raw) != 1:
                continue
            raw = raw[0]
        address = field_url(raw)
        url = public_url(urljoin(page_url, address)) if address.startswith(('http:', 'https:', '/', './', '../')) else None
        notice = _plain(row.get('copyrightNotice'), 300)
        licence = _recognized(url) if url else _licensed_text(address or notice)
        author = _name(row.get('creator') or row.get('author'))
        if notice or address:
            note = {'source': source, 'notice': notice or _plain(address, 250), 'url': url or ''}
        if licence.get('kind'):
            findings[licence['kind'], licence.get('url'), author] = {
                'source': source, 'license': licence, 'author': author, 'notice': notice, 'url': licence['url'],
            }
    if len(findings) == 1:
        return next(iter(findings.values()))
    if len(findings) > 1:
        return {'source': next(iter(findings.values()))['source'], 'conflict': True,
                'notice': 'На странице указаны разные лицензии или авторы изображения.'}
    return note


def _exiftool_executable():
    default = DEFAULT_EXIFTOOL_PATH if DEFAULT_EXIFTOOL_PATH and Path(DEFAULT_EXIFTOOL_PATH).is_absolute() else None
    for path in (settings.exiftool_path.strip(), default, 'exiftool'):
        if path and (executable := shutil.which(path)):
            return executable
    return None


def _image_metadata(data, image_url, deadline, executable):
    remaining = deadline - monotonic()
    if remaining <= 0:
        return {}
    suffix = Path(urlsplit(image_url).path).suffix.lower()
    suffix = suffix if suffix in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.avif'} else '.jpg'
    try:
        with TemporaryDirectory(prefix='tracemuse-exif-') as directory:
            image = Path(directory) / f'image{suffix}'
            image.write_bytes(data)
            response = subprocess.run(
                [executable, '-json', '-XMP-dc:Rights', '-XMP-xmpRights:WebStatement', '-IPTC:CopyrightNotice',
                 '-XMP-dc:Creator', '-IPTC:By-line', str(image)], capture_output=True, check=True,
                timeout=min(3, remaining),
            )
        payload = json.loads(response.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return {}
    return payload[0] if isinstance(payload, list) and payload and isinstance(payload[0], dict) else {}


def _rights_page(url, deadline, read):
    page = read(url, deadline, MAX_HTML)
    direct = _recognized(page.url)
    if direct.get('kind'):
        return direct, ''
    parser = PlainText()
    parser.feed(page.data.decode('utf-8', errors='replace'))
    parser.close()
    text = ' '.join(' '.join(parser.parts).split())
    matches = {}
    for link in re.findall(r'https?://creativecommons\.org/(?:licenses/[^\s<>"\']+|publicdomain/zero/[^\s<>"\']+)',
                           page.data.decode('utf-8', errors='replace'), re.I)[:30]:
        licence = _recognized(link)
        if licence.get('kind'):
            matches[licence['kind'], licence.get('url')] = licence
    if len(matches) == 1:
        return next(iter(matches.values())), _plain(text, 300)
    if matches:
        return {}, _plain(text, 300)
    for item in re.findall(r'\bCC\s*0(?:\s+1\.0)?\b|\bCC\s+BY(?:-NC)?(?:-SA|-ND)?\s+[1-4]\.\d\b', text, re.I)[:30]:
        if licence := _licensed_text(item):
            matches[licence['kind'], licence.get('url')] = licence
    if len(matches) == 1 and not re.search(r'\b(?:not|never|except)\s+(?:under\s+)?CC\b', text, re.I):
        return next(iter(matches.values())), _plain(text, 300)
    lower = text.lower()
    if re.search(r'\b(?:no commercial use|commercial use (?:is )?(?:not allowed|not permitted|prohibited))\b', lower):
        return {'kind': 'custom_noncommercial', 'name': 'Только некоммерческое использование',
                'url': page.url, 'commercial_allowed': False, 'restrictions': []}, _plain(text, 300)
    if not matches and not re.search(r'\b(?:non-commercial|editorial use only|except|prohibited|not permitted|not allowed)\b', lower):
        allowed = re.search(r'\b(?:commercial use (?:is )?(?:allowed|permitted)|free for commercial use)\b', lower)
        no_credit = re.search(r'\b(?:no attribution required|attribution is not required)\b', lower)
        credit = re.search(r'\b(?:attribution (?:is )?required|must provide attribution)\b', lower)
        if allowed and (no_credit or credit):
            return {'kind': 'custom_public', 'name': 'Коммерческое использование разрешено', 'url': page.url,
                    'commercial_allowed': True, 'attribution_required': not bool(no_credit), 'restrictions': [],
                    'derivatives_distribution_allowed': False,
                    'usage_note': 'Условия взяты со страницы прав, указанной в метаданных изображения.'}, _plain(text, 300)
    return {}, _plain(text, 300)


def _commons_section(data, url):
    try:
        page = html.fromstring(data)
    except (ValueError, etree.Error):
        return {}
    headings = page.xpath('//*[@id="Licensing"]')
    if not headings:
        return {}
    node = headings[0]
    if node.tag == 'span' and node.getparent() is not None:
        node = node.getparent()
    if node.getparent() is not None and 'mw-heading' in (node.getparent().get('class') or '').split():
        node = node.getparent()
    paragraphs = []
    licences = {}
    for sibling in node.itersiblings():
        if sibling.tag in {'h1', 'h2', 'h3'} or 'mw-heading' in (sibling.get('class') or '').split():
            break
        paragraphs.append(sibling.text_content())
        for link in sibling.xpath('.//a/@href')[:60]:
            licence = _recognized(urljoin(url, link))
            if licence.get('kind'):
                licences[licence['kind'], licence['url']] = licence
        if len(paragraphs) >= 8:
            break
    text = _plain(' '.join(paragraphs), 360)
    result = {'source': 'Wikimedia Commons', 'url': url + '#Licensing', 'notice': text} if text else {}
    if len(licences) == 1:
        licence = next(iter(licences.values()))
        authors = page.xpath('//*[@id="fileinfotpl_aut"]/following-sibling::td[1]')
        author = _plain(authors[0].text_content(), 180) if authors else ''
        result.update(license=licence, author=author)
    return result


def _bank_rule(page_url, image_url):
    page, media = urlsplit(page_url), urlsplit(image_url)
    base = {'commercial_allowed': True, 'derivatives_distribution_allowed': True, 'restrictions': []}
    if host_matches(page.hostname, 'freepik.com') and host_matches(media.hostname, 'freepik.com'):
        paths = ('/free-photo/', '/free-vector/', '/free-psd/')
        if page.path.startswith(paths) and media.path.startswith(paths):
            return {'source': 'Правило Freepik', 'author': 'Freepik', 'url': FREEPIK_TERMS,
                    'license': base | {'kind': 'freepik_attribution', 'name': 'Только с атрибуцией · Freepik License',
                                       'attribution_required': True, 'url': FREEPIK_TERMS,
                                       'usage_note': 'Скачайте материал по действующей лицензии Freepik и добавьте подпись, указанную в ней.'}}
    if page.hostname in {'unsplash.com', 'www.unsplash.com'} and page.path.startswith('/photos/') and media.hostname == 'images.unsplash.com':
        if re.fullmatch(r'/photos/[^/]+/?', page.path):
            return {'source': 'Правило Unsplash', 'url': UNSPLASH_TERMS,
                    'license': base | {'kind': 'unsplash', 'name': 'Коммерческое использование разрешено · Unsplash License',
                                       'attribution_required': False, 'url': UNSPLASH_TERMS,
                                       'usage_note': 'Лицензия Unsplash допускает коммерческое использование. Продажа фото без существенной переработки ограничена.'}}
    if page.hostname in {'www.pexels.com', 'pexels.com'} and media.hostname == 'images.pexels.com':
        item = re.fullmatch(r'/(?:[a-z-]+/)?photo/[^/]*?(\d+)/?', page.path)
        if item and media.path.startswith(f'/photos/{item[1]}/'):
            return {'source': 'Правило Pexels', 'url': PEXELS_TERMS,
                    'license': base | {'kind': 'pexels', 'name': 'Коммерческое использование разрешено · Pexels License',
                                       'attribution_required': False, 'url': PEXELS_TERMS,
                                       'usage_note': 'Лицензия Pexels допускает коммерческое использование с ограничениями площадки.'}}
    return {}


def extract_license(page_url, image_url, *, page_data=None, declarations=None, image_on_page=None,
                    deadline=None, read=fetch):
    """Return the best available evidence for this media URL."""
    deadline = monotonic() + 12 if deadline is None else deadline
    page_url, image_url = public_url(page_url), public_url(image_url)
    if not page_url or not image_url:
        return {}
    if page_data is None and page_url != image_url:
        try:
            page = read(page_url, deadline, MAX_HTML)
        except READ_ERRORS as exc:
            return {'source': 'Ручная проверка', 'manual_review': True,
                    'notice': 'Требуется ручная проверка: доступ ограничен (HTTP 403).'} if 'HTTP 403' in str(exc) else {}
        page_url, page_data = page.url, page.data
    if declarations is None and page_data:
        index, _ = page_declarations(page_data, page_url)
        declarations = index.get(image_url, [])
    if image_on_page is None:
        image_on_page = bool(declarations or (page_data and image_url.encode('utf-8') in page_data))
    structured = _page_result(declarations or [], page_url)
    if structured.get('license') or structured.get('conflict'):
        return structured

    metadata_note = {}
    if executable := _exiftool_executable():
        try:
            media = read(image_url, deadline, MAX_IMAGE)
            metadata = _image_metadata(media.data, image_url, deadline, executable)
            statement = field_url(metadata.get('WebStatement'))
            address = public_url(statement)
            notice = _plain(metadata.get('Rights') or metadata.get('CopyrightNotice'), 300)
            if address:
                metadata_note = {'source': 'ExifTool · IPTC/XMP', 'url': address, 'notice': notice}
                licence, terms = _rights_page(address, deadline, read)
                metadata_note['notice'] = notice or terms
                if licence.get('kind'):
                    return metadata_note | {'license': licence, 'author': _name(metadata.get('Creator') or metadata.get('By-line'))}
            elif notice:
                metadata_note = {'source': 'ExifTool · IPTC/XMP', 'notice': notice}
        except READ_ERRORS:
            pass

    if image_on_page and (rule := _bank_rule(page_url, image_url)):
        return rule
    parsed = urlsplit(page_url)
    if page_data and parsed.hostname == 'commons.wikimedia.org' and '/wiki/File:' in parsed.path:
        if result := _commons_section(page_data, page_url):
            return result
    return structured or metadata_note
