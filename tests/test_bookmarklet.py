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
            original_extension_dir = pick_products.CHROME_EXTENSION_DIR
            original_open = webbrowser.open
            try:
                pick_products.BOOKMARKLET_FILE = tmp_path / "install_bookmark.html"
                pick_products.SCRIPT_DIR = tmp_path
                pick_products.CHROME_EXTENSION_DIR = tmp_path / "chrome-extension"
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
                self.assertNotIn("all?isFilter=true", body)
                self.assertIn("const itemDate=it=>{const d=new Date(it.time_stamp)", body)
                self.assertNotIn("new Date(it.update_time||it.time_stamp)", body)
                self.assertIn("pageHasDate", body)
                self.assertIn("missesAfterDate", body)
                self.assertIn("missesAfterDate>=2", body)
                self.assertIn("stopReason='date-boundary'", body)
                self.assertNotIn("pagePastDate", body)
                self.assertIn("fetchOne(aid,pageTs)", body)
                self.assertIn("const fetchDaily=async aid=>", body)
                self.assertIn("const DAILY_MAX=50", body)
                self.assertIn("foundLatest&&misses>=2", body)
                self.assertIn("const rawLatestDate=raw.map(itemDate).sort().pop()", body)
                self.assertIn("raw.some(i=>itemDate(i)===latestDate)", body)
                self.assertIn("items:await fetchDaily(aid)", body)
                self.assertNotIn("const d=await fetchOne(aid,'');out.data", body)
                self.assertIn("const filt=arr=>arr.filter(i=>!i.isTop&&!i.forwardTime&&i.parent_goods_id===i.goods_id)", body)
                self.assertIn("const dailyClean=arr=>arr.filter(i=>!i.isTop).map(clean)", body)
                self.assertIn("const items=dailyClean(raw)", body)
                self.assertIn("(anchorDate||rangeStart)?anchorClean(rawItems):filt(rawItems)", body)
                self.assertIn("const anchorClean=arr=>arr.filter(i=>!i.isTop).map(clean)", body)
                self.assertNotIn("const anchorClean=arr=>arr.filter(i=>!i.isTop&&!i.forwardTime)", body)
                self.assertIn("&timestamp=", body)
                self.assertIn("update_time:it.update_time,", body)
                self.assertIn("anchor_code", body)
                self.assertIn("range_start", body)
                self.assertIn("range_end", body)
                self.assertIn("range_date", body)
                self.assertIn("const rangeItems=rangeDate?items.filter(i=>itemDate(i)===rangeDate):items", body)
                self.assertIn("pageHasRangeDate", body)
                self.assertIn("all.push(...rangeItems)", body)
                self.assertNotIn("pagePastRangeDate", body)
                self.assertIn("missesAfterRangeDate", body)
                self.assertIn("missesAfterRangeDate>=2", body)
                self.assertIn(
                    "if(pages===pageLimit&&hasMore&&stopReason==='end')",
                    body,
                )
                self.assertIn("const MAX=50", body)
                self.assertIn("const RANGE_MAX=50", body)
                self.assertNotIn("const RANGE_MAX=10", body)
                self.assertIn("const pageLimit=rangeDate?RANGE_MAX:MAX", body)
                self.assertIn("if(rawItems.length===0)break", body)
                self.assertNotIn("if(items.length===0)break", body)
                self.assertIn("incomplete", body)
                self.assertIn("missesAfterCode", body)
                self.assertIn("missesAfterCode>=2", body)
                self.assertIn("stopReason", body)
                self.assertIn("fullScan:false,dateWindow:!!anchorDate", body)
                self.assertIn("dateScan:!!rangeDate", body)
                self.assertNotIn("if(anchorCode&&items.some", body)
                self.assertNotIn("×tamp=", body)
            finally:
                pick_products.BOOKMARKLET_FILE = original_file
                pick_products.SCRIPT_DIR = original_dir
                pick_products.CHROME_EXTENSION_DIR = original_extension_dir
                webbrowser.open = original_open


if __name__ == "__main__":
    unittest.main()
