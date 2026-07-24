import importlib
import json
import os
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timedelta
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pick_products


BASE_TIME = datetime(2026, 7, 24, 12)


def make_item(goods_id, minute=0):
    return {
        "goods_id": goods_id,
        "title": goods_id,
        "imgsSrc": [f"https://example.com/{goods_id}.jpg"],
        "time_stamp": int(
            (BASE_TIME + timedelta(minutes=minute)).timestamp() * 1000
        ),
        "videoUrl": "",
    }


def import_capture_szwego():
    mitmproxy = types.ModuleType("mitmproxy")
    http = types.ModuleType("mitmproxy.http")
    http.HTTPFlow = object
    mitmproxy.http = http
    sys.modules.pop("capture_szwego", None)
    with patch.dict(
        sys.modules,
        {"mitmproxy": mitmproxy, "mitmproxy.http": http},
    ):
        return importlib.import_module("capture_szwego")


def find_helper(module, *names):
    return next((getattr(module, name) for name in names if hasattr(module, name)), None)


class DownloadSelectionTests(unittest.TestCase):
    def test_only_chrome_numbered_copies_are_selected_non_destructively(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            downloads = home / "Downloads"
            downloads.mkdir()
            files = {
                "base.json": ("original", 10),
                "base (2).json": ("newest download", 20),
                "base_backup.json": ("backup", 40),
                "base_old.json": ("old archive", 30),
            }
            for name, (content, mtime) in files.items():
                path = downloads / name
                path.write_text(content)
                os.utime(path, (mtime, mtime))

            with patch.object(Path, "home", return_value=home):
                selected = pick_products.pick_newest_download("base")

            self.assertEqual(downloads / "base (2).json", selected)
            self.assertEqual("newest download", selected.read_text())
            self.assertEqual("original", (downloads / "base.json").read_text())
            self.assertEqual("backup", (downloads / "base_backup.json").read_text())
            self.assertEqual("old archive", (downloads / "base_old.json").read_text())

    def test_invalid_newest_download_does_not_delete_previous_valid_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            downloads = home / "Downloads"
            downloads.mkdir()
            valid = downloads / "base.json"
            invalid = downloads / "base (1).json"
            valid.write_text('{"valid": true}')
            invalid.write_text("{broken")
            os.utime(valid, (10, 10))
            os.utime(invalid, (20, 20))

            with patch.object(Path, "home", return_value=home):
                selected = pick_products.pick_newest_download("base")

            self.assertEqual(invalid, selected)
            self.assertEqual('{"valid": true}', valid.read_text())


class AiGroupingFailureTests(unittest.TestCase):
    def test_ai_same_product_without_configuration_returns_none(self):
        self.assertIsNone(
            pick_products._ai_same_product(
                {}, make_item("a"), make_item("b", 1), Path("/unused")
            )
        )

    def test_ai_same_product_api_exception_returns_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            first = root / "first.jpg"
            second = root / "second.jpg"
            first.write_bytes(b"first")
            second.write_bytes(b"second")
            with patch.object(
                pick_products,
                "_ensure_first_image",
                side_effect=[first, second],
            ), patch.object(
                pick_products,
                "http_post_json",
                side_effect=RuntimeError("AI unavailable"),
            ):
                result = pick_products._ai_same_product(
                    {"base_url": "https://ai.example", "api_key": "key"},
                    make_item("a"),
                    make_item("b", 1),
                    root / "cache",
                )

        self.assertIsNone(result)

    def test_group_products_ai_fails_explicitly_on_unknown_comparison(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            pick_products, "_is_placeholder", return_value=False
        ), patch.object(
            pick_products, "_ai_same_product", return_value=None
        ):
            with self.assertRaisesRegex(RuntimeError, "AI"):
                pick_products.group_products_ai(
                    {},
                    [make_item("a"), make_item("b", 1)],
                    Path(tmp) / "cache",
                )

    def test_preview_falls_back_to_one_boundary_per_post(self):
        items = [make_item("a"), make_item("b", 1), make_item("c", 2)]
        data = {"album-1": {"supplier": "supplier", "items": items}}
        captured = {}

        def capture(previews, _out):
            captured["preview"] = previews[0]

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            pick_products, "SCRIPT_DIR", Path(tmp)
        ), patch.object(
            pick_products, "OUTPUT_DIR", Path(tmp) / "output"
        ), patch.object(
            pick_products,
            "select_suppliers",
            return_value=[("supplier", "album-1", len(items))],
        ), patch.object(
            pick_products,
            "group_products_ai",
            side_effect=RuntimeError("AI grouping failed"),
        ), patch.object(
            pick_products, "build_preview_html", side_effect=capture
        ), patch(
            "webbrowser.open"
        ), redirect_stdout(
            StringIO()
        ):
            pick_products.cmd_preview({}, {}, data)

        self.assertEqual(["a", "b", "c"], [
            post["goods_id"] for post in captured["preview"]["posts"]
        ])
        self.assertEqual([True, True], captured["preview"]["boundaries"])

    def test_automatic_process_returns_zero_when_grouping_fails(self):
        items = [make_item("a"), make_item("b", 1)]
        with patch.object(
            pick_products, "filter_new_items", return_value=items
        ), patch.object(
            pick_products,
            "group_products_ai",
            side_effect=RuntimeError("AI grouping failed"),
        ), patch.object(
            pick_products, "process_groups"
        ) as process_groups, redirect_stdout(
            StringIO()
        ):
            result = pick_products.process_supplier(
                "supplier", "album-1", items, {}, None, {}, {}
            )

        self.assertEqual(0, result)
        process_groups.assert_not_called()


