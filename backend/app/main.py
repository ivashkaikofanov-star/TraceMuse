import json
import logging
from contextlib import asynccontextmanager
from typing import Literal, get_args

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, StrictBool
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import UploadFile
from starlette.formparsers import MultiPartException
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .audits import MAX_FILES, AuditUnavailable, delete_audit, display_filename, get_audit, list_audits, original, recover_uploads, run_audit
from .config import AIProvider, BACKEND_DIR, settings
from .db import database_path, init_db
from .deepseek import is_configured, provider_label, provider_model, save_key, test_connection, verify_tls
from .evidence import LEGAL_REGISTRIES
from .extraction import ExtractionError, presentation_links
from .report import render_pdf

logger = logging.getLogger(__name__)
LOCAL_HOSTS = ['localhost', '127.0.0.1', '[::1]']
DEV_ORIGINS = ['http://localhost:5173', 'http://127.0.0.1:5173']
BODY_LIMIT_MESSAGE = 'Общий размер загрузки превышает допустимый предел.'
DIST_DIR = BACKEND_DIR.parent / 'frontend' / 'dist'
IntendedUse = Literal['unspecified', 'noncommercial', 'commercial', 'redistribute_modified', 'commercial_modified']


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    recover_uploads()
    yield


app = FastAPI(
    title='TraceMuse API',
    description='Локальный API предварительного аудита цифровых материалов.',
    version='0.2.0',
    lifespan=lifespan,
)
app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS,
                   allow_methods=['GET', 'POST', 'PUT', 'DELETE'], allow_headers=['Content-Type'])
app.add_middleware(TrustedHostMiddleware, allowed_hosts=LOCAL_HOSTS)


class AuditOptions(BaseModel):
    online_search: StrictBool = False
    ai_analysis: StrictBool = False
    reverse_search: StrictBool = False
    intended_use: IntendedUse = 'unspecified'


class DeepSeekKey(BaseModel):
    api_key: str = Field(max_length=512)


@app.exception_handler(RequestValidationError)
async def protect_secret_validation(request: Request, error: RequestValidationError):
    if request.method == 'PUT' and request.url.path.startswith('/api/settings/'):
        return JSONResponse({'detail': 'Проверьте формат API-ключа.'}, status_code=422)
    return await request_validation_exception_handler(request, error)


@app.middleware('http')
async def protect_local_requests(request: Request, call_next):
    if request.method in {'POST', 'PUT', 'DELETE'} and request.url.path.startswith('/api/'):
        origin = request.headers.get('origin')
        if origin and origin not in DEV_ORIGINS and origin != f"http://{request.headers.get('host')}":
            return JSONResponse({'detail': 'Изменения доступны из локального интерфейса.'}, status_code=403)
    return await call_next(request)


@app.get('/api/health')
def health():
    return {'status': 'ok', 'service': 'TraceMuse API', 'database': str(database_path()),
            'max_upload_mb': settings.max_upload_mb, 'max_files': MAX_FILES}


@app.get('/api')
def api_info():
    return {'name': 'TraceMuse', 'version': '0.2.0', 'stage': 2,
            'notice': 'Локальный аудит материалов с опциональным поиском и ИИ.'}


@app.get('/api/audits')
def audits():
    return {'items': list_audits()}


def _run_batch(files: list[UploadFile], options: AuditOptions, reference_file: UploadFile | None) -> dict:
    reference = None
    if reference_file:
        try:
            reference = (display_filename(reference_file.filename or ''), presentation_links(reference_file.file))
        except ExtractionError as exc:
            raise HTTPException(422, f'Проверьте презентацию со ссылками. {exc}') from exc
    items, errors = [], []
    for file in files:
        filename = file.filename or ''
        try:
            items.append(run_audit(file.file, filename, options.online_search, options.ai_analysis,
                                   options.intended_use, options.reverse_search, reference=reference))
        except ExtractionError as exc:
            errors.append({'filename': filename, 'message': str(exc)})
        except Exception as exc:
            logger.error('Audit failed (%s)', type(exc).__name__)
            errors.append({'filename': filename, 'message': 'Не удалось обработать файл. Проверьте свободное место и повторите попытку.'})
    return {'items': items, 'errors': errors}


