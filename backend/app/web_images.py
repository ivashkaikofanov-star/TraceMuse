"""Compare uploaded images with public files linked in the material."""

from concurrent.futures import ThreadPoolExecutor
from hashlib import sha256
from html.parser import HTMLParser
from io import BytesIO
from pathlib import PurePosixPath
from time import monotonic
from urllib.parse import parse_qs, unquote, urlsplit, urlunsplit

import httpx
from PIL import Image, ImageChops, ImageStat

from .evidence import HTTP_HEADERS, _http_url, _plain, _read
from .extraction import image_phash, pixel_hash

IMAGE_PATHS = {
    'backiee.com': ('/static/wallpapers/',),
    'blogger.googleusercontent.com': ('/img/',),
    'i.pinimg.com': ('/',),
    'img.freepik.com': ('/free-photo/', '/premium-photo/', '/free-vector/', '/premium-vector/'),
    'img.magnific.com': ('/free-photo/', '/premium-photo/', '/free-vector/', '/premium-vector/'),
    'iqtisadiyyat.az': ('/storage/posts/',),
    'images.pexels.com': ('/photos/',),
    'images.unsplash.com': ('/photo-',),
    'upload.wikimedia.org': ('/wikipedia/commons/',),
}
PAGE_PATHS = {
    'backiee.com': ('/wallpaper/',),
    'www.freepik.com': ('/free-photo/', '/premium-photo/', '/free-vector/', '/premium-vector/'),
    'www.magnific.com': ('/free-photo/', '/premium-photo/', '/free-vector/', '/premium-vector/'),
    'www.goodfon.ru': ('/wallpaper/',),
    'www.goodfon.com': ('/wallpaper/',),
    'www.pexels.com': ('/photo/',),
    'unsplash.com': ('/photos/',),
}
IMAGE_SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.gif'}
MAX_LINKS = 12
MAX_DOWNLOAD = 6_000_000
MAX_PIXELS = 12_000_000
NETWORK_ERRORS = (httpx.HTTPError, OSError, ValueError, Image.DecompressionBombError)


def _target(value):
    parsed = _http_url(value)
    if parsed and parsed.hostname in {'ya.ru', 'yandex.ru', 'yandex.com'} and parsed.path == '/images/search':
        value = parse_qs(parsed.query).get('img_url', [''])[0]
        parsed = _http_url(value)
    if not parsed or parsed.scheme != 'https' or parsed.netloc.lower() not in {parsed.hostname, f'{parsed.hostname}:443'}:
        return None
    path = unquote(parsed.path)
    if any(segment in {'.', '..'} for segment in path.split('/')) or '\\' in path or any(ord(c) < 32 for c in path):
        return None
    host = parsed.hostname
    if path.startswith(IMAGE_PATHS.get(host, ())) and (PurePosixPath(path).suffix.lower() in IMAGE_SUFFIXES
                                                       or host == 'images.unsplash.com'):
        kind = 'image'
    elif path.startswith(PAGE_PATHS.get(host, ())):
        kind = 'page'
    else:
        return None
    return urlunsplit(parsed._replace(fragment='')), host, kind


