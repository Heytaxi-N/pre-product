import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pick_products


class WorkbenchTests(unittest.TestCase):
    def test_payload_marks_incremental_items_and_keeps_video_media(self):
        items = [
            {
                "goods_id": "one",
                "title": "商品一",
                "imgsSrc": ["https://example.com/one.jpg"],
                "videoUrl": "https://example.com/one.mp4",
                "time_stamp": 1780000000000,
            },
            {
                "goods_id": "two",
                "title": "商品二",
                "imgsSrc": ["https://example.com/two.jpg"],
                "videoUrl": "",
                "time_stamp": 1780000060000,
            },
        ]
        payload = pick_products._workbench_payload(
            {"album": {"supplier": "测试供货商", "items": items}}, {}
        )

        self.assertEqual(2, payload[0]["newCount"])
        self.assertTrue(all(item["_new"] for item in payload[0]["items"]))
        self.assertEqual(["two", "one"], [
            item["goods_id"] for item in payload[0]["items"]
        ])
        first = next(item for item in payload[0]["items"] if item["goods_id"] == "one")
        self.assertEqual(
            ["image", "video"],
            [media["type"] for media in first["workbenchMedia"]],
        )
        self.assertEqual(
            "https://example.com/one.mp4?vframe/jpg/offset/0",
            first["workbenchMedia"][1]["thumb"],
        )

    def test_html_contains_lazy_media_and_existing_confirmation_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "workbench.html"
            pick_products.build_workbench_html(
                [{
                    "supplier": "测试供货商",
                    "albumId": "album",
                    "newCount": 1,
                    "items": [{
                        "goods_id": "one",
                        "title": "商品一",
                        "time_stamp": 1780000000000,
                        "_new": True,
                        "workbenchMedia": [{
                            "type": "image",
                            "thumb": "https://example.com/one-thumb.jpg",
                            "url": "https://example.com/one.jpg",
                        }, {
                            "type": "video",
                            "thumb": "",
                            "url": "https://example.com/one.mp4",
                        }],
                    }],
                }],
                out,
            )
            html = out.read_text()

        self.assertIn('loading="lazy"', html)
        self.assertIn("confirmed_groups_", html)
        self.assertIn("state.drafts=[]", html)
        self.assertIn("创建商品", html)
        self.assertIn('id="dateFilter"', html)
        self.assertIn('data-date=', html)
        self.assertIn('id="media-hover-preview"', html)
        self.assertIn("videoThumbFallback", html)
        self.assertIn("mouseenter", html)
        self.assertIn("先点击一个条目作为起点", html)
        self.assertIn("function clickEntry", html)
        self.assertNotIn("function clickMedia", html)

    def test_payload_keeps_supplier_capture_failure(self):
        payload = pick_products._workbench_payload(
            {
                "failed-album": {
                    "supplier": "抓取失败供货商",
                    "items": [],
                    "capture_ok": False,
                    "capture_error": "网络超时",
                }
            },
            {},
        )

        self.assertEqual(1, len(payload))
        self.assertFalse(payload[0]["captureOk"])
        self.assertEqual("网络超时", payload[0]["captureError"])

    def test_latest_scrape_archives_download_under_project_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            downloads = root / "Downloads"
            downloads.mkdir()
            source = downloads / "scrape_all.json"
            source.write_text('{"data": {}}')

            with patch.object(pick_products, "SCRIPT_DIR", root), \
                    patch.object(Path, "home", return_value=root):
                archived = pick_products._latest_scrape_path()
                self.assertEqual(root / "data" / "scrape_all.json", archived)
                self.assertTrue(archived.exists())
                self.assertFalse(source.exists())

    def test_wait_for_confirmed_accepts_timestamped_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "confirmed_groups_1234567890.json"
            path.write_text("{}")
            with patch.object(pick_products.time, "sleep", return_value=None):
                found = pick_products.wait_for_confirmed(Path(tmp), timeout=1)

        self.assertEqual(path, found)

    def test_wait_for_confirmed_can_wait_indefinitely(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "confirmed_groups_1234567890.json"
            path.write_text("{}")
            with patch.object(pick_products.time, "sleep", return_value=None):
                found = pick_products.wait_for_confirmed(Path(tmp), timeout=None)

        self.assertEqual(path, found)


if __name__ == "__main__":
    unittest.main()
