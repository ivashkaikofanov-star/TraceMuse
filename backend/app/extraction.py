"""Bounded local inspection of uploaded documents. No resource is executed or fetched."""

import base64
import binascii
import hashlib
import io
import math
import posixpath
import re
import stat
import statistics
import struct
import zipfile
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from typing import BinaryIO
from urllib.parse import urlsplit
from xml.etree.ElementTree import ParseError, TreeBuilder

from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from PIL import Image
from pypdf import PdfReader, apply_configuration
from pypdf.errors import PyPdfError

from .visual import identify_graphics

MIB = 1024 * 1024
MAX_TEXT = 80_000
MAX_LINKS = 150
MAX_IMAGES = 80
MAX_WARNINGS = 30
MAX_ENTRY = 24 * MIB
MAX_ARCHIVE_OUTPUT = 150 * MIB
MAX_ARCHIVE_READ = 64 * MIB
MAX_ARCHIVE_ENTRIES = 1500
MAX_XML = 8 * MIB
MAX_HTML = 4 * MIB
MAX_PIXELS = 30_000_000
MAX_PDF_PAGES = 100
EXTENSIONS = {
    '.pdf': 'PDF', '.docx': 'DOCX', '.pptx': 'PPTX', '.html': 'HTML',
    '.htm': 'HTML', '.png': 'PNG', '.jpg': 'JPEG', '.jpeg': 'JPEG',
    '.webp': 'WEBP', '.svg': 'SVG', '.zip': 'ZIP',
}
RASTER = {'PNG', 'JPEG', 'WEBP'}
SAFE_IMAGE_DECODERS = ('PNG', 'JPEG', 'WEBP', 'GIF', 'BMP', 'TIFF', 'ICO', 'AVIF', 'JPEG2000')
MEDIA_EXTENSIONS = {'.png', '.jpg', '.jpeg', '.webp', '.svg', '.gif', '.bmp', '.tif', '.tiff', '.wmf', '.emf', '.ico', '.avif'}
LINK_PATTERN = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
WHITESPACE = re.compile(r'\s+')
DCT = tuple(tuple(math.cos((2 * i + 1) * k * math.pi / 64) for i in range(32)) for k in range(8))


class ExtractionError(ValueError):
    """The input could not be identified or processed within the limits."""


class Result:
    def __init__(self, kind: str, previews: dict[str, bytes] | None = None):
        self.kind = kind
        self.text: list[str] = []
        self.text_length = 0
        self.links: dict[str, None] = {}
        self.metadata: dict = {}
        self.images: list[dict] = []
        self.warnings: list[str] = []
        self.entries: list[dict] | None = None
        self.previews = previews

    def warn(self, message: str) -> None:
        if message not in self.warnings and len(self.warnings) < MAX_WARNINGS:
            self.warnings.append(message)

    def add_text(self, value: str) -> None:
        value = WHITESPACE.sub(' ', value).strip()
        if not value:
            return
        available = MAX_TEXT - self.text_length
        if available < len(value):
            self.warn('Достигнут предел сохранённого текста.')
        if available > 0:
            value = value[:available]
            self.text.append(value)
            self.text_length += len(value) + 1
        for link in LINK_PATTERN.finditer(value[:MAX_TEXT]):
            self.add_link(link.group().rstrip('.,;:!?)]}'))

    def add_link(self, value: str) -> None:
        value = value.strip()
        if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
            return
        try:
            url = urlsplit(value)
        except ValueError:
            return
        if url.scheme not in {'', 'http', 'https', 'mailto', 'tel'}:
            return
        if url.scheme in {'http', 'https'} and not url.hostname:
            return
        if value in self.links:
            return
        if len(self.links) >= MAX_LINKS:
            self.warn('Достигнут предел сохранённых ссылок.')
            return
        self.links[value] = None

    def add_image(self, record: dict) -> None:
        if len(self.images) >= MAX_IMAGES:
            self.warn('Достигнут предел сохранённых изображений.')
            return
        self.images.append(record)

    def serialize(self) -> dict:
        result = {
            'format': self.kind,
            'text': '\n'.join(self.text),
            'links': list(self.links),
            'metadata': self.metadata,
            'images': self.images,
            'warnings': self.warnings,
        }
        if self.entries is not None:
            result['entries'] = self.entries
        return result


