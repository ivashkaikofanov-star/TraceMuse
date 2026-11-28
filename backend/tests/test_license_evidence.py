import base64
import json
from io import BytesIO

import pytest
from PIL import Image
from pypdf import PdfReader

from app import license_evidence, rights, web_images
from app.extraction import extract
from app.public_web import FetchError, PublicResponse
from app.reverse_search import license_links


CC_BY = 'https://creativecommons.org/licenses/by/4.0/'
CC0 = 'https://creativecommons.org/publicdomain/zero/1.0/'


def test_prioritize_stock_cards_and_ignore_proxies_and_reposts():
    paths = [
        'https://ca.pinterest.com/pin/a', 'https://storage.googleapis.com/file',
        'https://sub.vk.ru/test', 'https://mavink.com/photo', 'https://sprashivalka.com/question',
        'https://avatars.mds.yandex.net/search', 'https://freepik.com.example.org/media',
        'https://www.pexels.com/photo/forest-123/', 'https://www.freepik.com/free-photo/forest_124.htm',
        'https://commons.wikimedia.org/wiki/File:Forest.jpg', 'https://author.example.org/work',
    ]
    rows = [{'url': url, 'title': url} for url in paths]
    assert [row['url'] for row in license_links(rows)] == [paths[9], paths[8], paths[7]]
    assert [row['url'] for row in license_links(rows[:7] + rows[10:])] == [paths[6], paths[10]]


@pytest.mark.parametrize(('page_html', 'source'), [
    ('<script type="application/ld+json">{"@type":"CreativeWork","image":"/photo.jpg",'
     f'"license":"{CC_BY}","creator":{{"name":"Alice"}}}}</script>', 'JSON-LD на сайте'),
    ('<div itemscope itemtype="https://schema.org/ImageObject">'
     '<img itemprop="contentUrl" src="/photo.jpg">'
     f'<a itemprop="license" href="{CC_BY}">CC BY</a>'
     '<span itemprop="creator">Alice</span></div>', 'Microdata на сайте'),
])
def test_cascade_extracts_license_bound_to_image(page_html, source):
    page = 'https://example.org/work'
    media = 'https://example.org/photo.jpg'
    result = license_evidence.extract_license(page, media, page_data=page_html.encode())
    assert result['source'] == source
    assert result['license']['kind'] == 'cc_by'
    assert result['author'] == 'Alice'
    other = license_evidence.extract_license(page, 'https://example.org/different.jpg', page_data=page_html.encode())
    assert not other.get('license')


def test_copyright_notice_without_permission_stays_a_notice():
    body = b'<script type="application/ld+json">{"@type":"ImageObject","contentUrl":"/photo.jpg","copyrightNotice":"Copyright Alice"}</script>'
    result = license_evidence.extract_license('https://example.org/work', 'https://example.org/photo.jpg', page_data=body)
    assert result['source'] == 'JSON-LD на сайте'
    assert result['notice'] == 'Copyright Alice'
    assert not result.get('license')


def test_exiftool_uses_linked_rights_statement(monkeypatch):
    page = 'https://creator.example.org/photo'
    media = 'https://creator.example.org/media.jpg'
    terms = 'https://creator.example.org/rights'
    monkeypatch.setattr(license_evidence.shutil, 'which', lambda _: 'exiftool')

    def run(arguments, **kwargs):
        assert '-XMP-dc:Rights' in arguments and '-XMP-xmpRights:WebStatement' in arguments
        return license_evidence.subprocess.CompletedProcess(arguments, 0,
            stdout=json.dumps([{'WebStatement': terms, 'Rights': 'Copyright Alice', 'Creator': ['Alice']}]).encode())

    def read(url, _deadline, _limit):
        if url == media:
            return PublicResponse(url, 'image/jpeg', b'fixture')
        assert url == terms
        return PublicResponse(url, 'text/html', b'<p>CC BY 4.0</p>')

    monkeypatch.setattr(license_evidence.subprocess, 'run', run)
    result = license_evidence.extract_license(page, media, page_data=b'', read=read)
    assert result['source'] == 'ExifTool · IPTC/XMP'
    assert result['license']['kind'] == 'cc_by'
    assert result['author'] == 'Alice'


