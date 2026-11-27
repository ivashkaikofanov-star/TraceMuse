import io
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import SecretStr
from pypdf import PdfReader

from app import deepseek
from app.config import Settings
from app.report import render_pdf


class DeepSeekTests(unittest.TestCase):
    def mock_client(self, handler):
        return httpx.Client(
            base_url=deepseek.API_URL, transport=httpx.MockTransport(handler),
            follow_redirects=False,
        )

    def test_key_is_local_and_removable(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "secrets" / "deepseek"
            with patch.object(deepseek, "_key_path", return_value=path), patch.object(
                deepseek.settings, "deepseek_api_key", SecretStr("")
            ):
                self.assertFalse(deepseek.is_configured())
                deepseek.save_key("  sk-test-key  ")
                self.assertTrue(deepseek.is_configured())
                self.assertEqual(deepseek._key(), "sk-test-key")
                if os.name == "nt":
                    self.assertNotIn(b"sk-test-key", path.read_bytes())
                else:
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                deepseek.save_key("")
                self.assertFalse(path.exists())
                self.assertFalse(deepseek.is_configured())

    def test_disabled_analysis_makes_no_request(self):
        with patch.object(deepseek, "_client") as client:
            result = deepseek.analyze({}, {}, {}, False)
            self.assertEqual(result["status"], "disabled")
            client.assert_not_called()

    def test_missing_key_makes_no_request(self):
        with patch.object(deepseek, "_key", return_value=""), patch.object(deepseek, "_client") as client:
            self.assertEqual(deepseek.analyze({}, {}, {}, True)["status"], "not_configured")
            self.assertEqual(deepseek.test_connection()["status"], "not_configured")
            client.assert_not_called()

    def test_flash_catalog_accepts_default_and_legacy_settings(self):
        def provider(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/models")
            self.assertEqual(request.headers["authorization"], "Bearer sk-test-key")
            return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}, {"id": "deepseek-v4-pro"}]})

        for model in (None, "deepseek-v4-flash", "deepseek-v4-flash-vision-exp", "deepseek-v4-pro"):
            with self.subTest(model=model), patch.dict(os.environ, {}, clear=True):
                if model:
                    os.environ["DEEPSEEK_MODEL"] = model
                with patch.object(deepseek, "settings", Settings(_env_file=None)), patch.object(
                    deepseek, "_key", return_value="sk-test-key"
                ), patch.object(deepseek, "_client", return_value=self.mock_client(provider)):
                    expected = "deepseek-v4-pro" if model == "deepseek-v4-pro" else "deepseek-flash"
                    self.assertEqual(deepseek.settings.deepseek_model, expected)
                    self.assertEqual(deepseek.test_connection()["status"], "ok")

    def test_provider_authentication_error_does_not_echo_the_body(self):
        def provider(_request):
            return httpx.Response(401, json={"error": {"message": "do not disclose sk-test-key"}})

        with patch.object(deepseek, "_key", return_value="sk-test-key"), patch.object(
            deepseek, "_client", return_value=self.mock_client(provider)
        ):
            result = deepseek.test_connection()
            self.assertEqual(result["status"], "error")
            self.assertNotIn("sk-test-key", str(result))

    def test_analysis_only_preserves_observations_with_literal_evidence(self):
        def provider(request):
            self.assertEqual(str(request.url), "https://api.deepseek.com/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer sk-test-key")
            body = json.loads(request.content)
            self.assertEqual(body["model"], "deepseek-flash")
            self.assertEqual(body["response_format"], {"type": "json_object"})
            self.assertNotIn("unshared-filename.docx", str(body))
            self.assertNotIn("unshared-exif", str(body))
            record = json.loads(body["messages"][1]["content"])[0]
            self.assertEqual(record["value"], "Автор на титульном листе: Иван")
            answer = {
                "observations": [
                    {
                        "evidence_id": record["id"], "quote": "на титульном листе",
                        "finding": "В тексте упоминается автор.",
                        "action": "Уточните правообладателя у автора.",
                    },
                    {
                        "evidence_id": record["id"], "quote": "Несуществующая цитата",
                        "finding": "Лицензия найдена.", "action": "Используйте.",
                    },
                    {
                        "evidence_id": record["id"], "quote": "на титульном листе",
                        "finding": "Пример: https://example.org", "action": "Откройте ссылку.",
                    },
                ],
            }
            return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(answer)}}]})

        with patch.object(deepseek, "settings", Settings(_env_file=None, deepseek_model="deepseek-v4-flash")), patch.object(
            deepseek, "_key", return_value="sk-test-key"
        ), patch.object(
            deepseek, "_client", return_value=self.mock_client(provider)
        ):
            result = deepseek.analyze({
                "filename": "unshared-filename.docx",
                "metadata": {"camera": "unshared-exif"},
                "text": "Автор на титульном листе: Иван",
            }, {}, {}, True)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(len(result["observations"]), 1)
        self.assertIn("на титульном листе", result["observations"][0])


class ReportTests(unittest.TestCase):
    def test_pdf_paginates_and_keeps_cyrillic_evidence_and_urls(self):
        audit = {
            "id": 42, "filename": "Курсовой проект.pdf", "status": "completed",
            "created_at": "2026-11-27T19:20:00Z", "sha256": "a" * 64, "size_bytes": 490,
            "result": {
                "extraction": {
                    "format": "PDF", "text": ("Доказательство <описание>.\n" * 120) + "Конец текста.",
                    "metadata": {"title": "Курсовой проект"}, "links": ["https://example.org/"],
                    "images": [{"name": "Обложка", "sha256": "b" * 64}], "warnings": [],
                },
                "sources": {"status": "ok", "items": [{
                    "title": "Страница ресурса", "url": "https://example.org/asset?a=1&b=2",
                    "confidence": "Умеренная", "license": {"name": "CC BY 4.0", "commercial_use": True},
                    "creator": "Автор ресурса",
                }]},
                "legal": {"status": "manual_review", "items": [], "warnings": ["Проверьте реестр."]},
                "ai": {"status": "disabled", "warnings": []},
                "assessment": {
                    "level": "insufficient_data", "reason": "Источник требует подтверждения.",
                    "recommendations": ["Уточните условия."], "limitations": ["Проверка по открытым данным."],
                },
            },
        }
        document = PdfReader(io.BytesIO(render_pdf(audit)))
        self.assertGreater(len(document.pages), 1)
        text = "\n".join(page.extract_text() for page in document.pages)
        for phrase in (
            "Курсовой проект.pdf", "Источники и лицензии", "CC BY 4.0", "Автор ресурса",
            "Умеренная", "Недостаточно данных", "Проверьте реестр.", "Доказательство <описание>.",
            "Конец текста.",
        ):
            self.assertIn(phrase, text)
        urls = {
            annotation.get_object().get("/A", {}).get("/URI")
            for page in document.pages for annotation in page.get("/Annots", [])
        }
        self.assertIn("https://example.org/asset?a=1&b=2", urls)


if __name__ == "__main__":
    unittest.main()