def _digest(stream: BinaryIO, algorithm: str) -> str:
    stream.seek(0)
    digest = hashlib.file_digest(stream, algorithm).hexdigest()
    stream.seek(0)
    return digest


def _bounded_read(stream: BinaryIO, limit: int) -> bytes:
    data = stream.read(limit + 1)
    if len(data) > limit:
        raise ExtractionError(f'Превышен внутренний предел чтения ({limit // MIB} МБ).')
    return data


def _short(value: object) -> str:
    return str(value).strip()[:512]


def _local(tag: str) -> str:
    return tag.rsplit('}', 1)[-1]


class BoundedXMLTree(TreeBuilder):
    def __init__(self):
        super().__init__()
        self.depth = 0
        self.elements = 0

    def start(self, tag, attrs):
        self.depth += 1
        self.elements += 1
        if self.depth > 128 or self.elements > 100_000:
            raise ExtractionError('XML-разметка превышает допустимую сложность.')
        return super().start(tag, attrs)

    def end(self, tag):
        self.depth -= 1
        return super().end(tag)


def _xml(data: bytes):
    parser = ElementTree.DefusedXMLParser(target=BoundedXMLTree(), forbid_dtd=True)
    parser.feed(data)
    return parser.close()


def image_phash(image: Image.Image) -> str:
    """Return the first frame’s 64-bit perceptual image hash."""
    sample = image.convert('L').resize((32, 32), Image.Resampling.LANCZOS).tobytes()
    horizontal = [
        [sum(sample[row * 32 + i] * DCT[frequency][i] for i in range(32)) for frequency in range(8)]
        for row in range(32)
    ]
    coefficients = [
        sum(horizontal[row][column] * DCT[frequency][row] for row in range(32))
        for frequency in range(8)
        for column in range(8)
    ]
    median = statistics.median(coefficients[1:])
    signature = ''.join('1' if value > median else '0' for value in coefficients)
    return f'{int(signature, 2):016x}'


def pixel_hash(image: Image.Image) -> str:
    return hashlib.sha256(struct.pack('>II', *image.size) + image.convert('RGBA').tobytes()).hexdigest()


def jpeg_preview(image: Image.Image, side: int) -> bytes:
    scale = min(1, side / max(image.size))
    size = tuple(max(1, round(dim * scale)) for dim in image.size)
    preview = image.resize(size, Image.Resampling.LANCZOS).convert('RGBA')
    background = Image.new('RGB', preview.size, 'white')
    background.paste(preview, mask=preview.getchannel('A'))
    with io.BytesIO() as buffer:
        background.save(buffer, format='JPEG', quality=78)
        return buffer.getvalue()


def _image_record(stream: BinaryIO, name: str, expected: str | None = None,
                  previews: dict[str, bytes] | None = None) -> dict:
    record = {
        'name': name,
        'sha256': _digest(stream, 'sha256'),
        'sha1': _digest(stream, 'sha1'),
    }
    # Some Pillow plugins invoke external renderers for vector formats.
    formats = (expected,) if expected else SAFE_IMAGE_DECODERS
    with Image.open(stream, formats=formats) as image:
        width, height = image.size
        if width * height > MAX_PIXELS:
            raise ExtractionError('Изображение превышает предел в 30 мегапикселей.')
        record.update(width=width, height=height, format=image.format, frames=getattr(image, 'n_frames', 1))
        # Verify the encoding, then decode frame one for the visual comparison signature.
        image.verify()
    stream.seek(0)
    with Image.open(stream, formats=formats) as image:
        record['phash'] = image_phash(image)
        record['phash_algorithm'] = 'dct64'
        if width * height <= 2_000_000:
            record['pixel_sha256'] = pixel_hash(image)
        if matches := identify_graphics(image, split=True):
            record['logo_references'] = matches
        if previews is not None and len(previews) < MAX_IMAGES and record['sha256'] not in previews:
            previews[record['sha256']] = jpeg_preview(image, 640 if len(previews) < 6 else 360)
        if expected:
            record['metadata'] = {
                label: _short(image.info[field])
                for field, label in [('Title', 'title'), ('Author', 'author'), ('Copyright', 'copyright'), ('Description', 'description')]
                if image.info.get(field)
            }
            try:
                exif = image.getexif()
                for tag, key in {270: 'description', 271: 'camera_make', 272: 'camera_model', 306: 'image_datetime', 315: 'artist', 33432: 'copyright'}.items():
                    if exif.get(tag):
                        record['metadata'][key] = _short(exif[tag])
            except (OSError, ValueError, SyntaxError, TypeError):
                record['metadata_warning'] = 'Часть EXIF не удалось прочитать.'
    return record


