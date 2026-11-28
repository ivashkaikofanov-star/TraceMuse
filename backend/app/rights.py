"""Image-level recommendations grounded in Commons records and optional reverse search."""

import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from io import BytesIO
import math
import json
import re
from time import monotonic

import httpx
from PIL import Image

from . import pixabay
from .config import settings
from .deepseek import plan_image_searches, rank_image_replacements
from .evidence import HTTP_HEADERS, THUMBNAIL_HOSTS, _catalog_meta, _commons_title, _file_url, _http_url, _license, _pages, _plain, _query, _read
from .extraction import image_phash, jpeg_preview, pixel_hash
from .photo_license import commercial_conditions, photo_credit
from .web_images import compare_links

MAX_ASSETS = 12
VISION_URL = 'https://vision.googleapis.com/v1/images:annotate'
NETWORK_ERRORS = (httpx.HTTPError, OSError, ValueError, TypeError, AttributeError, Image.DecompressionBombError)
STOP_WORDS = set('image photo picture logo photograph illustration background slide png jpg jpeg svg webp img file picture vector and with from this version crop copy of the in a by on is for corbis stock фотография слайд фото изображение логотип презентация картинка рисунок скачать версия'.split())
FORMAT_TYPES = {'image/jpeg', 'image/png', 'image/webp'}


def _terms(text: str) -> set[str]:
    return {term for term in re.findall(r'[^\W\d_]{3,}', text.casefold()) if term not in STOP_WORDS}


def _meaningful(text: str) -> str:
    text = re.sub(r'https?://\S+', ' ', text).replace('_', ' ')
    text = re.sub(r'\(\d+\)|\b\d{5,}\b', ' ', text)
    return ' '.join(text.split())[:100] if _terms(text) else ''


def _assets(extraction: dict) -> list[dict]:
    assets = {}
    for image in extraction.get('images', []):
        if not image.get('sha256'):
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
         'features': [{'type': 'WEB_DETECTION', 'maxResults': 12}]}
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
        links = {}
        commons = set()
        stock_ids = set()
        pages = item.get('pagesWithMatchingImages')
        for page in pages[:12] if isinstance(pages, list) else []:
            if not isinstance(page, dict) or not _http_url(page.get('url')):
                continue
            full = page.get('fullMatchingImages')
            if not full and not page.get('partialMatchingImages'):
                continue
            url = page['url']
            if full or url not in links:
                links[url] = (0 if full else 2, {
                    'title': _plain(page.get('pageTitle')) or _http_url(url).hostname, 'url': url,
                    'match': ('Google Vision нашёл страницу с полным совпадением' if full
                              else 'Google Vision нашёл страницу с частичным совпадением'),
                })
            if full:
                if title := _commons_title(url):
                    commons.add(title)
                if stock_id := pixabay.photo_id(url):
                    stock_ids.add(stock_id)
        for key, priority, message in (
            ('fullMatchingImages', 1, 'Google Vision нашёл совпадающий файл'),
            ('partialMatchingImages', 3, 'Google Vision нашёл частичное совпадение файла'),
        ):
            entries = item.get(key)
            for entry in entries[:6] if isinstance(entries, list) else []:
                if isinstance(entry, dict) and (parsed := _http_url(entry.get('url'))):
                    url = entry['url']
                    links.setdefault(url, (priority, {'title': parsed.hostname, 'url': url, 'match': message}))
        labels = item.get('bestGuessLabels')
        labels = labels if isinstance(labels, list) else []
        label = next((value for entry in labels[:3] if isinstance(entry, dict)
                      and (value := _meaningful(_plain(entry.get('label'))))), '')
        if not label:
            label = next((value for priority, link in links.values() if priority == 0
                          and (value := _meaningful(link['title'])) and ' ' in value), '')
        results[sha] = {'links': [link for _, link in sorted(links.values(), key=lambda hit: hit[0])[:6]],
                        'label': label, 'commons': sorted(commons)[:4],
                        'stock_ids': sorted(stock_ids)[:1]}
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


def _eligible_replacement(material: dict) -> bool:
    licence = material['license']
    return bool(licence.get('url') and licence.get('kind') in {'cc0', 'cc_by'}
                and not licence['restrictions'] and (not licence['attribution_required'] or material['author']))


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


