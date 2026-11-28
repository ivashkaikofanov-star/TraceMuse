import hashlib
from io import BytesIO
from zipfile import ZipFile

import httpx
from PIL import Image
from pydantic import SecretStr

from app import deepseek, rights
from app.config import settings
from app.extraction import extract

SHA1 = 'a' * 40
SHA256 = 'b' * 64


def image_info(title, sha1=SHA1, license_path='by', declared='CC BY 4.0'):
    return {
        'canonicaltitle': f'File:{title}', 'sha1': sha1, 'mime': 'image/jpeg', 'width': 580, 'height': 400,
        'url': 'https://upload.wikimedia.org/wikipedia/commons/example.jpg',
        'extmetadata': {'LicenseShortName': {'value': declared},
                        'LicenseUrl': {'value': f'https://creativecommons.org/licenses/{license_path}/4.0/'},
                        'Artist': {'value': '<a>Example Author</a>'},
                        'ImageDescription': {'value': 'Ginger domestic cat on a bench'}},
    }


def extraction():
    return {'images': [{'name': 'image1.jpg', 'sha1': SHA1, 'sha256': SHA256, 'width': 580, 'height': 400,
                        'placements': [{'location': 'Слайд 2', 'element': 1}, {'location': 'Слайд 5', 'element': 3}]}]}


def test_exact_source_produces_copy_ready_credit_on_each_slide(monkeypatch):
    monkeypatch.setattr(rights, '_query', lambda *_: {'allimages': [image_info('Original cat.jpg')]})
    item, = rights.review_images(extraction(), 'commercial_modified', True, False)['items']
    assert item['status'] == 'credit'
    assert item['locations'] == ['Слайд 2 · изображение 1', 'Слайд 5 · изображение 3']
    for fragment in ('Example Author', 'https://commons.wikimedia.org/wiki/File:Original_cat.jpg',
                     'https://creativecommons.org/licenses/by/4.0/', 'Изображение изменено'):
        assert fragment in item['credit']


def test_model_selects_only_from_commercially_eligible_replacements(monkeypatch):
    restricted = image_info('Restricted cat.jpg', license_path='by-nc', declared='CC BY-NC 4.0')
    allowed = [image_info(f'Ginger domestic cat {index}.jpg', str(index) * 40) for index in (2, 3)]

    def query(_client, _deadline, params):
        if 'aisha1' in params:
            return {'allimages': [restricted]}
        assert params['gsrsearch'] == 'ginger domestic cat'
        return {'pages': {str(n): {'title': data['canonicaltitle'], 'imageinfo': [data]}
                          for n, data in enumerate([restricted, *allowed])}}

    def rank(items):
        assert [x['id'] for x in items[0]['candidates']] == [0, 1]
        return {'I1:with_credit': 1}

    monkeypatch.setattr(rights, '_query', query)
    monkeypatch.setattr(rights, '_candidate_photo', lambda *_: ('preview', 16))
    monkeypatch.setattr(rights, 'plan_image_searches', lambda *_: {
        'status': 'ok', 'queries': {'I1': {'replacement': 'ginger domestic cat'}},
    })
    monkeypatch.setattr(rights, 'rank_image_replacements', rank)
    review = rights.review_images(extraction(), 'commercial', True, True)
    item, = review['items']
    assert item['status'] == 'replace' and review['model_ranked'] == 1
    assert item['replacement']['title'] == 'Ginger domestic cat 3.jpg'
    assert item['replacement']['license']['name'] == 'CC BY 4.0'
    assert item['rights']['action'] == 'restricted'
    assert 'Подпись не поможет' in item['reason']


def test_reverse_search_hit_never_supplies_a_license(monkeypatch):
    monkeypatch.setattr(rights, '_reverse_matches', lambda _: ('complete', {
        SHA256: {'links': [{'title': 'Published copy', 'url': 'https://example.org/page', 'match': 'Yandex match'}]},
    }))
    monkeypatch.setattr(rights, '_query', lambda *_: {})
    item, = rights.review_images(extraction(), 'commercial', True, False, {SHA256: b'preview'})['items']
    assert item['status'] == 'review'
    assert item['source'] is None and 'credit' not in item
    assert item['web_matches'][0]['url'] == 'https://example.org/page'


def test_local_review_does_not_make_external_requests(monkeypatch):
    def fail(*_args, **_kwargs):
        raise AssertionError('request while offline')

    monkeypatch.setattr(rights, 'plan_image_searches', fail)
    monkeypatch.setattr(rights, '_reverse_matches', fail)
    monkeypatch.setattr(rights, '_query', fail)
    assert rights.review_images(extraction(), 'unspecified', False, True)['status'] == 'local_only'


def test_reverse_search_processes_all_extracted_images(monkeypatch):
    assets = extraction()
    assets['images'] = [assets['images'][0] | {'sha256': f'{i:064x}'} for i in range(13)]
    previews = {image['sha256']: b'sample' for image in assets['images']}

    def reverse(copies):
        assert copies == previews
        return 'complete', {sha: {'search': 'no_results'} for sha in copies}

    monkeypatch.setattr(rights, '_reverse_matches', reverse)
    monkeypatch.setattr(rights, '_query', lambda *_: {})
    review = rights.review_images(assets, 'commercial', True, False, previews)
    assert review['checked'] == review['reverse_checked'] == 13