class SocialPreview(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.image = ''
        self.title = ''

    def handle_starttag(self, tag, attributes):
        if tag != 'meta':
            return
        attrs = dict(attributes)
        property_name = attrs.get('property') or attrs.get('name')
        if property_name == 'og:image' and not self.image:
            self.image = attrs.get('content') or ''
        if property_name == 'og:title' and not self.title:
            self.title = _plain(attrs.get('content') or '', 120)


def _read_candidate(client, deadline, target):
    url, host, kind = target
    title = ''
    if kind == 'page':
        tags = SocialPreview()
        tags.feed(_read(client, url, None, deadline, 400_000).decode('utf-8', errors='replace'))
        media = _target(tags.image)
        if not media or media[2] != 'image':
            return None
        image_url, _, _ = media
        title = tags.title
    else:
        image_url = url
    data = _read(client, image_url, None, deadline, MAX_DOWNLOAD)
    with Image.open(BytesIO(data), formats=('JPEG', 'PNG', 'WEBP', 'GIF')) as image:
        if image.width * image.height > MAX_PIXELS or getattr(image, 'is_animated', False):
            return None
        stock = host in {'img.magnific.com', 'img.freepik.com'}
        slug = PurePosixPath(unquote(urlsplit(image_url).path)).stem.split('_', 1)[0] if stock else ''
        return {'url': url, 'title': title or host, 'label': title or slug[:120].replace('-', ' '),
                'sha256': sha256(data).hexdigest(), 'size': image.size,
                'pixels': pixel_hash(image) if image.width * image.height <= 2_000_000 else None,
                'phash': int(image_phash(image), 16), 'sample': _sample(image)}


def _sample(image):
    return image.convert('RGB').resize((32, 32), Image.Resampling.LANCZOS)


def _match(asset, candidate, sample):
    if asset['sha256'] == candidate['sha256']:
        return 0, 'Файл совпал с изображением в презентации'
    if candidate['pixels'] and asset.get('pixel_sha256') == candidate['pixels']:
        return 0, 'Декодированные пиксели совпали'
    if asset.get('phash') and min(asset['width'], asset['height'], *candidate['size']) >= 96:
        width, height = candidate['size']
        ratio = (width / height) / (asset['width'] / asset['height'])
        distance = (int(asset['phash'], 16) ^ candidate['phash']).bit_count()
        if 0.96 <= ratio <= 1.04 and distance <= 5:
            return 1, 'Изображение визуально совпадает по форме и пропорциям'
        if sample and 0.92 <= ratio <= 1.08 and distance <= 8:
            difference = ImageStat.Stat(ImageChops.difference(sample, candidate['sample'])).mean
            if sum(difference) / len(difference) <= 30:
                return 2, 'Возможное совпадение с другой обрезкой или обработкой'
    return None


def _evidence(rank, item, message):
    link = {'title': item['title'], 'url': item['url'], 'match': message}
    url = urlsplit(item['url'])
    if rank == 0 and url.hostname == 'backiee.com' and url.path.startswith('/wallpaper/'):
        link.update(terms_url='https://backiee.com/terms-and-conditions',
                    terms='Backiee разрешает пользователям личное некоммерческое использование обоев. '
                          'Для коммерческого использования получите разрешение правообладателя или замените изображение.')
    elif rank < 2 and url.hostname in {'img.magnific.com', 'img.freepik.com'} and url.path.startswith('/premium-'):
        link.update(terms_url='https://www.magnific.com/ai/docs/licenses-attribution',
                    terms='Найдена копия из премиум-каталога Magnific. '
                          'Скачайте материал с подходящей лицензией и сохраните её или замените изображение.')
    return link


def compare_links(assets, urls, previews):
    """Inspect known public hosts only. A visual match never grants a licence."""
    targets = list(dict.fromkeys(target for url in urls if (target := _target(url))))[:MAX_LINKS]
    by_image = {}
    if not targets:
        return {'checked': 0, 'eligible': 0, 'matched': 0}, by_image

    samples = {}
    for asset in assets:
        if data := previews.get(asset['sha256']):
            with Image.open(BytesIO(data), formats=('JPEG',)) as image:
                samples[asset['sha256']] = _sample(image)
    deadline = monotonic() + 10
    with httpx.Client(headers=HTTP_HEADERS, follow_redirects=False) as client, ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_read_candidate, client, deadline, target) for target in targets]
        checked = 0
        for future in futures:
            try:
                candidate = future.result()
            except NETWORK_ERRORS:
                continue
            if candidate is None:
                continue
            checked += 1
            for asset in assets:
                if result := _match(asset, candidate, samples.get(asset['sha256'])):
                    rank, message = result
                    by_image.setdefault(asset['sha256'], []).append((rank, candidate, message))

    matches = {}
    for sha, entries in by_image.items():
        ordered = sorted(entries, key=lambda entry: entry[0])[:4]
        matches[sha] = {'links': [_evidence(rank, item, message) for rank, item, message in ordered],
                        'labels': [item['label'] for rank, item, _ in ordered if rank < 2]}
    return {'checked': checked, 'eligible': len(targets), 'matched': len(matches)}, matches
