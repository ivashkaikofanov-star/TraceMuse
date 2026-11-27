"""Sources and registry evidence for an audit. Remote inputs remain untrusted."""
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from io import BytesIO
import json
from pathlib import Path
import re
from time import monotonic
import unicodedata
from urllib.parse import quote, unquote, urlsplit

import httpx
from PIL import Image

from .config import settings
from .extraction import image_phash

COMMONS_HOST = "commons.wikimedia.org"
COMMONS_API = f"https://{COMMONS_HOST}/w/api.php"
THUMBNAIL_HOSTS = {"thumb.wikimedia.org", "upload.wikimedia.org"}
SOURCE_RANK = {"identical_image": 0, "brand_visual": 1, "brand_text": 2,
               "visual_similarity": 3, "commons_link": 4, "catalog_candidate": 5}
HTTP_HEADERS = {"User-Agent": "TraceMuse/0.2 (https://github.com/ivashkaikofanov-star/TraceMuse)"}
REGISTRIES = {
    "foreign_agents": (
        "Реестр иностранных агентов · Минюст России",
        "https://minjust.gov.ru/ru/pages/reestr-inostryannykh-agentov/",
    ),
    "undesirable_organizations": (
        "Перечень нежелательных организаций · Минюст России",
        "https://minjust.gov.ru/ru/pages/perechen-inostrannyh-i-mezhdunarodnyh-organizacij-"
        "deyatelnost-kotoryh-priznana-nezhelatelnoj-na-territorii-rossijskoj-federacii/",
    ),
    "extremist_materials": (
        "Федеральный список экстремистских материалов · Минюст России",
        "https://minjust.gov.ru/ru/extremist-materials/",
    ),
    "extremist_organizations": (
        "Перечни организаций в сфере противодействия экстремизму · Минюст России",
        "https://minjust.gov.ru/ru/pages/perechen-nko-likvidirovannyh/",
    ),
    "terrorism_extremism": (
        "Перечень сведений о причастности к экстремизму или терроризму · Росфинмониторинг",
        "https://www.fedsfm.ru/DOCUMENTS/TERR-LIST",
    ),
    "restricted_sites": (
        "Проверка ограничения доступа к сайтам · Роскомнадзор",
        "https://eais.rkn.gov.ru/",
    ),
}
LEGAL_REGISTRIES = [{"id": key, "title": title, "url": url} for key, (title, url) in REGISTRIES.items()]

AUDIT_CAVEAT = "Совпадение имени, названия или домена само по себе не определяет применимость запрета или маркировки."
COMMONS_CAVEAT = "Wikimedia Commons охватывает только свой каталог. Совпадение не удостоверяет первоисточник или правообладателя."
BRAND_NAMES = (
    ("Coca-Cola", r"(?<!\w)(?:coca[\s\-‑–]+cola|кока[\s\-‑–]+кол[аыеуой]+)(?!\w)",
     "https://www.coca-cola.com/us/en/brands/coca-cola"),
    ("Pepsi", r"(?<!\w)(?:pepsi|пепси)(?!\w)", "https://www.pepsi.com/"),
    ("adidas", r"(?<!\w)(?:adidas|адидас)(?!\w)", "https://www.adidas.com/"),
)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []
        self.raw_text = False

    def handle_starttag(self, tag, attrs):
        if tag in {"script", "style"}:
            self.raw_text = True

    def handle_endtag(self, tag):
        if tag in {"script", "style"}:
            self.raw_text = False

    def handle_data(self, data):
        if not self.raw_text:
            self.parts.append(data)


def _plain(value, limit=400):
    parser = PlainText()
    parser.feed(str(value or "")[:2000])
    parser.close()
    return " ".join(" ".join(parser.parts).split())[:limit]


def _http_url(url, *, host=None):
    if not isinstance(url, str) or not url or len(url) > 2048:
        return None
    if any(ord(character) < 33 or ord(character) == 127 for character in url) or "\\" in url:
        return None
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        if parsed.scheme not in {"http", "https"} or not hostname or parsed.username or parsed.password:
            return None
        if host and (parsed.scheme != "https" or hostname != host or parsed.port not in {None, 443}):
            return None
        return parsed
    except ValueError:
        return None


def _catalog_meta(info, key):
    metadata = info.get("extmetadata")
    field = metadata.get(key) if isinstance(metadata, dict) else None
    return field.get("value", "") if isinstance(field, dict) else ""


