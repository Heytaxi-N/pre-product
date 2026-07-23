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


def make_item(goods_id, title, publish_minute, update_minute, publish_day=17):
    return {
        "goods_id": goods_id,
        "title": title,
        "imgsSrc": [f"https://example.com/{goods_id}.jpg"],
        "time_stamp": int(
            datetime(2026, 7, publish_day, 12, publish_minute).timestamp() * 1000
        ),
        "update_time": int(
            datetime(2026, 7, 22, 12, update_minute).timestamp() * 1000
        ),
        "videoUrl": "",
    }


class TitleRangeTests(unittest.TestCase):
    def test_title_range_includes_both_ends_and_middle_in_display_order(self):
        items = [
            make_item("middle", "中间细节图", 2, 2),
            make_item("visual-first", "首帖商品介绍", 1, 3),
            make_item("visual-last", "尾帖尺码信息", 3, 1),
            make_item("outside", "其他产品", 4, 4),
        ]

        group, problem = pick_products.find_title_range(
            items, "首帖商品", "尾帖尺码", "2026-07-17"
        )

        self.assertEqual("", problem)
        self.assertEqual(
            ["visual-last", "middle", "visual-first"],
            [item["goods_id"] for item in group],
        )

    def test_title_range_rejects_ambiguous_prefix(self):
        items = [
            make_item("start-1", "商品介绍 黑色", 1, 1),
            make_item("start-2", "商品介绍 白色", 2, 2),
            make_item("end", "尺码信息", 3, 3),
        ]

        group, problem = pick_products.find_title_range(
            items, "商品介绍", "尺码信息", "2026-07-17"
        )

        self.assertEqual([], group)
        self.assertIn("起始前缀", problem)
        self.assertIn("2 条", problem)

    def test_title_range_only_matches_the_requested_date(self):
        items = [
            make_item("other-date", "商品介绍 旧款", 1, 1, publish_day=16),
            make_item("start", "商品介绍 新款", 2, 2),
            make_item("end", "尺码信息", 3, 3),
        ]

        group, problem = pick_products.find_title_range(
            items, "商品介绍", "尺码信息", "2026-07-17"
        )

        self.assertEqual("", problem)
        self.assertEqual(["start", "end"], [item["goods_id"] for item in group])

    def test_range_anchor_requires_date_scan_and_matching_prefixes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scrape_anchor.json"
            items = [
                make_item("start", "商品介绍", 1, 1),
                make_item("end", "尺码信息", 2, 2),
            ]
            payload = {
                "albumId": "album-1",
                "items": items,
                "anchor": {
                    "rangeStart": "商品",
                    "rangeEnd": "尺码",
                    "rangeDate": "2026-07-17",
                    "dateScan": True,
                    "fullScan": False,
                    "incomplete": False,
                },
            }
            path.write_text(json.dumps(payload))

            self.assertEqual(
                "",
                pick_products._range_anchor_problem(
                    path, "album-1", "2026-07-17", "商品", "尺码"
                ),
            )
            payload["anchor"]["dateScan"] = False
            path.write_text(json.dumps(payload))
            self.assertIn(
                "旧版书签",
                pick_products._range_anchor_problem(
                    path, "album-1", "2026-07-17", "商品", "尺码"
                ),
            )

    def test_range_anchor_reports_missing_date_before_prefix_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scrape_anchor.json"
            path.write_text(json.dumps({
                "albumId": "album-1",
                "items": [],
                "anchor": {
                    "rangeStart": "T10 四季",
                    "rangeEnd": "26版",
                    "rangeDate": "2026-07-17",
                    "dateScan": True,
                    "fullScan": False,
                    "incomplete": False,
                    "rawCount": 63,
                    "pages": 2,
                    "stopReason": "end",
                },
            }))

            problem = pick_products._range_anchor_problem(
                path, "album-1", "2026-07-17", "T10 四季", "26版"
            )

        self.assertIn("未抓到 2026-07-17", problem)
        self.assertNotIn("起始前缀", problem)

    def test_date_scan_replaces_only_the_requested_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            scrape_path = Path(tmp) / "scrape_all.json"
            anchor_path = Path(tmp) / "scrape_anchor.json"
            stale = make_item("stale", "商品介绍 旧缓存", 1, 1)
            other_day = make_item(
                "other-day", "其他日期", 1, 1, publish_day=16
            )
            fresh = make_item("fresh", "商品介绍 新数据", 2, 2)
            scrape_path.write_text(json.dumps({
                "data": {"album-1": {
                    "supplier": "晓豪",
                    "items": [stale, other_day],
                }}
            }))
            anchor_path.write_text(json.dumps({
                "supplier": "晓豪",
                "albumId": "album-1",
                "items": [fresh],
                "anchor": {
                    "rangeDate": "2026-07-17",
                    "dateScan": True,
                    "fullScan": False,
                },
            }))

            pick_products._merge_anchor_into_scrape(scrape_path, anchor_path)
            merged = json.loads(scrape_path.read_text())

        self.assertEqual(
            {"other-day", "fresh"},
            {
                item["goods_id"]
                for item in merged["data"]["album-1"]["items"]
            },
        )

    def test_cmd_range_processes_one_group_without_advancing_progress(self):
        items = [
            make_item("start", "商品介绍", 1, 1),
            make_item("middle", "细节图", 2, 2),
            make_item("end", "尺码信息", 3, 3),
        ]
        captured = {}
        original = pick_products.process_groups

        def capture(*args, **kwargs):
            captured["groups"] = args[2]
            captured["advance_progress"] = kwargs["advance_progress"]
            return 1

        try:
            pick_products.process_groups = capture
            pick_products.cmd_title_range(
                {"suppliers": {"晓豪": "album-1"}},
                {},
                {"album-1": {"items": items}},
                "晓豪",
                "2026-07-17",
                "商品",
                "尺码",
            )
        finally:
            pick_products.process_groups = original

        self.assertEqual(
            [["start", "middle", "end"]],
            [[item["goods_id"] for item in group] for group in captured["groups"]],
        )
        self.assertFalse(captured["advance_progress"])

    def test_range_command_triggers_date_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            scrape_path = tmp_path / "scrape_all.json"
            config = {"suppliers": {"晓豪": "album-1"}}
            config_path.write_text(json.dumps(config))
            scrape_path.write_text(json.dumps({"data": {}}))

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", tmp_path / "progress.json"), \
                    patch.object(pick_products, "ensure_data_for_range", return_value=True) as deep, \
                    patch.object(pick_products, "load_scrape", return_value={"album-1": {"items": []}}), \
                    patch.object(pick_products, "cmd_title_range") as command, \
                    patch.object(
                        sys, "argv",
                        [
                            "pick_products.py", "range", "晓豪", "07-17",
                            "商品介绍", "尺码信息",
                        ],
                    ), \
                    patch.dict(os.environ, {"SCRAPE_JSON": str(scrape_path)}, clear=False), \
                    redirect_stdout(io.StringIO()):
                pick_products.main()

        deep.assert_called_once_with(
            str(scrape_path), config, "晓豪", "2026-07-17", "商品介绍", "尺码信息"
        )
        command.assert_called_once()

    def test_range_command_rejects_unquoted_extra_words(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            config_path = tmp_path / "config.json"
            config_path.write_text(json.dumps({"suppliers": {"晓豪": "album-1"}}))

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", tmp_path / "progress.json"), \
                    patch.object(pick_products, "ensure_data_for_range") as deep, \
                    patch.object(
                        sys, "argv",
                        [
                            "pick_products.py", "range", "晓豪", "07-17",
                            "26版", "T10四季裤", "尺码",
                        ],
                    ), \
                    redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit):
                    pick_products.main()

        deep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
