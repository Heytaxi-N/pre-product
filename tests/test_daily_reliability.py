import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

import pick_products


BASE_TIME = datetime(2026, 7, 24, 12)


def make_item(goods_id, publish_minute=0, update_minute=None, title=None):
    item = {
        "goods_id": goods_id,
        "title": title if title is not None else goods_id,
        "imgsSrc": [f"https://example.com/{goods_id}.jpg"],
        "time_stamp": int(
            (BASE_TIME + timedelta(minutes=publish_minute)).timestamp() * 1000
        ),
        "videoUrl": "",
    }
    if update_minute is not None:
        item["update_time"] = int(
            (BASE_TIME + timedelta(minutes=update_minute)).timestamp() * 1000
        )
    return item


class DailyReliabilityTests(unittest.TestCase):
    def test_partial_download_failure_discards_the_product(self):
        item = make_item("partial")
        item["imgsSrc"] = [
            "https://example.com/ok.jpg",
            "https://example.com/fail.jpg",
        ]

        with tempfile.TemporaryDirectory() as tmp, patch.object(
            pick_products,
            "http_get_bytes",
            side_effect=[b"ok", RuntimeError("network")],
        ):
            tmp_dir = Path(tmp) / "product"
            paths = pick_products.download_product_images([item], tmp_dir)

        self.assertEqual([], paths)
        self.assertFalse(tmp_dir.exists())

    def test_progress_dict_keeps_unprocessed_same_day_and_old_string_works(self):
        old = make_item("old", publish_minute=-24 * 60)
        done = make_item("done")
        pending = make_item("pending", publish_minute=1)
        tomorrow = make_item("tomorrow", publish_minute=24 * 60)
        items = [old, done, pending, tomorrow]

        progress = {
            "album-1": {
                "cutoff_date": "2026-07-24",
                "processed_ids": ["done"],
            }
        }
        self.assertEqual(
            ["pending", "tomorrow"],
            [
                item["goods_id"]
                for item in pick_products.filter_new_items(
                    "album-1", items, progress
                )
            ],
        )
        self.assertEqual(
            ["tomorrow"],
            [
                item["goods_id"]
                for item in pick_products.filter_new_items(
                    "album-1", items, {"album-1": "2026-07-24"}
                )
            ],
        )

    def test_process_groups_records_only_successful_goods_ids(self):
        success = make_item("success")
        failed = make_item("failed", publish_minute=1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            progress = {}

            def download(group, tmp_dir):
                if group[0]["goods_id"] == "failed":
                    return []
                tmp_dir.mkdir(parents=True, exist_ok=True)
                image = tmp_dir / "image.jpg"
                image.write_bytes(b"success")
                return [image]

            with patch.object(pick_products, "TMP_ROOT", root / "tmp"), \
                    patch.object(pick_products, "OUTPUT_DIR", root / "output"), \
                    patch.object(
                        pick_products, "download_product_images", side_effect=download
                    ), \
                    patch.object(
                        pick_products, "classify_images_ai", return_value=None
                    ), \
                    patch.object(
                        pick_products,
                        "create_product_folder",
                        return_value=root / "output" / "product",
                    ), \
                    patch.object(pick_products, "save_json") as save:
                count = pick_products.process_groups(
                    "supplier",
                    "album-1",
                    [[success], [failed]],
                    progress,
                    None,
                    {},
                    {},
                    review=False,
                )

        self.assertEqual(1, count)
        save.assert_called_once()
        album_progress = progress["album-1"]
        self.assertIsInstance(album_progress, dict)
        self.assertEqual("2026-07-24", album_progress["cutoff_date"])
        self.assertEqual(
            {"success"}, set(album_progress["processed_ids"])
        )
        self.assertEqual(
            ["failed"],
            [
                item["goods_id"]
                for item in pick_products.filter_new_items(
                    "album-1", [success, failed], progress
                )
            ],
        )

    def test_deleting_every_image_does_not_advance_progress(self):
        item = make_item("deleted")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            progress = {}

            def download(_group, tmp_dir):
                tmp_dir.mkdir(parents=True, exist_ok=True)
                image = tmp_dir / "image.jpg"
                image.write_bytes(b"image")
                return [image]

            with patch.object(pick_products, "TMP_ROOT", root / "tmp"), \
                    patch.object(
                        pick_products, "download_product_images", side_effect=download
                    ), \
                    patch.object(
                        pick_products, "classify_images_ai", return_value=None
                    ), \
                    patch.object(
                        pick_products,
                        "wait_for_classify_review",
                        return_value=[{"order": [], "sizes": []}],
                    ), \
                    patch.object(
                        pick_products, "create_product_folder"
                    ) as create_folder, \
                    patch.object(pick_products, "save_json") as save:
                count = pick_products.process_groups(
                    "supplier",
                    "album-1",
                    [[item]],
                    progress,
                    None,
                    {},
                    {},
                    review=True,
                )

        self.assertEqual(0, count)
        self.assertEqual({}, progress)
        create_folder.assert_not_called()
        save.assert_not_called()

    def test_unconfirmed_preview_does_not_create_folder_or_advance_progress(self):
        item = make_item("unconfirmed")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def download(_group, tmp_dir):
                tmp_dir.mkdir(parents=True, exist_ok=True)
                image = tmp_dir / "image.jpg"
                image.write_bytes(b"image")
                return [image]

            with patch.object(pick_products, "TMP_ROOT", root / "tmp"), \
                    patch.object(
                        pick_products, "download_product_images", side_effect=download
                    ), \
                    patch.object(
                        pick_products, "classify_images_ai", return_value=None
                    ), \
                    patch.object(
                        pick_products, "wait_for_classify_review", return_value=None
                    ), \
                    patch.object(
                        pick_products, "create_product_folder"
                    ) as create_folder:
                count = pick_products.process_groups(
                    "supplier", "album-1", [[item]], {}, None, {}, {}, review=True
                )

            self.assertEqual(0, count)
            create_folder.assert_not_called()
            self.assertFalse(any((root / "tmp").iterdir()))

    def test_feishu_failure_does_not_create_folder_or_advance_progress(self):
        item = make_item("feishu-failed")

        class BrokenFeishu:
            def create_record(self, _fields):
                raise RuntimeError("feishu unavailable")

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            image = root / "image.jpg"
            image.write_bytes(b"image")
            progress = {}
            with patch.object(
                pick_products, "download_product_images", return_value=[image]
            ), patch.object(
                pick_products, "classify_images_ai", return_value=None
            ), patch.object(
                pick_products, "create_product_folder"
            ) as create_folder, patch.object(
                pick_products, "save_json"
            ) as save:
                count = pick_products.process_groups(
                    "supplier",
                    "album-1",
                    [[item]],
                    progress,
                    BrokenFeishu(),
                    {},
                    {},
                    review=False,
                )

        self.assertEqual(0, count)
        self.assertEqual({}, progress)
        create_folder.assert_not_called()
        self.assertFalse(
            any(call.args[0] == pick_products.PROGRESS_FILE for call in save.call_args_list)
        )

    def test_group_products_ai_uses_update_order_and_gap(self):
        update_first = make_item(
            "update-first", publish_minute=120, update_minute=0
        )
        update_second = make_item(
            "update-second", publish_minute=0, update_minute=1
        )

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(
                    pick_products, "_is_placeholder", return_value=False
                ), \
                patch.object(
                    pick_products, "_ai_same_product", return_value=True
                ):
            groups = pick_products.group_products_ai(
                {}, [update_second, update_first], Path(tmp) / "cache"
            )

        self.assertEqual(
            [["update-first", "update-second"]],
            [[item["goods_id"] for item in group] for group in groups],
        )

    def test_daily_preview_uses_update_order(self):
        update_second = make_item(
            "update-second", publish_minute=0, update_minute=2
        )
        update_first = make_item(
            "update-first", publish_minute=1, update_minute=1
        )
        data = {
            "album-1": {
                "supplier": "supplier",
                "items": [update_second, update_first],
            }
        }
        captured = {}

        def build(previews, _out):
            captured["posts"] = previews[0]["posts"]

        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(pick_products, "SCRIPT_DIR", Path(tmp)), \
                patch.object(pick_products, "OUTPUT_DIR", Path(tmp) / "output"), \
                patch.object(
                    pick_products,
                    "select_suppliers",
                    return_value=[("supplier", "album-1", 2)],
                ), \
                patch.object(
                    pick_products,
                    "group_products_ai",
                    side_effect=lambda _cfg, items, _cache: [
                        [item] for item in sorted(items, key=pick_products._item_order)
                    ],
                ), \
                patch.object(
                    pick_products, "build_preview_html", side_effect=build
                ), \
                patch("webbrowser.open"):
            pick_products.cmd_preview({}, {}, data)

        self.assertEqual(
            ["update-first", "update-second"],
            [post["goods_id"] for post in captured["posts"]],
        )
        self.assertEqual(
            [update_first["update_time"], update_second["update_time"]],
            [post["update_time"] for post in captured["posts"]],
        )

    def test_daily_process_groups_uses_update_order(self):
        update_second = make_item(
            "update-second", publish_minute=0, update_minute=2
        )
        update_first = make_item(
            "update-first", publish_minute=1, update_minute=1
        )
        captured = {}

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            def download(group, tmp_dir):
                captured["order"] = [item["goods_id"] for item in group]
                tmp_dir.mkdir(parents=True, exist_ok=True)
                image = tmp_dir / "image.jpg"
                image.write_bytes(b"image")
                return [image]

            with patch.object(pick_products, "TMP_ROOT", root / "tmp"), \
                    patch.object(
                        pick_products, "download_product_images", side_effect=download
                    ), \
                    patch.object(
                        pick_products, "classify_images_ai", return_value=None
                    ), \
                    patch.object(pick_products, "create_product_folder"):
                pick_products.process_groups(
                    "supplier",
                    "album-1",
                    [[update_second, update_first]],
                    {},
                    None,
                    {},
                    {},
                    advance_progress=False,
                    review=False,
                )

        self.assertEqual(["update-first", "update-second"], captured["order"])

    def test_code_mode_still_uses_publish_time_order(self):
        publish_first = make_item(
            "publish-first", publish_minute=0, update_minute=2, title="sku"
        )
        publish_second = make_item(
            "publish-second", publish_minute=1, update_minute=1, title="sku"
        )

        with patch.object(
            pick_products, "process_groups", return_value=1
        ) as process:
            pick_products.process_supplier(
                "supplier",
                "album-1",
                [publish_second, publish_first],
                {},
                None,
                {},
                {},
                code="sku",
            )

        group = process.call_args.args[2][0]
        self.assertEqual(
            ["publish-first", "publish-second"],
            [item["goods_id"] for item in group],
        )

    def test_create_product_folder_refuses_existing_target(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            target = output / "existing"
            target.mkdir(parents=True)
            existing_image = target / "existing01.jpg"
            existing_image.write_bytes(b"old")
            (target / "keep.txt").write_text("keep")
            source = root / "source.jpg"
            source.write_bytes(b"new")

            try:
                pick_products.create_product_folder(
                    [(source, "其他", 0, "")], "existing", output
                )
            except FileExistsError:
                pass

            self.assertEqual(b"old", existing_image.read_bytes())
            self.assertEqual("keep", (target / "keep.txt").read_text())
            self.assertEqual(
                {"existing01.jpg", "keep.txt"},
                {path.name for path in target.iterdir()},
            )
            self.assertEqual({"existing"}, {path.name for path in output.iterdir()})

    def test_create_product_folder_leaves_no_temporary_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            output = root / "output"
            source = root / "source.jpg"
            source.write_bytes(b"image")

            folder = pick_products.create_product_folder(
                [(source, "其他", 0, "")], "newproduct", output
            )

            self.assertEqual(output / "newproduct", folder)
            self.assertEqual(
                b"image", (folder / "newproduct01.jpg").read_bytes()
            )
            self.assertEqual(
                {"newproduct"}, {path.name for path in output.iterdir()}
            )


if __name__ == "__main__":
    unittest.main()