def _add_embedded_image(data: bytes, name: str, result: Result, **context) -> None:
    if len(result.images) >= MAX_IMAGES:
        result.warn('Достигнут предел сохранённых изображений.')
        return
    try:
        record = _image_record(io.BytesIO(data), name, previews=result.previews)
    except (OSError, ValueError, SyntaxError, Image.DecompressionBombError):
        record = {'name': name, 'sha256': hashlib.sha256(data).hexdigest(), 'sha1': hashlib.sha1(data).hexdigest()}
        if PurePosixPath(name).suffix.lower() not in {'.svg', '.wmf', '.emf'}:
            result.warn('У части встроенных изображений доступны только хеши.')
    result.add_image(record | context)


def _add_data_image(value: str, name: str, result: Result) -> None:
    header, _, encoded = value.partition(',')
    if header.lower() not in {'data:image/png;base64', 'data:image/jpeg;base64', 'data:image/webp;base64', 'data:image/gif;base64'}:
        return
    if len(encoded) > 2 * MIB:
        result.warn('Встроенное в разметку изображение превышает предел чтения.')
        return
    try:
        content = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        result.warn('Не удалось прочитать изображение из разметки.')
    else:
        _add_embedded_image(content, name, result)


class LocalHTMLParser(HTMLParser):
    def __init__(self, result: Result):
        super().__init__(convert_charrefs=True)
        self.result = result
        self.hidden_tag = None
        self.in_title = False
        self.title = ''
        self.has_markup = False
        self.image_number = 0
        self.text_parts: list[str] = []
        self.text_parts_length = 0

    def handle_starttag(self, tag, attrs):
        self.has_markup = True
        if self.hidden_tag:
            return
        if tag in {'script', 'style', 'template'}:
            self.hidden_tag = tag
            return
        if tag in {'p', 'br', 'li', 'div', 'section', 'tr', 'h1', 'h2', 'h3'}:
            self.flush_text()
        self.in_title = self.in_title or tag == 'title'
        attrs = dict(attrs)
        for key in ('href', 'src', 'poster', 'cite'):
            value = attrs.get(key) or ''
            if tag == 'img' and key == 'src' and value.lower().startswith('data:'):
                self.image_number += 1
                _add_data_image(value, f'Встроенное изображение {self.image_number}', self.result)
            else:
                self.result.add_link(value)
        if tag == 'meta':
            name = (attrs.get('name') or attrs.get('property') or '').lower()
            if name in {'author', 'description', 'copyright', 'rights', 'og:title', 'og:description'} and attrs.get('content'):
                self.result.metadata[name] = _short(attrs['content'])
            if attrs.get('charset'):
                self.result.metadata['charset'] = _short(attrs['charset'])
        if attrs.get('lang') and tag == 'html':
            self.result.metadata['language'] = _short(attrs['lang'])

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.hidden_tag:
            if tag == self.hidden_tag:
                self.hidden_tag = None
            return
        if tag == 'title':
            self.in_title = False
            self.flush_text()
        elif tag in {'p', 'li', 'div', 'section', 'tr', 'h1', 'h2', 'h3'}:
            self.flush_text()

    def handle_data(self, data):
        if not self.hidden_tag:
            if self.in_title:
                self.title = (self.title + data)[:512]
            if self.text_parts_length < MAX_TEXT:
                self.text_parts.append(data[:MAX_TEXT - self.text_parts_length])
                self.text_parts_length += len(data)

    def flush_text(self):
        self.result.add_text(''.join(self.text_parts))
        self.text_parts.clear()
        self.text_parts_length = 0


