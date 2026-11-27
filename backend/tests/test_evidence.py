from datetime import UTC, datetime
from io import BytesIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx
from PIL import Image, ImageDraw

from app.evidence import LEGAL_REGISTRIES, check_legal, search_sources
from app.extraction import image_phash


SHA256 = "a" * 64
SHA1 = "b" * 40
FILE_TITLE = "File:Example; Cat.jpg"
COMMONS_URL = "https://commons.wikimedia.org/wiki/File:Example%3B_Cat.jpg"
PUBLIC_THUMBNAIL = "https://thumb.wikimedia.org/wikipedia/commons/thumb/c/c0/Example.png/250px-Example.png"
META = {
    "LicenseShortName": {"value": "CC BY-NC-ND 4.0"},
    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-nc-nd/4.0/"},
    "Artist": {"value": "<a>Author</a>"},
    "DateTimeOriginal": {"value": "2025-02-01"},
}


class SourcesTests(unittest.TestCase):
    def _commons(self, callback):
        client = httpx.Client(transport=httpx.MockTransport(callback), follow_redirects=False)
        return patch("app.evidence.httpx.Client", return_value=client)

    def test_local_embedded_link_never_opens_network(self):
        extraction = {"links": ["https://example.org/file", "javascript:doSomething()", "https://example.org/file"]}
        with patch("app.evidence.httpx.Client", side_effect=AssertionError("network accessed")):
            result = search_sources(extraction, SHA256, False)
        self.assertEqual(result["status"], "local_only")
        self.assertEqual([record["url"] for record in result["items"]], ["https://example.org/file"])
        self.assertNotIn("license", result["items"][0])
        self.assertIn("не проверены", result["items"][0]["confidence"])

    def test_exact_sha1_and_catalog_license_outrank_link_and_search(self):
        requests = []

        def response(request):
            requests.append(request)
            self.assertEqual(request.url.host, "commons.wikimedia.org")
            parameters = request.url.params
            if parameters.get("list") == "allimages":
                self.assertEqual(parameters["aisha1"], SHA1)
                return httpx.Response(200, json={"query": {"allimages": [
                    {"canonicaltitle": FILE_TITLE, "sha1": SHA1, "extmetadata": META}
                ]}})
            if "gsrsearch" in parameters and parameters["gsrsearch"] == SHA256:
                return httpx.Response(200, json={"batchcomplete": ""})
            if "titles" in parameters:
                self.assertEqual(parameters["titles"], FILE_TITLE)
            return httpx.Response(200, json={"query": {"pages": {"4": {
                "title": FILE_TITLE, "pageid": 4, "imageinfo": [{"extmetadata": {}}]
            }}}})

        extraction = {
            "text": "Cat",
            "links": [COMMONS_URL, "https://other.example/file"],
            "images": [{"name": "Cat.jpg", "sha1": SHA1}],
        }
        with self._commons(response):
            result = search_sources(extraction, SHA256, True)

        self.assertEqual(result["status"], "complete")
        self.assertEqual(len(requests), 4)
        exact = result["items"][0]
        self.assertEqual(exact["method"], "identical_image")
        self.assertEqual(exact["url"], COMMONS_URL)
        self.assertEqual(exact["creator"], "Author")
        self.assertEqual(exact["catalog_creation_date"], "2025-02-01")
        license = exact["license"]
        self.assertEqual(license["status"], "catalog_declaration")
        self.assertIn("отдельное разрешение", license["commercial_use"])
        self.assertIn("отдельного разрешения", license["modification"])
        self.assertIn("Требуется", license["attribution"])
        self.assertFalse(license["commercial_allowed"])
        self.assertFalse(license["derivatives_distribution_allowed"])
        self.assertTrue(license["attribution_required"])
        self.assertFalse(license["share_alike"])
        self.assertEqual([item["method"] for item in result["items"]], ["identical_image", "embedded_url"])

    def test_visual_candidate_downloads_only_wikimedia_thumbnail(self):
        with Image.new("RGB", (80, 80), "white") as raster:
            ImageDraw.Draw(raster).rectangle((9, 7, 38, 53), fill="black")
            phash = image_phash(raster)
            buffer = BytesIO()
            raster.save(buffer, format="PNG")
            data = buffer.getvalue()
        outbound = []

        def response(request):
            outbound.append(str(request.url))
            if request.url.host == "thumb.wikimedia.org":
                self.assertEqual(str(request.url), PUBLIC_THUMBNAIL)
                return httpx.Response(200, content=data)
            self.assertEqual(request.url.host, "commons.wikimedia.org")
            if "pageids" in request.url.params:
                return httpx.Response(200, json={"query": {"pages": {
                    "4": {"title": FILE_TITLE, "pageid": 4, "imageinfo": [{"thumburl": PUBLIC_THUMBNAIL}]},
                    "5": {"title": "File:Other.jpg", "pageid": 5,
                          "imageinfo": [{"thumburl": "https://other.example/thumb.jpg"}]},
                }}})
            self.assertEqual(request.url.params["gsrsearch"], "resized Cat")
            return httpx.Response(200, json={"query": {"pages": {
                "4": {"title": FILE_TITLE, "pageid": 4, "imageinfo": [{"extmetadata": META}]},
                "5": {"title": "File:Other.jpg", "pageid": 5},
            }}})

        extraction = {"images": [{"name": "resized_Cat.jpg", "phash": phash}]}
        with self._commons(response):
            result = search_sources(extraction, "", True)
        self.assertEqual(len(outbound), 3)
        self.assertNotIn(phash, " ".join(outbound))
        self.assertEqual([item["method"] for item in result["items"]], ["visual_similarity", "catalog_candidate"])
        self.assertEqual(result["items"][1]["license"]["status"], "unestablished")

    def test_catalog_unavailable_preserves_link(self):
        def response(_request):
            return httpx.Response(503, text="provider unavailable")

        with self._commons(response):
            result = search_sources({"text": "Example", "links": ["https://example.org/"]}, SHA256, True)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["items"][0]["method"], "embedded_url")


