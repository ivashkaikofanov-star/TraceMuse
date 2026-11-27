import hashlib
import sqlite3
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from pypdf import PdfReader

from app.config import settings
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