@app.post('/api/audits', status_code=201, openapi_extra={
    'requestBody': {'required': True, 'content': {'multipart/form-data': {'schema': {
        'type': 'object', 'required': ['files'], 'properties': {
            'files': {'type': 'array', 'maxItems': MAX_FILES, 'items': {'type': 'string', 'format': 'binary'}},
            'reference': {'type': 'string', 'format': 'binary', 'description': 'Презентация PPTX со ссылками на источники'},
            'online_search': {'type': 'boolean', 'default': False},
            'ai_analysis': {'type': 'boolean', 'default': False},
            'reverse_search': {'type': 'boolean', 'default': False},
            'intended_use': {'type': 'string', 'enum': list(get_args(IntendedUse)), 'default': 'unspecified'},
        },
    }}}},
})
async def create_audits(request: Request):
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'multipart/form-data':
        raise HTTPException(415, 'Загрузите файлы в формате multipart/form-data.')
    limit = settings.max_upload_mb * 1024 * 1024
    wire_limit = limit + 512 * 1024
    raw_length = request.headers.get('content-length')
    if raw_length and (not raw_length.isascii() or not raw_length.isdecimal() or len(raw_length) > 12):
        raise HTTPException(400, 'Некорректный размер запроса.')
    if raw_length and int(raw_length) > wire_limit:
        raise HTTPException(413, BODY_LIMIT_MESSAGE)

    received = 0

    async def bounded_receive():
        nonlocal received
        message = await request.receive()
        received += len(message.get('body', b''))
        if received > wire_limit:
            raise MultiPartException(BODY_LIMIT_MESSAGE)
        return message

    bounded_request = Request(request.scope, receive=bounded_receive)
    try:
        async with bounded_request.form(max_files=MAX_FILES + 1, max_fields=4, max_part_size=1024) as form:
            files = form.getlist('files')
            if not files or not all(isinstance(file, UploadFile) for file in files):
                raise HTTPException(400, 'Выберите хотя бы один файл для проверки.')
            if len(files) > MAX_FILES:
                raise HTTPException(400, f'За один раз можно проверить до {MAX_FILES} файлов.')
            if any(name not in {'files', 'reference', *AuditOptions.model_fields} for name in form):
                raise HTTPException(400, 'Запрос содержит неизвестные поля.')
            if any(len(form.getlist(name)) > 1 for name in AuditOptions.model_fields):
                raise HTTPException(400, 'Параметры проверки можно передать один раз.')
            references = form.getlist('reference')
            reference = form.get('reference')
            if len(references) > 1 or (references and (
                not isinstance(reference, UploadFile) or not (reference.filename or '').lower().endswith('.pptx')
            )):
                raise HTTPException(400, 'Прикрепите одну презентацию со ссылками в формате PPTX.')
            uploads = [*files, *references]
            if sum(file.size or 0 for file in uploads) > limit:
                raise HTTPException(413, BODY_LIMIT_MESSAGE)
            flags = {name: form.get(name, 'false') for name in ('online_search', 'ai_analysis', 'reverse_search')}
            if any(value not in ('true', 'false') for value in flags.values()):
                raise HTTPException(400, 'Параметры согласия должны иметь значение true или false.')
            if flags['reverse_search'] == 'true' and flags['online_search'] != 'true':
                raise HTTPException(400, 'Обратный поиск требует включённого онлайн-поиска.')
            if reference and flags['online_search'] != 'true':
                raise HTTPException(400, 'Для проверки презентации со ссылками включите онлайн-поиск.')
            intended_use = form.get('intended_use', 'unspecified')
            if intended_use not in get_args(IntendedUse):
                raise HTTPException(400, 'Выберите поддерживаемый вариант использования материала.')
            options = AuditOptions(intended_use=intended_use, **{name: value == 'true' for name, value in flags.items()})
            result = await run_in_threadpool(_run_batch, files, options, reference)
    except HTTPException as exc:
        if exc.detail == BODY_LIMIT_MESSAGE:
            raise HTTPException(413, BODY_LIMIT_MESSAGE) from exc
        raise
    return JSONResponse(result, status_code=201 if result['items'] else 422)


def require_audit(audit_id: int) -> dict:
    audit = get_audit(audit_id)
    if audit is None:
        raise HTTPException(404, 'Проверка не найдена.')
    return audit


@app.get('/api/audits/{audit_id}')
def audit_detail(audit_id: int):
    return require_audit(audit_id)


@app.post('/api/audits/{audit_id}/rerun', status_code=201)
def rerun_audit(audit_id: int, options: AuditOptions = AuditOptions()):
    if options.reverse_search and not options.online_search:
        raise HTTPException(400, 'Обратный поиск требует включённого онлайн-поиска.')
    try:
        with original(audit_id) as (stream, filename):
            return run_audit(stream, filename, options.online_search, options.ai_analysis,
                             options.intended_use, options.reverse_search)
    except KeyError as exc:
        raise HTTPException(404, 'Проверка не найдена.') from exc
    except (ExtractionError, AuditUnavailable) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete('/api/audits/{audit_id}', status_code=204)
def remove_audit(audit_id: int):
    if not delete_audit(audit_id):
        raise HTTPException(404, 'Проверка не найдена.')
    return Response(status_code=204)


@app.get('/api/audits/{audit_id}/report.json')
def json_report(audit_id: int):
    return Response(json.dumps(require_audit(audit_id), ensure_ascii=False, indent=2), media_type='application/json',
                    headers={'Content-Disposition': f'attachment; filename="TraceMuse-{audit_id}.json"'})


@app.get('/api/audits/{audit_id}/report.pdf')
def pdf_report(audit_id: int):
    return Response(render_pdf(require_audit(audit_id)), media_type='application/pdf',
                    headers={'Content-Disposition': f'attachment; filename="TraceMuse-{audit_id}.pdf"'})


@app.get('/api/settings')
def get_settings():
    provider = settings.ai_provider
    return {'deepseek_configured': is_configured(), 'deepseek_model': settings.deepseek_model,
            'ai': {'provider': provider, 'label': provider_label(provider), 'model': provider_model(provider),
                   'configured': is_configured(provider), 'verify_tls': verify_tls(provider)},
            'reverse_search_configured': bool(settings.google_vision_api_key.get_secret_value().strip()),
            'pixabay_configured': bool(settings.pixabay_api_key.get_secret_value().strip()),
            'legal_registries': LEGAL_REGISTRIES}


@app.put('/api/settings/{provider}')
def set_provider_key(provider: AIProvider, body: DeepSeekKey):
    try:
        save_key(body.api_key, provider)
    except ValueError:
        raise HTTPException(422, 'Введите API-ключ в виде одной строки без пробелов.') from None
    except OSError:
        raise HTTPException(500, 'Сохранить ключ не удалось. Проверьте доступ к локальному хранилищу.') from None
    return get_settings()


@app.post('/api/settings/{provider}/test')
def verify_provider(provider: AIProvider):
    return test_connection(provider)


if (DIST_DIR / 'index.html').is_file():
    for directory in ('assets', 'fonts'):
        if (DIST_DIR / directory).is_dir():
            app.mount(f'/{directory}', StaticFiles(directory=DIST_DIR / directory), name=directory)

    @app.get('/')
    def frontend():
        return FileResponse(DIST_DIR / 'index.html')