def test_stock_rules_require_image_and_keep_unsplash_plus_separate():
    page = 'https://www.freepik.com/free-photo/forest_123.htm'
    media = 'https://img.freepik.com/free-photo/forest_12-123.jpg'
    result = license_evidence.extract_license(page, media, page_data=b'', image_on_page=True)
    assert result['source'] == 'Правило Freepik'
    assert result['license']['attribution_required'] is True
    assert not license_evidence.extract_license(page, media, page_data=b'', image_on_page=False)

    page = 'https://unsplash.com/photos/forest-example'
    free = license_evidence.extract_license(page, 'https://images.unsplash.com/photo-1', page_data=b'', image_on_page=True)
    plus = license_evidence.extract_license(page, 'https://plus.unsplash.com/premium_photo-1', page_data=b'', image_on_page=True)
    assert free['license']['kind'] == 'unsplash'
    assert free['license']['attribution_required'] is False
    assert not plus


def test_freepik_403_has_specific_manual_review(monkeypatch):
    url = 'https://www.freepik.com/free-photo/forest_123.htm'

    def read(*_):
        raise FetchError('сайт ответил HTTP 403')

    assert license_evidence.extract_license(url, 'https://img.freepik.com/free-photo/forest.jpg', read=read)['manual_review']
    monkeypatch.setattr(web_images, 'fetch', read)
    result, _ = web_images.compare_links([], [url], {})
    assert 'Требуется ручная проверка' in result['pages'][0]['reason']


def test_commons_licensing_section_supplies_evidence_and_author():
    page = 'https://commons.wikimedia.org/wiki/File:Forest.jpg'
    data = ('<table><tr><td id="fileinfotpl_aut">Author</td><td>Alice</td></tr></table>'
            '<div class="mw-heading"><h2 id="Licensing">Licensing</h2></div>'
            f'<div>Released under <a href="{CC_BY}">CC BY 4.0</a></div>'
            '<div class="mw-heading"><h2>File history</h2></div>').encode()
    result = license_evidence.extract_license(page, 'https://upload.wikimedia.org/photo.jpg', page_data=data)
    assert result['source'] == 'Wikimedia Commons'
    assert result['url'] == page + '#Licensing'
    assert result['license']['kind'] == 'cc_by'
    assert result['notice'] == 'Released under CC BY 4.0'
    assert result['author'] == 'Alice'


def test_yandex_replacement_requires_licensed_card_and_embeds_photo(tmp_path, monkeypatch):
    original = tmp_path / 'forest lake.jpg'
    Image.new('RGB', (600, 400), 'red').save(original, 'JPEG')
    replacement = BytesIO()
    Image.new('RGB', (540, 360), 'blue').save(replacement, 'JPEG')
    blocked = 'https://www.freepik.com/free-photo/forest_42.htm'
    page = 'https://www.pexels.com/photo/forest-lake-123/'
    media = 'https://images.pexels.com/photos/123/forest.jpg'

    def read(url, _deadline, _limit):
        if url == blocked:
            raise FetchError('сайт ответил HTTP 403')
        if url == page:
            return PublicResponse(url, 'text/html', f'<meta property="og:image" content="{media}">'.encode())
        assert url == media
        return PublicResponse(url, 'image/jpeg', replacement.getvalue())

    requests = []

    def yandex(_client, _deadline, term):
        requests.append(term)
        return [{'title': 'Forest lake', 'url': page, 'image_url': media}]

    monkeypatch.setattr(web_images, 'fetch', read)
    monkeypatch.setattr(rights, '_query', lambda *_: {})
    monkeypatch.setattr(rights, 'search_replacements', yandex)
    previews = {}
    extraction = extract(original, original.name, previews=previews)
    report = rights.review_images(extraction, 'commercial', True, False, report_previews=previews,
                                  reference=('sources.pptx', [blocked]))
    assert requests == ['forest lake']
    item, = report['items']
    other, = item['replacements']
    assert other['license']['kind'] == 'pexels'
    assert base64.b64decode(other['preview']).startswith(b'\xff\xd8')
    assert item['rights']['license_type'] == 'Не установлено'
    assert item['rights']['next_action'] == 'Заменить'

    from app.report import render_pdf
    pdf = render_pdf({'filename': original.name, 'id': 1, 'status': 'completed',
                      'result': {'extraction': extraction, 'image_review': report}})
    reader = PdfReader(BytesIO(pdf))
    text = ' '.join(page.extract_text() for page in reader.pages)
    assert all(field in text for field in ('Источник лицензии', 'Тип лицензии', 'Действие', 'Требуется ручная проверка'))
    assert sum(len(p.images) for p in reader.pages) >= 2