def _license(info):
    name = _plain(_catalog_meta(info, "LicenseShortName") or _catalog_meta(info, "License"))
    raw_url = _catalog_meta(info, "LicenseUrl")
    if isinstance(raw_url, str) and raw_url.startswith("//"):
        raw_url = "https:" + raw_url
    parsed = _http_url(raw_url)
    restrictions = _plain(_catalog_meta(info, "Restrictions"))
    record = {
        "name": name or "Лицензия не установлена",
        "status": "catalog_declaration" if name else "unestablished",
        "commercial_use": "Не установлено",
        "modification": "Не установлено",
        "attribution": "Не установлено",
        "restrictions": [restrictions] if restrictions else [],
    }
    if parsed:
        record["url"] = raw_url

    if not parsed or parsed.hostname != "creativecommons.org":
        return record
    if re.match(r"^/publicdomain/zero/1\.0(?:/|$)", parsed.path):
        record.update(commercial_use="Разрешено по заявленной CC0",
                      modification="Разрешено по заявленной CC0",
                      attribution="CC0 не требует атрибуции",
                      commercial_allowed=True, derivatives_distribution_allowed=True,
                      attribution_required=False, share_alike=False)
    match = re.match(r"^/licenses/(by(?:-nc)?(?:-sa|-nd)?)/[1-4]\.\d(?:/|$)", parsed.path)
    if match:
        terms = set(match[1].split("-"))
        record.update(
            commercial_use="Требуется отдельное разрешение" if "nc" in terms else "Допускается при соблюдении условий",
            modification="Распространение изменённой версии требует отдельного разрешения"
            if "nd" in terms else "Допускается при соблюдении условий",
            attribution="Требуется указание авторства",
            commercial_allowed="nc" not in terms,
            derivatives_distribution_allowed="nd" not in terms,
            attribution_required=True,
            share_alike="sa" in terms,
        )
        if "sa" in terms:
            record["restrictions"].append("Производные работы распространяются на совместимых условиях ShareAlike")
    return record


def _source(date_checked, title, url, match, confidence, *, info=None, method=None, query=None):
    result = {"title": title, "url": url, "checked_at": date_checked, "match": match, "confidence": confidence}
    if method:
        result["method"] = method
    if query:
        result["query"] = query
    if info is not None:
        result["license"] = _license(info)
        creator = _plain(_catalog_meta(info, "Artist"))
        if creator:
            result["creator"] = creator
        photo_date = _plain(_catalog_meta(info, "DateTimeOriginal"), 80)
        if photo_date:
            result["catalog_creation_date"] = photo_date
    return result


def _commons_title(url):
    parsed = _http_url(url, host=COMMONS_HOST)
    if not parsed or not parsed.path.startswith("/wiki/File:"):
        return None
    title = unquote(parsed.path.removeprefix("/wiki/")).replace("_", " ")
    return title if len(title) <= 240 and not any(marker in title for marker in "|\n\r") else None


def _quote_query(text):
    cleaned = " ".join(re.sub(r"https?://\S+", " ", text).split()).strip()
    if len(cleaned) > 150:
        cleaned = cleaned[:150].rsplit(" ", maxsplit=1)[0] if " " in cleaned[:150] else cleaned[:150]
    return cleaned.replace('"', " ").strip()


def _query_phrase(extraction):
    for line in extraction.get("text", "")[:1800].splitlines():
        if (usable := _quote_query(line)) and len(usable) >= 5:
            return usable
    meta = extraction.get("metadata", {})
    for key, value in meta.items():
        if str(key).casefold() in {"title", "subject", "dc:title", "description", "заголовок"} and isinstance(value, str):
            if usable := _quote_query(value):
                return usable
    for image in extraction.get("images", [])[:4]:
        basename = str(image.get("name") or "").replace("\\", "/").rsplit("/", maxsplit=1)[-1]
        title = _quote_query(re.sub(r"[_-]+", " ", Path(basename).stem))
        if len(title) >= 3 and any(character.isalpha() for character in title):
            return title
    return None


def _read(client, url, params, deadline, limit):
    if monotonic() >= deadline:
        raise TimeoutError
    remaining = min(3.5, deadline - monotonic())
    if remaining <= 0:
        raise TimeoutError
    with client.stream("GET", url, params=params, timeout=remaining) as response:
        response.raise_for_status()
        content = bytearray()
        for chunk in response.iter_bytes(32_768):
            if len(content) + len(chunk) > limit or monotonic() >= deadline:
                raise ValueError("Research response exceeds its limits")
            content.extend(chunk)
    return bytes(content)