def _same_pixels(client, deadline, asset, info, hosts=('upload.wikimedia.org',)):
    url = info.get('url')
    size = info.get('size')
    if (not asset.get('pixel_sha256') or (info.get('width'), info.get('height')) != (asset.get('width'), asset.get('height'))
            or not isinstance(size, int) or not 0 < size <= 2_000_000 or not any(_http_url(url, host=host) for host in hosts)):
        return False
    data = _read(client, url, None, deadline, 2_000_000)
    with Image.open(BytesIO(data), formats=('JPEG', 'PNG', 'WEBP', 'GIF')) as image:
        return image.size == (asset['width'], asset['height']) and pixel_hash(image) == asset['pixel_sha256']


def _candidate_photo(client, deadline, asset, info, hosts=THUMBNAIL_HOSTS):
    thumbnail = info.get('thumburl')
    if not any(_http_url(thumbnail, host=host) for host in hosts):
        return None
    data = _read(client, thumbnail, None, deadline, 360_000)
    with Image.open(BytesIO(data), formats=('JPEG', 'PNG', 'WEBP', 'GIF')) as image:
        if image.width * image.height > 350_000:
            return None
        distance = None
        if asset.get('phash') and asset.get('width') and asset.get('height'):
            ratio = (image.width / image.height) / (asset['width'] / asset['height'])
            difference = (int(asset['phash'], 16) ^ int(image_phash(image), 16)).bit_count()
            if difference <= 2 and 0.9 <= ratio <= 1.1:
                return None  # A recoded copy does not replace the original.
            distance = difference + min(12, round(8 * abs(math.log(ratio))))
        return base64.b64encode(jpeg_preview(image, 360)).decode('ascii'), distance


def _item_outcome(matches, branding, intended_use, failed, alternatives):
    modified = intended_use in {'redistribute_modified', 'commercial_modified'}
    decision = commercial_conditions(matches, branded=branding, modified=modified)
    source = min(matches, key=lambda item: item['match_kind'] == 'similar', default=None)
    action = decision['action']
    if failed and action == 'unverified':
        decision['detail'] += ' Часть запросов к источникам не завершилась.'
    item = {'status': {'keep': 'reuse', 'credit': 'credit'}.get(action, 'review'),
            'source': source, 'rights': decision, 'reason': decision['detail'], 'failed': failed,
            'alternatives': alternatives}
    if action == 'credit':
        item['credit'] = photo_credit(source, modified)
    return item


def _stock_match(client, deadline, asset, image_id):
    for hit in pixabay.lookup(client, deadline, image_id=image_id):
        candidates = [
            {'url': hit['imageURL'], 'width': hit['imageWidth'], 'height': hit['imageHeight'], 'size': hit['imageSize']},
            {'url': hit['webformatURL'], 'width': hit['webformatWidth'], 'height': hit['webformatHeight'], 'size': 2_000_000},
        ]
        if any(_same_pixels(client, deadline, asset, info, pixabay.MEDIA_HOSTS) for info in candidates):
            return pixabay.material(hit) | {'match': 'Все декодированные пиксели совпали с файлом Pixabay', 'match_kind': 'pixels'}
    return None


def _stock_replacements(client, deadline, asset, term, excluded):
    candidates = []
    words = _terms(term)
    if not words:
        return candidates
    for hit in pixabay.lookup(client, deadline, term=term):
        tags = _terms(hit['tags'])
        overlap = words & tags
        if hit['pageURL'] in excluded or len(overlap) < min(2, len(words)):
            continue
        if re.search(r'\b(?:logo|logotype|trademark|brand|branding|логотип|бренд)\b', hit['tags'], re.IGNORECASE):
            continue
        photo = _candidate_photo(client, deadline, asset, {'thumburl': hit['webformatURL']}, pixabay.MEDIA_HOSTS)
        if photo:
            preview, distance = photo
            candidates.append({**pixabay.material(hit), 'preview': preview, 'distance': distance, 'relevance': len(overlap)})
        if len(candidates) >= 3:
            break
    return candidates


