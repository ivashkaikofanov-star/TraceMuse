import json
from io import BytesIO
from time import monotonic

import pytest
from PIL import Image, ImageDraw

from app import public_web, rights, web_images
from app.extraction import extract
from app.public_web import FetchError, PublicResponse
from app.reverse_search import SearchResult


def raster(tmp_path):
    picture = Image.new('RGB', (360, 240), '#76cce1')
    draw = ImageDraw.Draw(picture)
    draw.polygon([(0, 240), (110, 56), (240, 240)], fill='#18634e')
    draw.ellipse((220, 23, 306, 103), fill='#f0ca63')
    draw.rectangle((0, 154, 360, 240), fill='#4235a9')
    draw.ellipse((18, 171, 105, 216), fill='#ee7668')
    path = tmp_path / 'photo.jpg'
    picture.save(path, quality=95)
    return picture, path


def install_pages(monkeypatch, pages):
    def fetch(url, _deadline, _limit):
        if url not in pages:
            raise FetchError('сайт ответил HTTP 404')
        value = pages[url]
        if isinstance(value, Exception):
            raise value
        kind, data = value
        return PublicResponse(url, kind, data)
    monkeypatch.setattr(web_images, 'fetch', fetch)
    monkeypatch.setattr(rights, '_query', lambda *_args: {})
    monkeypatch.setattr(rights, 'search_replacements', lambda *_args: [])


@pytest.mark.parametrize(('licence_path', 'action'), [
    ('licenses/by/4.0/', 'credit'),
    ('publicdomain/zero/1.0/', 'keep'),
    ('licenses/by-nc/4.0/', 'restricted'),
    (None, 'unverified'),
])
def test_public_web_page_needs_an_image_specific_licence(tmp_path, monkeypatch, licence_path, action):
    _, path = raster(tmp_path)
    previews = {}
    ex = extract(path, path.name, previews=previews)
    photo = 'https://files.example.net/photo.jpg'
    origin = 'https://museum.example.org/collections/photo'
    copy = 'https://journal.example.com/article'
    licence = 'https://creativecommons.org/' + (licence_path or 'licenses/by/4.0/')
    declaration = {'@type': 'ImageObject', 'contentUrl': photo if licence_path else '/different.jpg',
                   'license': licence, 'creator': {'name': 'Example Author'}}
    origin_html = '<meta property="og:image" content="'+photo+'"><meta property="og:site_name" content="Example Museum">' \
                  '<link rel="license" href="'+licence+'"><script type="application/ld+json">' + json.dumps(declaration) + '</script>'
    install_pages(monkeypatch, {
        copy: ('text/html', f'<meta property="og:image" content="{photo}"><a rel="original" href="{origin}">Source</a>'.encode()),
        origin: ('text/html', origin_html.encode()),
        photo: ('image/jpeg', path.read_bytes()),
    })
    item, = rights.review_images(ex, 'commercial', True, False, report_previews=previews,
                                reference=('sources.pptx', [copy]))['items']
    assert item['rights']['action'] == action
    assert any(link['url'] == origin and link['verification'] == 'verified' for link in item['web_matches'])
    if action == 'credit':
        assert origin in item['credit'] and 'Example Author' in item['credit']
    if licence_path:
        assert item['source']['url'] == origin
    else:
        assert item['source'] is None


def test_blind_search_distinguishes_search_candidates_and_confirmed_resize(tmp_path, monkeypatch):
    picture, path = raster(tmp_path)
    previews = {}
    ex = extract(path, path.name, previews=previews)
    photo = 'https://assets.example.net/preview.jpg'
    found = 'https://artist.example.com/portfolio'
    unavailable = 'https://archive.example.org/post'
    resized = BytesIO()
    picture.resize((240, 160)).save(resized, 'JPEG', quality=61)
    install_pages(monkeypatch, {
        found: ('text/html', b'<meta property="og:title" content="Mountain evening">'),
        photo: ('image/jpeg', resized.getvalue()),
        unavailable: FetchError('сайт ответил HTTP 403'),
    })

    def reverse(copies):
        assert copies == previews
        result = SearchResult('found', [
            {'title': 'Mountain evening', 'url': found, 'thumbnail': photo},
            {'title': 'Archive', 'url': unavailable, 'thumbnail': photo},
        ], [{'provider': 'yandex', 'status': 'found'}], 'yandex')
        return {sha: result for sha in copies}

    monkeypatch.setattr(rights, 'search_previews', reverse)
    review = rights.review_images(ex, 'commercial', True, False, previews, report_previews=previews)
    assert review['reverse_checked'] == 1
    item, = review['items']
    confirmed, failed = item['web_matches']
    assert item['reverse_search'] == 'found' and item['reverse_provider'] == 'Яндекс'
    assert confirmed['url'] == found and confirmed['match_kind'] == 'transformed'
    assert failed['verification'] == 'unavailable' and '403' in failed['reason']
    assert item['rights']['action'] == 'unverified'


