import unittest
import os
from pathlib import Path
from tempfile import TemporaryDirectory

from PIL import Image

import pick_products


class WechatNoteTests(unittest.TestCase):
    def test_loads_text_and_child_page_images_from_latest_cache(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            cache = root / "wxid_test" / "business" / "favorite" / "temp"
            cache.mkdir(parents=True)
            html_path = cache / "note.htm"
            html_path.write_text(
                "<p>主商品N813</p><p>颜色：卡其/浅绿</p>"
                "<hr><p>上身素材参考</p><div><object data-type='2' id='WeNote_17'></object></div>",
                encoding="utf-8",
            )
            for suffix in ("0002", "0010", "0001"):
                (cache / f"微信图片_20260805104017_{suffix}.jpg").write_bytes(
                    suffix.encode()
                )
            old = cache / "微信图片_20250101000000_0001.jpg"
            old.write_bytes(b"old")
            os.utime(old, (1, 1))

            note = pick_products.load_wechat_note(root)

            self.assertIn("主商品N813", note["item"]["title"])
            self.assertIn("上身素材参考", note["item"]["title"])
            self.assertEqual(
                ["微信图片_20260805104017_0001.jpg",
                 "微信图片_20260805104017_0002.jpg"],
                [Path(path).name for path in note["item"]["imgsSrc"]],
            )
            self.assertTrue(note["item"]["goods_id"].startswith("wx_"))

    def test_download_product_images_accepts_local_cache_paths(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / "微信图片_20260805104017_0001.jpg"
            source.write_bytes(b"image")
            output = root / "out"

            paths = pick_products.download_product_images([{
                "goods_id": "wx_local",
                "imgsSrc": [str(source)],
            }], output)

            self.assertEqual(1, len(paths))
            self.assertEqual(b"image", paths[0].read_bytes())

    def test_removes_wechat_thumbnail_when_original_follows(self):
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            small = root / "微信图片_20260805104017_2708.jpg"
            large = root / "微信图片_20260805104017_2710.jpg"
            Image.new("RGB", (424, 424), "red").save(small)
            Image.new("RGB", (1280, 1280), "red").save(large)

            kept = pick_products._remove_wechat_thumbnail_pairs([small, large])

            self.assertEqual([large], kept)


if __name__ == "__main__":
    unittest.main()