def _read_html(stream: BinaryIO, result: Result) -> None:
    data = _bounded_read(stream, MAX_HTML)
    encodings = ['utf-8-sig', 'cp1252']
    if data.startswith((b'\xff\xfe', b'\xfe\xff')):
        encodings = ['utf-16']
    else:
        match = re.search(rb'charset\s*=\s*["\']?([\w.-]+)', data[:4096], re.IGNORECASE)
        if match:
            declared = match.group(1).lower()
            if declared in {b'windows-1251', b'cp1251', b'koi8-r', b'iso-8859-5'}:
                encodings.insert(0, declared.decode('ascii'))
    source = ''
    for encoding in encodings:
        try:
            source = data.decode(encoding)
        except UnicodeDecodeError:
            continue
        result.metadata['encoding'] = encoding
        break
    if not source or '\0' in source:
        raise ExtractionError('Не удалось прочитать HTML.')
    parser = LocalHTMLParser(result)
    parser.feed(source)
    parser.close()
    parser.flush_text()
    if not parser.has_markup:
        raise ExtractionError('В файле не найдена HTML-разметка.')
    if parser.title.strip():
        result.metadata['title'] = parser.title.strip()


def _read_svg(stream: BinaryIO, name: str, result: Result) -> None:
    root = _xml(_bounded_read(stream, MAX_HTML))
    if root.tag not in {'svg', '{http://www.w3.org/2000/svg}svg'}:
        raise ExtractionError('XML-файл не содержит корневой элемент SVG.')
    record = {'name': name, 'format': 'SVG', 'sha256': _digest(stream, 'sha256'), 'sha1': _digest(stream, 'sha1')}
    for key in ('width', 'height'):
        value = root.get(key, '')
        match = re.fullmatch(r'(\d{1,6})(?:px)?', value)
        if match:
            record[key] = int(match.group(1))
        if value:
            result.metadata[key] = _short(value)
    if root.get('viewBox'):
        result.metadata['viewBox'] = _short(root.get('viewBox'))
    result.add_image(record)
    image_number = 0
    for element in root.iter():
        tag = _local(element.tag)
        if tag in {'text', 'title', 'desc'}:
            result.add_text(''.join(element.itertext()))
        if tag in {'title', 'desc', 'creator', 'rights', 'description', 'license'}:
            value = _short(' '.join(element.itertext()))
            if value:
                result.metadata.setdefault(tag, value)
        value = element.get('href') or element.get('{http://www.w3.org/1999/xlink}href') or ''
        if tag == 'image' and value.lower().startswith('data:'):
            image_number += 1
            _add_data_image(value, f'{name} · изображение {image_number}', result)
        else:
            result.add_link(value)


EOCD_SIGNATURE = b'PK\x05\x06'
EOCD = struct.Struct('<4s4H2LH')


class ReadBudget:
    def __init__(self):
        self.remaining = MAX_ARCHIVE_READ