def _query(client, deadline, params):
    response = _read(client, COMMONS_API, {"action": "query", **params, "format": "json"}, deadline, 1_000_000)
    answer = json.loads(response)
    if not isinstance(answer, dict) or "error" in answer:
        raise ValueError("Unsuccessful Wikimedia query")
    query = answer.get("query", {})
    if not isinstance(query, dict):
        raise ValueError("Unexpected Wikimedia response")
    return query


def _pages(query):
    results = query.get("pages", {})
    if not isinstance(results, dict):
        return []
    valid = (item for item in results.values() if isinstance(item, dict) and isinstance(item.get("title"), str))
    return sorted(valid, key=lambda item: item["index"] if isinstance(item.get("index"), int) else 0)


def _file_url(title):
    return f"https://{COMMONS_HOST}/wiki/{quote(title.replace(' ', '_'), safe=':')}"


def _record_candidate(records, checked, page, match, confidence, method, *, info=None, query=None):
    title = page.get("title", "")
    if not title.startswith("File:") or "missing" in page:
        return
    url = _file_url(title)
    if url in records and SOURCE_RANK[records[url]["method"]] <= SOURCE_RANK[method]:
        return
    if method == "commons_link":
        info = next(iter(page.get("imageinfo", [])), {})
    records[url] = _source(checked, title.removeprefix("File:"), url, match, confidence,
                           info=info, method=method, query=query)


def _visual_matches(client, checked, deadline, candidates, records, images, warnings):
    hashes = [(image.get("name", "изображение"), int(value, 16)) for image in images
              if re.fullmatch(r"[0-9a-f]{16}", value := image.get("phash") or "")]
    ids = [str(page["pageid"]) for page in candidates if isinstance(page.get("pageid"), int)]
    if not hashes or not ids:
        return
    answer = _query(client, deadline, {
        "pageids": "|".join(ids[:3]), "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 160,
    })
    original_pages = {page["pageid"]: page for page in candidates if isinstance(page.get("pageid"), int)}
    compared = 0
    for page in _pages(answer):
        thumbnail = next(iter(page.get("imageinfo", [])), {}).get("thumburl")
        if not any(_http_url(thumbnail, host=host) for host in THUMBNAIL_HOSTS):
            continue
        try:
            image_bytes = _read(client, thumbnail, None, deadline, 360_000)
            with Image.open(BytesIO(image_bytes), formats=("JPEG", "PNG", "WEBP", "GIF")) as image:
                if image.width * image.height > 350_000:
                    continue
                remote_hash = int(image_phash(image), 16)
        except (httpx.HTTPError, TimeoutError, OSError, ValueError, Image.DecompressionBombError):
            continue
        if page.get("pageid") not in original_pages:
            continue
        compared += 1
        name, distance = min(((name, (local_hash ^ remote_hash).bit_count()) for name, local_hash in hashes),
                             key=lambda item: item[1])
        if distance > 10:
            continue
        original = original_pages[page["pageid"]]
        existing = records.get(_file_url(original["title"]), {})
        if existing.get("method") == "identical_image":
            continue
        _record_candidate(records, checked, original, f"Перцептивный хеш публичной миниатюры близок к изображению «{name}»",
                          "Вероятное визуальное сходство", "visual_similarity", query=existing.get("query"))
    if compared:
        warnings.append(f"Локально сопоставлены публичные миниатюры кандидатов Wikimedia Commons: {compared}.")
    else:
        warnings.append("Публичные миниатюры кандидатов для визуального сравнения получить не удалось.")


