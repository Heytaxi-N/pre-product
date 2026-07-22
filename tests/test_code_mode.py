import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

import pick_products


def make_item(goods_id, minute):
    return {
        "goods_id": goods_id,
        "title": f"商品编码 0714c {goods_id}",
        "imgsSrc": [f"https://example.com/{goods_id}.jpg"],
        "time_stamp": int(datetime(2026, 7, 14, 12, minute).timestamp() * 1000),
        "videoUrl": "",
    }


class CodeModeTests(unittest.TestCase):
    def test_code_anchor_reports_validation_problem(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "scrape_anchor.json"
            base = {
                "albumId": "album-1",
                "items": [make_item("match", 1)],
                "anchor": {"incomplete": False, "code": "0714c"},
            }
            path.write_text(json.dumps(base))
            self.assertEqual("", pick_products._code_anchor_problem(path, "album-1", "0714c"))

            path.write_text(json.dumps({**base, "anchor": {
                "incomplete": True, "code": "0714c", "stopReason": "limit", "pages": 50,
            }}))
            self.assertIn("50 页上限", pick_products._code_anchor_problem(path, "album-1", "0714c"))

            path.write_text(json.dumps({**base, "anchor": {}}))
            self.assertIn("旧版书签", pick_products._code_anchor_problem(path, "album-1", "0714c"))

            path.write_text(json.dumps({**base, "albumId": "other-album"}))
            self.assertIn("相册不符", pick_products._code_anchor_problem(path, "album-1", "0714c"))

            path.write_text(json.dumps({**base, "anchor": {
                "incomplete": False, "code": "other-code",
            }}))
            self.assertIn("编码不符", pick_products._code_anchor_problem(path, "album-1", "0714c"))

    def test_code_mode_always_refreshes_remote_pages(self):
        with patch.object(pick_products, "ensure_data_for_date", return_value=True) as ensure:
            result = pick_products.ensure_data_for_code(
                "/tmp/scrape_all.json", {}, "晨星外贸06", "0722b"
            )

        self.assertTrue(result)
        self.assertTrue(ensure.call_args.kwargs["force_fetch"])

    def test_code_mode_rejects_regular_scrape_fallback(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            regular_scrape = tmp_path / "scrape_all.json"
            regular_scrape.write_text("{}")
            clock = iter((100, 100, 100, 102))

            with patch.object(pick_products, "_data_has_code", side_effect=(False, True)) as has_code, \
                    patch.object(pick_products, "pick_newest_download",
                                 side_effect=lambda base: regular_scrape if base == "scrape_all" else None), \
                    patch.object(pick_products.time, "time", side_effect=lambda: next(clock)), \
                    patch.object(pick_products.time, "sleep"), \
                    patch("webbrowser.open"):
                result = pick_products.ensure_data_for_date(
                    tmp_path / "local.json",
                    {"suppliers": {"晨星外贸06": "album-1"}},
                    "晨星外贸06", "", timeout=1, code="0722b", force_fetch=True,
                )

        self.assertFalse(result)
        self.assertEqual(1, has_code.call_count)

    def test_preview_embeds_video_first_frame_and_hover_player(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            video = tmp_path / "preview.mp4"
            video.write_bytes(b"video")
            output = tmp_path / "preview.html"
            prepared = [{
                "gi": 1,
                "latest_time": "2026-07-15 12:00",
                "sorted_imgs": [(video, "视频", 0, "")],
            }]

            with patch.object(
                pick_products, "_video_frame_data_url",
                return_value="data:image/jpeg;base64,first-frame",
            ):
                pick_products.build_classify_preview_html("测试供货商", prepared, output)

            html = output.read_text()
            self.assertIn("data:image/jpeg;base64,first-frame", html)
            self.assertIn(video.resolve().as_uri(), html)
            self.assertIn('id="hover-preview"', html)
            self.assertIn("<video", html)
            self.assertIn("previewBtn.addEventListener('mouseenter'", html)
            self.assertIn("previewBtn.addEventListener('focus'", html)
            self.assertIn("c.draggable=false", html)
            self.assertNotIn("mediaEl.addEventListener('mouseenter'", html)

    def test_code_matches_are_one_ordered_product_with_review(self):
        newer = make_item("newer", 2)
        older = make_item("older", 1)

        with patch.object(pick_products, "group_products_ai") as ai_group, \
                patch.object(pick_products, "process_groups", return_value=1) as process:
            result = pick_products.process_supplier(
                "晨星外贸06", "album-1", [newer, older], {}, None, {}, {}, code="0714c"
            )

        self.assertEqual(1, result)
        ai_group.assert_not_called()
        groups = process.call_args.args[2]
        self.assertEqual([["older", "newer"]], [
            [item["goods_id"] for item in group] for group in groups
        ])
        self.assertFalse(process.call_args.kwargs["advance_progress"])
        self.assertTrue(process.call_args.kwargs["review"])

    def test_classification_api_failure_returns_fallback_signal(self):
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "image.jpg"
            image.write_bytes(b"not-a-real-image")
            with patch.object(pick_products, "http_post_json", side_effect=RuntimeError("401")):
                result = pick_products.classify_images_ai(
                    {"base_url": "https://example.com", "api_key": "expired"}, [image]
                )
        self.assertIsNone(result)

    def test_fallback_keeps_original_old_to_new_image_order(self):
        older = make_item("older", 1)
        newer = make_item("newer", 2)
        with tempfile.TemporaryDirectory() as tmp:
            old_image = Path(tmp) / "old.jpg"
            new_image = Path(tmp) / "new.jpg"
            old_image.write_bytes(b"old")
            new_image.write_bytes(b"new")
            captured = {}

            def fake_download(items, _tmp_dir):
                captured["posts"] = [item["goods_id"] for item in items]
                return [old_image, new_image]

            def fake_preview(_supplier, prepared):
                captured["preview"] = prepared[0]["sorted_imgs"]
                return None

            def fake_create(final, *_args):
                captured["final"] = final

            with patch.object(pick_products, "download_product_images", fake_download), \
                    patch.object(pick_products, "classify_images_ai", return_value=None), \
                    patch.object(pick_products, "wait_for_classify_review", fake_preview), \
                    patch.object(pick_products, "create_product_folder", fake_create):
                count = pick_products.process_groups(
                    "晨星外贸06", "album-1", [[newer, older]], {}, None, {}, {},
                    advance_progress=False, review=True,
                )

        self.assertEqual(1, count)
        self.assertEqual(["older", "newer"], captured["posts"])
        self.assertEqual([old_image, new_image], [item[0] for item in captured["preview"]])
        self.assertEqual([old_image, new_image], [item[0] for item in captured["final"]])


if __name__ == "__main__":
    unittest.main()
