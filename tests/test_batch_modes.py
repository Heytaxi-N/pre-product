import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import pick_products


class BatchTargetParsingTests(unittest.TestCase):
    def test_run_parses_and_deduplicates_codes(self):
        self.assertEqual(
            [("晨星外贸06", "0708d"), ("晨星外贸06", "0712c")],
            pick_products._parse_batch_targets(
                "run", ["晨星外贸06", "0708d", "0712c", "0708d"]
            ),
        )

    def test_anchor_parses_repeated_triplets(self):
        self.assertEqual(
            [
                ("南在南方", "凯乐石女款裙裤", "04-30"),
                ("晓豪", "宽松透气", "07-10"),
            ],
            pick_products._parse_batch_targets(
                "anchor",
                [
                    "南在南方", "凯乐石女款裙裤", "04-30",
                    "晓豪", "宽松透气", "07-10",
                ],
            ),
        )

    def test_range_parses_repeated_quartets(self):
        self.assertEqual(
            [
                ("晓豪", "07-17", "T10", "26版"),
                ("南在南方", "07-17", "T10", "26版"),
            ],
            pick_products._parse_batch_targets(
                "range",
                [
                    "晓豪", "07-17", "T10", "26版",
                    "南在南方", "07-17", "T10", "26版",
                ],
            ),
        )

    def test_anchor_and_range_reject_incomplete_groups(self):
        with self.assertRaisesRegex(ValueError, "每 3 个参数"):
            pick_products._parse_batch_targets(
                "anchor", ["南在南方", "关键词", "04-30", "晓豪"]
            )
        with self.assertRaisesRegex(ValueError, "每 4 个参数"):
            pick_products._parse_batch_targets(
                "range", ["晓豪", "07-17", "T10", "26版", "南在南方"]
            )


