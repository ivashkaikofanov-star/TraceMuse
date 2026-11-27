"""Optional stock evidence. Pixabay responses are cached locally for 24 hours."""

import hashlib
import json
import re
from threading import Lock
from time import time

from .config import settings
from .db import connect
from .evidence import _http_url, _read

API_URL = 'https://pixabay.com/api/'
LICENCE_URL = 'https://pixabay.com/service/terms/'
MEDIA_HOSTS = {'pixabay.com', 'cdn.pixabay.com'}
USAGE_NOTE = ('Лицензия Pixabay подходит для использования в составе материала. Она ограничивает отдельную '
              'перепродажу файла и коммерческое использование контента с узнаваемыми знаками.')
_CACHE_LOCK = Lock()


def photo_id(url):
    parsed = _http_url(url, host='pixabay.com')
    if parsed and (match := re.fullmatch(r'/(?:[a-z]{2}/)?(?:photos|illustrations)/[^/]+-(\d+)/?', parsed.path)):
        return match[1]
    return None


def lookup(client, deadline, *, term='', image_id=None):
    api_key = settings.pixabay_api_key.get_secret_value().strip()
    if not api_key or not (term or image_id):
        return []
    params = {'id': image_id} if image_id else {
        'q': term, 'image_type': 'photo', 'safesearch': 'true', 'per_page': 6,
    }
    cache_key = hashlib.sha256(json.dumps(params, sort_keys=True).encode()).hexdigest()
    with _CACHE_LOCK:
        with connect() as db:
            row = db.execute('SELECT value FROM pixabay_cache WHERE cache_key=? AND expires>?', (cache_key, int(time()))).fetchone()
        if row:
            return json.loads(row['value'])
        raw = _read(client, API_URL, {'key': api_key, **params}, deadline, 192_000)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or not isinstance(payload.get('hits'), list):
            raise ValueError('Pixabay returned no results list')
        matches = []
        for hit in payload['hits'][:6]:
            if not isinstance(hit, dict) or not photo_id(hit.get('pageURL')):
                continue
            if not isinstance(hit.get('tags'), str):
                continue
            media = hit.get('webformatURL')
            if not any(_http_url(media, host=host) for host in MEDIA_HOSTS):
                continue
            matches.append({name: hit.get(name) for name in (
                'id', 'pageURL', 'tags', 'user', 'type', 'webformatURL', 'webformatWidth', 'webformatHeight',
                'imageURL', 'imageWidth', 'imageHeight', 'imageSize',
            )})
        now = int(time())
        with connect() as db:
            db.execute('DELETE FROM pixabay_cache WHERE expires<=?', (now,))
            db.execute('INSERT OR REPLACE INTO pixabay_cache VALUES (?, ?, ?)',
                       (cache_key, json.dumps(matches), now + 86_400))
    return matches


def material(hit):
    tags = hit['tags']
    return {
        'title': f'Pixabay · {tags[:130]}', 'url': hit['pageURL'],
        'author': str(hit.get('user') or '')[:160], 'description': tags[:200],
        'license': {'kind': 'stock', 'name': 'Pixabay Content License', 'url': LICENCE_URL, 'restrictions': [],
                    'commercial_allowed': True, 'attribution_required': False, 'usage_note': USAGE_NOTE},
    }