class FeishuRetryTests(unittest.TestCase):
    def test_corrupt_pending_file_falls_back_without_crashing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "feishu_pending.json"
            path.write_text("{broken")
            with patch.object(
                pick_products, "FEISHU_PENDING_FILE", path
            ), redirect_stdout(StringIO()):
                self.assertEqual({}, pick_products._load_feishu_pending())

    def test_unconfirmed_creation_marker_prevents_duplicate_record(self):
        item = make_item("product")

        class Feishu:
            def create_record(self, _fields):
                raise AssertionError("must not create a duplicate record")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pending_file = root / "feishu_pending.json"
            key = pick_products._feishu_pending_key("album-1", ["product"])
            pending_file.write_text(json.dumps({key: {"status": "creating"}}))
            image = root / "image.jpg"
            image.write_bytes(b"image")
            with patch.object(
                pick_products, "FEISHU_PENDING_FILE", pending_file
            ), patch.object(
                pick_products, "download_product_images", return_value=[image]
            ), patch.object(
                pick_products, "classify_images_ai", return_value=None
            ), patch.object(
                pick_products, "create_product_folder"
            ) as create_folder, redirect_stdout(StringIO()):
                count = pick_products.process_groups(
                    "supplier",
                    "album-1",
                    [[item]],
                    {},
                    Feishu(),
                    {},
                    {},
                    advance_progress=False,
                    review=False,
                )

        self.assertEqual(0, count)
        create_folder.assert_not_called()

    def test_wait_failure_persists_and_reuses_created_record(self):
        item = make_item("product")

        class RetryingFeishu:
            def __init__(self):
                self.create_calls = 0
                self.waited_ids = []

            def create_record(self, _fields):
                self.create_calls += 1
                return "record-123"

            def wait_for_field(self, record_id, _field):
                self.waited_ids.append(record_id)
                if len(self.waited_ids) == 1:
                    raise RuntimeError("field not ready")
                return "ready-product"

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pending_file = root / "feishu_pending.json"
            feishu = RetryingFeishu()

            def download(_group, tmp_dir):
                tmp_dir.mkdir(parents=True, exist_ok=True)
                image = tmp_dir / "image.jpg"
                image.write_bytes(b"image")
                return [image]

            with patch.object(
                pick_products, "TMP_ROOT", root / "tmp"
            ), patch.object(
                pick_products, "OUTPUT_DIR", root / "output"
            ), patch.object(
                pick_products,
                "FEISHU_PENDING_FILE",
                pending_file,
                create=True,
            ), patch.object(
                pick_products,
                "download_product_images",
                side_effect=download,
            ), patch.object(
                pick_products, "classify_images_ai", return_value=None
            ), patch.object(
                pick_products, "create_product_folder"
            ) as create_folder, redirect_stdout(
                StringIO()
            ):
                first = pick_products.process_groups(
                    "supplier",
                    "album-1",
                    [[item]],
                    {},
                    feishu,
                    {},
                    {},
                    advance_progress=False,
                    review=False,
                )

                self.assertEqual(0, first)
                self.assertTrue(pending_file.exists())
                self.assertIn("record-123", pending_file.read_text())

                second = pick_products.process_groups(
                    "supplier",
                    "album-1",
                    [[item]],
                    {},
                    feishu,
                    {},
                    {},
                    advance_progress=False,
                    review=False,
                )

        self.assertEqual(1, second)
        self.assertEqual(1, feishu.create_calls)
        self.assertEqual(["record-123", "record-123"], feishu.waited_ids)
        create_folder.assert_called_once()


class CaptureRedactionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.capture = import_capture_szwego()

    def test_url_headers_and_json_body_helpers_redact_secrets(self):
        redact_url = find_helper(
            self.capture, "_redact_url", "redact_url"
        )
        redact_headers = find_helper(
            self.capture, "_redact_headers", "redact_headers"
        )
        redact_json_body = find_helper(
            self.capture, "_redact_json_body", "redact_json_body"
        )
        self.assertIsNotNone(redact_url)
        self.assertIsNotNone(redact_headers)
        self.assertIsNotNone(redact_json_body)

        url = redact_url(
            "https://www.szwego.com/api/list?token=url-secret"
            "&sign=sign-secret&page=2"
        )
        headers = redact_headers({
            "Authorization": "Bearer header-secret",
            "Cookie": "sid=cookie-secret",
            "X-Api-Key": "key-secret",
            "Content-Type": "application/json",
            "Referer": "https://www.szwego.com/page?token=referer-secret",
        })
        body = redact_json_body(json.dumps({
            "token": "body-secret",
            "nested": {"app_secret": "nested-secret"},
            "imgsSrc": ["https://cdn.example/image.jpg?sign=image-secret"],
            "title": "keep-this-title",
        }))

        self.assertNotIn("url-secret", str(url))
        self.assertNotIn("sign-secret", str(url))
        self.assertIn("page=2", str(url))
        self.assertNotIn("header-secret", str(headers))
        self.assertNotIn("cookie-secret", str(headers))
        self.assertNotIn("key-secret", str(headers))
        self.assertNotIn("referer-secret", str(headers))
        self.assertIn("application/json", str(headers))
        self.assertNotIn("body-secret", str(body))
        self.assertNotIn("nested-secret", str(body))
        self.assertNotIn("image-secret", str(body))
        self.assertIn("keep-this-title", str(body))

    def test_capture_writes_only_redacted_request_data(self):
        request_body = json.dumps({
            "token": "body-secret",
            "filters": {"name": "keep-this-name"},
        })

        class Request:
            pretty_host = "www.szwego.com"
            pretty_url = (
                "https://www.szwego.com/api/list?"
                "access_token=url-secret&page=2"
            )
            method = "POST"
            headers = {
                "Authorization": "Bearer header-secret",
                "Cookie": "sid=cookie-secret",
                "Content-Type": "application/json",
            }
            content = request_body.encode()

            def get_text(self):
                return request_body

        class Response:
            headers = {"content-type": "application/json"}
            status_code = 200
            content = b'{"ok": true}'

            @staticmethod
            def get_text():
                return '{"ok": true}'

        flow = types.SimpleNamespace(request=Request(), response=Response())
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            self.capture, "OUTPUT", str(Path(tmp) / "captured_api.jsonl")
        ), redirect_stdout(
            StringIO()
        ):
            self.capture.response(flow)
            entry = json.loads(Path(self.capture.OUTPUT).read_text())

        serialized = json.dumps(entry)
        for secret in (
            "url-secret",
            "header-secret",
            "cookie-secret",
            "body-secret",
        ):
            self.assertNotIn(secret, serialized)
        self.assertIn("keep-this-name", serialized)
        self.assertIn("page=2", serialized)

    def test_capture_output_is_gitignored(self):
        ignored = {
            line.strip()
            for line in (Path(__file__).parents[1] / ".gitignore").read_text().splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        }
        self.assertIn("captured_api.jsonl", ignored)


if __name__ == "__main__":
    unittest.main()