def search_sources(extraction: dict, sha256: str, enabled: bool) -> dict:
    checked = datetime.now(UTC).isoformat()
    brand_signals = _brand_signals(extraction, checked)
    embedded = {}
    for url in extraction.get("links", [])[:50]:
        if parsed := _http_url(url):
            embedded[url] = _source(checked, parsed.hostname, url, "Ссылка из материала",
                                    "Страница и связь с первоисточником не проверены", method="embedded_url")
    warnings = [COMMONS_CAVEAT]
    if extraction.get("images"):
        warnings.append("Локальный каталог логотипов охватывает образцы Pepsi, adidas и Coca-Cola.")
    if brand_signals:
        warnings.append("Справочные признаки бренда помогают опознать обозначение. Они не подтверждают происхождение вашего файла или право использовать знак.")
    if not enabled:
        warnings.append("Пользователь не включил поиск в Wikimedia Commons.")
        return {"status": "local_only", "items": brand_signals + list(embedded.values()), "warnings": warnings}

    images = extraction.get("images", [])[:24]
    hashes = {}
    for image in images:
        value = image.get("sha1") or ""
        if re.fullmatch(r"[0-9a-f]{40}", value):
            hashes.setdefault(value, image.get("name", "изображение"))

    requests = []
    for value, name in list(hashes.items())[:2]:
        requests.append(("hash", {"list": "allimages", "aisha1": value, "ailimit": 3,
                                   "aiprop": "sha1|canonicaltitle|extmetadata"}, value, name))
    if len(hashes) > 2:
        warnings.append("Точный поиск SHA-1 ограничен первыми двумя различными изображениями.")
    titles = "|".join(sorted({title for url in embedded if (title := _commons_title(url))})[:4])
    if titles:
        requests.append(("link", {"titles": titles, "prop": "imageinfo", "iiprop": "extmetadata"}, None, None))
    for term in (_query_phrase(extraction), sha256 if re.fullmatch(r"[0-9a-f]{64}", sha256) else None):
        if term:
            requests.append(("search", {"generator": "search", "gsrsearch": term, "gsrnamespace": 6,
                                        "gsrlimit": 3, "prop": "imageinfo", "iiprop": "extmetadata"}, term, None))
    if not requests:
        warnings.append("В материале недостаточно данных для запроса к каталогу.")
        return {"status": "no_match", "items": brand_signals + list(embedded.values()), "warnings": warnings}

    deadline = monotonic() + 9.0
    records, candidates = {}, []
    completed = 0
    with httpx.Client(headers=HTTP_HEADERS, follow_redirects=False) as client:
        for method, params, term, name in requests:
            try:
                answer = _query(client, deadline, params)
                if method == "hash":
                    for info in answer.get("allimages", []):
                        if info.get("sha1") == term:
                            title = info.get("canonicaltitle") or f"File:{info.get('name', '')}"
                            _record_candidate(records, checked, {"title": title}, f"Совпал SHA-1 изображения «{name}»",
                                              "Точное совпадение хеша изображения", "identical_image", info=info)
                else:
                    pages = _pages(answer)
                    for page in pages:
                        if method == "link":
                            _record_candidate(records, checked, page, "Страница из ссылки в материале",
                                              "Связь с авторством и первоисточником требует проверки", "commons_link")
                        else:
                            _record_candidate(records, checked, page, "Результат поиска по строке в каталоге",
                                              "Поисковый кандидат, соответствие материалу требует проверки",
                                              "catalog_candidate", query=term)
                    if method == "search" and term != sha256:
                        candidates = pages
                completed += 1
            except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
                continue

        if candidates and images:
            try:
                _visual_matches(client, checked, deadline, candidates, records, images, warnings)
            except (httpx.HTTPError, TimeoutError, ValueError, TypeError, AttributeError):
                warnings.append("Публичные миниатюры для визуального сравнения недоступны.")
    if completed < len(requests):
        warnings.append(f"Wikimedia Commons вернула пригодный ответ на {completed} из {len(requests)} запросов.")
    candidates_without_match = [url for url, record in records.items() if record["method"] == "catalog_candidate"]
    for url in candidates_without_match:
        del records[url]
    if candidates_without_match:
        warnings.append("Текстовые результаты без подтверждённого совпадения исключены. Их лицензии к проверяемому материалу не применялись.")

    status = "unavailable"
    if completed:
        status = "partial" if completed < len(requests) else "complete"
        if not records and status == "complete":
            status = "no_match"
    if status == "no_match":
        warnings.append("В проверенных результатах Wikimedia Commons не удалось установить совпадение с материалом.")

    sorted_sources = sorted([*records.values(), *brand_signals], key=lambda source: SOURCE_RANK[source["method"]])
    seen = {source["url"] for source in sorted_sources}
    return {"status": status, "items": sorted_sources + [source for url, source in embedded.items() if url not in seen],
            "warnings": warnings}


def _brand_signals(extraction, checked):
    visual, seen_brands = {}, set()
    for image in extraction.get("images", []):
        for reference in image.get("logo_references", []):
            location = f" · {image['location']}" if image.get("location") else ""
            part = "фрагмент изображения" if reference["part"] else "изображение"
            visual.setdefault(reference["url"], _source(
                checked, reference["title"], reference["url"],
                f"По форме сопоставлен {part} «{image['name']}»{location}.",
                "Визуальное сходство со справочным образцом", method="brand_visual",
            ))
            seen_brands.add(reference["brand"])

    text = extraction.get("text", "")
    mentions = []
    for brand, pattern, url in BRAND_NAMES:
        if brand not in seen_brands and (match := re.search(pattern, text, re.IGNORECASE)):
            mentions.append(_source(checked, brand, url, f"В извлечённом тексте найдено «{match[0]}».",
                                    "Подтверждено текстом документа", method="brand_text"))
    return [*visual.values(), *mentions]