class Archive:
    def __init__(self, stream: BinaryIO, budget: ReadBudget):
        self.budget = budget
        self._check_directory(stream)
        self.zip = zipfile.ZipFile(stream)
        try:
            self.members = self._inventory(self.zip.infolist())
        except ExtractionError:
            self.zip.close()
            raise

    @staticmethod
    def _check_directory(stream: BinaryIO) -> None:
        stream.seek(0)
        if stream.read(4) not in {b'PK\x03\x04', EOCD_SIGNATURE}:
            raise ExtractionError('Содержимое не является ZIP-контейнером.')
        length = stream.seek(0, io.SEEK_END)
        offset = max(0, length - EOCD.size - 65535)
        stream.seek(offset)
        tail = stream.read()
        position = tail.rfind(EOCD_SIGNATURE)
        if position < 0 or position + EOCD.size > len(tail):
            raise ExtractionError('Не найден каталог ZIP-контейнера.')
        _, disk, directory_disk, disk_count, entries, directory_size, directory_start, comment = EOCD.unpack_from(tail, position)
        if position + EOCD.size + comment != len(tail):
            raise ExtractionError('Некорректный завершающий блок ZIP.')
        if disk or directory_disk or disk_count != entries or entries > MAX_ARCHIVE_ENTRIES or directory_size > 6 * MIB:
            raise ExtractionError('ZIP содержит слишком большой каталог или состоит из нескольких томов.')
        # Check record counts before ZipFile builds the in-memory list. The end record's count is advisory.
        if directory_start + directory_size != offset + position:
            raise ExtractionError('Некорректная позиция каталога ZIP.')
        stream.seek(directory_start)
        cursor = 0
        count = 0
        while cursor < directory_size:
            header = stream.read(46)
            if len(header) != 46 or not header.startswith(b'PK\x01\x02'):
                raise ExtractionError('Повреждён каталог ZIP.')
            name_length, extra_length, comment_length = struct.unpack_from('<3H', header, 28)
            skip = name_length + extra_length + comment_length
            cursor += 46 + skip
            count += 1
            if count > entries or cursor > directory_size:
                raise ExtractionError('Каталог ZIP не соответствует заявленному размеру или числу записей.')
            stream.seek(skip, io.SEEK_CUR)
        if count != entries:
            raise ExtractionError('Число записей ZIP не соответствует каталогу.')

    @staticmethod
    def _inventory(items: list[zipfile.ZipInfo]) -> dict[str, zipfile.ZipInfo]:
        members = {}
        total = 0
        for info in items:
            name = info.orig_filename
            path = PurePosixPath(name)
            if (
                not name or len(name) > 1024 or any(ord(char) < 32 for char in name)
                or path.is_absolute() or '\\' in name or ':' in name or '..' in path.parts
                or not path.parts or name in members
            ):
                raise ExtractionError('В ZIP есть запись с небезопасным или повторяющимся именем.')
            file_type = stat.S_IFMT(info.external_attr >> 16) if info.create_system == 3 else 0
            if file_type not in {0, stat.S_IFREG, stat.S_IFDIR}:
                raise ExtractionError('В ZIP обнаружена ссылка или специальный файл.')
            if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
                raise ExtractionError('ZIP использует неподдерживаемый метод сжатия.')
            if info.flag_bits & 1:
                raise ExtractionError('ZIP содержит зашифрованные записи.')
            if info.file_size > 2 * MIB and info.file_size > max(1, info.compress_size) * 300:
                raise ExtractionError('В ZIP превышен предел степени сжатия.')
            total += info.file_size
            if info.file_size > MAX_ARCHIVE_OUTPUT or total > MAX_ARCHIVE_OUTPUT:
                raise ExtractionError('ZIP превышает предел суммарного размера после распаковки (150 МБ).')
            members[name] = info
        return members

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.zip.close()

    def read(self, name: str, limit: int = MAX_ENTRY) -> bytes:
        info = self.members[name]
        if info.file_size > min(limit, self.budget.remaining):
            raise ExtractionError('Один из элементов ZIP превышает оставшийся предел чтения.')
        self.budget.remaining -= info.file_size
        with self.zip.open(info) as stream:
            data = _bounded_read(stream, info.file_size)
        if len(data) != info.file_size:
            raise ExtractionError('Размер элемента ZIP не совпадает с каталогом.')
        return data

    def xml(self, name: str):
        return _xml(self.read(name, MAX_XML))


def _relationships(archive: Archive, name: str):
    if name in archive.members:
        return list(archive.xml(name))
    return []


def _slide_order(archive: Archive, main_part, slides: list[str]) -> list[str]:
    slide_set = set(slides)
    targets = {
        relation.get('Id'): 'ppt/' + relation.get('Target', '')
        for relation in _relationships(archive, 'ppt/_rels/presentation.xml.rels')
        if relation.get('Type', '').endswith('/slide')
    }
    ordered = []
    for element in main_part.iter():
        if _local(element.tag) == 'sldId':
            rel_id = next((value for attr, value in element.attrib.items() if attr.endswith('}id')), None)
            name = targets.get(rel_id)
            if name in slide_set and name not in ordered:
                ordered.append(name)
    return ordered + [name for name in slides if name not in ordered]


def _slide_region(picture, slide_size: tuple[int, int]) -> str:
    transform = next((node for node in picture.iter() if _local(node.tag) == 'xfrm'), None)
    if transform is None or min(slide_size) <= 0:
        return ''
    parts = {_local(node.tag): node.attrib for node in transform}
    try:
        x = (int(parts['off']['x']) + int(parts['ext']['cx']) / 2) / slide_size[0]
        y = (int(parts['off']['y']) + int(parts['ext']['cy']) / 2) / slide_size[1]
    except (KeyError, ValueError, OverflowError):
        return ''
    if not (0 <= x <= 1 and 0 <= y <= 1):
        return ''
    horizontal = 'слева' if x < 1 / 3 else 'справа' if x > 2 / 3 else 'по центру'
    vertical = 'сверху' if y < 1 / 3 else 'снизу' if y > 2 / 3 else 'посередине'
    return f'{horizontal}, {vertical}'


