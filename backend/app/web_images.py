"""Verify image matches and material-specific declarations on public websites."""

import base64
import re
from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePosixPath
from time import monotonic
from urllib.parse import parse_qs, unquote, urljoin, urlsplit

from PIL import Image, ImageChops, ImageStat

from .evidence import PlainText, _plain
from .extraction import image_phash, jpeg_preview, pixel_hash
from .license_evidence import extract_license, page_declarations
from .public_web import FetchError, fetch, public_url

MAX_LINKS = 18
MAX_DOWNLOAD = 6_000_000
MAX_PIXELS = 12_000_000
BACKIEE_TERMS = 'https://backiee.com/terms-and-conditions'
STOCK_TERMS = 'https://www.magnific.com/ai/docs/licenses-attribution'
STOCK_CDNS = {'img.freepik.com', 'img.magnific.com'}
STOCK_PAGES = {'www.freepik.com', 'www.magnific.com'}
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif'}
IMAGE_FORMATS = ('JPEG', 'PNG', 'WEBP', 'GIF', 'AVIF')
READ_ERRORS = (OSError, ValueError, SyntaxError, Image.DecompressionBombError)


def _target(value):
    url = public_url(value)
    if not url:
        return None
    parsed = urlsplit(url)
    if parsed.hostname in {'ya.ru', 'yandex.ru', 'yandex.com'} and parsed.path == '/images/search':
        query = parse_qs(parsed.query)
        return public_url((query.get('img_url') or [''])[0])
    return url