def test_empty_visual_index_still_uses_metadata_for_text_search(tmp_path, monkeypatch):
    picture, path = raster(tmp_path)
    description = Image.Exif()
    description[270] = 'Mountain evening lake'
    picture.save(path, exif=description)
    previews = {}
    ex = extract(path, path.name, previews=previews)
    monkeypatch.setattr(rights, '_reverse_matches', lambda images: (
        'complete', {sha: {'search': 'no_results', 'links': []} for sha in images},
    ))
    terms = []

    def search(_client, _deadline, params):
        if 'gsrsearch' in params:
            terms.append(params['gsrsearch'])
        return {}

    monkeypatch.setattr(rights, '_query', search)
    monkeypatch.setattr(rights, 'search_replacements', lambda *_args: [])
    item, = rights.review_images(ex, 'commercial', True, False, previews, report_previews=previews)['items']
    assert item['reverse_search'] == 'no_results'
    assert terms == ['Mountain evening lake']


def test_backiee_requires_exact_file_and_fetched_terms(tmp_path, monkeypatch):
    _, path = raster(tmp_path)
    previews = {}
    ex = extract(path, path.name, previews=previews)
    page = 'https://backiee.com/wallpaper/photo/1234'
    photo = 'https://backiee.com/static/wallpapers/photo.jpg'
    install_pages(monkeypatch, {
        page: ('text/html', f'<meta property="og:image" content="{photo}">'.encode()),
        photo: ('image/jpeg', path.read_bytes()),
        web_images.BACKIEE_TERMS: ('text/html', b'<h2>Personal, non-commercial use</h2><p>Any commercial use requires permission from the original copyright holder.</p>'),
    })
    item, = rights.review_images(ex, 'commercial', True, False, report_previews=previews,
                                reference=('reference.pptx', [page]))['items']
    assert item['rights']['action'] == 'restricted'
    assert item['source']['license']['url'] == web_images.BACKIEE_TERMS


def test_crop_remains_a_possible_match(tmp_path, monkeypatch):
    picture, path = raster(tmp_path)
    previews = {}
    ex = extract(path, path.name, previews=previews)
    crop = BytesIO()
    picture.crop((60, 0, 300, 240)).save(crop, 'JPEG', quality=80)
    media = 'https://artist.example.com/crop.jpg'
    install_pages(monkeypatch, {media: ('image/jpeg', crop.getvalue())})
    item, = rights.review_images(ex, 'commercial', True, False, report_previews=previews,
                                reference=('references.pptx', [media]))['items']
    assert item['web_matches'][0]['verification'] == 'possible'
    assert item['rights']['action'] == 'unverified'


def test_public_fetch_revalidates_dns_at_each_redirect(monkeypatch):
    import socket

    def resolve(name, *_args, **_kwargs):
        ip = '93.184.216.34' if name == 'public.example.org' else '127.0.0.1'
        return [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', (ip, 443))]

    def connect(parsed, addresses, _deadline, _limit):
        assert parsed.hostname == 'public.example.org'
        assert addresses == ['93.184.216.34']
        return 'https://redirect.example.org/somewhere'

    monkeypatch.setattr(public_web.socket, 'getaddrinfo', resolve)
    monkeypatch.setattr(public_web, '_fetch_direct', connect)
    with pytest.raises(FetchError, match='публичной сети'):
        public_web.fetch('https://public.example.org/', monotonic() + 5, 500)