class BatchExecutionTests(unittest.TestCase):
    def test_run_keyboard_interrupt_stops_the_entire_batch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            progress_path = root / "progress.json"
            scrape_path = root / "scrape_all.json"
            config_path.write_text(json.dumps({
                "suppliers": {"晨星外贸06": "album-1"}
            }))
            scrape_path.write_text(json.dumps({"data": {}}))

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", progress_path), \
                    patch.object(pick_products, "OUTPUT_DIR", root / "output"), \
                    patch.object(
                        pick_products,
                        "ensure_data_for_code",
                        side_effect=[True, KeyboardInterrupt],
                    ) as ensure, \
                    patch.object(
                        pick_products,
                        "load_scrape",
                        return_value={"album-1": {"items": [{"goods_id": "item"}]}},
                    ), \
                    patch.object(
                        pick_products, "process_supplier", return_value=1
                    ) as process, \
                    patch.object(
                        sys,
                        "argv",
                        [
                            "pick_products.py", "run", "晨星外贸06",
                            "0708d", "0712c",
                        ],
                    ), \
                    patch.dict(
                        os.environ, {"SCRAPE_JSON": str(scrape_path)}, clear=False
                    ), \
                    redirect_stdout(StringIO()) as output:
                pick_products.main()

        self.assertEqual(2, ensure.call_count)
        process.assert_called_once()
        self.assertIn("已取消批量任务", output.getvalue())
        self.assertIn("取消前汇总", output.getvalue())
        self.assertIn("成功 1 项", output.getvalue())

    def test_deep_fetch_ignores_anchor_file_that_existed_before_this_item(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            scrape_path = root / "scrape_all.json"
            stale_anchor = root / "scrape_anchor.json"
            config = {"suppliers": {"晨星外贸06": "album-1"}}
            scrape_path.write_text(json.dumps({
                "data": {"album-1": {"supplier": "晨星外贸06", "items": []}}
            }))
            stale_anchor.write_text(json.dumps({
                "supplier": "其他供应商",
                "albumId": "other-album",
                "items": [],
            }))

            def newest(base):
                return stale_anchor if base == "scrape_anchor" else None

            with patch.object(
                    pick_products, "pick_newest_download", side_effect=newest), \
                    patch.object(
                        pick_products, "_code_anchor_problem"
                    ) as validate_anchor, \
                    patch.object(
                        pick_products.time,
                        "time",
                        side_effect=[100, 100, 100, 102],
                    ), \
                    patch.object(pick_products.time, "sleep"), \
                    patch("webbrowser.open"), \
                    redirect_stdout(StringIO()):
                ready = pick_products.ensure_data_for_code(
                    scrape_path, config, "晨星外贸06", "0712c", timeout=1
                )

        self.assertFalse(ready)
        validate_anchor.assert_not_called()

    def test_run_continues_after_one_code_deep_fetch_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            progress_path = root / "progress.json"
            scrape_path = root / "scrape_all.json"
            config = {"suppliers": {"晨星外贸06": "album-1"}}
            config_path.write_text(json.dumps(config))
            scrape_path.write_text(json.dumps({"data": {}}))
            events = []

            def ensure(_path, _config, _supplier, code, **kwargs):
                events.append(("ensure", code, kwargs.get("raise_interrupt")))
                return code == "0712c"

            def process(_name, _aid, _items, *_args, **kwargs):
                events.append((
                    "process",
                    kwargs["code"],
                    kwargs["review_id"],
                    kwargs["review_label"],
                ))
                return 1

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", progress_path), \
                    patch.object(pick_products, "ensure_data_for_code", side_effect=ensure), \
                    patch.object(
                        pick_products,
                        "load_scrape",
                        return_value={"album-1": {"items": [{"goods_id": "item"}]}},
                    ), \
                    patch.object(pick_products, "process_supplier", side_effect=process), \
                    patch.object(
                        sys,
                        "argv",
                        [
                            "pick_products.py", "run", "晨星外贸06",
                            "0708d", "0712c",
                        ],
                    ), \
                    patch.dict(
                        os.environ, {"SCRAPE_JSON": str(scrape_path)}, clear=False
                    ), \
                    redirect_stdout(StringIO()) as output:
                pick_products.main()

        self.assertEqual(
            [
                ("ensure", "0708d", True),
                ("ensure", "0712c", True),
                ("process", "0712c", "02_01", "第 2/2 项 · 晨星外贸06 · 编码 0712c"),
            ],
            events,
        )
        self.assertIn("成功 1 项", output.getvalue())
        self.assertIn("失败 1 项", output.getvalue())

    def test_anchor_batch_runs_triplets_in_input_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            progress_path = root / "progress.json"
            scrape_path = root / "scrape_all.json"
            config = {
                "suppliers": {"南在南方": "album-1", "晓豪": "album-2"}
            }
            config_path.write_text(json.dumps(config))
            scrape_path.write_text(json.dumps({"data": {}}))
            calls = []

            def command(
                    _config, _progress, _data, supplier, keyword, date,
                    **kwargs):
                calls.append((
                    supplier, keyword, date,
                    kwargs["review_prefix"], kwargs["review_label"],
                ))
                return 1

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", progress_path), \
                    patch.object(pick_products, "ensure_data_for_date", return_value=True), \
                    patch.object(pick_products, "load_scrape", return_value={}), \
                    patch.object(pick_products, "cmd_anchor", side_effect=command), \
                    patch.object(
                        sys,
                        "argv",
                        [
                            "pick_products.py", "anchor",
                            "南在南方", "凯乐石女款裙裤", "04-30",
                            "晓豪", "宽松透气", "07-10",
                        ],
                    ), \
                    patch.dict(
                        os.environ, {"SCRAPE_JSON": str(scrape_path)}, clear=False
                    ), \
                    redirect_stdout(StringIO()):
                pick_products.main()

        self.assertEqual(
            [
                (
                    "南在南方", "凯乐石女款裙裤", "04-30", "01",
                    "第 1/2 项 · 南在南方 · 锚点 凯乐石女款裙裤 · 2026-04-30",
                ),
                (
                    "晓豪", "宽松透气", "07-10", "02",
                    "第 2/2 项 · 晓豪 · 锚点 宽松透气 · 2026-07-10",
                ),
            ],
            calls,
        )

    def test_anchor_batch_reuses_successful_fetch_for_same_supplier_and_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            progress_path = root / "progress.json"
            scrape_path = root / "scrape_all.json"
            config_path.write_text(json.dumps({
                "suppliers": {"A外贸": "album-1"}
            }))
            scrape_path.write_text(json.dumps({"data": {}}))

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", progress_path), \
                    patch.object(
                        pick_products, "ensure_data_for_date", return_value=True
                    ) as ensure, \
                    patch.object(pick_products, "load_scrape", return_value={}), \
                    patch.object(
                        pick_products, "cmd_anchor", return_value=1
                    ) as command, \
                    patch.object(
                        sys,
                        "argv",
                        [
                            "pick_products.py", "anchor",
                            "A外贸", "348包邮", "07-23",
                            "A外贸", "248包邮", "07-23",
                        ],
                    ), \
                    patch.dict(
                        os.environ, {"SCRAPE_JSON": str(scrape_path)}, clear=False
                    ), \
                    redirect_stdout(StringIO()) as output:
                pick_products.main()

        ensure.assert_called_once()
        self.assertEqual(2, command.call_count)
        self.assertIn("复用已深挖数据", output.getvalue())

    def test_anchor_batch_does_not_cache_a_failed_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            progress_path = root / "progress.json"
            scrape_path = root / "scrape_all.json"
            config_path.write_text(json.dumps({
                "suppliers": {"A外贸": "album-1"}
            }))
            scrape_path.write_text(json.dumps({"data": {}}))

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", progress_path), \
                    patch.object(
                        pick_products,
                        "ensure_data_for_date",
                        side_effect=[False, True],
                    ) as ensure, \
                    patch.object(pick_products, "load_scrape", return_value={}), \
                    patch.object(
                        pick_products, "cmd_anchor", return_value=1
                    ) as command, \
                    patch.object(
                        sys,
                        "argv",
                        [
                            "pick_products.py", "anchor",
                            "A外贸", "348包邮", "07-23",
                            "A外贸", "248包邮", "07-23",
                        ],
                    ), \
                    patch.dict(
                        os.environ, {"SCRAPE_JSON": str(scrape_path)}, clear=False
                    ), \
                    redirect_stdout(StringIO()):
                pick_products.main()

        self.assertEqual(2, ensure.call_count)
        command.assert_called_once()

    def test_range_batch_runs_quartets_in_input_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            progress_path = root / "progress.json"
            scrape_path = root / "scrape_all.json"
            config = {"suppliers": {"晓豪": "album-1", "南在南方": "album-2"}}
            config_path.write_text(json.dumps(config))
            scrape_path.write_text(json.dumps({"data": {}}))
            calls = []

            def command(
                    _config, _progress, _data, supplier, date, start, end,
                    **kwargs):
                calls.append((
                    supplier, date, start, end,
                    kwargs["review_id"], kwargs["review_label"],
                ))
                return 1

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", progress_path), \
                    patch.object(pick_products, "ensure_data_for_range", return_value=True), \
                    patch.object(pick_products, "load_scrape", return_value={}), \
                    patch.object(pick_products, "cmd_title_range", side_effect=command), \
                    patch.object(
                        sys,
                        "argv",
                        [
                            "pick_products.py", "range",
                            "晓豪", "07-17", "T10", "26版",
                            "南在南方", "07-17", "T10", "26版",
                        ],
                    ), \
                    patch.dict(
                        os.environ, {"SCRAPE_JSON": str(scrape_path)}, clear=False
                    ), \
                    redirect_stdout(StringIO()):
                pick_products.main()

        self.assertEqual(
            [
                (
                    "晓豪", "2026-07-17", "T10", "26版", "01_01",
                    "第 1/2 项 · 晓豪 · 首尾 T10 → 26版 · 2026-07-17",
                ),
                (
                    "南在南方", "2026-07-17", "T10", "26版", "02_01",
                    "第 2/2 项 · 南在南方 · 首尾 T10 → 26版 · 2026-07-17",
                ),
            ],
            calls,
        )

    def test_range_batch_reuses_successful_fetch_for_same_supplier_and_date(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            progress_path = root / "progress.json"
            scrape_path = root / "scrape_all.json"
            config_path.write_text(json.dumps({
                "suppliers": {"晓豪": "album-1"}
            }))
            scrape_path.write_text(json.dumps({"data": {}}))

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", progress_path), \
                    patch.object(
                        pick_products, "ensure_data_for_range", return_value=True
                    ) as ensure, \
                    patch.object(pick_products, "load_scrape", return_value={}), \
                    patch.object(
                        pick_products, "cmd_title_range", return_value=1
                    ) as command, \
                    patch.object(
                        sys,
                        "argv",
                        [
                            "pick_products.py", "range",
                            "晓豪", "07-17", "T10", "26版",
                            "晓豪", "07-17", "T11", "27版",
                        ],
                    ), \
                    patch.dict(
                        os.environ, {"SCRAPE_JSON": str(scrape_path)}, clear=False
                    ), \
                    redirect_stdout(StringIO()) as output:
                pick_products.main()

        ensure.assert_called_once()
        self.assertEqual(2, command.call_count)
        self.assertIn("复用已深挖数据", output.getvalue())

    def test_range_batch_does_not_cache_a_failed_fetch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config_path = root / "config.json"
            progress_path = root / "progress.json"
            scrape_path = root / "scrape_all.json"
            config_path.write_text(json.dumps({
                "suppliers": {"晓豪": "album-1"}
            }))
            scrape_path.write_text(json.dumps({"data": {}}))

            with patch.object(pick_products, "CONFIG_FILE", config_path), \
                    patch.object(pick_products, "PROGRESS_FILE", progress_path), \
                    patch.object(
                        pick_products,
                        "ensure_data_for_range",
                        side_effect=[False, True],
                    ) as ensure, \
                    patch.object(pick_products, "load_scrape", return_value={}), \
                    patch.object(
                        pick_products, "cmd_title_range", return_value=1
                    ) as command, \
                    patch.object(
                        sys,
                        "argv",
                        [
                            "pick_products.py", "range",
                            "晓豪", "07-17", "T10", "26版",
                            "晓豪", "07-17", "T11", "27版",
                        ],
                    ), \
                    patch.dict(
                        os.environ, {"SCRAPE_JSON": str(scrape_path)}, clear=False
                    ), \
                    redirect_stdout(StringIO()):
                pick_products.main()

        self.assertEqual(2, ensure.call_count)
        command.assert_called_once()


