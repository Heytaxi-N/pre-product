import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import pick_products


def make_item(goods_id, minute, title, image_count, update_minute=None):
    item = {
        "goods_id": goods_id,
        "title": title,
        "imgsSrc": [f"https://example.com/{goods_id}-{i}.jpg" for i in range(image_count)],
        "time_stamp": int(datetime(2026, 4, 30, 12, minute).timestamp() * 1000),
        "videoUrl": "",
    }
    if update_minute is not None:
        item["update_time"] = int(
            datetime(2026, 4, 30, 12, update_minute).timestamp() * 1000
        )
    return item


class AnchorTests(unittest.TestCase):
    def capture_anchor_groups(self, items):
        captured = []

        def capture_groups(_supplier, _album, groups, *_args, **_kwargs):
            captured.extend(groups)
            return len(groups)

        original_process_groups = pick_products.process_groups
        original_output_dir = pick_products.OUTPUT_DIR
        try:
            with tempfile.TemporaryDirectory() as tmp:
                pick_products.process_groups = capture_groups
                pick_products.OUTPUT_DIR = Path(tmp)
                pick_products.cmd_anchor(
                    {"suppliers": {"南在南方": "album-1"}},
                    {},
                    {"album-1": {"items": items}},
                    "南在南方",
                    "凯乐石女款裙裤",
                    "2026-04-30",
                )
        finally:
            pick_products.process_groups = original_process_groups
            pick_products.OUTPUT_DIR = original_output_dir
        return [[item["goods_id"] for item in group] for group in captured]

    def test_anchor_uses_nearest_enclosing_placeholders(self):
        items = [
            make_item("old-product", 0, "前一件商品", 3),
            make_item("opening-placeholder", 1, "", 1),
            make_item("product-title", 2, "女款新款裙裤", 3),
            make_item("anchor", 3, "凯乐石女款裙裤来咯", 5),
            make_item("detail-color", 3, "黑色", 2),
            make_item("detail-size", 5, "尺码表", 2),
            make_item("closing-placeholder", 6, "", 1),
            make_item("next-product", 7, "下一件商品", 4),
        ]
        self.assertEqual(
            [["product-title", "anchor", "detail-color", "detail-size"]],
            self.capture_anchor_groups(items),
        )

    def test_anchor_sorts_by_update_time(self):
        items = [
            make_item("product-title", 0, "女款新款裙裤", 3, update_minute=3),
            make_item("anchor", 1, "凯乐石女款裙裤来咯", 5, update_minute=4),
            make_item("opening-placeholder", 2, "", 1, update_minute=2),
            make_item("detail", 3, "细节", 4, update_minute=5),
            make_item("closing-placeholder", 4, "", 1, update_minute=6),
        ]
        self.assertEqual(
            [["product-title", "anchor", "detail"]],
            self.capture_anchor_groups(items),
        )

    def test_anchor_merge_refreshes_existing_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            scrape_path = Path(tmp) / "scrape_all.json"
            anchor_path = Path(tmp) / "scrape_anchor.json"
            scrape_path.write_text(json.dumps({"data": {"album-1": {
                "supplier": "南在南方",
                "items": [{"goods_id": "same", "time_stamp": 1}],
            }}}))
            anchor_path.write_text(json.dumps({
                "supplier": "南在南方",
                "albumId": "album-1",
                "items": [{"goods_id": "same", "time_stamp": 1, "update_time": 2}],
            }))

            self.assertTrue(pick_products._merge_anchor_into_scrape(scrape_path, anchor_path))
            merged = json.loads(scrape_path.read_text())
            self.assertEqual(2, merged["data"]["album-1"]["items"][0]["update_time"])

    def test_existing_date_requires_display_order_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            scrape_path = Path(tmp) / "scrape_all.json"
            config = {"suppliers": {"南在南方": "album-1"}}
            item = make_item("same", 1, "凯乐石女款裙裤", 2)
            payload = {"data": {"album-1": {"supplier": "南在南方", "items": [item]}}}
            scrape_path.write_text(json.dumps(payload))

            self.assertFalse(
                pick_products._data_has_date(
                    scrape_path, config, "南在南方", "2026-04-30"
                )
            )
            self.assertTrue(
                pick_products._data_has_date(
                    scrape_path, config, "南在南方", "2026-04-30", require_order=False
                )
            )


if __name__ == "__main__":
    unittest.main()
