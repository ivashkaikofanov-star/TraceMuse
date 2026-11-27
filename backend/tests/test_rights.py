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
        return {'I1': 1}

    monkeypatch.setattr(rights, '_query', query)
    monkeypatch.setattr(rights, 'plan_image_searches', lambda *_: {
        'status': 'ok', 'queries': {'I1': {'replacement': 'ginger domestic cat'}},
    })
    monkeypatch.setattr(rights, 'rank_image_replacements', rank)
    review = rights.review_images(extraction(), 'commercial', True, True)
    item, = review['items']
    assert item['status'] == 'replace' and review['model_ranked'] == 1
    assert item['replacement']['title'] == 'Ginger domestic cat 3.jpg'
    assert item['replacement']['license']['name'] == 'CC BY 4.0'
    assert 'ограничивают' in item['reason']


def test_reverse_search_hit_never_supplies_a_license(monkeypatch):
    monkeypatch.setattr(rights, '_vision_matches', lambda _: ('complete', {
        SHA256: {'links': [{'title': 'Published copy', 'url': 'https://example.org/page', 'match': 'Vision match'}]},
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
    monkeypatch.setattr(rights, '_vision_matches', fail)
    monkeypatch.setattr(rights, '_query', fail)
    assert rights.review_images(extraction(), 'unspecified', False, True)['status'] == 'local_only'


def test_vision_posts_opted_in_preview_and_preserves_full_matches(monkeypatch):
    monkeypatch.setattr(settings, 'google_vision_api_key', SecretStr('sample-key'))

    def provider(request):
        assert str(request.url) == rights.VISION_URL
        assert request.headers['x-goog-api-key'] == 'sample-key'
        assert b'WEB_DETECTION' in request.content
        return httpx.Response(200, json={'responses': [{'webDetection': {
            'pagesWithMatchingImages': [
                {'url': 'https://example.org/photo', 'fullMatchingImages': [{'url': 'https://example.org/photo.jpg'}]},
                {'url': 'https://example.org/partial', 'partialMatchingImages': [{'url': 'https://example.org/photo.jpg'}]},
            ],
        }}]})

    client = httpx.Client(transport=httpx.MockTransport(provider))
    monkeypatch.setattr(rights.httpx, 'Client', lambda **_kwargs: client)
    status, matches = rights._vision_matches({'hash': b'preview'})
    assert status == 'complete'
    assert [link['url'] for link in matches['hash']['links']] == ['https://example.org/photo']


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