class BatchPreviewTests(unittest.TestCase):
    def test_anchor_multiple_products_use_separate_reviews(self):
        def item(goods_id, minute, title="", image_count=2):
            return {
                "goods_id": goods_id,
                "title": title,
                "imgsSrc": [
                    f"https://example.com/{goods_id}-{i}.jpg"
                    for i in range(image_count)
                ],
                "time_stamp": int(
                    datetime(2026, 7, 24, 12, minute).timestamp() * 1000
                ),
                "update_time": int(
                    datetime(2026, 7, 24, 12, minute).timestamp() * 1000
                ),
                "videoUrl": "",
            }

        items = [
            item("placeholder-1", 0, image_count=1),
            item("product-1", 1, "目标商品一"),
            item("placeholder-2", 2, image_count=1),
            item("product-2", 3, "目标商品二"),
            item("placeholder-3", 4, image_count=1),
        ]
        calls = []

        def process(_supplier, _album, groups, *_args, **kwargs):
            calls.append((
                [post["goods_id"] for post in groups[0]],
                kwargs["review_id"],
                kwargs["review_label"],
                kwargs["raise_interrupt"],
            ))
            return 1

        with patch.object(pick_products, "process_groups", side_effect=process):
            count = pick_products.cmd_anchor(
                {"suppliers": {"南在南方": "album-1"}},
                {},
                {"album-1": {"items": items}},
                "南在南方",
                "目标商品",
                "07-24",
                review_prefix="03",
                review_label="第 3/3 项 · 南在南方 · 锚点 目标商品",
                raise_interrupt=True,
            )

        self.assertEqual(2, count)
        self.assertEqual(
            [
                (
                    ["product-1"], "03_01",
                    "第 3/3 项 · 南在南方 · 锚点 目标商品 · 商品 1/2",
                    True,
                ),
                (
                    ["product-2"], "03_02",
                    "第 3/3 项 · 南在南方 · 锚点 目标商品 · 商品 2/2",
                    True,
                ),
            ],
            calls,
        )

    def test_batch_preview_uses_unique_html_and_confirmation_names(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "image.jpg"
            image.write_bytes(b"image")
            prepared = [{
                "gi": 1,
                "latest_time": "2026-07-24 12:00",
                "sorted_imgs": [(image, "其他", 0, "")],
            }]
            captured = {}

            def build(supplier, products, out, **kwargs):
                captured["supplier"] = supplier
                captured["out"] = out.name
                captured.update(kwargs)

            with patch.object(pick_products, "SCRIPT_DIR", root), \
                    patch.object(pick_products, "build_classify_preview_html", side_effect=build), \
                    patch.object(pick_products, "_open_in_browser"), \
                    patch.object(pick_products.time, "time", return_value=0), \
                    patch.object(pick_products.time, "sleep"):
                result = pick_products.wait_for_classify_review(
                    "晨星外贸06",
                    prepared,
                    timeout=0,
                    review_id="02_01",
                    review_label="第 2/2 项 · 晨星外贸06 · 编码 0712c",
                )

        self.assertIsNone(result)
        self.assertEqual("分类预览_02_01.html", captured["out"])
        self.assertEqual("分类确认_02_01", captured["confirm_base"])
        self.assertEqual(
            "第 2/2 项 · 晨星外贸06 · 编码 0712c",
            captured["review_label"],
        )

    def test_preview_html_embeds_its_own_label_and_confirmation_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "image.jpg"
            image.write_bytes(b"image")
            output = root / "分类预览_01_01.html"
            pick_products.build_classify_preview_html(
                "晨星外贸06",
                [{
                    "gi": 1,
                    "latest_time": "2026-07-24 12:00",
                    "sorted_imgs": [(image, "其他", 0, "")],
                }],
                output,
                review_label="第 1/2 项 · 晨星外贸06 · 编码 0708d",
                confirm_base="分类确认_01_01",
            )
            html = output.read_text()

        self.assertIn("第 1/2 项 · 晨星外贸06 · 编码 0708d", html)
        self.assertIn('"confirmName": "分类确认_01_01.json"', html)
        self.assertIn("document.title=D.label+' · 排序预览'", html)
        self.assertIn("a.download=D.confirmName", html)

    def test_preview_html_escapes_script_closing_text_in_batch_label(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "image.jpg"
            image.write_bytes(b"image")
            output = root / "分类预览_01_01.html"
            pick_products.build_classify_preview_html(
                "晨星外贸06",
                [{
                    "gi": 1,
                    "latest_time": "2026-07-24 12:00",
                    "sorted_imgs": [(image, "其他", 0, "")],
                }],
                output,
                review_label="编码 </script><script>alert(1)</script>",
                confirm_base="分类确认_01_01",
            )
            html = output.read_text()

        self.assertNotIn("编码 </script><script>alert(1)</script>", html)
        self.assertIn(
            r"编码 \u003c/script>\u003cscript>alert(1)\u003c/script>",
            html,
        )


if __name__ == "__main__":
    unittest.main()
