import json
import io
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

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
    def capture_anchor_groups(self, items, date_str="2026-04-30"):
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
                    date_str,
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

    def test_anchor_selects_posts_by_publish_date(self):
        items = [
            make_item("opening-placeholder", 1, "", 1),
            make_item("anchor", 2, "凯乐石女款裙裤", 3),
            make_item("closing-placeholder", 3, "", 1),
        ]
        for minute, item in enumerate(items, 1):
            item["update_time"] = int(
                datetime(2026, 7, 22, 12, minute).timestamp() * 1000
            )

        self.assertEqual(
            [["anchor"]], self.capture_anchor_groups(items, "2026-04-30")
        )

    def test_anchor_uses_surrounding_placeholders_from_other_dates(self):
        items = [
            make_item("opening-placeholder", 0, "", 1, update_minute=0),
            make_item("anchor", 1, "凯乐石女款裙裤", 3, update_minute=1),
            make_item("detail", 2, "细节图", 3, update_minute=2),
            make_item("closing-placeholder", 3, "", 1, update_minute=3),
        ]
        items[0]["time_stamp"] = int(datetime(2026, 4, 29, 12).timestamp() * 1000)
        items[3]["time_stamp"] = int(datetime(2026, 5, 1, 12).timestamp() * 1000)

        self.assertEqual(
            [["anchor", "detail"]], self.capture_anchor_groups(items, "2026-04-30")
        )

    def test_anchor_merge_refreshes_existing_items(self):
        with tempfile.TemporaryDirectory() as tmp:
            scrape_path = Path(tmp) / "scrape_all.json"
            anchor_path = Path(tmp) / "scrape_anchor.json"
            scrape_path.write_text(json.dumps({"data": {"album-1": {
                "supplier": "南在南方",
                "items": [{"goods_id": "same", "time_stamp": 1, "update_time": 999}],
            }}}))
            anchor_path.write_text(json.dumps({
                "supplier": "南在南方",
                "albumId": "album-1",
                "items": [{"goods_id": "same", "time_stamp": 2}],
            }))

            self.assertTrue(pick_products._merge_anchor_into_scrape(scrape_path, anchor_path))
            merged = json.loads(scrape_path.read_text())
            self.assertEqual(2, merged["data"]["album-1"]["items"][0]["time_stamp"])
            self.assertNotIn("update_time", merged["data"]["album-1"]["items"][0])

    def test_date_anchor_reports_empty_capture_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scrape_anchor.json"
            path.write_text(json.dumps({
                "albumId": "album-1",
                "items": [],
                "anchor": {
                    "date": "2026-07-17", "rawCount": 80,
                    "pages": 4, "stopReason": "end", "fullScan": True,
                },
            }))

            problem = pick_products._date_anchor_problem(
                path, "album-1", "2026-07-17"
            )

        self.assertIn("原始 80 条", problem)
        self.assertIn("清洗后 0 条", problem)

    def test_date_anchor_rejects_partial_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scrape_anchor.json"
            item = make_item("match", 1, "T10", 2)
            item["update_time"] = int(
                datetime(2026, 7, 17, 12, 1).timestamp() * 1000
            )
            path.write_text(json.dumps({
                "albumId": "album-1",
                "items": [item],
                "anchor": {
                    "date": "2026-07-17", "rawCount": 20,
                    "pages": 1, "incomplete": True, "stopReason": "network",
                    "fullScan": True,
                },
            }))

            problem = pick_products._date_anchor_problem(
                path, "album-1", "2026-07-17"
            )

        self.assertIn("深挖未完成", problem)
        self.assertIn("network", problem)

    def test_date_anchor_rejects_old_bookmark_capture(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scrape_anchor.json"
            item = make_item("match", 1, "T10", 2, update_minute=1)
            path.write_text(json.dumps({
                "albumId": "album-1",
                "items": [item],
                "anchor": {"date": "2026-04-30", "pages": 1},
            }))

            problem = pick_products._date_anchor_problem(
                path, "album-1", "2026-04-30"
            )

        self.assertIn("旧版书签", problem)

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

    def test_date_anchor_always_refreshes_complete_supplier_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            downloads = home / "Downloads"
            downloads.mkdir()
            scrape_path = downloads / "scrape_all.json"
            config = {"suppliers": {"南在南方": "album-1"}}
            item = make_item("anchor", 1, "凯乐石女款裙裤", 2, update_minute=1)
            scrape_path.write_text(json.dumps({
                "data": {"album-1": {"supplier": "南在南方", "items": [item]}}
            }))

            with patch.object(Path, "home", return_value=home), \
                    patch("webbrowser.open") as open_browser:
                result = pick_products.ensure_data_for_date(
                    scrape_path, config, "南在南方", "2026-04-30", timeout=0
                )

        self.assertFalse(result)
        open_browser.assert_called_once()

    def test_run_with_missing_code_triggers_targeted_deep_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            scrape_path = tmp_path / "scrape_all.json"
            config_path.write_text(json.dumps({
                "suppliers": {"晨星外贸06": "album-1"}
            }))
            scrape_path.write_text(json.dumps({"data": {}}))

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", tmp_path / "progress.json"), \
                    patch.object(pick_products, "ensure_data_for_code", return_value=False) as deep, \
                    patch.object(sys, "argv", ["pick_products.py", "run", "晨星外贸06", "0714c"]), \
                    patch.dict(os.environ, {"SCRAPE_JSON": str(scrape_path)}, clear=False), \
                    redirect_stdout(io.StringIO()) as output:
                pick_products.main()

            deep.assert_called_once_with(
                str(scrape_path), {"suppliers": {"晨星外贸06": "album-1"}},
                "晨星外贸06", "0714c")
            self.assertIn("深挖后仍未找到", output.getvalue())
            self.assertNotIn("所有供货商都已处理到最新", output.getvalue())


if __name__ == "__main__":
    unittest.main()