def _check_asset(client, deadline, asset, query, vision, intended_use):
    excluded, matches, alternatives = set(), [], []
    sha1 = asset.get('sha1')
    failed = False
    if isinstance(sha1, str) and re.fullmatch(r'[0-9a-f]{40}', sha1):
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
        try:
            pages = _pages(_query(client, deadline, {'titles': '|'.join(vision['commons']), 'prop': 'imageinfo',
                                                      'iiprop': 'sha1|extmetadata|url|mime|size'}))
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
    if not matches and not branding and vision.get('stock_ids'):
        try:
            if match := _stock_match(client, deadline, asset, vision['stock_ids'][0]):
                matches.append(match)
                excluded.add(match['url'])
        except NETWORK_ERRORS:
            failed = True
    source_term = _meaningful(query.get('source', '') or vision.get('label', '') or asset['description'])
    replacement_term = _meaningful(query.get('replacement', '') or vision.get('label', '') or asset['description'])
    if matches:
        replacement_term = replacement_term or _meaningful(asset['search_context']) or _meaningful(matches[0]['title'].rsplit('.', 1)[0])
    elif not source_term:
        context = _meaningful(asset['search_context'])
        name = _meaningful(asset['name'].rsplit('.', 1)[0])
        source_term = (context or name) if asset['prefer_context'] else (name or context)
    replacement_term = replacement_term or source_term
    original = _item_outcome(matches, branding, intended_use, failed, [])
    if original['status'] == 'reuse':
        return original

    look_for = list(dict.fromkeys(term for term in (source_term, replacement_term if not branding else '') if term))
    preview_budget = 6
    for term in look_for:
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
            if (term != replacement_term or branding or not preview_budget or not _eligible_replacement(material)
                    or not isinstance(width, int) or width < 320 or not isinstance(height, int) or height < 240):
                continue
            words = _terms(replacement_term)
            overlap = words & _terms(f"{material['title']} {material['description']}")
            if not words or len(overlap) < min(2, len(words)):
                continue
            preview_budget -= 1
            try:
                photo = _candidate_photo(client, deadline, asset, info)
            except NETWORK_ERRORS:
                failed = True
                continue
            if photo:
                preview, distance = photo
                alternatives.append({**material, 'preview': preview, 'distance': distance, 'relevance': len(overlap)})
    alternatives = [material for material in alternatives if material['url'] not in excluded]
    if replacement_term and not branding and not any(not material['license']['attribution_required'] for material in alternatives):
        try:
            alternatives.extend(_stock_replacements(client, deadline, asset, replacement_term, excluded))
        except NETWORK_ERRORS:
            failed = True
    return _item_outcome(matches, branding, intended_use, failed, alternatives)


def _ready_replacement(material: dict) -> dict:
    result = {key: value for key, value in material.items() if key not in {'distance', 'relevance', 'description'}}
    requires_credit = material['license']['attribution_required']
    result['kind'] = 'with_credit' if requires_credit else 'no_credit'
    if requires_credit:
        result['credit'] = photo_credit(material)
    return result


def _candidate_group(alternatives, attributed):
    eligible = [item for item in alternatives if item['license']['attribution_required'] is attributed]
    return sorted(eligible, key=lambda item: (
        item['distance'] if item['distance'] is not None else 100,
        -item['relevance'],
    ))


def _rank_replacements(outcomes, assets, ai_enabled):
    groups, requests = {}, []
    for outcome, asset in zip(outcomes, assets):
        if outcome['rights']['action'] == 'keep':
            continue
        for attributed in (False, True):
            if attributed and outcome['rights']['action'] == 'credit':
                continue
            kind = 'with_credit' if attributed else 'no_credit'
            group_id = f"{asset['id']}:{kind}"
            group = _candidate_group(outcome['alternatives'], attributed)
            if not group:
                continue
            # The model may choose among comparably close photos. Keep the visual ground
            # truth for deciding whether a second, attributed option is actually closer.
            best = group[0]['distance']
            group = [item for item in group if item['distance'] == best][:3]
            groups[group_id] = group
            if len(group) > 1:
                requests.append({'id': group_id, 'context': asset['search_context'], 'description': asset['description'],
                                 'candidates': [{'id': n, 'title': item['title'], 'description': item['description']}
                                                for n, item in enumerate(group)]})
    ranks = rank_image_replacements(requests) if ai_enabled and requests else {}
    selected = {}
    for key, group in groups.items():
        choice = ranks.get(key, 0)
        if choice == -1:
            continue
        selected[key] = group[choice] if 0 <= choice < len(group) else group[0]
    return selected, len(ranks)


def _add_photos(asset, outcome, report_previews, reverse_evidence=None):
    outcome.update(id=asset['id'], name=asset['name'], locations=asset['locations'],
                   web_matches=(reverse_evidence or {}).get('links', []))
    if preview := (report_previews or {}).get(asset['sha256']):
        outcome['original_preview'] = base64.b64encode(preview).decode('ascii')
    return outcome


def _unchecked(asset, report_previews, *, limit=False):
    detail = ('Изображение вне лимита онлайн-поиска этой проверки.' if limit
              else 'Поиск источника выключен. Разрешение на коммерческое использование не установлено.')
    decision = {'category': 'unknown', 'label': 'Условия не проверялись',
                'action': 'unverified', 'detail': detail, 'warning': '', 'changes': ''}
    return _add_photos(asset, {'status': 'review', 'rights': decision, 'reason': detail,
                              'failed': False, 'replacements': []}, report_previews)


