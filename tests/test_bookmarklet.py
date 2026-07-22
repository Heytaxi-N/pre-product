import tempfile
import unittest
import webbrowser
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote

import pick_products


class BookmarkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.href = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a" and attrs.get("class") == "btn":
            self.href = attrs.get("href")


class BookmarkletTests(unittest.TestCase):
    def test_bookmarklet_preserves_szwego_api_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            original_file = pick_products.BOOKMARKLET_FILE
            original_dir = pick_products.SCRIPT_DIR
            original_open = webbrowser.open
            try:
                pick_products.BOOKMARKLET_FILE = tmp_path / "install_bookmark.html"
                pick_products.SCRIPT_DIR = tmp_path
                webbrowser.open = lambda *args, **kwargs: True

                pick_products.cmd_install_bookmark(
                    {"suppliers": {"测试供货商": "_d_test_album"}}
                )

                parser = BookmarkParser()
                parser.feed(pick_products.BOOKMARKLET_FILE.read_text(encoding="utf-8"))
                self.assertIsNotNone(parser.href)
                body = unquote(
                    parser.href[len("javascript:"):]
                    if parser.href.startswith("javascript:")
                    else parser.href
                )
                self.assertIn(
                    "https://www.szwego.com/album/personal/new",
                    body,
                )
                self.assertIn("&timestamp=", body)
                self.assertIn("update_time", body)
                self.assertIn("anchor_code", body)
                self.assertIn("const MAX=50", body)
                self.assertIn("incomplete", body)
                self.assertIn("missesAfterCode", body)
                self.assertIn("missesAfterCode>=2", body)
                self.assertIn("stopReason", body)
                self.assertNotIn("if(anchorCode&&items.some", body)
                self.assertNotIn("×tamp=", body)
            finally:
                pick_products.BOOKMARKLET_FILE = original_file
                pick_products.SCRIPT_DIR = original_dir
                webbrowser.open = original_open


if __name__ == "__main__":
    unittest.main()
