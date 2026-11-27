import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from zipfile import ZIP_DEFLATED, ZipFile

from PIL import Image
from reportlab.lib.utils import ImageReader
from reportlab.pdfgen.canvas import Canvas

from app import extraction
from app.extraction import ExtractionError, extract


class ExtractionTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.directory = Path(temp.name)
        self.path = self.directory / 'file'

    def image_bytes(self, image_format='PNG'):
        content = io.BytesIO()
        with Image.new('RGB', (16, 12), 'orange') as image:
            image.save(content, format=image_format)
        return content.getvalue()

    def archive(self, members):
        with ZipFile(self.path, 'w', compression=ZIP_DEFLATED) as archive:
            for name, data in members.items():
                archive.writestr(name, data)

    def test_html_cyrillic_links_and_metadata(self):
        self.path.write_bytes('''<!doctype html><html lang="ru"><meta charset="windows-1251">
        <title>Пробная страница</title><meta name="author" content="Автор"><body>
        <p>Пробный <em>текст</em> с <a href="https://example.org/source">источником</a>.</p>
        <img src="graphic.png" alt="Графика"><script>never part of readable text</script></body></html>'''.encode('cp1251'))
        result = extract(self.path, 'страница.HTML')
        self.assertIn('Пробный текст с источником.', result['text'])
        self.assertNotIn('never part', result['text'])
        self.assertEqual(result['links'], ['https://example.org/source', 'graphic.png'])
        self.assertEqual(result['metadata']['author'], 'Автор')
        self.assertEqual(result['metadata']['title'], 'Пробная страница')

    def test_raster_hashes_and_dimensions(self):
        for image_format, extension in [('PNG', 'png'), ('JPEG', 'jpg'), ('WEBP', 'webp')]:
            with self.subTest(image_format=image_format):
                content = self.image_bytes(image_format)
                self.path.write_bytes(content)
                result = extract(self.path, f'image.{extension}')
                image, = result['images']
                self.assertEqual(result['format'], image_format)
                self.assertEqual(image['sha256'], hashlib.sha256(content).hexdigest())
                self.assertEqual(image['sha1'], hashlib.sha1(content).hexdigest())
                self.assertEqual(result['sha256'], image['sha256'])
                self.assertEqual((image['width'], image['height']), (16, 12))
                self.assertRegex(image['phash'], r'^[\da-f]{16}$')

    def test_svg_static_inventory(self):
        self.path.write_text('''<svg xmlns="http://www.w3.org/2000/svg" width="32" height="20">
        <title>Образец</title><a href="https://example.org/graphic"><text>Метка</text></a>
        <script>not visible</script></svg>''', encoding='utf-8')
        result = extract(self.path, 'graphic.svg')
        self.assertIn('Метка', result['text'])
        self.assertNotIn('not visible', result['text'])
        self.assertEqual(result['images'][0]['width'], 32)
        self.assertEqual(result['links'], ['https://example.org/graphic'])
        self.assertEqual(result['metadata']['title'], 'Образец')

    def test_pdf_text_link_and_embedded_image(self):
        pdf = Canvas(str(self.path))
        pdf.setAuthor('TraceMuse test authors')
        pdf.drawString(20, 40, 'A local extraction example')
        pdf.linkURL('https://example.org/report', (20, 30, 200, 45))
        pdf.drawImage(ImageReader(io.BytesIO(self.image_bytes())), 20, 80)
        pdf.save()
        result = extract(self.path, 'report.pdf')
        self.assertIn('A local extraction example', result['text'])
        self.assertEqual(result['links'], ['https://example.org/report'])
        self.assertEqual(result['metadata']['Author'], 'TraceMuse test authors')
        self.assertEqual(result['metadata']['pages'], 1)
        self.assertEqual(result['images'][0]['location'], 'Страница 1')
        self.assertEqual(result['images'][0]['hash_origin'], 'pdf_export')

    def test_docx_inventory(self):
        content = self.image_bytes()
        self.archive({
            '[Content_Types].xml': '<Types><Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/></Types>',
            'word/document.xml': '<w:document xmlns:w="urn:word"><w:body><w:p>'
                                 '<w:r><w:t>Первый </w:t></w:r><w:r><w:t>абзац.</w:t></w:r>'
                                 '</w:p></w:body></w:document>',
            'word/_rels/document.xml.rels': '<Relationships><Relationship TargetMode="External" Target="https://example.org/word"/></Relationships>',
            'word/media/image1.png': content,
            'docProps/core.xml': '<coreProperties><title>Документ</title></coreProperties>',
        })
        result = extract(self.path, 'example.docx')
        self.assertEqual(result['text'], 'Первый абзац.')
        self.assertEqual(result['metadata']['title'], 'Документ')
        self.assertEqual(result['links'], ['https://example.org/word'])
        self.assertEqual(result['images'][0]['location'], 'word/media/image1.png')
        self.assertEqual(result['images'][0]['sha256'], hashlib.sha256(content).hexdigest())

    def test_pptx_follows_presentation_order(self):
        self.archive({
            '[Content_Types].xml': '<Types><Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/></Types>',
            'ppt/presentation.xml': '<presentation xmlns:r="urn:rels"><sldId r:id="slide2"/><sldId r:id="slide1"/></presentation>',
            'ppt/_rels/presentation.xml.rels': '<Relationships>'
                '<Relationship Id="slide1" Type="urn:rels/slide" Target="slides/slide1.xml"/>'
                '<Relationship Id="slide2" Type="urn:rels/slide" Target="slides/slide2.xml"/></Relationships>',
            'ppt/slides/slide1.xml': '<sld><p><t>Следующий слайд</t></p></sld>',
            'ppt/slides/slide2.xml': '<sld><p><t>Начальный слайд</t></p></sld>',
            'ppt/media/image1.png': self.image_bytes(),
        })
        result = extract(self.path, 'slides.pptx')
        self.assertEqual(result['text'], 'Начальный слайд\nСледующий слайд')
        self.assertEqual(result['metadata']['slides'], 2)
        self.assertEqual(result['images'][0]['location'], 'ppt/media/image1.png')

    def test_zip_keeps_per_member_status_and_never_creates_members(self):
        self.archive({
            'site/page.html': '<p>Архивная страница</p>', 'site/image.png': self.image_bytes(),
            'site/broken.svg': '<svg>', 'readme.txt': 'another entry', 'another.zip': b'listed only',
        })
        result = extract(self.path, 'assets.zip')
        self.assertIn('Архивная страница', result['text'])
        self.assertEqual(result['metadata']['file_count'], 5)
        self.assertEqual([entry['status'] for entry in result['entries']], ['extracted', 'extracted', 'skipped', 'listed', 'listed'])
        self.assertEqual(result['images'][0]['location'], 'site/image.png')
        self.assertEqual(list(self.directory.iterdir()), [self.path])

    def test_configured_archive_budget(self):
        self.archive({'page.html': '<p>Локальный документ</p>'})
        with patch.object(extraction, 'MAX_ARCHIVE_READ', 5):
            result = extract(self.path, 'assets.zip')
        self.assertEqual(result['entries'][0]['status'], 'skipped')
        self.assertEqual(result['text'], '')

    def test_rejects_invalid_inputs(self):
        for filename, data in [
            ('unsupported.txt', b'text'), ('empty.pdf', b''),
            ('fake.png', b'plain text'), ('fake.jpg', self.image_bytes()),
            ('broken.pdf', b'%PDF-1.4\n'), ('broken.docx', b'not an Office file'),
            ('broken.svg', b'<svg><path></svg>'),
        ]:
            with self.subTest(filename=filename):
                self.path.write_bytes(data)
                with self.assertRaises(ExtractionError):
                    extract(self.path, filename)