def review_images(extraction: dict, intended_use: str, online: bool, ai_enabled: bool,
                  previews: dict[str, bytes] | None = None, *, report_previews: dict[str, bytes] | None = None,
                  reference: tuple[str, list[str]] | None = None) -> dict:
    assets = _assets(extraction)
    selected = assets[:MAX_ASSETS]
    if not selected:
        return {'status': 'no_images', 'total': 0, 'checked': 0, 'items': []}
    if not online:
        items = [_unchecked(asset, report_previews) for asset in assets]
        return {'status': 'local_only', 'total': len(assets), 'checked': 0, 'items': items}
    limited = [{'id': item['id'], 'context': item['search_context'][:360], 'description': item['description'][:180],
                'name': item['name'][:100], 'logo': ', '.join(ref['brand'] for ref in item.get('logo_references', []))}
               for item in selected]
    reverse_status, reversed_images = (_vision_matches(dict(list(previews.items())[:6])) if previews is not None
                                       else ('disabled', {}))
    reverse_checked = len(reversed_images)
    urls = extraction.get('links', []) + (reference[1] if reference else [])
    linked_check, linked_images = compare_links(selected, urls, report_previews or {})
    if reference:
        linked_check['reference'] = reference[0]
    # Provider-reported matches are leads. Check supported pages directly when possible.
    reverse_urls = [link['url'] for image in reversed_images.values() for link in image.get('links', [])[:3]]
    _, checked_reverse = compare_links(selected, reverse_urls, report_previews or {})
    linked_commons = list(dict.fromkeys(title for url in urls if (title := _commons_title(url))))[:2]
    linked_stock = list(dict.fromkeys(photo_id for url in urls if (photo_id := pixabay.photo_id(url))))[:1]
    for image, item in zip(limited, selected):
        hints = reversed_images.setdefault(item['sha256'], {})
        checked = [findings[item['sha256']] for findings in (linked_images, checked_reverse)
                   if item['sha256'] in findings]
        if checked:
            links = {hit['url']: hit for result in checked for hit in result['links']}
            for hit in hints.get('links', []):
                links.setdefault(hit['url'], hit)
            hints['links'] = list(links.values())[:8]
            hints['label'] = next((label for result in checked for candidate in result['labels']
                                   if (label := _meaningful(candidate))),
                                  hints.get('label', ''))
        image['visual_hint'] = hints.get('label', '')
        hints['commons'] = list(dict.fromkeys(hints.get('commons', []) + linked_commons))[:4]
        hints['stock_ids'] = list(dict.fromkeys(hints.get('stock_ids', []) + linked_stock))[:1]
    planning = plan_image_searches(limited, 'commercial') if ai_enabled else {'status': 'disabled', 'queries': {}}
    deadline = monotonic() + 24
    with httpx.Client(headers=HTTP_HEADERS, follow_redirects=False) as client, ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_check_asset, client, deadline, item, planning['queries'].get(item['id'], {}),
                               reversed_images.get(item['sha256'], {}), intended_use) for item in selected]
        outcomes = [future.result() for future in futures]
    ranked, count = _rank_replacements(outcomes, selected, ai_enabled)
    items = []
    for asset, outcome in zip(selected, outcomes):
        outcome.pop('alternatives')
        no_credit = ranked.get(f"{asset['id']}:no_credit")
        with_credit = ranked.get(f"{asset['id']}:with_credit")
        choices = [_ready_replacement(no_credit)] if no_credit else []
        if with_credit and (not no_credit or (
                with_credit['distance'] is not None and no_credit['distance'] is not None
                and with_credit['distance'] < no_credit['distance'])):
            choices.append(_ready_replacement(with_credit))
        outcome['replacements'] = choices
        if choices:
            outcome['replacement'] = {key: value for key, value in choices[0].items() if key != 'preview'}
            if outcome['status'] == 'review':
                outcome['status'] = 'replace'
        items.append(_add_photos(asset, outcome, report_previews, reversed_images.get(asset['sha256'])))
    items.extend(_unchecked(asset, report_previews, limit=True) for asset in assets[MAX_ASSETS:])
    return {'status': 'partial' if len(assets) > len(selected) or any(item['failed'] for item in items)
                      or reverse_status in {'unavailable', 'partial', 'not_configured', 'not_available'} else 'complete',
            'total': len(assets), 'checked': len(selected), 'model_status': planning['status'],
            'model_ranked': count, 'reverse_status': reverse_status, 'reverse_checked': reverse_checked,
            'linked_check': linked_check,
            'items': items, 'checked_at': datetime.now(UTC).isoformat(timespec='seconds')}
