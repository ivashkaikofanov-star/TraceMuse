"""Image-level recommendations grounded in Commons records and optional reverse search."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
import json
import re
from time import monotonic

import httpx
from PIL import Image

from .config import settings
from .deepseek import plan_image_searches, rank_image_replacements
from .evidence import HTTP_HEADERS, THUMBNAIL_HOSTS, _catalog_meta, _commons_title, _file_url, _http_url, _license, _pages, _plain, _query, _read
from .extraction import image_phash, pixel_hash

MAX_ASSETS = 12
VISION_URL = 'https://vision.googleapis.com/v1/images:annotate'
NETWORK_ERRORS = (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError, Image.DecompressionBombError)
STOP_WORDS = set('image photo picture logo photograph illustration background slide png jpg jpeg svg webp img file picture vector and with from this version crop copy of the in a by on is for corbis stock фотография слайд фото изображение логотип презентация картинка рисунок скачать версия'.split())
FORMAT_TYPES = {'image/jpeg', 'image/png', 'image/webp'}


def _terms(text: str) -> set[str]:
    return {term for term in re.findall(r'[^\W\d_]{3,}', text.casefold()) if term not in STOP_WORDS}


def _meaningful(text: str) -> str:
    text = re.sub(r'https?://\S+', ' ', text).replace('_', ' ')
    return ' '.join(text.split())[:100] if _terms(text) else ''


def _assets(extraction: dict) -> list[dict]:
    assets = {}
    for image in extraction.get('images', []):
        if not image.get('sha256'):
            continue
        if 0 < min(image.get('width', 0), image.get('height', 0)) < 32:
            continue
        asset = assets.get(image['sha256'])
        if asset is None:
            asset = {**image, 'id': f'I{len(assets) + 1}', 'locations': [], 'search_context': '', 'description': '',
                     'prefer_context': extraction.get('format') == 'PDF' or image.get('hash_origin') == 'pdf_export'}
            assets[image['sha256']] = asset
        for placement in image.get('placements') or [image]:
            location = placement.get('location') or 'Загруженное изображение'
            if placement.get('element'):
                location = f"{location} · изображение {placement['element']}"
            if placement.get('region'):
                location = f"{location} · {placement['region']}"
            if location not in asset['locations']:
                asset['locations'].append(location)
            asset['search_context'] = asset['search_context'] or placement.get('context', '')
            asset['description'] = asset['description'] or placement.get('description', '')

    return list(assets.values())


def _vision_matches(previews: dict[str, bytes]) -> tuple[str, dict]:
    if not previews:
        return 'not_available', {}
    key = settings.google_vision_api_key.get_secret_value().strip()
    if not key:
        return 'not_configured', {}
    body = {'requests': [
        {'image': {'content': base64.b64encode(data).decode('ascii')},
         'features': [{'type': 'WEB_DETECTION', 'maxResults': 6}]}
        for data in previews.values()
    ]}
    try:
        with httpx.Client(follow_redirects=False, timeout=httpx.Timeout(12, connect=4)) as client:
            with client.stream('POST', VISION_URL, headers={'X-Goog-Api-Key': key}, json=body) as response:
                response.raise_for_status()
                data = bytearray()
                deadline = monotonic() + 12
                for chunk in response.iter_bytes(32768):
                    if len(data) + len(chunk) > 2_000_000 or monotonic() > deadline:
                        return 'unavailable', {}
                    data.extend(chunk)
            responses = json.loads(data)['responses']
        if not isinstance(responses, list) or len(responses) != len(previews):
            return 'unavailable', {}
    except NETWORK_ERRORS + (KeyError,):
        return 'unavailable', {}

    results = {}
    errors = 0
    for sha, response in zip(previews, responses):
        if not isinstance(response, dict) or response.get('error'):
            errors += 1
            continue
        item = response.get('webDetection') or {}
        if not isinstance(item, dict):
            errors += 1
            continue
        links = []
        commons = set()
        pages = item.get('pagesWithMatchingImages')
        for page in pages[:8] if isinstance(pages, list) else []:
            if not isinstance(page, dict) or not _http_url(page.get('url')):
                continue
            full = page.get('fullMatchingImages')
            if not full:
                continue
            url = page['url']
            if url not in [link['url'] for link in links]:
                links.append({'title': _plain(page.get('pageTitle')) or _http_url(url).hostname,
                              'url': url, 'match': 'Google Vision указал страницу с полным совпадением изображения'})
            if title := _commons_title(url):
                commons.add(title)
        labels = item.get('bestGuessLabels')
        labels = labels if isinstance(labels, list) else []
        label = next((_meaningful(_plain(entry.get('label'))) for entry in labels[:2]
                      if isinstance(entry, dict) and isinstance(entry.get('label'), str)), '')
        results[sha] = {'links': links[:4], 'label': label, 'commons': sorted(commons)[:4]}
    return ('unavailable' if errors == len(previews) else 'partial' if errors else 'complete'), results


def _material(title: str, info: dict) -> dict:
    license = _license(info)
    artist = _plain(_catalog_meta(info, 'Attribution') or _catalog_meta(info, 'Artist'), 250)
    if artist.casefold().strip('. ') in {'unknown', 'own work', 'n/a', 'неизвестен', 'неизвестно', 'anonymous', 'see below', 'see author'}:
        artist = ''
    record = {
        'title': title.removeprefix('File:'), 'url': _file_url(title), 'author': artist,
        'license': license, 'description': _plain(_catalog_meta(info, 'ImageDescription'), 240),
    }
    if _http_url(info.get('url'), host='upload.wikimedia.org'):
        record['download_url'] = info['url']
    return record


def _credit(material: dict, modified: bool = False) -> str:
    license = material['license']
    author = f" — {material['author']}" if material['author'] else ''
    change = ' · Изображение изменено.' if modified else ''
    return f"«{material['title']}»{author}. Источник: {material['url']}. {license['name']}: {license['url']}.{change}"


def _eligible(material: dict, intended_use: str, *, replacement=False) -> bool:
    license = material['license']
    if license['status'] != 'catalog_declaration' or not license.get('url'):
        return False
    restrictions = license.get('restrictions', [])
    unchanged = intended_use in {'noncommercial', 'commercial'} and not replacement
    share_alike_only = restrictions == ['Производные работы распространяются на совместимых условиях ShareAlike']
    if (restrictions and not (unchanged and share_alike_only)) or license.get('attribution_required') is None:
        return False
    if license['attribution_required'] and not material['author']:
        return False
    if (replacement or intended_use != 'noncommercial') and license.get('commercial_allowed') is not True:
        return False
    if (replacement or intended_use not in {'noncommercial', 'commercial'}) and license.get('derivatives_distribution_allowed') is not True:
        return False
    return True


def _catalog_pages(client, deadline, term):
    return _pages(_query(client, deadline, {
        'generator': 'search', 'gsrsearch': term, 'gsrnamespace': 6, 'gsrlimit': 8,
        'prop': 'imageinfo', 'iiprop': 'sha1|extmetadata|url|mime|size', 'iiurlwidth': 220,
    }))


def _page_image(page: dict) -> dict:
    entries = page.get('imageinfo')
    return entries[0] if isinstance(entries, list) and entries and isinstance(entries[0], dict) else {}


def _similar(client, deadline, asset, info):
    thumbnail = info.get('thumburl')
    if not asset.get('phash') or not any(_http_url(thumbnail, host=host) for host in THUMBNAIL_HOSTS):
        return False
    if not asset.get('width') or not asset.get('height'):
        return False
    data = _read(client, thumbnail, None, deadline, 360_000)
    with Image.open(BytesIO(data), formats=('JPEG', 'PNG', 'WEBP', 'GIF')) as image:
        if image.width * image.height > 350_000:
            return False
        ratio = (image.width / image.height) / (asset['width'] / asset['height'])
        return 0.85 <= ratio <= 1.18 and (int(asset['phash'], 16) ^ int(image_phash(image), 16)).bit_count() <= 7


def _same_pixels(client, deadline, asset, info):
    url = info.get('url')
    size = info.get('size')
    if (not asset.get('pixel_sha256') or (info.get('width'), info.get('height')) != (asset.get('width'), asset.get('height'))
            or not isinstance(size, int) or not 0 < size <= 2_000_000 or not _http_url(url, host='upload.wikimedia.org')):
        return False
    data = _read(client, url, None, deadline, 2_000_000)
    with Image.open(BytesIO(data), formats=('JPEG', 'PNG', 'WEBP', 'GIF')) as image:
        return image.size == (asset['width'], asset['height']) and pixel_hash(image) == asset['pixel_sha256']


def _approved_original(matches, intended_use, branding, failed):
    exact = [match for match in matches if match['match_kind'] != 'similar']
    if not exact or branding or len({match['author'] for match in exact}) != 1:
        return None
    if any(not _eligible(match, intended_use) for match in exact):
        return None
    material = exact[0]
    modified = intended_use in {'redistribute_modified', 'commercial_modified'}
    status = 'credit' if material['license']['attribution_required'] else 'reuse'
    return {'status': status, 'source': material, 'credit': _credit(material, modified),
            'reason': 'Подтверждено точное совпадение изображения. В каталоге указаны условия для выбранного сценария.',
            'alternatives': [], 'failed': failed}


def _check_asset(client, deadline, asset, query, vision, intended_use):
    excluded, matches, alternatives = set(), [], []
    sha1 = asset.get('sha1')
    failed = False
    tried = False
    if isinstance(sha1, str) and re.fullmatch(r'[0-9a-f]{40}', sha1):
        tried = True
        try:
            answer = _query(client, deadline, {'list': 'allimages', 'aisha1': sha1, 'ailimit': 5,
                                              'aiprop': 'sha1|canonicaltitle|extmetadata'})
            entries = answer.get('allimages', [])
            for info in entries if isinstance(entries, list) else []:
                if not isinstance(info, dict):
                    continue
                title = info.get('canonicaltitle', '')
                if info.get('sha1') == sha1 and isinstance(title, str) and title.startswith('File:'):
                    record = _material(title, info) | {'match': 'Совпадение SHA-1 извлечённого изображения', 'match_kind': 'bytes'}
                    matches.append(record)
                    excluded.add(record['url'])
        except NETWORK_ERRORS:
            failed = True
    if vision.get('commons') and not matches:
        tried = True
        try:
            pages = _pages(_query(client, deadline, {'titles': '|'.join(vision['commons']), 'prop': 'imageinfo',
                                                      'iiprop': 'sha1|extmetadata|url|mime'}))
            for page in pages:
                info = _page_image(page)
                if not page['title'].startswith('File:'):
                    continue
                exact_bytes = bool(sha1 and info.get('sha1') == sha1)
                if exact_bytes or _same_pixels(client, deadline, asset, info):
                    record = _material(page['title'], info) | {
                        'match': 'Совпадение SHA-1 извлечённого изображения' if exact_bytes else 'Все декодированные пиксели совпали',
                        'match_kind': 'bytes' if exact_bytes else 'pixels',
                    }
                    matches.append(record)
                    excluded.add(record['url'])
        except NETWORK_ERRORS:
            failed = True

    branding = bool(asset.get('logo_references'))
    source_term = _meaningful(query.get('source', '') or vision.get('label', '') or asset['description'])
    replacement_term = _meaningful(query.get('replacement', '') or vision.get('label', '') or asset['description'])
    if matches:
        replacement_term = replacement_term or _meaningful(asset['search_context']) or _meaningful(matches[0]['title'].rsplit('.', 1)[0])
    elif not source_term:
        context = _meaningful(asset['search_context'])
        name = _meaningful(asset['name'].rsplit('.', 1)[0])
        source_term = (context or name) if asset['prefer_context'] else (name or context)
    replacement_term = replacement_term or source_term
    if ready := _approved_original(matches, intended_use, branding, failed):
        return ready

    look_for = list(dict.fromkeys(term for term in (source_term, replacement_term if not branding else '') if term))
    for term in look_for:
        tried = True
        try:
            pages = _catalog_pages(client, deadline, term)
        except NETWORK_ERRORS:
            failed = True
            continue
        comparison_budget = 3
        for page in pages:
            info = _page_image(page)
            if not page['title'].startswith('File:') or info.get('mime') not in FORMAT_TYPES:
                continue
            material = _material(page['title'], info)
            if material['url'] in excluded:
                continue
            if sha1 and info.get('sha1') == sha1:
                matches.append({**material, 'match': 'Совпадение SHA-1 извлечённого изображения', 'match_kind': 'bytes'})
                excluded.add(material['url'])
                continue
            try:
                if not any(match['match_kind'] == 'bytes' for match in matches) and _same_pixels(client, deadline, asset, info):
                    matches.append({**material, 'match': 'Все декодированные пиксели совпали', 'match_kind': 'pixels'})
                    excluded.add(material['url'])
                    continue
            except NETWORK_ERRORS:
                failed = True
            if term == source_term and comparison_budget and asset.get('phash') and not matches:
                comparison_budget -= 1
                try:
                    if _similar(client, deadline, asset, info):
                        matches.append({**material, 'match': 'Близкая форма и пропорции публичной миниатюры', 'match_kind': 'similar'})
                        excluded.add(material['url'])
                        continue
                except NETWORK_ERRORS:
                    failed = True
            width, height = info.get('width'), info.get('height')
            if (term == replacement_term and not branding and _eligible(material, intended_use, replacement=True)
                    and isinstance(width, int) and width >= 320 and isinstance(height, int) and height >= 240):
                words = _terms(replacement_term)
                overlap = words & _terms(f"{material['title']} {material['description']}")
                if len(overlap) >= min(2, len(words)):
                    alternatives.append((len(overlap), material))
    if ready := _approved_original(matches, intended_use, branding, failed):
        return ready
    sorted_alternatives = sorted(alternatives, key=lambda candidate: candidate[0], reverse=True)
    source = min(matches, key=lambda m: m['match_kind'] == 'similar', default=None)
    conflict = bool(source and source['match_kind'] != 'similar' and (
        (intended_use in {'commercial', 'commercial_modified'} and source['license'].get('commercial_allowed') is False)
        or (intended_use in {'redistribute_modified', 'commercial_modified'} and source['license'].get('derivatives_distribution_allowed') is False)
    ))
    if branding:
        reason = 'Распознан логотип. Условия использования знака требуют отдельного подтверждения.'
    elif conflict:
        reason = 'Условия найденной копии ограничивают выбранное использование.'
    elif source and source['match_kind'] != 'similar' and source['license'].get('share_alike'):
        reason = 'Подтверждено точное совпадение. При распространении изменённой версии условия ShareAlike требуют совместимой лицензии.'
    elif source:
        reason = 'Найден кандидат в источники. Разрешение на использование данного изображения не подтверждено.'
    elif failed:
        reason = 'Часть запросов к каталогу не завершилась. Разрешение подтвердить не удалось.'
    else:
        reason = 'В проверенных результатах не удалось подтвердить разрешение для исходного изображения.'
    return {'status': 'replace' if sorted_alternatives else 'review' if tried or branding else 'no_query',
            'source': source, 'reason': reason, 'failed': failed,
            'alternatives': [material for _, material in sorted_alternatives[:3] if material['url'] not in excluded]}


def review_images(extraction: dict, intended_use: str, online: bool, ai_enabled: bool,
                  previews: dict[str, bytes] | None = None) -> dict:
    assets = _assets(extraction)
    selected = assets[:MAX_ASSETS]
    if not selected:
        return {'status': 'no_images', 'total': 0, 'checked': 0, 'items': []}
    if not online:
        return {'status': 'local_only', 'total': len(assets), 'checked': 0, 'items': []}
    limited = [{'id': item['id'], 'context': item['search_context'][:360], 'description': item['description'][:180],
                'name': item['name'][:100], 'logo': ', '.join(ref['brand'] for ref in item.get('logo_references', []))}
               for item in selected]
    reverse_status, reversed_images = _vision_matches(previews) if previews is not None else ('disabled', {})
    for image, item in zip(limited, selected):
        image['visual_hint'] = reversed_images.get(item['sha256'], {}).get('label', '')
    planning = plan_image_searches(limited, intended_use) if ai_enabled else {'status': 'disabled', 'queries': {}}
    deadline = monotonic() + 20
    with httpx.Client(headers=HTTP_HEADERS, follow_redirects=False) as client, ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_check_asset, client, deadline, item, planning['queries'].get(item['id'], {}),
                               reversed_images.get(item['sha256'], {}), intended_use) for item in selected]
        outcomes = [future.result() for future in futures]
    ranking_data = [{'id': item['id'], 'context': item['search_context'], 'description': item['description'],
                     'candidates': [{'id': n, 'title': candidate['title'], 'description': candidate['description']}
                                    for n, candidate in enumerate(outcome['alternatives'])]}
                    for item, outcome in zip(selected, outcomes) if len(outcome['alternatives']) > 1]
    ranks = rank_image_replacements(ranking_data) if ai_enabled and ranking_data else {}
    items = []
    for item, outcome in zip(selected, outcomes):
        alternatives = outcome.pop('alternatives')
        if alternatives:
            chosen = ranks.get(item['id'], 0)
            if chosen == -1:
                outcome['status'] = 'review'
                outcome['reason'] += ' По теме элемента подходящей замены не найдено.'
            else:
                replacement = alternatives[chosen] if 0 <= chosen < len(alternatives) else alternatives[0]
                outcome['replacement'] = replacement | {'credit': _credit(replacement)}
        elif outcome['status'] == 'replace':
            outcome['status'] = 'review'
        reverse_evidence = reversed_images.get(item['sha256'], {})
        items.append({**outcome, 'id': item['id'], 'name': item['name'], 'locations': item['locations'],
                      'web_matches': reverse_evidence.get('links', [])})
    return {'status': 'partial' if len(assets) > len(selected) or any(item['failed'] for item in items)
                      or reverse_status in {'unavailable', 'partial', 'not_configured', 'not_available'} else 'complete',
            'total': len(assets), 'checked': len(selected), 'model_status': planning['status'],
            'model_ranked': len(ranks),
            'reverse_status': reverse_status, 'reverse_checked': len(reversed_images), 'items': items,
            'checked_at': datetime.now(UTC).isoformat(timespec='seconds')}