def _slide_images(archive: Archive, name: str, slide, number: int, context: str,
                  slide_size: tuple[int, int]) -> list[tuple[str, dict]]:
    part = PurePosixPath(name)
    rels = _relationships(archive, str(part.parent / '_rels' / (part.name + '.rels')))
    targets = {}
    for relation in rels:
        if relation.get('Type', '').endswith('/image') and relation.get('TargetMode', '').lower() != 'external':
            raw = relation.get('Target', '')
            target = posixpath.normpath(raw.lstrip('/') if raw.startswith('/') else str(part.parent / raw))
            if target.startswith('ppt/media/') and target in archive.members:
                targets[relation.get('Id')] = target
    uses = []
    shape_tree = next((node for node in slide.iter() if _local(node.tag) == 'spTree'), None)
    direct_pictures = set(shape_tree) if shape_tree is not None else set()
    for element, picture in enumerate((node for node in slide.iter() if _local(node.tag) == 'pic'), start=1):
        properties = next((node for node in picture.iter() if _local(node.tag) == 'cNvPr'), None)
        description = _short(properties.get('descr') or properties.get('title') or '') if properties is not None else ''
        for node in picture.iter():
            if _local(node.tag) != 'blip':
                continue
            embed = next((value for attr, value in node.attrib.items() if _local(attr) == 'embed'), None)
            if embed in targets:
                uses.append((targets[embed], {'location': f'Слайд {number}', 'element': element,
                                              'context': context, 'description': description,
                                              'region': _slide_region(picture, slide_size) if picture in direct_pictures else ''}))
    return uses


def _read_office(stream: BinaryIO, result: Result, budget: ReadBudget) -> None:
    if result.kind == 'DOCX':
        prefix, main, root_tag = 'word', 'word/document.xml', 'document'
        expected_type = 'application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml'
        part_pattern = re.compile(r'word/(?:header\d+|footer\d+|footnotes|endnotes)\.xml')
    else:
        prefix, main, root_tag = 'ppt', 'ppt/presentation.xml', 'presentation'
        expected_type = 'application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml'
        part_pattern = re.compile(r'ppt/slides/slide(\d+)\.xml')
    with Archive(stream, budget) as archive:
        if '[Content_Types].xml' not in archive.members or main not in archive.members:
            raise ExtractionError(f'В контейнере отсутствуют обязательные части {result.kind}.')
        content_types = archive.xml('[Content_Types].xml')
        if not any(part.get('PartName') == f'/{main}' and part.get('ContentType') == expected_type for part in content_types):
            raise ExtractionError(f'Тип содержимого контейнера не соответствует {result.kind}.')
        main_part = archive.xml(main)
        if _local(main_part.tag) != root_tag:
            raise ExtractionError(f'Повреждена основная часть {result.kind}.')
        parts = [name for name in archive.members if part_pattern.fullmatch(name)]
        slide_size = (0, 0)
        if result.kind == 'DOCX':
            parts.insert(0, main)
        else:
            parts.sort(key=lambda name: int(part_pattern.fullmatch(name).group(1)))
            parts = _slide_order(archive, main_part, parts)
            result.metadata['slides'] = len(parts)
            size = next((node.attrib for node in main_part if _local(node.tag) == 'sldSz'), {})
            try:
                slide_size = (int(size['cx']), int(size['cy']))
            except (KeyError, ValueError):
                pass
        placements = {}
        for number, name in enumerate(parts, start=1):
            try:
                part = main_part if name == main else archive.xml(name)
                text_start = len(result.text)
                for element in part.iter():
                    if _local(element.tag) == 'p':
                        result.add_text(''.join(node.text or '' for node in element.iter() if _local(node.tag) in {'t', 'instrText'}))
                if result.kind == 'PPTX':
                    context = ' '.join(result.text[text_start:])[:360]
                    for target, placement in _slide_images(archive, name, part, number, context, slide_size):
                        locations = placements.setdefault(target, [])
                        if len(locations) < MAX_PDF_PAGES and placement not in locations:
                            locations.append(placement)
            except (ExtractionError, ParseError, DefusedXmlException):
                result.warn(f'Не удалось прочитать часть документа «{name}».')
        for name in archive.members:
            try:
                if name.startswith(f'{prefix}/') and name.endswith('.rels'):
                    for relation in _relationships(archive, name):
                        if relation.get('TargetMode', '').lower() == 'external':
                            result.add_link(relation.get('Target', ''))
                elif name.startswith(f'{prefix}/media/') and PurePosixPath(name).suffix.lower() in MEDIA_EXTENSIONS:
                    if len(result.images) >= MAX_IMAGES:
                        result.warn('Достигнут предел сохранённых изображений.')
                    else:
                        _add_embedded_image(archive.read(name), PurePosixPath(name).name, result,
                                            location=name, **({'placements': placements[name]} if name in placements else {}))
            except (ExtractionError, ParseError, DefusedXmlException, zipfile.BadZipFile, RuntimeError, NotImplementedError):
                result.warn(f'Не удалось прочитать часть документа «{name}».')
        for name in ('docProps/core.xml', 'docProps/app.xml'):
            if name not in archive.members:
                continue
            try:
                for element in archive.xml(name):
                    field = _local(element.tag)
                    if field in {'title', 'subject', 'creator', 'keywords', 'description', 'lastModifiedBy', 'created', 'modified', 'Application', 'Company', 'Pages'}:
                        result.metadata[field] = _short(element.text or '')
            except (ExtractionError, ParseError, DefusedXmlException, zipfile.BadZipFile):
                result.warn(f'Не удалось прочитать метаданные «{name}».')


