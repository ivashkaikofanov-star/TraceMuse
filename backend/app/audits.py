import hashlib
import json
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from time import time
from typing import BinaryIO
from uuid import UUID, uuid4

from .config import settings
from .db import connect, database_path
from .deepseek import analyze
from .rights import review_images
from .evidence import check_legal, search_sources
from .extraction import ExtractionError, extract

CHUNK_SIZE = 128 * 1024
MAX_FILES = 10


class AuditUnavailable(Exception):
    pass


def upload_directory() -> Path:
    database = database_path()
    directory = database.parent / 'uploads' / database.name
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    return directory


def stored_file(storage_id: str) -> Path:
    return upload_directory() / f'{UUID(storage_id).hex}.bin'


def recover_uploads() -> None:
    directory = upload_directory()
    with connect() as conn:
        conn.execute('BEGIN IMMEDIATE')
        registered = {row[0] for row in conn.execute('SELECT storage_id FROM audits WHERE storage_id IS NOT NULL')}
        for file in directory.iterdir():
            if file.suffix not in {'.bin', '.deleted', '.part'} or not file.is_file() or file.is_symlink():
                continue
            try:
                if UUID(file.stem).hex != file.stem:
                    continue
            except ValueError:
                continue
            if file.suffix == '.deleted' and file.stem in registered:
                file.replace(file.with_suffix('.bin'))
            elif file.suffix != '.part' and file.stem not in registered:
                file.unlink(missing_ok=True)
            elif file.suffix == '.part':
                # Another process may be staging an upload before it opens a transaction.
                try:
                    if time() - file.stat().st_mtime > 24 * 60 * 60:
                        file.unlink(missing_ok=True)
                except FileNotFoundError:
                    pass


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def display_filename(raw: str) -> str:
    name = raw.replace('\\', '/').rsplit('/', 1)[-1].strip()
    name = ''.join(character for character in name if character.isprintable())
    if not name or len(name) > 255:
        raise ExtractionError('Имя файла пустое или слишком длинное.')
    return name


def summary(row, result: dict) -> dict:
    created = row['created_at']
    if 'T' not in created and ' ' in created:
        created = created.replace(' ', 'T', 1) + '+00:00'
    return {
        'id': row['id'],
        'filename': row['filename'],
        'status': row['status'],
        'created_at': created,
        'sha256': row['sha256'],
        'size_bytes': row['size_bytes'],
        'risk_level': result.get('assessment', {}).get('level', 'insufficient_data'),
        'format': result.get('extraction', {}).get('format'),
    }


def row_result(row) -> dict:
    try:
        result = json.loads(row['result_json'] or '{}')
    except (json.JSONDecodeError, TypeError):
        return {}
    return result if isinstance(result, dict) else {}


def list_audits() -> list[dict]:
    with connect() as conn:
        return [summary(row, row_result(row)) for row in conn.execute('SELECT * FROM audits ORDER BY id DESC')]


def get_audit(audit_id: int) -> dict | None:
    with connect() as conn:
        row = conn.execute('SELECT * FROM audits WHERE id = ?', (audit_id,)).fetchone()
    if row is None:
        return None
    result = row_result(row)
    if not result.get('extraction'):
        result = {
            'extraction': {'format': None, 'text': '', 'links': [], 'metadata': {}, 'images': [],
                           'warnings': ['Предыдущая версия не сохранила результаты извлечения.']},
            'sources': {'status': 'unavailable', 'items': [], 'warnings': []},
            'legal': {'status': 'unavailable', 'items': [], 'warnings': []},
            'ai': {'status': 'unavailable', 'warnings': []},
            'assessment': {'level': 'insufficient_data', 'confidence': 'insufficient_data',
                           'reason': 'В прежней записи нет структурированных результатов.',
                           'recommendations': ['Создайте новую проверку с исходным файлом.'],
                           'limitations': ['Предыдущая версия формата отчёта.']},
            'legacy_result': result,
        }
    return {**summary(row, result), 'result': result}


