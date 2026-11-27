from argparse import ArgumentParser
from base64 import b64encode
from io import BytesIO
from zipfile import ZipFile
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

parser = ArgumentParser(description='Run the TraceMuse browser smoke test against a running local instance.')
parser.add_argument('--url', default='http://127.0.0.1:8000/')
parser.add_argument('--chromium', help='Optional path to a system Chromium executable.')
parser.add_argument('--screenshots', type=Path, help='Capture coursework screenshots using an empty instance.')
options = parser.parse_args()
base = options.url.rstrip('/') + '/'
shots = options.screenshots

fixture = {
    'name': 'демо-материал.html',
    'mimeType': 'text/html',
    'buffer': b'<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>TraceMuse demo</title><meta name="author" content="TraceMuse test fixture"></head><body><h1>Combined browser scenario</h1><p>Preview an original sample before publishing.</p><a href="https://example.org/source">source reference</a></body></html>',
}
second = {
    'name': 'рисунок.svg',
    'mimeType': 'image/svg+xml',
    'buffer': b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 24 24"><title>Demo vector</title><circle cx="12" cy="12" r="8"/></svg>',
}
broken = {'name': 'пустой-документ.pdf', 'mimeType': 'application/pdf', 'buffer': b'not a PDF'}

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path=options.chromium)
    if shots:
        shots.mkdir(parents=True, exist_ok=True)
    desktop = browser.new_context(viewport={'width': 1440, 'height': 1024}, locale='ru-RU', timezone_id='Europe/Moscow', accept_downloads=True)
    page = desktop.new_page()
    created_ids = set()
    errors = []
    try:
        page.on('pageerror', lambda event: errors.append(str(event)))
        page.goto(base, wait_until='networkidle')
        expect(page.get_by_role('heading', name='Проверьте материал до публикации.')).to_be_visible()
        expect(page.get_by_text('Локальный API доступен')).to_be_visible()
        assert page.evaluate("() => document.documentElement.scrollWidth === window.innerWidth")
        assert page.request.get(base + 'api/health').ok
        wave = page.locator('.hero-image').evaluate(r"""node => getComputedStyle(node).backgroundImage.match(/url\("([^"]+)"\)/)?.[1]""")
        assert wave and page.request.get(wave).ok
        assert page.evaluate("() => document.fonts.check('500 14px Golos Text', 'Проверка')")
        if shots:
            assert not page.request.get(base + 'api/audits').json()['items'], 'Use an empty instance to capture screenshots.'
            page.screenshot(path=shots / 'dashboard-desktop.png', full_page=True, animations='disabled')

        page.get_by_role('link', name='Новая проверка').last.click()
        expect(page.get_by_role('heading', name='Новая проверка')).to_be_visible()
        expect(page.get_by_role('checkbox', name='Поиск источников онлайн')).not_to_be_checked()
        expect(page.get_by_role('checkbox', name='Анализ с DeepSeek')).not_to_be_checked()
        page.get_by_label('Планируемое использование').select_option('commercial')
        page.locator('#audit-files').set_input_files([fixture, broken])

        with page.expect_response(lambda response: response.url.endswith('/api/audits') and response.request.method == 'POST') as uploaded:
            page.get_by_role('button', name='Проверить материалы').click()
        assert uploaded.value.status == 201, uploaded.value.text()
        mixed = uploaded.value.json()
        assert len(mixed['items']) == len(mixed['errors']) == 1
        audit_id = mixed['items'][0]['id']
        created_ids.add(audit_id)
        expect(page.get_by_role('heading', name='Проверки готовы')).to_be_visible()
        expect(page.get_by_role('alert')).to_contain_text('пустой-документ.pdf')
        page.get_by_role('link', name=fixture['name']).click()
        expect(page.get_by_role('heading', name=fixture['name'])).to_be_visible()
        expect(page.locator('.extract-text')).to_contain_text('Combined browser scenario')
        expect(page.get_by_role('link', name='https://example.org/source')).to_have_attribute('href', 'https://example.org/source')
        detail = page.request.get(f'{base}api/audits/{audit_id}').json()
        assert detail['result']['options']['intended_use'] == 'commercial'
        assert detail['result']['options']['online_search'] is detail['result']['options']['ai_analysis'] is False

        for extension, prefix in [('pdf', b'%PDF-'), ('json', b'{')]:
            with page.expect_download() as downloaded:
                page.get_by_role('link', name=extension.upper(), exact=True).click()
            assert downloaded.value.failure() is None
            assert downloaded.value.suggested_filename == f'TraceMuse-{audit_id}.{extension}'
            assert downloaded.value.path().read_bytes().startswith(prefix)
        page.get_by_role('link', name='Отчёты', exact=True).click()
        expect(page.get_by_role('heading', name='Отчёты')).to_be_visible()
        expect(page.get_by_role('link', name=fixture['name'])).to_be_visible()
        page.get_by_role('link', name='История', exact=True).click()
        page.get_by_role('link', name=f"Открыть проверку {fixture['name']}", exact=True).click()
        page.get_by_text('Повторить проверку', exact=True).click()
        with page.expect_response(lambda response: response.url.endswith('/rerun')) as rerun:
            page.get_by_role('button', name='Запустить повторно').click()
        assert rerun.value.status == 201, rerun.value.text()
        rerun_id = rerun.value.json()['id']
        created_ids.add(rerun_id)
        assert rerun_id != audit_id
        expect(page).to_have_url(f'{base}#/audits/{rerun_id}')
        page.get_by_role('button', name='Удалить проверку').click()
        page.get_by_role('button', name='Удалить', exact=True).click()
        expect(page).to_have_url(f'{base}#/history')
        assert page.request.get(f'{base}api/audits/{rerun_id}').status == 404
        created_ids.remove(rerun_id)

        page.get_by_role('link', name='Настройки').click()
        expect(page.get_by_role('heading', name='DeepSeek API')).to_be_visible()
        expect(page.get_by_text('Ключ отсутствует', exact=False)).to_be_visible()
        expect(page.get_by_role('button', name='Проверить подключение')).to_be_disabled()
        expect(page.get_by_role('heading', name='Официальные реестры')).to_be_visible()
        assert not errors, errors

        mobile = browser.new_context(viewport={'width': 390, 'height': 844}, is_mobile=True, device_scale_factor=1, has_touch=True, locale='ru-RU', timezone_id='Europe/Moscow')
        phone = mobile.new_page()
        phone.goto(base + '#/new')
        expect(phone.get_by_role('heading', name='Новая проверка')).to_be_visible()
        assert phone.evaluate("() => document.documentElement.scrollWidth === window.innerWidth")
        if shots:
            phone.screenshot(path=shots / 'upload-mobile.png', full_page=True, animations='disabled')
        phone.locator('#audit-files').set_input_files(broken)
        with phone.expect_response(lambda response: response.url.endswith('/api/audits') and response.request.method == 'POST') as rejected:
            phone.get_by_role('button', name='Проверить материалы').tap()
        assert rejected.value.status == 422, rejected.value.text()
        expect(phone.get_by_role('alert')).to_contain_text('пустой-документ.pdf')
        phone.get_by_role('button', name='Убрать пустой-документ.pdf').tap()
        phone.locator('#audit-files').set_input_files(second)
        with phone.expect_response(lambda response: response.url.endswith('/api/audits') and response.request.method == 'POST') as mobile_uploaded:
            phone.get_by_role('button', name='Проверить материалы').tap()
        assert mobile_uploaded.value.status == 201, mobile_uploaded.value.text()
        mobile_id = mobile_uploaded.value.json()['items'][0]['id']
        created_ids.add(mobile_id)
        expect(phone).to_have_url(f'{base}#/audits/{mobile_id}')
        expect(phone.get_by_role('heading', name=second['name'])).to_be_visible()
        assert phone.evaluate("() => document.documentElement.scrollWidth === window.innerWidth")
        expect(phone.get_by_role('link', name='История', exact=True)).to_have_attribute('aria-current', 'page')
        if shots:
            phone.screenshot(path=shots / 'report-mobile.png', animations='disabled')

        zip_data = BytesIO()
        with ZipFile(zip_data, 'w') as archive:
            archive.writestr('example.html', fixture['buffer'])
            archive.writestr('notes.txt', 'A file listed in the ZIP inventory.')
        page.goto(base + '#/new')
        page.locator('.dropzone').evaluate(
            """(target, b64) => {
                const bytes = Uint8Array.from(atob(b64), letter => letter.charCodeAt(0));
                const transfer = new DataTransfer();
                transfer.items.add(new File([bytes], 'демо-архив.zip', {type: 'application/zip'}));
                target.dispatchEvent(new DragEvent('drop', {dataTransfer: transfer, bubbles: true, cancelable: true}));
            }""",
            b64encode(zip_data.getvalue()).decode('ascii'),
        )
        expect(page.get_by_text('демо-архив.zip', exact=True)).to_be_visible()
        with page.expect_response(lambda response: response.url.endswith('/api/audits') and response.request.method == 'POST') as archive_uploaded:
            page.get_by_role('button', name='Проверить материалы').click()
        assert archive_uploaded.value.status == 201, archive_uploaded.value.text()
        archive_id = archive_uploaded.value.json()['items'][0]['id']
        created_ids.add(archive_id)
        expect(page).to_have_url(f'{base}#/audits/{archive_id}')
        page.get_by_text('Файлы в архиве', exact=False).click()
        expect(page.get_by_text('example.html', exact=True)).to_be_visible()
        expect(page.get_by_text('Формат не поддерживается', exact=True)).to_be_visible()
        assert not errors, errors
    finally:
        for audit_id in created_ids:
            desktop.request.delete(f'{base}api/audits/{audit_id}')
        browser.close()

print('Chromium: dashboard assets, 1440px/390px layout, multiple upload with partial failure, rejected-only upload, local consent defaults, intended use, extraction, ZIP drag-and-drop and inventory, history/reports, PDF & JSON downloads, rerun/delete, settings without key')