PDF_ERRORS = (PyPdfError, ValueError, KeyError, TypeError, OverflowError, RecursionError, OSError, AssertionError, struct.error)
PDF_LIMITS = {
    'jbig2dec_binary': None,
    'maximum_declared_stream_length': 32 * MIB,
    'array_based_stream_maximum_output_length': 32 * MIB,
    'brotli_maximum_output_length': 32 * MIB,
    'lzw_maximum_output_length': 32 * MIB,
    'run_length_maximum_output_length': 32 * MIB,
    'zlib_maximum_output_length': 32 * MIB,
    'stream_decoding_work_maximum_length': 64 * MIB,
    'image_maximum_buffer_size': 48 * MIB,
}


def _read_pdf(stream: BinaryIO, result: Result) -> None:
    if not stream.read(1024).lstrip().startswith(b'%PDF-'):
        raise ExtractionError('Содержимое файла не соответствует формату PDF.')
    stream.seek(0)
    reader = PdfReader(stream)
    if reader.is_encrypted and not reader.decrypt(''):
        raise ExtractionError('Для извлечения содержимого PDF требуется пароль.')
    metadata = reader.metadata or {}
    for field in ('Title', 'Author', 'Subject', 'Keywords', 'Creator', 'Producer', 'CreationDate', 'ModDate'):
        if metadata.get('/' + field):
            result.metadata[field] = _short(metadata.get('/' + field))
    page_count = len(reader.pages)
    result.metadata['pages'] = page_count
    if page_count == 0:
        result.warn('В PDF нет страниц.')
    if page_count > MAX_PDF_PAGES:
        result.warn(f'Обработаны первые {MAX_PDF_PAGES} страниц PDF.')
    for page_number, page in enumerate(reader.pages[:MAX_PDF_PAGES], start=1):
        context = ''
        try:
            text = page.extract_text() or ''
            result.add_text(text)
            context = WHITESPACE.sub(' ', text[:1200]).strip()[:360]
        except PDF_ERRORS:
            result.warn(f'Не удалось извлечь текст со страницы {page_number}.')
        try:
            annotations = page.get('/Annots') or []
            for reference in annotations.get_object()[:300] if annotations else []:
                annotation = reference.get_object()
                action = annotation.get('/A')
                if action and action.get_object().get('/S') == '/URI':
                    result.add_link(str(action.get_object().get('/URI', '')))
        except PDF_ERRORS + (AttributeError,):
            result.warn(f'Не удалось прочитать часть ссылок на странице {page_number}.')
        if len(result.images) >= MAX_IMAGES:
            continue
        try:
            keys = page.images.keys()
        except PDF_ERRORS:
            result.warn(f'Не удалось перечислить изображения на странице {page_number}.')
            continue
        remaining = MAX_IMAGES - len(result.images)
        if len(keys) > remaining:
            result.warn('Достигнут предел сохранённых изображений.')
        for key in keys[:remaining]:
            try:
                image = page.images[key]
                if len(image.data) <= MAX_ENTRY:
                    _add_embedded_image(image.data, image.name, result, location=f'Страница {page_number}',
                                        context=context, hash_origin='pdf_export')
                else:
                    result.warn(f'Размер изображения на странице {page_number} превышает предел чтения.')
            except PDF_ERRORS + (AttributeError, Image.DecompressionBombError):
                result.warn(f'Не удалось прочитать часть изображений на странице {page_number}.')
    if page_count and not result.text:
        result.warn('Текстовый слой PDF не найден. Распознавание текста на изображениях не выполнялось.')