def assessment(extraction: dict, sources: dict, legal: dict, intended_use: str) -> dict:
    candidates = sources.get('items', [])
    exact_terms = [item.get('license') or {} for item in candidates if item.get('method') == 'identical_image']
    commercial = intended_use in {'commercial', 'commercial_modified'}
    modified = intended_use in {'redistribute_modified', 'commercial_modified'}
    conflict = any((commercial and term.get('commercial_allowed') is False) or
                   (modified and term.get('derivatives_distribution_allowed') is False) for term in exact_terms)
    restrictions = any(term.get('commercial_allowed') is False or
                       term.get('derivatives_distribution_allowed') is False for term in exact_terms)
    attribution = any(term.get('attribution_required') is True or term.get('share_alike') is True
                      for term in exact_terms)
    freely_reusable = bool(exact_terms) and all(term.get('commercial_allowed') is True and
                                               term.get('derivatives_distribution_allowed') is True and
                                               term.get('attribution_required') is False and not term.get('restrictions')
                                               for term in exact_terms)
    legal_matches = any(item.get('status') == 'local_snapshot_match' for item in legal.get('items', []))
    brand_graphic = any(item.get('method') == 'brand_visual' for item in candidates)
    brand_mention = any(item.get('method') == 'brand_text' for item in candidates)
    standalone_image = extraction.get('format') in {'PNG', 'JPG', 'JPEG', 'WEBP'} and len(extraction.get('images', [])) == 1
    level = 'insufficient_data'
    reason = 'Собранных сведений недостаточно для оценки условий использования материала.'
    confidence = 'limited'
    risks = []
    recommendations = ['Подтвердите первоисточник и права на каждый материал, который планируете публиковать.']
    if conflict:
        level = 'high'
        reason = 'Для точного совпадения указано условие, которое может конфликтовать с выбранным использованием.'
        risks.append('Для выбранного использования может потребоваться отдельное разрешение.')
    elif restrictions or legal_matches:
        level = 'manual_review'
        reason = 'Обнаружены сведения об условиях использования или совпадения с записями правовых реестров. Их применимость зависит от контекста.'
    elif brand_graphic:
        level = 'manual_review'
        reason = 'Найдено сходство с образцом логотипа. Условия использования знака нужно проверять отдельно.'
    elif attribution:
        level = 'medium'
        reason = 'Лицензия точного совпадения предусматривает условия атрибуции или распространения производных работ.'
        risks.append('Публикация может требовать выполнения условий лицензии.')
    elif standalone_image and freely_reusable and not extraction.get('warnings'):
        level = 'low'
        reason = 'Для точной копии отдельного изображения заявлена открытая лицензия без ограничений на выбранное использование.'
    elif exact_terms:
        level = 'manual_review'
        reason = 'Найдено точное совпадение. Нужна проверка лицензии, полномочий автора и всех компонентов файла.'
    elif any(item.get('method') != 'brand_text' for item in candidates):
        recommendations.append('Откройте кандидаты и проверьте, относится ли найденное совпадение к вашему материалу.')
    if restrictions:
        risks.append('Каталог указывает ограничения на коммерческое использование или изменение кандидата.')
        recommendations.append('Сверьте коммерческие условия и разрешение на изменения на странице исходного ресурса.')
    if legal_matches:
        risks.append('В локальном снимке реестра найдено совпадение названия.')
        recommendations.append('Сверьте название, дату и применимость требования по ссылке на официальный реестр.')
    if exact_terms:
        recommendations.append('Откройте найденный материал и сверьте условия из каталога с заявлением правообладателя.')
    if brand_graphic:
        risks.append('Условия использования найденных логотипов не установлены.')
        recommendations.append('Сверьте найденные обозначения с правилами бренда для вашего сценария использования.')
    if brand_mention:
        recommendations.append('Для публикации отдельно проверьте происхождение материала и права на фотографии и иллюстрации.')
    if not risks and level != 'low':
        risks.append('Условия использования всего материала не подтверждены.')
    limitations = [
        'Правовой статус, применимость ограничений и необходимость маркировки оцениваются по конкретному контексту.',
        'Метаданные и каталожные совпадения требуют подтверждения происхождения и полномочий правообладателя.',
        'Поиск охватывает доступные индексы и поддерживаемые ссылки. OCR не выполняется.',
    ]
    if extraction.get('warnings'):
        limitations.append('Извлечение выполнено с замечаниями. Подробности есть в блоке извлечения.')
    return {'level': level, 'reason': reason, 'confidence': confidence, 'risks': risks,
            'recommendations': recommendations, 'limitations': limitations}