def test_model_cannot_supply_links_or_invent_a_candidate(monkeypatch):
    monkeypatch.setattr(deepseek, '_key', lambda _: 'fake')
    monkeypatch.setattr(deepseek, '_search_json', lambda *_: [
        {'id': 'I1', 'source': 'https://example.org', 'replacement': 'ginger cat'},
        {'id': 'unknown', 'source': 'copy'},
    ])
    plan = deepseek.plan_image_searches([{'id': 'I1'}, {'id': 'I2'}], 'commercial')
    assert plan['queries'] == {'I1': {'replacement': 'ginger cat'}}
    monkeypatch.setattr(deepseek, '_search_json', lambda *_: [{'id': 'I1', 'candidate': 99}])
    assert deepseek.rank_image_replacements([{'id': 'I1', 'candidates': [{'id': 0}]}]) == {}


def test_slide_relationship_locations_and_ephemeral_previews(tmp_path):
    pptx = tmp_path / 'fixture.pptx'
    raster = BytesIO()
    Image.new('RGB', (200, 160), 'red').save(raster, format='PNG')
    slide = '<sld xmlns:r="urn:rs"><p><t>Ginger domestic cat</t></p><pic><cNvPr descr="Pet on a bench"/><blip r:embed="rIdImg"/></pic></sld>'
    rels = '<Relationships><Relationship Id="rIdImg" Type="urn:test/image" Target="../media/image1.png"/></Relationships>'
    with ZipFile(pptx, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>')
        archive.writestr('ppt/presentation.xml', '<presentation/>')
        for n in (1, 2):
            archive.writestr(f'ppt/slides/slide{n}.xml', slide)
            archive.writestr(f'ppt/slides/_rels/slide{n}.xml.rels', rels)
        archive.writestr('ppt/media/image1.png', raster.getvalue())
    previews = {}
    data = extract(pptx, pptx.name, previews=previews)
    media, = data['images']
    assert media['sha256'] == hashlib.sha256(raster.getvalue()).hexdigest()
    assert [value['location'] for value in media['placements']] == ['Слайд 1', 'Слайд 2']
    assert media['placements'][0]['description'] == 'Pet on a bench'
    assert media['placements'][1]['context'] == 'Ginger domestic cat'
    assert previews[media['sha256']].startswith(bytes.fromhex('ffd8'))
    assert 'previews' not in data


def _photo(c="red"):
    import base64
    data = BytesIO()
    Image.new('RGB', (180, 110), c).save(data, format='JPEG')
    return base64.b64encode(data.getvalue()).decode('ascii')


def _cc0_info(title, sha1):
    info = image_info(title, sha1, declared='CC0')
    info['extmetadata']['LicenseUrl']['value'] = 'https://creativecommons.org/publicdomain/zero/1.0/'
    return info


def test_credit_and_no_credit_replacement_are_independent(monkeypatch):
    original = image_info('Ginger domestic cat original.jpg')
    free = _cc0_info('Ginger domestic cat alternative.jpg', 'c' * 40)
    with_credit = image_info('Ginger domestic cat close.jpg', 'd' * 40)

    def query(_client, _deadline, params):
        if 'aisha1' in params:
            return {'allimages': [original]}
        return {'pages': {str(n): {'title': data['canonicaltitle'], 'imageinfo': [data]}
                          for n, data in enumerate([with_credit, free, original])}}

    monkeypatch.setattr(rights, '_query', query)
    monkeypatch.setattr(rights, '_candidate_photo', lambda *_: (_photo('blue'), 15))
    item, = rights.review_images(extraction(), 'commercial', True, False)['items']
    assert item['rights']['category'] == 'cc_by' and item['rights']['action'] == 'credit'
    assert item['credit'] and 'может предъявить требования' in item['rights']['warning']
    replacement, = item['replacements']
    assert replacement['kind'] == 'no_credit'
    assert 'credit' not in replacement
    assert replacement['preview'] and replacement['license']['kind'] == 'cc0'


def test_unknown_original_keeps_second_replacement_only_if_visually_closer(monkeypatch):
    data = extraction()
    data['images'][0]['name'] = 'ginger domestic cat.jpg'
    free = _cc0_info('Ginger domestic cat alternative.jpg', 'c' * 40)
    closer = image_info('Ginger domestic cat closer.jpg', 'd' * 40)

    def query(_client, _deadline, params):
        if 'aisha1' in params:
            return {'allimages': []}
        return {'pages': {str(n): {'title': image['canonicaltitle'], 'imageinfo': [image]}
                          for n, image in enumerate([free, closer])}}

    monkeypatch.setattr(rights, '_query', query)
    monkeypatch.setattr(rights, 'search_replacements', lambda *_: [])
    monkeypatch.setattr(rights, '_candidate_photo', lambda _c, _d, _a, image: (
        (_photo('green'), 28) if image['sha1'] == 'c' * 40 else (_photo('blue'), 13)
    ))
    item, = rights.review_images(data, 'commercial', True, False)['items']
    assert item['rights']['category'] == 'unknown'
    assert 'не установлено' in item['rights']['detail']
    assert [candidate['kind'] for candidate in item['replacements']] == ['no_credit', 'with_credit']
    assert 'credit' not in item['replacements'][0]
    assert 'Example Author' in item['replacements'][1]['credit']
    monkeypatch.setattr(rights, '_candidate_photo', lambda *_: (_photo(), 22))
    item, = rights.review_images(data, 'commercial', True, False)['items']
    assert [candidate['kind'] for candidate in item['replacements']] == ['no_credit']


def test_pdf_embeds_original_and_both_replacement_photos(monkeypatch):
    import base64
    from pypdf import PdfReader
    from app.report import render_pdf

    data = extraction()
    data['images'][0]['name'] = 'ginger domestic cat.jpg'
    free = _cc0_info('Ginger domestic cat alternative.jpg', 'c' * 40)
    closer = image_info('Ginger domestic cat closer.jpg', 'd' * 40)

    def query(_client, _deadline, params):
        if 'aisha1' in params:
            return {}
        return {'pages': {str(n): {'title': image['canonicaltitle'], 'imageinfo': [image]}
                          for n, image in enumerate([free, closer])}}

    monkeypatch.setattr(rights, '_query', query)
    monkeypatch.setattr(rights, 'search_replacements', lambda *_: [])
    monkeypatch.setattr(rights, '_candidate_photo', lambda _c, _d, _a, image: (
        (_photo('green'), 28) if image['sha1'] == 'c' * 40 else (_photo('blue'), 13)
    ))
    review = rights.review_images(data, 'commercial', True, False,
                                  report_previews={SHA256: base64.b64decode(_photo('red'))})
    report = PdfReader(BytesIO(render_pdf({'filename': 'Пример.pptx', 'result': {
        'image_review': review, 'extraction': data,
    }})))
    text = ' '.join(' '.join(page.extract_text().split()) for page in report.pages)
    assert sum(len(page.images) for page in report.pages) == 3
    assert 'Тип лицензии · Не установлено' in text
    assert 'Действие · Заменить' in text
    assert 'Замена без подписи' in text and 'Более похожая замена с подписью' in text
    assert 'SHA-256' not in text


def test_sharealike_and_cc0_explain_terms_for_original(monkeypatch):
    sharealike = image_info('Ginger domestic cat.jpg', license_path='by-sa', declared='CC BY-SA 4.0')
    monkeypatch.setattr(rights, '_query', lambda _c, _d, params: {'allimages': [sharealike]} if 'aisha1' in params else {})
    original, = rights.review_images(extraction(), 'commercial', True, False)['items']
    assert original['rights']['category'] == 'cc_by_sa'
    assert original['rights']['action'] == 'credit'
    assert 'ShareAlike' in original['rights']['changes']
    modified, = rights.review_images(extraction(), 'commercial_modified', True, False)['items']
    assert modified['rights']['action'] == 'conditional' and 'credit' not in modified

    free = _cc0_info('Ginger domestic cat.jpg', SHA1)
    monkeypatch.setattr(rights, '_query', lambda *_: {'allimages': [free]})
    item, = rights.review_images(extraction(), 'commercial', True, False)['items']
    assert item['rights']['action'] == 'keep' and item['rights']['category'] == 'cc0'
    assert 'credit' not in item and not item['replacements']


def test_pixabay_stock_terms_and_daily_cache(monkeypatch, tmp_path):
    import json
    from time import monotonic
    from app import pixabay
    from app.db import init_db
    from app.photo_license import commercial_conditions

    monkeypatch.setattr(settings, 'database_path', str(tmp_path / 'test.sqlite3'))
    monkeypatch.setattr(settings, 'pixabay_api_key', SecretStr('private-test-key'))
    init_db()
    calls = []

    def load(_client, endpoint, params, *_):
        assert endpoint == 'https://pixabay.com/api/'
        assert params['key'] == 'private-test-key'
        calls.append(endpoint)
        return json.dumps({'hits': [{'pageURL': 'https://pixabay.com/photos/cat-12345/', 'id': 12345,
                                      'user': 'Example', 'tags': 'cat, animal',
                                      'webformatURL': 'https://cdn.pixabay.com/photo/2025/cat.jpg'}]}).encode()

    monkeypatch.setattr(pixabay, '_read', load)
    one, = pixabay.lookup(None, monotonic() + 5, image_id='12345')
    repeated, = pixabay.lookup(None, monotonic() + 5, image_id='12345')
    assert one == repeated and len(calls) == 1
    rights_record = commercial_conditions([pixabay.material(one) | {'match_kind': 'pixels'}],
                                          branded=False, modified=False)
    assert rights_record['action'] == 'keep' and rights_record['category'] == 'stock'
    assert 'перепродажу' in rights_record['warning']
    assert 'private-test-key' not in str(one)