def _read_zip(stream: BinaryIO, result: Result, budget: ReadBudget) -> None:
    entries = []
    result.entries = entries
    with Archive(stream, budget) as archive:
        files = [info for info in archive.members.values() if not info.is_dir()]
        result.metadata['file_count'] = len(files)
        result.metadata['expanded_bytes'] = sum(info.file_size for info in files)
        if not files:
            result.warn('В ZIP нет файлов.')
        if len(files) > 250:
            result.warn('Обработаны первые 250 файлов ZIP.')
        for info in files[:250]:
            entry = {'name': info.filename, 'size': info.file_size, 'status': 'listed'}
            entries.append(entry)
            child_kind = EXTENSIONS.get(PurePosixPath(info.filename).suffix.lower())
            if not child_kind or child_kind == 'ZIP':
                entry['reason'] = 'nested_zip' if child_kind == 'ZIP' else 'unsupported'
                continue
            entry['format'] = child_kind
            try:
                data = archive.read(info.filename)
                entry['sha256'] = hashlib.sha256(data).hexdigest()
                child = _extract(io.BytesIO(data), info.filename, child_kind, budget, result.previews)
            except (ExtractionError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as error:
                entry['status'] = 'skipped'
                entry['message'] = str(error) if isinstance(error, ExtractionError) else 'Не удалось прочитать элемент архива.'
                result.warn(f'Элемент «{info.filename}» не удалось обработать.')
                continue
            entry['status'] = 'extracted'
            if child.metadata:
                entry['metadata'] = child.metadata
            for fragment in child.text:
                result.add_text(fragment)
            for link in child.links:
                result.add_link(link)
            for image in child.images:
                location = image.get('location')
                placements = [{**item, 'location': f"{info.filename} · {item['location']}"} for item in image.get('placements', [])]
                result.add_image(image | {'location': f'{info.filename} · {location}' if location else info.filename,
                                          **({'placements': placements} if placements else {})})
            for warning in child.warnings:
                result.warn(f'{info.filename}: {warning}')


def _extract(stream: BinaryIO, name: str, kind: str, budget: ReadBudget, previews: dict[str, bytes] | None = None) -> Result:
    if not stream.read(1):
        raise ExtractionError('Невозможно извлечь содержимое пустого файла.')
    stream.seek(0)
    result = Result(kind, previews)
    try:
        if kind in RASTER:
            record = _image_record(stream, name, kind, previews)
            result.metadata.update(record.pop('metadata'))
            if record.pop('metadata_warning', None):
                result.warn('Часть EXIF не удалось прочитать.')
            result.metadata.update({key: record[key] for key in ('width', 'height', 'frames')})
            result.add_image(record)
        elif kind == 'PDF':
            with apply_configuration(**PDF_LIMITS):
                _read_pdf(stream, result)
        elif kind == 'HTML':
            _read_html(stream, result)
        elif kind == 'SVG':
            _read_svg(stream, name, result)
        elif kind == 'ZIP':
            _read_zip(stream, result, budget)
        else:
            _read_office(stream, result, budget)
    except ExtractionError:
        raise
    except (ParseError, DefusedXmlException):
        raise ExtractionError(f'Повреждённая или небезопасная XML-разметка в {kind}.') from None
    except Image.DecompressionBombError:
        raise ExtractionError('Изображение превышает допустимый размер.') from None
    except (zipfile.BadZipFile, RuntimeError, SyntaxError, NotImplementedError) + PDF_ERRORS:
        raise ExtractionError(f'Не удалось прочитать {kind}. Файл повреждён или использует неподдерживаемое кодирование.') from None
    return result


def extract(path: Path, filename: str, *, previews: dict[str, bytes] | None = None) -> dict:
    """Inspect a file stored locally under a server-controlled name."""
    kind = EXTENSIONS.get(Path(filename).suffix.lower())
    if not kind:
        raise ExtractionError('Формат файла не поддерживается.')
    with path.open('rb') as stream:
        result = _extract(stream, filename, kind, ReadBudget(), previews)
        serialized = result.serialize()
        serialized['sha256'] = _digest(stream, 'sha256')
        serialized['size'] = path.stat().st_size
        return serialized