class LegalTests(unittest.TestCase):
    def _example_group(self, key, entries):
        return {
            "source_url": next(registry["url"] for registry in LEGAL_REGISTRIES if registry["id"] == key),
            "retrieved_at": datetime.now(UTC).date().isoformat(),
            "entries": entries,
        }

    def test_configured_groups_report_exact_and_unconfigured_separately(self):
        groups = {
            "foreign_agents": self._example_group("foreign_agents", [
                {"value": "Тестовое бюро", "reference": "учебная запись 1"},
                {"value": "Проверка", "reference": "учебная запись 2"},
            ]),
            "restricted_sites": self._example_group("restricted_sites", [{"value": "example.org", "reference": "учебная запись 3"}]),
        }
        extraction = {
            "text": "ТЕСТОВОЕ  БЮРО. Перепроверка.",
            "links": ["https://www.example.org"],
        }
        with TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps({"version": 1, "registries": groups}), encoding="utf-8")
            result = check_legal(extraction, path)
        self.assertEqual(result["status"], "partial")
        match = [item for item in result["items"] if item["status"] == "local_snapshot_match"]
        self.assertEqual([item["entry"] for item in match], ["Тестовое бюро"])
        self.assertEqual(match[0]["reference"], "учебная запись 1")
        self.assertIn("snapshot_date", match[0])
        site = next(item for item in result["items"] if item["registry"] == "restricted_sites")
        self.assertEqual(site["status"], "local_snapshot_no_match")
        self.assertEqual(sum(item["status"] == "manual_review" for item in result["items"]), 4)

    def test_invalid_provenance_falls_back_to_manual(self):
        group = self._example_group("foreign_agents", [{"value": "Тестовое бюро", "reference": "учебная запись 1"}])
        group["source_url"] = "https://example.org/not-an-official-registry"
        with TemporaryDirectory() as directory:
            path = Path(directory) / "registry.json"
            path.write_text(json.dumps({"version": 1, "registries": {"foreign_agents": group}}), encoding="utf-8")
            result = check_legal({"text": "Тестовое бюро"}, path)
        self.assertEqual(result["status"], "manual_review")
        self.assertTrue(all(item["status"] == "manual_review" for item in result["items"]))
        self.assertTrue(any("схеме" in warning for warning in result["warnings"]))


if __name__ == "__main__":
    unittest.main()