class ImagePage(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.images = []
        self.photos = []
        self.title = ''
        self.author = ''
        self.publisher = ''
        self.original = ''
        self.canonical = ''

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        rel = (attrs.get('rel') or '').lower().split()
        if tag == 'meta':
            field = (attrs.get('property') or attrs.get('name') or attrs.get('itemprop') or '').lower()
            value = attrs.get('content') or ''
            if value and field in {'og:image', 'og:image:url', 'og:image:secure_url', 'twitter:image', 'image'}:
                self.images.append(value)
            if not self.title and field in {'og:title', 'twitter:title'}:
                self.title = _plain(value, 150)
            if not self.author and field in {'author', 'article:author'} and not value.startswith(('https:', 'http:')):
                self.author = _plain(value, 150)
            if field == 'og:site_name':
                self.publisher = _plain(value, 150)
        if tag == 'link' and 'canonical' in rel:
            self.canonical = attrs.get('href') or ''
        if tag in {'a', 'link'} and {'original', 'via'}.intersection(rel):
            self.original = self.original or (attrs.get('href') or '')
        if tag == 'img' and len(self.photos) < 12:
            value = attrs.get('src') or attrs.get('data-src') or ''
            if value and not value.startswith('data:'):
                self.photos.append(value)


def _sample(image):
    return image.convert('RGB').resize((96, 96), Image.Resampling.LANCZOS)


def _image(data):
    with Image.open(BytesIO(data), formats=IMAGE_FORMATS) as image:
        if image.width * image.height > MAX_PIXELS or getattr(image, 'is_animated', False):
            raise FetchError('картинка превышает лимит или является анимацией')
        return {'sha256': sha256(data).hexdigest(), 'size': image.size,
                'pixels': pixel_hash(image) if image.width * image.height <= 2_000_000 else None,
                'phash': int(image_phash(image), 16), 'sample': _sample(image),
                'preview': jpeg_preview(image, 360)}


def _difference(left, right):
    channels = ImageStat.Stat(ImageChops.difference(left, right)).mean
    return sum(channels) / len(channels)


def _crop_distance(full, smaller, ratio):
    width, height = (96, round(96 / ratio)) if ratio >= 1 else (round(96 * ratio), 96)
    if min(width, height) < 34:
        return 255
    for fraction in (1, 0.82):
        w, h = round(width * fraction), round(height * fraction)
        for x, y in ((0.5, 0.5), (0, 0.5), (1, 0.5), (0.5, 0), (0.5, 1)):
            dx, dy = round((96 - w) * x), round((96 - h) * y)
            if _difference(full.crop((dx, dy, dx + w, dy + h)).resize((96, 96), Image.Resampling.BILINEAR), smaller) <= 12:
                return 12
    return 255


def _match(asset, candidate, sample):
    if asset['sha256'] == candidate['sha256']:
        return 0, 'bytes', 'Файл совпал с загруженным изображением'
    if candidate['pixels'] and asset.get('pixel_sha256') == candidate['pixels']:
        return 0, 'pixels', 'Декодированные пиксели совпали'
    if not sample or not asset.get('phash') or min(asset['width'], asset['height'], *candidate['size']) < 96:
        return None
    width, height = candidate['size']
    ratio = (width / height) / (asset['width'] / asset['height'])
    other = candidate['sample']
    distance = (int(asset['phash'], 16) ^ candidate['phash']).bit_count()
    color = _difference(sample, other)
    textured = max(ImageStat.Stat(sample.convert('L')).stddev) > 12
    if 0.96 <= ratio <= 1.04 and distance <= 5 and color <= 17 and textured:
        return 1, 'transformed', 'Совпадение после изменения размера или сжатия'
    if 0.92 <= ratio <= 1.08 and distance <= 8 and color <= 30 and textured:
        return 2, 'similar', 'Возможное совпадение с другой обрезкой или обработкой'
    if textured and (0.45 <= ratio <= 0.96 or 1.04 <= ratio <= 2.2) and (
        _crop_distance(other, sample, 1 / ratio) <= 12 or _crop_distance(sample, other, ratio) <= 12
    ):
        return 2, 'similar', 'Возможная обрезанная копия'
    return None


def _stock_card(url):
    parsed = urlsplit(url)
    if parsed.hostname not in STOCK_CDNS:
        return None
    name = re.fullmatch(r'/(premium|free)-(photo|vector)/([a-z0-9-]+)_\d+-(\d+)\.[a-z]+',
                        unquote(parsed.path), re.IGNORECASE)
    if name:
        return f'https://www.magnific.com/{name[1]}-{name[2]}/{name[3]}_{name[4]}.htm'
    return None


def _page_images(response):
    page = ImagePage()
    page.feed(response.data.decode('utf-8', errors='replace'))
    by_url, original = page_declarations(response.data, response.url)
    page.original = page.original or original
    urls = list(by_url)
    for value in page.images + page.photos:
        if url := public_url(urljoin(response.url, value)):
            if url != response.url and url not in urls:
                urls.append(url)
    return page, [(url, by_url.get(url, [])) for url in urls[:4]]


def _describe(response, deadline, read, *, page_hint=None):
    is_image = response.content_type.startswith('image/') or (
        response.content_type in {'', 'application/octet-stream'}
        and PurePosixPath(urlsplit(response.url).path).suffix.lower() in IMAGE_SUFFIXES
    )
    if is_image:
        return [{**_image(response.data), 'url': response.url, 'media': response.url}], None, []
    if response.content_type and response.content_type not in {'text/html', 'application/xhtml+xml'}:
        raise FetchError('сайт вернул неподдерживаемый тип содержимого')
    page, entries = _page_images(response)
    own_images = {url for url, _ in entries}
    candidates, errors = [], []
    if page_hint:
        # The index identifies a particular file on the page. Keep page licence data
        # tied to the same URL if the publisher declares it.
        hints = [url for url in page_hint if url != response.url]
        known = dict(entries)
        entries = list(dict.fromkeys([*known, *hints]))[:5]
        entries = [(url, known.get(url, [])) for url in entries]
    for url, declarations in entries:
        try:
            media = read(url, deadline, MAX_DOWNLOAD)
            record = _image(media.data)
        except READ_ERRORS as exc:
            errors.append(str(exc) if isinstance(exc, FetchError) else 'не удалось прочитать картинку')
            continue
        candidates.append({**record, 'url': response.url, 'media': media.url, 'title': page.title,
                           'page_author': page.author, 'publisher': page.publisher, 'declarations': declarations,
                           'page_data': response.data, 'image_on_page': url in own_images})
    original = public_url(urljoin(response.url, page.original or page.canonical))
    return candidates, original if original != response.url else None, errors


def _material(candidate, match, deadline, read):
    rank, kind, message = match
    parsed = urlsplit(candidate['url'])
    title = candidate.get('title') or parsed.hostname
    result = {'title': title, 'url': candidate['url'], 'match': message, 'match_kind': kind,
              'verification': 'possible' if rank == 2 else 'verified'}
    if candidate.get('publisher'):
        result['publisher'] = candidate['publisher']
    if candidate.get('page_author'):
        result['page_author'] = candidate['page_author']
    if rank == 2:
        return result, None

    record = {'title': title, 'url': candidate['url'], 'author': '', 'match': message, 'match_kind': kind}
    evidence = extract_license(
        candidate['url'], candidate['media'], page_data=candidate.get('page_data', b''),
        declarations=candidate.get('declarations', []), image_on_page=candidate.get('image_on_page', False),
        deadline=deadline, read=read,
    )
    if evidence.get('source'):
        result['license_source'] = evidence['source']
    if evidence.get('url'):
        result['terms_url'] = evidence['url']
    if evidence.get('notice'):
        result['terms'] = evidence['notice']
    if licence := evidence.get('license'):
        if evidence.get('author'):
            result['author'] = record['author'] = evidence['author']
        result.setdefault('terms', f'Для этого изображения указана {licence["name"]}.')
        return result, record | {'license': licence, 'license_source': evidence['source']}
    if evidence.get('conflict'):
        return result, None
    if parsed.hostname == 'backiee.com' and parsed.path.startswith('/wallpaper/') and rank == 0:
        try:
            text = PlainText()
            text.feed(read(BACKIEE_TERMS, deadline, 400_000).data.decode('utf-8', errors='replace'))
            text.close()
            policy = ' '.join(' '.join(text.parts).split()).casefold()
        except FetchError as exc:
            result['terms'] = f'Условия площадки недоступны: {exc}.'
            return result, None
        if 'non-commercial use' in policy and 'commercial use requires permission' in policy:
            result.update(terms_url=BACKIEE_TERMS, terms='Для коммерческого использования нужно разрешение правообладателя.')
            record['license_source'] = 'Условия Backiee'
            record['license'] = {'kind': 'backiee_personal', 'name': 'Backiee · личное использование',
                                 'url': BACKIEE_TERMS, 'commercial_allowed': False, 'restrictions': []}
            return result, record
    if parsed.hostname in STOCK_PAGES and parsed.path.startswith(('/premium-photo/', '/premium-vector/')):
        asset_id = re.search(r'_(\d+)\.htm$', parsed.path)
        media = urlsplit(candidate['media'])
        if asset_id and media.hostname in STOCK_CDNS and re.search(rf'-{asset_id[1]}\.[a-z]+$', media.path, re.I):
            result.update(terms_url=STOCK_TERMS,
                          terms='Скачайте этот материал с подходящей лицензией и сохраните её.')
            record['license_source'] = 'Правило Magnific / Freepik'
            record['license'] = {'kind': 'stock_download', 'name': 'Magnific / Freepik · премиум-материал',
                                 'url': STOCK_TERMS, 'restrictions': ['Требуется лицензия на скачанный материал.']}
            return result, record
    if parsed.hostname in STOCK_CDNS and parsed.path.startswith('/premium-'):
        result.update(terms_url=STOCK_TERMS, terms='Найдена премиум-копия. Условия проверьте в карточке материала.')

    return result, None


def _check_error(error):
    if isinstance(error, FetchError):
        if 'HTTP 403' in str(error):
            return 'Требуется ручная проверка: доступ к странице ограничен (HTTP 403).'
        return str(error)
    return 'Не удалось прочитать ответ сайта.'


def _inspect(target, deadline, read, hints):
    reasons = []
    candidates = []
    try:
        response = read(target, deadline, MAX_DOWNLOAD)
        candidates, original, reasons = _describe(response, deadline, read, page_hint=hints)
    except READ_ERRORS as exc:
        return [], {'url': target, 'status': 'unavailable', 'reason': _check_error(exc)}

    stock_card = _stock_card(response.url)
    original = original or stock_card
    if not original and response.link:
        linked = re.search(r'<([^>]+)>\s*;\s*rel\s*=\s*"?canonical', response.link, re.IGNORECASE)
        if linked:
            original = public_url(urljoin(response.url, linked[1]))
    if original and original != response.url:
        try:
            upstream = read(original, deadline, MAX_DOWNLOAD)
            if original == stock_card and urlsplit(original).path.split('/')[1] != urlsplit(upstream.url).path.split('/')[1]:
                raise FetchError('каталог перенаправил на материал другого типа')
            found, _, errors = _describe(upstream, deadline, read)
            candidates.extend(found)
            reasons.extend(errors)
        except READ_ERRORS as exc:
            reasons.append(f'исходная страница {urlsplit(original).hostname}: '
                           + _check_error(exc))
    if not candidates and not reasons:
        reasons.append('на странице не найдено доступное изображение для сверки')
    return candidates, {'url': target, 'status': 'checked' if candidates else 'unavailable',
                        'reason': '; '.join(dict.fromkeys(reasons))[:320]}


def compare_links(assets, urls, previews, *, image_hints=None):
    """Verify files on arbitrary public hosts while retaining per-link diagnostics."""
    targets = list(dict.fromkeys(target for url in urls if (target := _target(url))))[:MAX_LINKS]
    if not targets:
        return {'checked': 0, 'eligible': 0, 'matched': 0}, {}
    image_hints = image_hints or {}
    samples = {}
    for asset in assets:
        if data := previews.get(asset['sha256']):
            with Image.open(BytesIO(data), formats=('JPEG',)) as image:
                samples[asset['sha256']] = _sample(image)
    deadline = monotonic() + 15
    by_image, checks = {}, []
    with ThreadPoolExecutor(max_workers=6) as pool:
        futures = [pool.submit(_inspect, target, deadline, fetch, image_hints.get(target)) for target in targets]
        for future in futures:
            candidates, check = future.result()
            checks.append(check)
            matches = False
            for candidate in candidates:
                for asset in assets:
                    sha = asset['sha256']
                    if result := _match(asset, candidate, samples.get(sha)):
                        matches = True
                        by_image.setdefault(sha, []).append((result, candidate, check['url']))
            if matches:
                check['status'] = 'matched'
    matches = {}
    policies = {}
    for sha, entries in by_image.items():
        links, labels, materials = {}, [], []
        for match, item, origin in sorted(entries, key=lambda entry: entry[0][0]):
            key = (item['url'], item['media'], match)
            if key not in policies:
                policies[key] = _material(item, match, deadline + 4, fetch)
            evidence, material = policies[key]
            evidence = evidence | {'checked_url': origin}
            current = links.get(item['url'])
            if not current or (material and not current.get('terms_url')):
                links[item['url']] = evidence
            if material:
                materials.append(material)
            if item.get('title'):
                labels.append(item['title'])
            elif urlsplit(item['url']).hostname in STOCK_CDNS:
                labels.append(PurePosixPath(unquote(urlsplit(item['url']).path)).stem.split('_', 1)[0].replace('-', ' '))
        matches[sha] = {'links': list(links.values())[:6], 'labels': labels[:6], 'materials': materials[:6]}
    return {'checked': sum(check['status'] != 'unavailable' for check in checks),
            'eligible': len(targets), 'matched': len(matches), 'pages': checks}, matches


def licensed_replacements(asset, suggestions, preview, deadline, excluded):
    """Check distinct candidate images and return only previews with confirmed terms."""
    sample = None
    if preview:
        with Image.open(BytesIO(preview), formats=('JPEG',)) as image:
            sample = _sample(image)
    choices = []
    for result in suggestions[:6]:
        if monotonic() >= deadline or len(choices) >= 3:
            break
        url = result['url']
        if url in excluded:
            continue
        candidates, _ = _inspect(url, deadline, fetch, None)
        for candidate in candidates:
            if (not candidate.get('image_on_page') or candidate['url'] in excluded or min(candidate['size']) < 200
                    or _match(asset, candidate, sample)):
                continue
            if (hint := result.get('image_url')):
                # A search hit can lead to a gallery or a different card after a redirect.
                actual, hinted = urlsplit(candidate['media']), urlsplit(hint)
                if actual.hostname != hinted.hostname or actual.path != hinted.path:
                    continue
            _, material = _material(candidate, (0, 'pixels', 'Изображение на карточке'), deadline, fetch)
            if not material:
                continue
            licence = material['license']
            if (licence.get('commercial_allowed') is not True or licence.get('restrictions')
                    or licence.get('derivatives_distribution_allowed') is not True
                    or (licence.get('attribution_required') and not material['author'])):
                continue
            distance = ((int(asset['phash'], 16) ^ candidate['phash']).bit_count() if asset.get('phash') else None)
            choices.append(material | {'preview': base64.b64encode(candidate['preview']).decode('ascii'),
                                       'download_url': candidate['media'], 'description': '',
                                       'distance': distance, 'relevance': 1})
            excluded.add(material['url'])
            break
    return choices