def run_audit(stream: BinaryIO, raw_filename: str, online_search: bool, ai_analysis: bool,
              intended_use: str = 'unspecified', reverse_search: bool = False, *,
              reference: tuple[str, list[str]] | None = None) -> dict:
    filename = display_filename(raw_filename)
    limit = settings.max_upload_mb * 1024 * 1024
    digest = hashlib.sha256()
    size = 0
    storage_id = uuid4().hex
    target = stored_file(storage_id)
    pending = target.with_suffix('.part')
    try:
        with pending.open('xb') as output:
            while chunk := stream.read(CHUNK_SIZE):
                size += len(chunk)
                if size > limit:
                    raise ExtractionError(f'Размер файла превышает {settings.max_upload_mb} МиБ.')
                digest.update(chunk)
                output.write(chunk)
        if not size:
            raise ExtractionError('Файл пуст.')
        sha256 = digest.hexdigest()
        previews = {}
        extracted = extract(pending, filename, previews=previews)
        image_review = review_images(extracted, intended_use, online_search, ai_analysis,
                                     previews if online_search and reverse_search else None,
                                     report_previews=previews, reference=reference)
        sources = search_sources(extracted, sha256, online_search)
        legal = check_legal(extracted)
        ai = analyze(extracted, sources, legal, ai_analysis, image_review=image_review)
        result = {'extraction': extracted, 'sources': sources, 'legal': legal, 'ai': ai, 'image_review': image_review,
                  'assessment': assessment(extracted, sources, legal, intended_use),
                  'options': {'online_search': online_search, 'ai_analysis': ai_analysis, 'intended_use': intended_use}}
        if reverse_search:
            result['options']['reverse_search'] = True
        raw_result = json.dumps(result, ensure_ascii=False, allow_nan=False)
        with connect() as conn:
            audit_id = conn.execute(
                'INSERT INTO audits (filename, status, created_at, result_json, storage_id, sha256, size_bytes) '
                'VALUES (?, ?, ?, ?, ?, ?, ?)',
                (filename, 'completed', utc_now(), raw_result, storage_id, sha256, size),
            ).lastrowid
            pending.replace(target)
            row = conn.execute('SELECT * FROM audits WHERE id = ?', (audit_id,)).fetchone()
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    finally:
        pending.unlink(missing_ok=True)
    return summary(row, result)


@contextmanager
def original(audit_id: int):
    # Copy while the row is protected against concurrent deletion. Processing and network calls happen after commit.
    with tempfile.TemporaryFile(dir=upload_directory()) as snapshot:
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT filename, storage_id FROM audits WHERE id = ?', (audit_id,)).fetchone()
            if row is None:
                raise KeyError(audit_id)
            if not row['storage_id']:
                raise AuditUnavailable('Исходный файл для этой записи не сохранён.')
            try:
                with stored_file(row['storage_id']).open('rb') as source:
                    shutil.copyfileobj(source, snapshot, CHUNK_SIZE)
            except FileNotFoundError as exc:
                raise AuditUnavailable('Исходный файл отсутствует в локальном хранилище.') from exc
        snapshot.seek(0)
        yield snapshot, row['filename']


def delete_audit(audit_id: int) -> bool:
    removed = None
    try:
        with connect() as conn:
            conn.execute('BEGIN IMMEDIATE')
            row = conn.execute('SELECT storage_id FROM audits WHERE id = ?', (audit_id,)).fetchone()
            if row is None:
                return False
            if row['storage_id']:
                target = stored_file(row['storage_id'])
                if target.exists():
                    trash = target.with_suffix('.deleted')
                    target.replace(trash)
                    removed = target, trash
            conn.execute('DELETE FROM audits WHERE id = ?', (audit_id,))
    except BaseException:
        if removed:
            removed[1].replace(removed[0])
        raise
    if removed:
        removed[1].unlink(missing_ok=True)
    return True
