import tempfile
import unittest
from pathlib import Path

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
                        }],
                    }],
                }],
                out,
            )
            html = out.read_text()

        self.assertIn('loading="lazy"', html)
        self.assertIn("confirmed_groups.json", html)
        self.assertIn("创建商品", html)
        self.assertIn('id="dateFilter"', html)
        self.assertIn('data-date=', html)


if __name__ == "__main__":
    unittest.main()
