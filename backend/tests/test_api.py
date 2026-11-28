import hashlib
import sqlite3
from io import BytesIO
from urllib.parse import urlencode
from zipfile import ZipFile

import pytest
import httpx
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader

from app.config import settings
from app import deepseek, rights, web_images
from app.main import app
from app.audits import assessment, upload_directory


HTML = '<!doctype html><title>Курсовая</title><p>Проверка перед публикацией</p>' \
       '<a href="https://example.org/material">Ссылка источника</a>'.encode('utf-8')


@pytest.fixture
def isolated_data(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, 'database_path', str(tmp_path / 'testing.sqlite3'))
    monkeypatch.setattr(settings, 'legal_snapshot_path', '')
    monkeypatch.setattr(settings, 'max_upload_mb', 1)
    return tmp_path


def client():
    return TestClient(app, base_url='http://127.0.0.1')


def upload(session, content=HTML, filename='Курсовая.html'):
    return session.post('/api/audits', files=[('files', (filename, content, 'text/html'))])


def test_upload_report_reopen_rerun_and_delete(isolated_data):
    with client() as session:
        assert session.get('/api/health').json()['max_upload_mb'] == 1
        response = session.post('/api/audits', files=[
            ('files', ('Курсовая.html', HTML, 'text/html')),
            ('files', ('source.html', HTML, 'text/html')),
            ('files', ('empty.svg', b'', 'image/svg+xml')),
        ])
        assert response.status_code == 201, response.text
        result = response.json()
        first, second = result['items']
        assert first['sha256'] == second['sha256'] == hashlib.sha256(HTML).hexdigest()
        assert first['size_bytes'] == len(HTML)
        assert result['errors'][0]['filename'] == 'empty.svg'
        assert len(list(upload_directory().glob('*.bin'))) == 2
        audit_url = f"/api/audits/{first['id']}"
        detail = session.get(audit_url).json()
        data = detail['result']
        assert data['extraction']['format'] == 'HTML'
        assert 'Проверка перед публикацией' in data['extraction']['text']
        assert 'https://example.org/material' in data['extraction']['links']
        assert data['options'] == {'online_search': False, 'ai_analysis': False, 'intended_use': 'unspecified'}
        assert data['sources']['status'] == 'local_only'
        assert data['legal']['status'] == 'manual_review'
        json_report = session.get(audit_url + '/report.json')
        assert json_report.json() == detail
        assert json_report.headers['content-disposition'].startswith('attachment;')
        pdf_report = session.get(audit_url + '/report.pdf')
        assert pdf_report.headers['content-type'] == 'application/pdf'
        reader = PdfReader(BytesIO(pdf_report.content))
        assert any('Курсовая.html' in page.extract_text() for page in reader.pages)
    with client() as session:
        assert [entry['id'] for entry in session.get('/api/audits').json()['items']] == [second['id'], first['id']]
        rerun = session.post(audit_url + '/rerun', json={})
        assert rerun.status_code == 201, rerun.text
        assert rerun.json()['id'] not in {first['id'], second['id']}
        assert rerun.json()['sha256'] == first['sha256']
        assert session.delete(audit_url).status_code == 204
        assert session.get(audit_url).status_code == 404
        assert session.get(audit_url + '/report.json').status_code == 404
        assert len(list(upload_directory().glob('*.bin'))) == 2


def test_input_errors_and_local_origin_guard(isolated_data):
    with client() as session:
        empty = upload(session, b'')
        assert empty.status_code == 422
        assert empty.json()['errors'][0]['message']
        large = upload(session, b'a' * (1024 * 1024 + 1))
        assert large.status_code == 413
        absent = session.post('/api/audits', json={})
        assert absent.status_code == 415
        no_consent = session.post('/api/audits', files={'files': ('one.html', HTML)}, data={'online_search': 'maybe'})
        assert no_consent.status_code == 400
        denied = session.post('/api/audits', files={'files': ('one.html', HTML)}, headers={'Origin': 'https://example.org'})
        assert denied.status_code == 403
        assert session.get('/api/audits').json() == {'items': []}


def test_source_presentation_verifies_link_without_granting_permission(isolated_data, monkeypatch):
    primary = BytesIO()
    Image.new('RGB', (200, 150), 'blue').save(primary, format='JPEG')
    media = 'https://i.pinimg.com/originals/example.jpg'
    nested = 'https://ya.ru/images/search?' + urlencode({'img_url': media})
    unsafe = 'https://ya.ru/images/search?' + urlencode({'img_url': 'https://127.0.0.1/example.jpg'})
    reference = BytesIO()
    with ZipFile(reference, 'w') as archive:
        archive.writestr('[Content_Types].xml', '<Types><Override PartName="/ppt/presentation.xml" '
                         'ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>')
        archive.writestr('ppt/presentation.xml', '<presentation/>')
        archive.writestr('ppt/slides/slide1.xml', f'<sld><p><t>{nested}</t></p><p><t>{unsafe}</t></p></sld>')

    def fetch(_client, url, _params, _deadline, _limit):
        assert url == media
        return primary.getvalue()

    monkeypatch.setattr(web_images, '_read', fetch)
    monkeypatch.setattr(rights, '_query', lambda *_: {})
    monkeypatch.setattr('app.audits.search_sources', lambda *_: {'status': 'complete', 'items': []})
    files = [('files', ('photo.jpg', primary.getvalue())), ('reference', ('sources.pptx', reference.getvalue()))]
    with client() as session:
        assert session.post('/api/audits', files=files).status_code == 400
        response = session.post('/api/audits', files=files, data={'online_search': 'true'})
        assert response.status_code == 201, response.text
        audit_id = response.json()['items'][0]['id']
        audit_url = f'/api/audits/{audit_id}'
        review = session.get(audit_url).json()['result']['image_review']
        assert review['linked_check'] == {'reference': 'sources.pptx', 'checked': 1, 'eligible': 1, 'matched': 1}
        assert review['reverse_status'] == 'disabled'
        item, = review['items']
        assert item['web_matches'][0]['url'] == media
        assert item['rights']['action'] == 'unverified'
        assert len(list(upload_directory().glob('*.bin'))) == 1
        pdf = PdfReader(BytesIO(session.get(audit_url + '/report.pdf').content))
        assert 'Файл совпал' in ''.join(page.extract_text() for page in pdf.pages)