def _normalized(value):
    return " ".join(unicodedata.normalize("NFKC", value).casefold().split())


def _exact_phrase(haystack, phrase):
    start = 0
    while (index := haystack.find(phrase, start)) >= 0:
        end = index + len(phrase)
        if (index == 0 or not haystack[index - 1].isalnum()) and (end == len(haystack) or not haystack[end].isalnum()):
            return True
        start = end
    return False


def _registered_snapshot(path, registry):
    with path.open("rb") as handle:
        contents = handle.read(3_000_001)
    if len(contents) > 3_000_000:
        raise ValueError("Snapshot limit exceeded")
    content = json.loads(contents)
    if not isinstance(content, dict) or content.get("version") != 1 or not isinstance(content.get("registries"), dict):
        raise ValueError("Invalid snapshot schema")
    validated = {}
    for key, group in content["registries"].items():
        if key not in registry or not isinstance(group, dict):
            raise ValueError("Invalid registry group")
        official_url = registry[key][1]
        snapshot_date = date.fromisoformat(group.get("retrieved_at", ""))
        if group.get("source_url") != official_url or snapshot_date > datetime.now(UTC).date():
            raise ValueError("Invalid source URL or date")
        entries = group.get("entries")
        if not isinstance(entries, list) or not entries or len(entries) > 10_000:
            raise ValueError("Invalid snapshot size")
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("value"), str) or not 4 <= len(entry["value"]) <= 400:
                raise ValueError("Invalid snapshot entry")
            if not any(character.isalnum() for character in entry["value"]):
                raise ValueError("Empty snapshot entry")
            reference = entry.get("reference")
            if not isinstance(reference, str) or not reference.strip() or len(reference) > 100:
                raise ValueError("Invalid snapshot reference")
            if key == "restricted_sites" and not re.fullmatch(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+", entry["value"], re.I):
                raise ValueError("Invalid snapshot domain")
        validated[key] = (snapshot_date.isoformat(), entries)
    return validated


def check_legal(extraction: dict, snapshot_path: Path | str | None = None) -> dict:
    checked = datetime.now(UTC).isoformat()
    warnings = [AUDIT_CAVEAT, "Актуальные официальные страницы доступны по ссылкам для ручной сверки."]
    configured = snapshot_path if snapshot_path is not None else getattr(settings, "legal_snapshot_path", "")
    snapshots = {}
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = Path(__file__).resolve().parents[1] / path
        try:
            snapshots = _registered_snapshot(path, REGISTRIES)
        except (OSError, UnicodeError, ValueError, TypeError):
            warnings.append("Локальную копию реестров не удалось прочитать или проверить по схеме. Сопоставление не выполнено.")
    if snapshots:
        warnings.append("Охват и актуальность локальной копии зависят от её подготовки. Проверьте совпадения на официальном сайте.")
    else:
        warnings.append("Локальная копия реестров не настроена. Правовой статус и необходимость маркировки не проверены.")

    content = [_normalized(str(extraction.get("text") or "")[:160_000])]
    content.extend(_normalized(value[:1000]) for value in extraction.get("metadata", {}).values()
                   if isinstance(value, str))
    domains = {parsed.hostname.casefold() for url in extraction.get("links", []) if (parsed := _http_url(url))}
    items = []
    for registry, (title, url) in REGISTRIES.items():
        record = {"title": title, "url": url, "checked_at": checked, "registry": registry}
        if registry not in snapshots:
            items.append({**record, "status": "manual_review",
                          "match": "Совпадение и применимость требований требуют ручной сверки."})
            continue
        snapshot_date, entries = snapshots[registry]
        matches = []
        for entry in entries:
            name = _normalized(entry["value"])
            matched = name in domains if registry == "restricted_sites" else any(_exact_phrase(text, name) for text in content)
            if matched:
                matches.append(entry)
        record["snapshot_date"] = snapshot_date
        if not matches:
            items.append({**record, "status": "local_snapshot_no_match",
                          "match": "Точных совпадений в локальной копии не найдено."})
        for entry in matches[:10]:
            items.append({**record, "status": "local_snapshot_match", "entry": entry["value"],
                          "reference": entry["reference"], "confidence": "Точное строковое совпадение",
                          "match": f"В локальной копии совпало «{entry['value']}» (запись {entry['reference']})."})
        if len(matches) > 10:
            warnings.append(f"Часть совпадений скрыта из-за лимита отчёта. {title}.")
    status = "manual_review"
    if snapshots:
        status = "local_snapshot" if len(snapshots) == len(REGISTRIES) else "partial"
    return {"status": status, "items": items, "warnings": warnings}