def test_original_database_migration(isolated_data):
    with sqlite3.connect(settings.database_path) as conn:
        conn.execute('''CREATE TABLE audits (id INTEGER PRIMARY KEY, project_id INTEGER, filename TEXT NOT NULL,
                        status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, result_json TEXT)''')
        conn.execute("INSERT INTO audits (filename, status) VALUES ('старый.html', 'created')")
    with client() as session:
        detail = session.get('/api/audits/1').json()
        assert detail['filename'] == 'старый.html'
        assert detail['result']['extraction']['warnings']
        assert session.post('/api/audits/1/rerun', json={}).status_code == 422
        assert upload(session).status_code == 201


def test_secret_validation_does_not_echo_input(isolated_data):
    with client() as session:
        invalid = 'fake-secret-value-' * 40
        response = session.put('/api/settings/deepseek', json={'api_key': invalid})
        assert response.status_code == 422
        assert invalid not in response.text


def test_mai_settings_route_and_audit(isolated_data, monkeypatch):
    monkeypatch.setattr(settings, 'ai_provider', 'mai')
    monkeypatch.setattr(settings, 'mai_tls_verify', False)
    monkeypatch.setattr(settings, 'mai_model', 'deepseek-v4.1-flash')

    def provider(request):
        assert request.headers['authorization'] == 'Bearer sk-mai-test'
        if request.url.path == '/v1/models':
            return httpx.Response(200, json={'data': [{'id': 'deepseek-v4.1-flash'}]})
        assert request.url.path == '/v1/chat/completions'
        return httpx.Response(200, json={'choices': [{'message': {'content': '{"observations": []}'}}]})

    def mock_client(provider_id):
        assert provider_id == 'mai'
        return httpx.Client(base_url=deepseek.MAI_API_URL, transport=httpx.MockTransport(provider))

    monkeypatch.setattr(deepseek, '_client', mock_client)
    with client() as session:
        saved = session.put('/api/settings/mai', json={'api_key': 'sk-mai-test'})
        assert saved.status_code == 200
        assert saved.json()['ai'] == {
            'provider': 'mai', 'label': 'МАИ', 'model': 'deepseek-v4.1-flash', 'configured': True, 'verify_tls': False,
        }
        assert 'sk-mai-test' not in saved.text
        assert session.post('/api/settings/mai/test').json()['status'] == 'ok'
        response = session.post('/api/audits', files={'files': ('sample.html', HTML)}, data={'ai_analysis': 'true'})
        assert response.status_code == 201
        audit_id = response.json()['items'][0]['id']
        result = session.get(f'/api/audits/{audit_id}').json()['result']['ai']
        assert result['provider'] == 'mai' and result['status'] == 'ok'


def test_recovery_after_interrupted_storage_change(isolated_data):
    with client() as session:
        item = upload(session).json()['items'][0]
    directory = upload_directory()
    original = next(directory.glob('*.bin'))
    trash = original.with_suffix('.deleted')
    orphan = directory / '00000000000040008000000000000000.bin'
    original.replace(trash)
    orphan.write_bytes(HTML)
    with client() as session:
        assert original.exists()
        assert not trash.exists()
        assert not orphan.exists()
        assert session.get(f"/api/audits/{item['id']}").status_code == 200


def test_rating_uses_intended_use_only_for_exact_evidence():
    extracted = {'format': 'JPEG', 'images': [{}]}
    license_terms = {'commercial_allowed': False, 'derivatives_distribution_allowed': True}
    weak = {'items': [{'method': 'visual_similarity', 'license': license_terms}]}
    exact = {'items': [{'method': 'identical_image', 'license': license_terms}]}
    assert assessment(extracted, weak, {}, 'commercial')['level'] == 'insufficient_data'
    assert assessment(extracted, exact, {}, 'commercial')['level'] == 'high'
    assert assessment(extracted, exact, {}, 'unspecified')['level'] == 'manual_review'
    license_terms.update(commercial_allowed=True, attribution_required=True)
    assert assessment(extracted, exact, {}, 'commercial')['level'] == 'medium'
    license_terms.update(attribution_required=False)
    assert assessment(extracted, exact, {}, 'commercial')['level'] == 'low'
