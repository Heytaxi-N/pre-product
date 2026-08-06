"""
微购相册挑品脚本 — 从供货商拉取产品、按条目顺序下载图片、建文件夹、写飞书
用法:
  # 让 Claude Code 用浏览器抓当天上新, 保存为 scrape.json (无需登录)
  # 然后运行:
  SCRAPE_JSON=~/Downloads/scrape.json python3 pick_products.py
"""
import json, math, os, sys, time, shutil, base64, hashlib, tempfile, re, threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime
from html.parser import HTMLParser
import urllib.request, urllib.parse, urllib.error

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / (sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].endswith(".json") else "config.json")
PROGRESS_FILE = SCRIPT_DIR / "progress.json"
FEISHU_PENDING_FILE = SCRIPT_DIR / "feishu_pending.json"
WEIDIAN_CATEGORIES_FILE = SCRIPT_DIR / "data" / "weidian_categories.json"
WEIDIAN_MODELS_FILE = SCRIPT_DIR / "data" / "weidian_models.json"
LOCAL_CATEGORY_KEYWORDS_FILE = SCRIPT_DIR / "data" / "local_category_keywords.json"
OUTPUT_DIR = Path("/Users/nick/Downloads/weidian_products-main/商品图")
WECHAT_NOTE_TEMP_ROOT = Path(
    "/Users/nick/Library/Containers/com.tencent.xinWeiXin2/Data/Documents/xwechat_files"
)
TMP_ROOT = Path(tempfile.gettempdir()) / "weidian_pick"  # 临时/缓存, 不落在商品图里
MAX_PRODUCTS = int(os.environ.get("MAX_PRODUCTS", "0"))  # 0 = 不限, 也可 config.json 里配 defaults.max_products
BOOKMARKLET_FILE = SCRIPT_DIR / "install_bookmark.html"
CHROME_EXTENSION_DIR = SCRIPT_DIR / "chrome-extension"
GROUP_TIME_GAP = 120  # 秒: 帖子时间间隔 < 此值归为同一产品
_PROGRESS_LOCK = threading.Lock()


def _project_data_dir():
    return SCRIPT_DIR / "data"


# ── HTTP 会话 ────────────────────────────────────────
def http_get_bytes(url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()

def http_post_json(url, payload, headers=None):
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=hdr, method="POST")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def http_put_json(url, payload, headers=None):
    hdr = {"Content-Type": "application/json"}
    if headers:
        hdr.update(headers)
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=hdr, method="PUT")
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read())

def http_get_json(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


# ── 产品分组 (AI 图文判断) ───────────────────────────
GROUP_MAX_GAP = 1800  # 秒: 间隔超过此值直接判为不同产品(省 AI 调用)

_VIDEO_EXT = (".mp4", ".mov", ".avi", ".webm", ".mkv")

def _looks_like_image(url):
    """URL 是否可当作图片: 非视频扩展名, 或视频扩展名但带七牛云截帧参数(?vframe/jpg/...)"""
    path = url.split("?")[0].lower()
    if not path.endswith(_VIDEO_EXT):
        return True
    return "vframe" in url and "jpg" in url  # 视频截帧

def _ensure_first_image(item, cache_dir):
    """下载帖子第一张真实图片(跳过纯视频, 允许视频截帧)到缓存. 全无可用则返回 None."""
    imgs = item.get("imgsSrc") or []
    img_url = next((u for u in imgs if _looks_like_image(u)), None)
    if not img_url:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    # 下载用完整 URL (保留 ?vframe 参数才能拿到截帧); 缓存名统一 .jpg 避免 mp4 后缀误导
    is_vframe = "vframe" in img_url
    ext = ".jpg" if is_vframe else (Path(img_url.split("?")[0]).suffix or ".jpg")
    dest = cache_dir / f"{item['goods_id'][-10:]}{ext}"
    if not dest.exists():
        try:
            dest.write_bytes(http_get_bytes(img_url if is_vframe else img_url.split("?")[0]))
        except Exception:
            return None
    return dest

def _ai_same_product(ai_cfg, a, b, cache_dir):
    """AI 判断两帖是否同一件商品. None 表示调用不可用, 交给上层显式兜底."""
    base_url = (ai_cfg or {}).get("base_url", "").rstrip("/")
    api_key = (ai_cfg or {}).get("api_key", "")
    model = (ai_cfg or {}).get("model", "qwen3-vl-flash")
    if not (base_url and api_key):
        return None
    ia, ib = _ensure_first_image(a, cache_dir), _ensure_first_image(b, cache_dir)
    if not ia or not ib:
        return None
    ta = (a.get("title") or "").replace("\n", " ")[:120]
    tb = (b.get("title") or "").replace("\n", " ")[:120]
    prompt = (
        "下面是微商相册里两个帖子的主图和文案。判断它们是否属于【完全同一件商品SKU】。\n"
        "严格标准 — 同一件商品必须满足:\n"
        "  1) 品牌一致 (如都是凯乐石/都是lululemon)\n"
        "  2) 品类完全一致 (裙裤 vs 短裤 vs 长裤 vs 上衣 — 不能混)\n"
        "  3) 性别/受众一致 (男款/女款/男女同款 — 不能混)\n"
        "  4) 款式/系列一致 (同一版型或同一系列, 如都是'户外速干裙裤系列')\n"
        "只有全部满足才算同一件商品(允许不同颜色/角度/是价格图/尺码表/模特图/细节图/面料介绍)。\n"
        "只要有一条不满足(比如都是凯乐石运动裤但一个男款短裤一个女款裙裤), 判为不同商品。\n"
        f"帖子A文案: {ta}\n帖子B文案: {tb}\n"
        "只回复一个字: 是 或 否"
    )
    try:
        def durl(p):
            mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
            return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"
        payload = {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": durl(ia)}},
            {"type": "image_url", "image_url": {"url": durl(ib)}},
            {"type": "text", "text": prompt},
        ]}]}
        resp = http_post_json(f"{base_url}/chat/completions", payload,
                              headers={"Authorization": f"Bearer {api_key}"})
        ans = resp["choices"][0]["message"]["content"].strip()
        return ans.startswith("是") or "是" in ans[:3]
    except Exception:
        return None

def _is_placeholder(ai_cfg, item, cache_dir):
    """判断某帖是否'与服装无关的占位/分割图'.
    结构性前置: 必须 1 图 + 空文案 + 无视频. 不满足 → 一定不是占位图, 直接 False (免 AI 调用).
    满足后再用 AI 视觉确认是不是"与服装无关".
    三态返回: True 占位 / False 商品 / None 调用失败.
    """
    imgs = item.get("imgsSrc") or []
    title = (item.get("title") or "").strip()
    has_video = bool(item.get("videoUrl") or item.get("videoURL"))
    if len(imgs) != 1 or title or has_video:
        return False  # 结构不符 → 一定不是占位, 免 AI
    base_url = (ai_cfg or {}).get("base_url", "").rstrip("/")
    api_key = (ai_cfg or {}).get("api_key", "")
    model = (ai_cfg or {}).get("model", "qwen3-vl-flash")
    if not (base_url and api_key):
        return None
    ip = _ensure_first_image(item, cache_dir)
    if not ip:
        return None
    try:
        mime = "image/png" if ip.suffix.lower() == ".png" else "image/jpeg"
        durl = f"data:{mime};base64,{base64.b64encode(ip.read_bytes()).decode()}"
    except OSError:
        return None
    prompt = (
        "这是微商相册里的一张图。判断它是【服装商品图】还是【占位/分割图】。\n"
        "占位/分割图: 与具体服装商品无关, 用来隔开不同产品的图, 例如纯文字提示图、"
        "logo图、下单引导图、二维码图、装饰海报、空白/背景图等。\n"
        "服装商品图: 能看到具体的衣服/裤子/鞋帽/包等商品(平铺、挂拍、模特、细节都算)。\n"
        "只回复一个字: 占位 或 商品"
    )
    payload = {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": durl}},
        {"type": "text", "text": prompt},
    ]}]}
    # 偶发抖动/限流 → 重试, 每次退避递增. 全失败才 return None
    last_err = None
    for attempt in range(3):
        try:
            resp = http_post_json(f"{base_url}/chat/completions", payload,
                                  headers={"Authorization": f"Bearer {api_key}"})
            ans = resp["choices"][0]["message"]["content"].strip()
            return "占位" in ans[:4]
        except Exception as e:
            last_err = e
            if attempt < 2:
                print(f"    ⚠ AI 调用失败 (第{attempt+1}次): {str(e)[:80]}, 等 {(attempt+1)*0.8:.1f}s 后重试")
                time.sleep((attempt + 1) * 0.8)
    print(f"    ❌ 重试 3 次仍失败: {str(last_err)[:100]}")
    return None

def group_products_ai(ai_cfg, items, cache_dir):
    """先用占位图(分割线)切硬边界并剔除占位帖; 段内再跑相邻 AI 图文分组.
    带占位图的供货商靠占位图切干净; 不带的沿用原逻辑.
    """
    items = sorted(items, key=_item_order)
    if len(items) <= 1:
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        return [list(items)] if items else []

    # 第一遍: 占位判定, 按占位帖切成若干段(占位帖本身丢弃, 不下载)
    # ponytail: 每帖一次占位判定, 若调用量成问题再合并进分组调用
    segments = [[]]
    for it in items:
        placeholder = _is_placeholder(ai_cfg, it, cache_dir)
        if placeholder is None:
            shutil.rmtree(cache_dir, ignore_errors=True)
            raise RuntimeError("AI 占位图判断失败")
        if placeholder:
            if segments[-1]:
                segments.append([])   # 遇占位图 → 起新段
        else:
            segments[-1].append(it)
    segments = [s for s in segments if s]

    # 第二遍: 每段内部再跑相邻 AI 图文分组
    groups = []
    for seg in segments:
        if len(seg) == 1:
            groups.append(seg)
            continue
        cur_groups = [[seg[0]]]
        for cur in seg[1:]:
            prev = cur_groups[-1][-1]
            gap = (_item_order(cur) - _item_order(prev)) / 1000
            same_product = (
                _ai_same_product(ai_cfg, prev, cur, cache_dir)
                if gap <= GROUP_MAX_GAP else False
            )
            if same_product is None:
                shutil.rmtree(cache_dir, ignore_errors=True)
                raise RuntimeError("AI 商品分组失败")
            if same_product:
                cur_groups[-1].append(cur)
            else:
                cur_groups.append([cur])
        groups.extend(cur_groups)

    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)  # 用完删首图缓存
    return groups


# ── 图片下载 ─────────────────────────────────────────
def download_product_images(product_items, tmp_dir):
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    failed = False
    for item in product_items:
        for i, url in enumerate(item["imgsSrc"]):
            clean_url = url.split("?")[0]
            ext = Path(clean_url).suffix or ".jpg"
            fname = f"{item['goods_id'][-8:]}_{i:02d}{ext}"
            dest = tmp_dir / fname
            if not dest.exists():
                try:
                    source_path = Path(clean_url)
                    if source_path.is_file():
                        shutil.copy2(source_path, dest)
                    else:
                        dest.write_bytes(http_get_bytes(clean_url))
                except Exception as e:
                    print(f"  下载失败 {clean_url}: {e}")
                    failed = True
                    continue
            paths.append(dest)
        video_url = item.get("videoUrl") or item.get("videoURL")
        if video_url:
            vurl = video_url.split("?")[0]
            ext = Path(vurl).suffix or ".mp4"
            dest = tmp_dir / f"{item['goods_id'][-8:]}_video{ext}"
            if not dest.exists():
                try:
                    dest.write_bytes(http_get_bytes(vurl))
                except Exception as e:
                    print(f"  视频下载失败: {e}")
                    failed = True
                    continue
            paths.append(dest)
    if failed:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        return []
    return paths


# ── 微信笔记缓存输入 ─────────────────────────────────
class _WxNoteTextParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []

    def _newline(self):
        if self.parts and self.parts[-1] != "\n":
            self.parts.append("\n")

    def handle_starttag(self, tag, attrs):
        if tag in ("br", "hr", "p", "div", "li"):
            self._newline()

    def handle_endtag(self, tag):
        if tag in ("p", "div", "li"):
            self._newline()

    def handle_data(self, data):
        self.parts.append(data.replace("\xa0", " "))

    def text(self):
        lines = [re.sub(r"[ \t]+", " ", line).strip()
                 for line in "".join(self.parts).splitlines()]
        return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


_WX_IMAGE_RE = re.compile(r"^微信图片_(\d+)_(\d+)\.(?:jpg|jpeg|png|webp)$", re.I)


def _wechat_cache_mtime(html_path):
    mtimes = [html_path.stat().st_mtime]
    mtimes.extend(
        path.stat().st_mtime
        for path in html_path.parent.iterdir()
        if _WX_IMAGE_RE.match(path.name) and path.is_file()
    )
    return max(mtimes)


def _jpeg_size(path):
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    sof_markers = set(range(0xC0, 0xC4)) | set(range(0xC5, 0xC8)) | set(range(0xC9, 0xCC)) | set(range(0xCD, 0xD0))
    while index + 3 < len(data):
        if data[index] != 0xFF:
            index += 1
            continue
        while index < len(data) and data[index] == 0xFF:
            index += 1
        marker = data[index]
        index += 1
        if marker in (0xD8, 0xD9):
            continue
        length = int.from_bytes(data[index:index + 2], "big")
        if marker in sof_markers and index + 7 <= len(data):
            height = int.from_bytes(data[index + 3:index + 5], "big")
            width = int.from_bytes(data[index + 5:index + 7], "big")
            return width, height
        index += length
    return None


def _remove_wechat_thumbnail_pairs(images):
    kept = []
    index = 0
    while index < len(images):
        current = _WX_IMAGE_RE.match(images[index].name)
        following = _WX_IMAGE_RE.match(images[index + 1].name) if index + 1 < len(images) else None
        if current and following and int(following.group(2)) - int(current.group(2)) == 2:
            small, large = _jpeg_size(images[index]), _jpeg_size(images[index + 1])
            if small and large:
                small_ratio = small[0] / small[1]
                large_ratio = large[0] / large[1]
                if small[0] <= 424 and large[0] > small[0] and abs(small_ratio - large_ratio) < 0.02:
                    kept.append(images[index + 1])
                    index += 2
                    continue
        kept.append(images[index])
        index += 1
    return kept


def _wechat_images_for_html(html_path, expected_image_count=0):
    images = []
    for path in html_path.parent.iterdir():
        match = _WX_IMAGE_RE.match(path.name)
        if match and path.is_file():
            images.append(path)
    if not images:
        raise RuntimeError(f"笔记缓存没有找到配套图片: {html_path}")
    by_mtime = sorted(images, key=lambda path: path.stat().st_mtime)
    bursts = [[]]
    for path in by_mtime:
        if bursts[-1] and path.stat().st_mtime - bursts[-1][-1].stat().st_mtime > 60:
            bursts.append([])
        bursts[-1].append(path)
    images = sorted(
        bursts[-1],
        key=lambda path: tuple(int(value) for value in _WX_IMAGE_RE.match(path.name).groups()),
    )
    if expected_image_count and len(images) >= expected_image_count * 2:
        # ponytail: 微信当前缓存按对象顺序先写缩略图/原图, 对象映射未公开时用 HTML 数量截断历史尾巴。
        images = images[:expected_image_count * 2]
    # ponytail: 60 秒素材时间簇是当前 NoteCache 的最小可验证边界;若缓存暴露对象到图片映射再替换启发式。
    images = sorted(
        images,
        key=lambda path: tuple(int(value) for value in _WX_IMAGE_RE.match(path.name).groups()),
    )
    return _remove_wechat_thumbnail_pairs(images)


def load_wechat_note(cache_root=WECHAT_NOTE_TEMP_ROOT):
    """读取当前微信笔记对应的最新本地缓存;主笔记缓存已包含其子页面素材."""
    html_paths = sorted(
        Path(cache_root).glob("*/business/favorite/temp/*.htm"),
        key=_wechat_cache_mtime,
        reverse=True,
    )
    for html_path in html_paths:
        try:
            html = html_path.read_text(encoding="utf-8")
            expected_image_count = len(re.findall(
                r"<object\b[^>]*data-type=[\"']2[\"'][^>]*>", html,
                re.I,
            ))
            images = _wechat_images_for_html(html_path, expected_image_count)
        except (OSError, UnicodeError, RuntimeError):
            continue
        parser = _WxNoteTextParser()
        parser.feed(html)
        modified = max([html_path.stat().st_mtime] + [p.stat().st_mtime for p in images])
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()[:20]
        stamp = int(modified * 1000)
        return {
            "html_path": html_path,
            "item": {
                "goods_id": f"wx_{digest}",
                "title": parser.text(),
                "imgsSrc": [str(path) for path in images],
                "time_stamp": stamp,
                "update_time": stamp,
            },
        }
    raise RuntimeError(f"没有找到可用微信笔记缓存: {cache_root}")


# ── 旧版图片分类能力（默认流程不调用） ─────────────────────
CATEGORIES = ["合图", "价格图", "产品包装图", "模特图", "官方介绍图", "细节图", "尺码表", "其他"]

# 常见服饰颜色词(复合色在前, 便于优先匹配整词)
COLOR_WORDS = [
    "岩灰绿", "雾霾蓝", "克莱因蓝", "藏青", "墨绿", "军绿", "橄榄绿", "牛仔蓝", "浅蓝", "深蓝",
    "浅灰", "深灰", "浅绿", "深绿", "米白", "米色", "杏色", "驼色", "卡其", "咖啡", "焦糖",
    "奶茶", "香槟", "裸粉", "豆沙", "酒红", "枣红", "姜黄",
    "黑色", "白色", "灰色", "红色", "橙色", "黄色", "绿色", "蓝色", "紫色", "粉色", "棕色", "银色", "金色",
    "黑", "白", "灰", "红", "橙", "黄", "绿", "青", "蓝", "紫", "粉", "棕", "米", "驼",
]

def extract_colors(text):
    """从文案抽出出现过的颜色词(复合色优先, 去掉被包含的子词)。用于约束按色分组, 减少 AI 过度细分。
    ponytail: 词表启发式; 漏词就在 COLOR_WORDS 里补。"""
    found = []
    for w in COLOR_WORDS:
        if w in (text or "") and not any((w in f or f in w) for f in found):
            found.append(w)
    return found

def classify_images_ai(ai_config, image_paths, palette=None):
    """OpenAI 兼容视觉 API: 返回 [(path, category, score, color), ...], API 不可用时返回 None.
    score: 合图/模特图 1-5(选封面/挑最佳), 其它类 0.
    color: 服装主色简称, 只用于细节图按色分组; 不适用为空串.
    palette: 文案里抽出的颜色列表, 非空时约束 AI 只能从中选色(避免把2个色判成4个)。"""
    base_url = (ai_config or {}).get("base_url", "").rstrip("/")
    api_key = (ai_config or {}).get("api_key", "")
    model = (ai_config or {}).get("model", "qwen3-vl-flash")
    if not api_key or not base_url:
        print("  ⚠ 未配置 ai_vision, 按原始时间顺序处理")
        return None

    if palette:
        color_line = ("color: 该商品文案里的颜色只有[" + "/".join(palette)
                      + "], 请从中选该图最接近的一个填入;实在都不像才给空串")
    else:
        color_line = "color: 该图服装的主色简称(如 白/黑/灰/岩灰绿/卡其/藏青),看不清或不适用给空串"
    prompt = (
        "这是服装产品图,请严格分类。只回复 JSON,格式: {\"cat\":\"类别\",\"score\":数字,\"color\":\"主色\"}\n"
        "类别按优先级判断:\n"
        "1) 尺码表: 含尺码数据的数字表格/尺寸数据表(即使背景有商品)\n"
        "2) 价格图: 官网/电商店铺(如天猫/淘宝)的价格截图(带价格数字);商品吊牌/价格牌不算价格图\n"
        "3) 产品包装图: 主体是商品包装(盒子/牛皮纸/包装袋/防尘袋/吊牌包装等),而非衣物本身\n"
        "4) 模特图: 画面中有真人的身体部位(手/脚/腿/上身/全身),不论露不露脸\n"
        "5) 官方介绍图: 图文排版的卖点/功能/面料/工艺说明这类官方宣传图(通常带成段文字, 非实拍穿搭)\n"
        "6) 合图: 无真人,拍到完整的商品整体,且画面里有2件以上的相同款不同色/不同版本平铺或挂拍(多色)\n"
        "7) 细节图: 无真人的单张商品展示——整件单色的平铺/挂拍(非多色), 或局部特写(腰头/口袋/拉链/logo/标签/吊牌/面料/走线/裤脚等)\n"
        "8) 其他: 都不符合(如封面海报/纯背景图/无关配图)\n"
        "重要:只有多色平铺挂拍才算合图,单色整件归细节图;带整段文字说明的官方图优先归官方介绍图;主体是包装物的归产品包装图;吊牌归细节图不归价格图。\n"
        "score: 仅当 cat=合图 或 模特图 时给 1-5 分(实拍、清晰、美观、整件可见/信息量大越高);其它类均为 0\n"
        + color_line
    )

    results = []
    for img_path in image_paths:
        if img_path.suffix.lower() in (".mp4", ".mov", ".avi"):
            results.append((img_path, "视频", 0, ""))
            continue
        img_b64 = base64.b64encode(img_path.read_bytes()).decode()
        mime = "image/png" if img_path.suffix.lower() == ".png" else "image/jpeg"
        payload = {
            "model": model,
            "max_tokens": 80,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{img_b64}"}},
                    {"type": "text", "text": prompt},
                ]
            }]
        }
        try:
            resp = http_post_json(f"{base_url}/chat/completions", payload,
                                  headers={"Authorization": f"Bearer {api_key}"})
            text = resp["choices"][0]["message"]["content"].strip()
            # 提取 JSON
            start = text.find("{"); end = text.rfind("}")
            parsed = json.loads(text[start:end+1]) if start >= 0 else {}
            cat = parsed.get("cat", "其他")
            if cat not in CATEGORIES:
                cat = "其他"
            score = int(parsed.get("score", 0) or 0)
            color = str(parsed.get("color", "") or "").strip()
            # 兜底: AI 若没照调色板返回, 就近吸附到文案里的颜色
            if palette and color and color not in palette:
                color = next((c for c in palette if c in color or color in c), color)
            results.append((img_path, cat, score, color))
            tag = f"[{cat}" + (f" 分{score}" if cat in ("合图", "模特图") else "") \
                + (f" {color}" if cat == "细节图" and color else "") + "]"
            print(f"    {img_path.name} → {tag}")
        except Exception as e:
            print(f"    {img_path.name} → 分类失败({e}), 全部按原始时间顺序处理")
            return None
        time.sleep(0.15)

    return results


# ── 商品字段自动填充 ─────────────────────────────────
def load_weidian_categories(path=WEIDIAN_CATEGORIES_FILE):
    """读取微店分类缓存, 返回扁平化的 {name, cate_id, parent_id} 列表."""
    raw = load_json_or(Path(path), [])
    rows = []
    for parent in raw if isinstance(raw, list) else raw.get("cateInfos", []):
        if not isinstance(parent, dict):
            continue
        rows.append({
            "name": str(parent.get("cateName", "")).strip(),
            "cate_id": parent.get("cateId"),
            "parent_id": parent.get("parentId", 0),
        })
        for child in parent.get("children", []):
            if isinstance(child, dict):
                rows.append({
                    "name": str(child.get("cateName", "")).strip(),
                    "cate_id": child.get("cateId"),
                    "parent_id": child.get("parentId", parent.get("cateId")),
                })
    return [row for row in rows if row["name"]]


def _category_key(text):
    return re.sub(r"[\s【】\[\]（）()、，,/\\_\-]+", "", str(text or "")).lower()


def build_feishu_category_options(categories=None, prefix="品牌分类-"):
    """按微店分类树生成飞书分类选项, 品牌子项加前缀并去重."""
    categories = categories or load_weidian_categories()
    excluded = {"未分类", "品牌分类"}
    options = []
    seen = set()
    for row in categories:
        name = str(row.get("name", "")).strip()
        if not name or name in excluded:
            continue
        option = f"{prefix}{name}" if row.get("parent_id") else name
        if option not in seen:
            seen.add(option)
            options.append(option)
    return options


def load_local_category_keywords(path=LOCAL_CATEGORY_KEYWORDS_FILE):
    raw = load_json_or(Path(path), {})
    keywords = raw.get("keywords", {}) if isinstance(raw, dict) else {}
    return {
        str(name): [str(word).strip() for word in words if str(word).strip()]
        for name, words in keywords.items() if isinstance(words, list)
    }


def _classification_head(text):
    """只取商品标题段, 避免把搭配说明当成商品种类."""
    source = str(text or "").strip()
    if not source:
        return ""
    return re.split(r"[\n\r，。；;|｜]", source, maxsplit=1)[0][:160]


def _category_name(categories, prefix):
    return next(
        (row["name"] for row in categories
         if row["name"].startswith(prefix)),
        None,
    )


def _category_brand_aliases(name):
    aliases = {
        "始祖鸟": ("始祖鸟", "arcteryx", "arc'teryx", "鸟家"),
        "北极狐FJALL": ("北极狐", "fjallraven", "fjall"),
    }
    return aliases.get(name, (name,))


def _fallback_categories(text, categories, local_keywords=None):
    """按商品标题推断结构化分类; 品牌始终有可用兜底值."""
    head = _classification_head(text)
    selected = set()
    kind_patterns = {
        "【上装】": (
            r"防晒衣|皮肤衣|冲锋衣|夹克|外套|羽绒|卫衣|t恤|短袖|长袖|衬衫|抓绒|软壳|"
            r"风衣|棉服|马甲|背心|上衣",
        ),
        "【下装】": (r"长裤|短裤|裤子|裙子|半身裙|内裤",),
        "【鞋子】": (r"登山鞋|溯溪鞋|跑山鞋|凉鞋|拖鞋|鞋子|鞋",),
        "【配件】": (
            r"双肩包|背包|腰包|斜挎包|胸包|帽子|棒球帽|渔夫帽|防晒帽|袜子|腰带|"
            r"手套|围巾|水壶|水杯|眼镜",
        ),
    }
    kind_hits = []
    for prefix, patterns in kind_patterns.items():
        score = sum(bool(re.search(pattern, head, re.I)) for pattern in patterns)
        if score:
            kind_hits.append((score, prefix))
    if kind_hits:
        _, kind_prefix = max(kind_hits, key=lambda item: item[0])
        kind = _category_name(categories, kind_prefix)
        if kind:
            selected.add(kind)

    gender = _category_name(categories, "【男装】")
    female = _category_name(categories, "【女装】")
    if re.search(r"男女同款|男装|男款|男士|男生|男性|猛男", head, re.I):
        if gender:
            selected.add(gender)
    if re.search(r"男女同款|女装|女款|女士|女生|女性|女神|美女", head, re.I):
        if female:
            selected.add(female)

    if re.search(r"特价|折扣|秒杀|清仓|处理价|促销|优惠", head, re.I):
        sale = _category_name(categories, "【特价区】")
        if sale:
            selected.add(sale)

    brand_root = next((row for row in categories if row["name"] == "品牌分类"), None)
    brand_rows = [row for row in categories
                  if brand_root and row.get("parent_id") == brand_root.get("cate_id")]
    brand = next(
        (row["name"] for row in brand_rows
         if any(_category_key(alias) in _category_key(head)
                for alias in _category_brand_aliases(row["name"]))),
        None,
    )
    if not brand:
        brand = next((row["name"] for row in brand_rows if "其他大牌" in row["name"]), None)
    if brand:
        selected.add(brand)

    return [row["name"] for row in categories if row["name"] in selected]


def match_weidian_categories(text, ai_config, categories=None):
    """按商品种类、性别、营销和品牌生成分类."""
    categories = categories or load_weidian_categories()
    return _fallback_categories(text, categories)


def load_weidian_models(path=WEIDIAN_MODELS_FILE):
    """读取微店型号缓存, 返回 [{name, values}] 白名单."""
    raw = load_json_or(Path(path), [])
    source = raw.get("attr_list", []) if isinstance(raw, dict) else raw
    rows = []
    seen = set()
    for item in source if isinstance(source, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("attr_title", item.get("name", ""))).strip()
        values = item.get("attr_values", item.get("values", []))
        if not name or not isinstance(values, list):
            continue
        key = _category_key(name)
        if key in seen:
            continue
        seen.add(key)
        rows.append({"name": name, "values": [str(v).strip() for v in values if str(v).strip()]})
    return rows


def _model_value_key(value):
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _sort_model_values(group_name, values):
    if not any(marker in group_name for marker in ("尺码", "鞋码", "码数")):
        return list(values)
    order = {
        "均码": 0, "通码": 0, "自由码": 0,
        "XXS": 1, "XS": 2, "S": 3, "M": 4, "L": 5, "XL": 6,
    }

    def key(item):
        value = re.sub(r"\s+", "", str(item).upper())
        if value in order:
            return order[value]
        match = re.fullmatch(r"(\d+)XL", value)
        if match:
            return 6 + int(match.group(1))
        if re.fullmatch(r"\d+(?:\.\d+)?", value):
            return 100 + float(value)
        return 1000

    return [value for _, value in sorted(enumerate(values), key=lambda item: (key(item[1]), item[0]))]


def _explicit_color_alias(text, alias):
    if len(alias) > 1:
        return alias in text
    return bool(
        re.search(
            rf"(?<![\u4e00-\u9fff]){re.escape(alias)}(?![\u4e00-\u9fff])",
            text,
        )
        or re.search(rf"[深浅淡亮暗]{re.escape(alias)}色?", text)
    )


def _covered_by_longer_value(text, value, values):
    spans = [match.span() for match in re.finditer(re.escape(value), text)]
    longer_spans = [
        match.span()
        for other in values
        if other != value and value in other
        for match in re.finditer(re.escape(other), text)
    ]
    return bool(spans) and all(
        any(start >= left and end <= right for left, right in longer_spans)
        for start, end in spans
    )


_BASE_COLOR_ALIASES = {
    "红色": ("红", "玫瑰", "桃红", "酒红", "大红", "橙红", "红褐", "梅紫红"),
    "橙色": ("橙", "橘"),
    "黄色": ("黄", "姜黄", "芥末", "柠檬"),
    "绿色": ("绿", "苔藓", "抹茶", "豆沙绿"),
    "蓝色": ("蓝", "藏青", "宝蓝", "湖蓝"),
    "紫色": ("紫", "梅紫", "薰衣草"),
    "粉色": ("粉", "肉粉"),
    "黑色": ("黑", "墨黑"),
    "白色": ("白", "骨白", "本白", "象牙白"),
    "灰色": ("灰", "银灰"),
    "棕色": ("棕", "咖", "褐"),
    "米色": ("米", "杏", "香草"),
    "卡其": ("卡其", "土驼"),
}


def _resolve_model_value(category, value, allowed):
    by_key = {_model_value_key(item): item for item in allowed}
    exact = by_key.get(_model_value_key(value))
    if exact:
        return [exact]
    if "颜色" not in category:
        return []
    text = str(value or "")
    result = []
    for base, aliases in _BASE_COLOR_ALIASES.items():
        if any(alias in text for alias in aliases):
            match = by_key.get(_model_value_key(base))
            if match and match not in result:
                result.append(match)
    return result


def _fallback_models(text, models):
    source = str(text or "")
    result = []
    for group in models:
        group_named = group["name"] in source
        values = []
        for value in group["values"]:
            if value.isdigit() and not group_named:
                continue
            if re.search(
                rf"(?<![A-Za-z0-9]){re.escape(value)}(?![A-Za-z0-9])", source, re.I
            ):
                values.append(value)
        for base, aliases in _BASE_COLOR_ALIASES.items():
            if "颜色" in group["name"] and any(
                _explicit_color_alias(source, alias) for alias in aliases
            ):
                values.extend(_resolve_model_value(group["name"], base, group["values"]))
        unique = []
        allowed = {_model_value_key(value): value for value in group["values"]}
        for value in values:
            value = allowed.get(_model_value_key(value), value)
            if value in group["values"] and value not in unique:
                unique.append(value)
        unique = [
            value for value in unique
            if not any(
                value != other
                and re.search(r"[\u4e00-\u9fff]", value)
                and value in other
                and _covered_by_longer_value(source, value, unique)
                for other in unique
            )
        ]
        if unique:
            def source_position(value):
                patterns = [value]
                if "颜色" in group["name"]:
                    aliases = next((names for base, names in _BASE_COLOR_ALIASES.items()
                                    if _model_value_key(base) == _model_value_key(value)), ())
                    patterns.extend(aliases)
                positions = [match.start() for pattern in patterns
                             if (match := re.search(re.escape(pattern), source, re.I))]
                return min(positions) if positions else len(source)
            unique.sort(key=source_position)
            unique = _sort_model_values(group["name"], unique)
            result.append({"name": group["name"], "values": unique})
    return result


_HAT_PRODUCT_RE = re.compile(
    r"(?:帽子|棒球帽|鸭舌帽|渔夫帽|遮阳帽|防晒帽|针织帽|毛线帽|冷帽|"
    r"户外帽|贝雷帽|登山帽|礼帽|盆帽|空顶帽|帽款|帽类|"
    r"(?<!连)帽(?=$|[\s，。,:：/]))"
)
_EXPLICIT_SIZE_RE = re.compile(
    r"(?:尺码|帽围|头围|尺寸|规格)\s*[:：]?\s*"
    r"(?:均码|通码|自由码|F码|XXXL?|XXL?|XL|L|M|S|XS|"
    r"\d{2,3}(?:\.\d+)?(?:\s*[-~～]\s*\d{2,3}(?:\.\d+)?)?)",
    re.I,
)


def _add_hat_default_size(text, matches, models):
    source = str(text or "")
    if (
        not _HAT_PRODUCT_RE.search(source)
        or any(group["name"].startswith("尺码") for group in matches)
        or _EXPLICIT_SIZE_RE.search(source)
    ):
        return matches
    size_group = next(
        (group for group in models
         if group["name"] == "尺码" and "均码" in group["values"]),
        None,
    )
    if not size_group:
        return matches
    return [*matches, {"name": "尺码", "values": ["均码"]}]


def match_weidian_models(text, ai_config, models=None):
    """按文案识别已有型号; 所有输出必须来自微店型号白名单."""
    models = [group for group in (models or load_weidian_models())
              if group["name"] != "类别"]
    fallback = _fallback_models(text, models)
    base_url = (ai_config or {}).get("base_url", "").rstrip("/")
    api_key = (ai_config or {}).get("api_key", "")
    if not (base_url and api_key and models):
        return _add_hat_default_size(text, fallback, models)
    prompt = (
        "根据商品文案识别已有型号。只返回文案明确提到的型号,不能猜测或编造。\n"
        "颜色可以把复杂颜色归并到候选中的基础色,例如玫瑰红归并为红色;其他规格必须严格匹配候选值。\n"
        "只返回 JSON: {\"models\":[{\"name\":\"颜色\",\"values\":[\"红色\"]}]}。\n"
        f"候选型号: {json.dumps(models, ensure_ascii=False)}\n商品文案:\n{text}\n"
    )
    payload = {"model": (ai_config or {}).get("model", "qwen3-vl-flash"),
               "max_tokens": 240,
               "messages": [{"role": "user", "content": prompt}]}
    try:
        resp = http_post_json(f"{base_url}/chat/completions", payload,
                              headers={"Authorization": f"Bearer {api_key}"})
        answer = resp["choices"][0]["message"]["content"].strip()
        start, end = answer.find("{"), answer.rfind("}")
        data = json.loads(answer[start:end + 1]) if start >= 0 else {}
        groups = {_category_key(group["name"]): group for group in models}
        fallback_by_name = {
            _category_key(group["name"]): set(group["values"])
            for group in fallback
        }
        matched = []
        for item in data.get("models", []):
            if not isinstance(item, dict):
                continue
            group = groups.get(_category_key(item.get("name")))
            if not group:
                continue
            values = []
            for value in item.get("values", []):
                for resolved in _resolve_model_value(group["name"], value, group["values"]):
                    if (
                        (group["name"] != "颜色"
                         or resolved in fallback_by_name.get(
                             _category_key(group["name"]), set()))
                        and resolved not in values
                    ):
                        values.append(resolved)
            if values:
                matched.append({"name": group["name"], "values": values})
        by_name = {_category_key(group["name"]): group for group in matched}
        for group in fallback:
            target = by_name.setdefault(_category_key(group["name"]), group)
            for value in group["values"]:
                if value not in target["values"]:
                    target["values"].append(value)
        matched = [by_name[_category_key(group["name"])] for group in models
                   if _category_key(group["name"]) in by_name]
        matched = [
            {**group, "values": _sort_model_values(group["name"], group["values"])}
            for group in matched
        ]
        return _add_hat_default_size(text, matched, models)
    except Exception as e:
        print(f"  ⚠ 微店型号识别失败, 使用白名单文字命中: {str(e)[:100]}")
        return _add_hat_default_size(text, fallback, models)


def format_model_field(matches):
    return "\n".join(
        f"{group['name']}：{'、'.join(_sort_model_values(group['name'], group['values']))}"
        for group in matches if group["values"]
    )


def parse_cost_price(text):
    """提取💰或P/p后的成本价, 找不到返回 None."""
    source = str(text or "")
    patterns = (
        r"💰\s*[:：]?\s*(\d+(?:\.\d+)?)",
        r"(?<![A-Za-z0-9])p\s*[:：]?\s*(\d+(?:\.\d+)?)(?![A-Za-z0-9])",
    )
    for pattern in patterns:
        match = re.search(pattern, source, re.I)
        if match:
            value = float(match.group(1))
            return int(value) if value.is_integer() else value
    return None


def calculate_sale_price(cost):
    """售价至少加15元且达到成本价的120%, 向上取到个位8."""
    target = max(float(cost) * 1.2, float(cost) + 15)
    rounded = math.ceil(target)
    return rounded + (8 - rounded % 10) % 10


def build_auto_fields(text, ai_config, fs_config, category_options=None):
    fields = {}
    all_categories = load_weidian_categories()
    categories = match_weidian_categories(text, ai_config, all_categories)
    if categories:
        category_rows = {row["name"]: row for row in all_categories}
        prefix = fs_config.get("category_child_prefix", "品牌分类-")
        formatted = [
            f"{prefix}{name}" if category_rows[name]["parent_id"] else name
            for name in categories
        ]
        if category_options is not None:
            formatted = [name for name in formatted if name in category_options]
        if formatted:
            fields[fs_config.get("category_field", "分类")] = formatted
    cost = parse_cost_price(text)
    if cost is not None:
        fields[fs_config.get("cost_field", "成本价")] = cost
        fields[fs_config.get("sale_field", "售价")] = calculate_sale_price(cost)
    models = match_weidian_models(text, ai_config)
    if models:
        fields[fs_config.get("model_field", "型号")] = format_model_field(models)
    return fields, categories, cost


def sort_by_new_rule(classified):
    """新顺序(每类空则跳过):
      合图(实拍最美1张) → 价格图(1张) → 产品包装图(≤3) → 模特图(评分前15)
      → 官方介绍图(≤5) → 合图其余 → 细节图(按颜色分组) → 尺码表(1张) → 视频
    "其他"(无法辨别)整类丢弃, 不进文件夹。元素为 (path, cat, score, color) 四元组。"""
    by_cat = {}
    for t in classified:
        by_cat.setdefault(t[1], []).append(t)

    # 合图: score 降序, 最高作封面, 其余排后
    hetu = sorted(by_cat.get("合图", []), key=lambda x: -x[2])
    cover, hetu_rest = hetu[:1], hetu[1:]
    # 价格图留 1 张; 包装图 ≤3; 模特图取评分前 10; 官方介绍图 ≤5
    price = by_cat.get("价格图", [])[:1]
    package = by_cat.get("产品包装图", [])[:3]
    models = sorted(by_cat.get("模特图", []), key=lambda x: -x[2])[:15]
    official = by_cat.get("官方介绍图", [])[:5]
    # 细节图按颜色分组(同色相邻), 颜色按首次出现顺序; 组内保持原序(stable sort)
    detail = by_cat.get("细节图", [])
    color_order = []
    for t in detail:
        if (t[3] or "") not in color_order:
            color_order.append(t[3] or "")
    detail = sorted(detail, key=lambda t: color_order.index(t[3] or ""))

    ordered = []
    ordered += cover
    ordered += price
    ordered += package
    ordered += models
    ordered += official
    ordered += hetu_rest
    ordered += detail
    ordered += by_cat.get("尺码表", [])[:1]
    ordered += by_cat.get("视频", [])
    # "其他"整类丢弃
    return ordered


# ── 飞书多维表格 ──────────────────────────────────────
class Feishu:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = None
        self.token_expire = 0
        self.field_options = {}

    def _tenant_token(self):
        if self.token and time.time() < self.token_expire - 60:
            return self.token
        data = http_post_json(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            {"app_id": self.cfg["app_id"], "app_secret": self.cfg["app_secret"]},
        )
        if data.get("code") != 0:
            raise RuntimeError(f"飞书 token 获取失败: {data}")
        self.token = data["tenant_access_token"]
        self.token_expire = time.time() + data.get("expire", 7200)
        return self.token

    def _auth_header(self):
        return {"Authorization": f"Bearer {self._tenant_token()}", "Content-Type": "application/json"}

    def create_record(self, fields):
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.cfg['base_id']}/tables/{self.cfg['table_id']}/records"
        data = http_post_json(url, {"fields": fields}, headers=self._auth_header())
        if data.get("code") != 0:
            raise RuntimeError(f"飞书写入失败: {data}")
        return data["data"]["record"]["record_id"]

    def get_record(self, record_id):
        url = f"https://open.feishu.cn/open-apis/bitable/v1/apps/{self.cfg['base_id']}/tables/{self.cfg['table_id']}/records/{record_id}"
        data = http_get_json(url, headers=self._auth_header())
        if data.get("code") != 0:
            raise RuntimeError(f"飞书读取失败: {data}")
        return data["data"]["record"]["fields"]

    def update_record(self, record_id, fields):
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{self.cfg['base_id']}/tables/{self.cfg['table_id']}/records/{record_id}"
        )
        data = http_put_json(
            url, {"fields": fields}, headers=self._auth_header()
        )
        if data.get("code") != 0:
            raise RuntimeError(f"飞书更新失败: {data}")
        return data["data"]["record"]

    def list_records(self, page_size=500):
        """读取多维表格记录, 只读并自动翻页."""
        base = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{self.cfg['base_id']}/tables/{self.cfg['table_id']}/records"
        )
        records = []
        page_token = ""
        while True:
            params = {"page_size": str(page_size)}
            if page_token:
                params["page_token"] = page_token
            url = f"{base}?{urllib.parse.urlencode(params)}"
            data = http_get_json(url, headers=self._auth_header())
            if data.get("code") != 0:
                raise RuntimeError(f"飞书记录读取失败: {data}")
            page = data.get("data", {})
            records.extend(page.get("items", []))
            if not page.get("has_more"):
                return records
            page_token = page.get("page_token", "")
            if not page_token:
                return records

    def get_field(self, field_name):
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{self.cfg['base_id']}/tables/{self.cfg['table_id']}/fields?page_size=100"
        )
        data = http_get_json(url, headers=self._auth_header())
        if data.get("code") != 0:
            raise RuntimeError(f"读取飞书字段失败: {data}")
        field = next(
            (item for item in data.get("data", {}).get("items", [])
             if item.get("field_name") == field_name),
            None,
        )
        if not field:
            raise RuntimeError(f"飞书字段不存在: {field_name}")
        return field

    def get_field_options(self, field_name):
        if field_name in self.field_options:
            return self.field_options[field_name]
        field = self.get_field(field_name)
        if field.get("type") != 4:
            raise RuntimeError(f"飞书字段不是多选类型: {field_name}")
        options = [
            str(item.get("name", "")).strip()
            for item in (field.get("property") or {}).get("options", [])
        ]
        self.field_options[field_name] = {name for name in options if name}
        return self.field_options[field_name]

    def update_field_options(self, field_name, options):
        field = self.get_field(field_name)
        if field.get("type") != 4:
            raise RuntimeError(f"飞书字段不是多选类型: {field_name}")
        names = list(dict.fromkeys(str(name).strip() for name in options if str(name).strip()))
        property_data = dict(field.get("property") or {})
        property_data["options"] = [{"name": name} for name in names]
        url = (
            f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
            f"{self.cfg['base_id']}/tables/{self.cfg['table_id']}/fields/{field['field_id']}"
        )
        data = http_put_json(
            url,
            {"field_name": field_name, "type": 4, "property": property_data},
            headers=self._auth_header(),
        )
        if data.get("code") != 0:
            raise RuntimeError(f"飞书字段选项更新失败: {data}")
        self.field_options.pop(field_name, None)
        return self.get_field_options(field_name)

    def wait_for_field(self, record_id, field_name, timeout=90):
        """轮询等待自动生成字段(如"图片名")出现"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            fields = self.get_record(record_id)
            val = fields.get(field_name)
            # 飞书字段返回可能是 [{"text": "..."}] 或 字符串
            if isinstance(val, list) and val:
                v = val[0].get("text") if isinstance(val[0], dict) else val[0]
                if v:
                    return str(v)
            elif isinstance(val, str) and val:
                return val
            time.sleep(2)
        raise RuntimeError(f"等待字段 {field_name} 超时")


def sync_category_field_options(feishu, fs_config):
    field_name = fs_config.get("category_field", "分类")
    options = build_feishu_category_options(
        prefix=fs_config.get("category_child_prefix", "品牌分类-")
    )
    current = feishu.get_field_options(field_name)
    if current == set(options):
        return {"field": field_name, "added": [], "removed": [], "options": options}
    verified = feishu.update_field_options(field_name, options)
    if verified != set(options):
        raise RuntimeError(f"飞书分类选项回读不一致: 期望 {len(options)} 个, 实际 {len(verified)} 个")
    return {
        "field": field_name,
        "added": sorted(set(options) - current),
        "removed": sorted(current - set(options)),
        "options": options,
    }

_KEYWORD_STOPWORDS = {
    "商品", "新款", "现货", "上新", "专柜", "正品", "男女", "均码", "图片", "颜色",
    "尺码", "发货", "到货", "推荐", "下单", "链接", "质量", "面料", "细节", "同款",
}


def _feishu_text(value):
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "、".join(_feishu_text(item) for item in value if _feishu_text(item))
    if isinstance(value, dict):
        for key in ("text", "name", "value"):
            if value.get(key) is not None:
                return _feishu_text(value[key])
    return ""


def _feishu_field_matches(actual, expected):
    if isinstance(expected, list):
        actual_items = actual if isinstance(actual, list) else [actual]
        return [
            _feishu_text(item).strip() for item in actual_items
        ] == [str(item).strip() for item in expected]
    return _feishu_text(actual).strip() == str(expected).strip()


def fill_missing_feishu_fields(feishu, fs_config, ai_config):
    """给「待编辑」且分类/型号为空的记录补齐分类和型号, 写后回读校验."""
    status_field = fs_config.get("status_field", "状态")
    info_field = fs_config.get("info_field", "信息")
    category_field = fs_config.get("category_field", "分类")
    model_field = fs_config.get("model_field", "型号")
    category_options = feishu.get_field_options(category_field)
    result = {"scanned": 0, "matched": 0, "updated": 0, "no_match": 0, "failed": []}

    for record in feishu.list_records():
        result["scanned"] += 1
        fields = record.get("fields") or {}
        if (
            _feishu_text(fields.get(status_field)).strip() != "待编辑"
            or not _feishu_text(fields.get(info_field)).strip()
            or _feishu_text(fields.get(category_field)).strip()
            or _feishu_text(fields.get(model_field)).strip()
        ):
            continue
        result["matched"] += 1
        record_id = record.get("record_id") or record.get("id")
        if not record_id:
            result["failed"].append("缺少 record_id")
            continue
        try:
            current = feishu.get_record(record_id)
            if (
                _feishu_text(current.get(status_field)).strip() != "待编辑"
                or not _feishu_text(current.get(info_field)).strip()
                or _feishu_text(current.get(category_field)).strip()
                or _feishu_text(current.get(model_field)).strip()
            ):
                continue
            auto_fields, _, _ = build_auto_fields(
                current[info_field], ai_config, fs_config, category_options
            )
            updates = {
                name: auto_fields[name]
                for name in (category_field, model_field)
                if auto_fields.get(name)
            }
            if not updates:
                result["no_match"] += 1
                continue
            feishu.update_record(record_id, updates)
            verified = feishu.get_record(record_id)
            if not all(
                _feishu_field_matches(verified.get(name), value)
                for name, value in updates.items()
            ):
                raise RuntimeError("写入后回读字段不一致")
            result["updated"] += 1
        except Exception as exc:
            result["failed"].append(f"{record_id}: {exc}")
    return result


def _feishu_record_time(record):
    fields = record.get("fields") or {}
    value = (fields.get("创建时间") or record.get("created_time") or
             record.get("last_modified_time"))
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return float(value) / 1000
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError, OSError, OverflowError):
        return 0


def build_local_category_keywords(records, categories, info_field="信息",
                                  category_field="分类", days=30, now=None):
    """从近期已分类文案提取重复中文词, 仅挂到已有根分类."""
    cutoff = (now or time.time()) - days * 86400
    roots = {
        row["name"] for row in categories
        if not row.get("parent_id") and row["name"] not in ("未分类", "店长推荐", "品牌分类")
    }
    counts = {name: {} for name in roots}
    for record in records:
        if _feishu_record_time(record) < cutoff:
            continue
        fields = record.get("fields") or {}
        info = _feishu_text(fields.get(info_field))
        raw_labels = fields.get(category_field) or []
        if not isinstance(raw_labels, list):
            raw_labels = [raw_labels]
        label_text = "、".join(_feishu_text(value).strip() for value in raw_labels)
        labels = {root for root in roots if root in label_text}
        if not info or not labels:
            continue
        seen_words = set()
        for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", info):
            for size in (2, 3, 4):
                for index in range(len(chunk) - size + 1):
                    word = chunk[index:index + size]
                    if word in _KEYWORD_STOPWORDS or word in seen_words:
                        continue
                    seen_words.add(word)
                    for label in labels:
                        counts[label][word] = counts[label].get(word, 0) + 1
    category_hits = {}
    for words in counts.values():
        for word in words:
            category_hits[word] = category_hits.get(word, 0) + 1
    return {
        name: [word for word, count in sorted(words.items(), key=lambda item: (-item[1], item[0]))
               if count >= 2 and category_hits.get(word, 0) <= 2][:80]
        for name, words in counts.items() if any(count >= 2 for count in words.values())
    }


def refresh_local_category_keywords(feishu, fs_config, days=30):
    categories = load_weidian_categories()
    records = feishu.list_records()
    keywords = build_local_category_keywords(
        records, categories,
        fs_config.get("info_field", "信息"),
        fs_config.get("category_field", "分类"),
        days=days,
    )
    # 本地已有值视为人工维护内容, 刷新时保留.
    keywords = {**keywords, **load_local_category_keywords()}
    payload = {
        "updated_at": datetime.now().isoformat(timespec="seconds"),
        "days": days,
        "records": len(records),
        "keywords": keywords,
    }
    save_json(LOCAL_CATEGORY_KEYWORDS_FILE, payload)
    return payload


# ── 输出文件夹 ────────────────────────────────────────
def sanitize_name(name):
    """清理文件夹名: 去掉 markdown 标记、换行、非法字符, 截断过长."""
    import re
    name = re.sub(r"[#*`>\-]", "", name)           # markdown 标记
    name = re.sub(r"[/\\:*?\"<>|\n\r\t]", "", name)  # 文件系统非法字符
    name = name.strip()
    return name[:40] if name else "未命名"


def create_product_folder(sorted_images, folder_name, output_dir):
    folder_name = sanitize_name(folder_name)
    """命名规则:
      - 尺码表 → "尺码表.jpg" (多张时加序号: 尺码表01.jpg)
      - 视频 → "视频NN.mp4" (独立序号)
      - 其他 → "{图片名}NN.jpg" (独立序号)
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    folder = output_dir / folder_name
    if folder.exists():
        raise FileExistsError(f"产品目录已存在: {folder}")
    staging = Path(tempfile.mkdtemp(prefix=f".{folder_name}.", dir=output_dir))
    img_idx = 0
    vid_idx = 0
    size_idx = 0
    size_count = sum(1 for t in sorted_images if t[1] == "尺码表")
    try:
        for src, cat, *_rest in sorted_images:
            if cat == "尺码表":
                size_idx += 1
                name = "尺码表" if size_count == 1 else f"尺码表{size_idx:02d}"
            elif cat == "视频":
                vid_idx += 1
                name = f"视频{vid_idx:02d}"
            else:
                img_idx += 1
                name = f"{folder_name}{img_idx:02d}"
            dest = staging / f"{name}{src.suffix}"
            shutil.copy2(src, dest)
        staging.rename(folder)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    print(f"  ✓ 文件夹: {folder} ({len(sorted_images)} 个文件)")
    return folder


# ── 进度/配置 ────────────────────────────────────────
def load_json_or(path, default):
    return json.loads(path.read_text()) if path.exists() else default

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


def _feishu_pending_key(album_id, goods_ids):
    raw = album_id + ":" + ",".join(sorted(goods_ids))
    return hashlib.sha256(raw.encode()).hexdigest()


def _load_feishu_pending():
    try:
        data = load_json_or(FEISHU_PENDING_FILE, {})
        return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"  ⚠ feishu_pending.json 无法读取, 本次重新建记录: {e}")
        return {}


# ── 主流程 ────────────────────────────────────────────
def load_scrape(scrape_path, config):
    """把抓取的 JSON 统一成 {album_id: {"supplier": name, "items": [...]}}
    兼容三种格式:
      - {"data": {album_id: {"supplier":.., "items":[..]}}}  (多供货商, 推荐)
      - {"supplier":.., "albumId":.., "items":[..]}           (单供货商)
      - [item, ...]                                            (纯数组)
    """
    raw = json.loads(Path(scrape_path).read_text())
    if isinstance(raw, dict) and "data" in raw:
        return raw["data"]
    if isinstance(raw, dict) and "items" in raw:
        name = raw.get("supplier") or next(iter(config["suppliers"]))
        aid = raw.get("albumId") or config["suppliers"].get(name, name)
        return {aid: {"supplier": name, "items": raw["items"]}}
    if isinstance(raw, list):
        name = next(iter(config["suppliers"]))
        aid = config["suppliers"][name]
        fname = Path(scrape_path).stem.lower()
        for n, a in config["suppliers"].items():
            if n.lower() in fname or a[-8:].lower() in fname:
                name, aid = n, a
                break
        return {aid: {"supplier": name, "items": raw}}
    raise ValueError("无法识别的抓取 JSON 格式")


def select_suppliers(available):
    """交互式多选. available: [(name, album_id, item_count), ...]
    返回选中的子集. 支持环境变量 SUPPLIERS 跳过菜单(自动化用).
    """
    env = os.environ.get("SUPPLIERS", "").strip()
    if env:
        if env.lower() == "all":
            return available
        wanted = {s.strip() for s in env.split(",")}
        return [a for a in available if a[0] in wanted]

    print("\n可处理的供货商:")
    for i, (name, aid, cnt) in enumerate(available, 1):
        print(f"  {i:2d}. {name}  ({cnt} 条新内容)")
    print("\n输入编号选择要跑的供货商:")
    print("  例: 1,3,5  或  1-4  或  all(全部)  或直接回车=全部")
    raw = input("> ").strip()
    if not raw or raw.lower() == "all":
        return available
    chosen = set()
    for part in raw.replace("，", ",").split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-")
            chosen.update(range(int(a), int(b) + 1))
        elif part.isdigit():
            chosen.add(int(part))
    return [available[i - 1] for i in sorted(chosen) if 1 <= i <= len(available)]


def _item_date(it):
    return datetime.fromtimestamp(it["time_stamp"] / 1000).strftime("%Y-%m-%d")

def _item_order(it):
    """页面编排顺序使用 update_time; 旧抓取数据回退到发布时间."""
    return it.get("update_time") or it["time_stamp"]


def _workbench_datetime(it):
    """工作台统一使用 update_time; 没有时回退 time_stamp."""
    value = it.get("update_time") or it.get("time_stamp")
    try:
        if isinstance(value, (int, float)) or str(value).isdigit():
            return datetime.fromtimestamp(float(value) / 1000)
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError, OSError, OverflowError):
        return datetime.fromtimestamp(float(it["time_stamp"]) / 1000)

def _item_display_date(it):
    """页面日期按发布时间计算."""
    return _item_date(it)


def _normalize_title_prefix(value):
    """标题前缀匹配忽略空白和英文大小写, 原始文案保持不变."""
    return re.sub(r"\s+", "", value or "").casefold()


def find_title_range(items, start_prefix, end_prefix, date_str):
    """在指定发布日期内, 按页面编排顺序返回两个标题前缀之间的帖子."""
    ordered = sorted(
        [it for it in items if _item_display_date(it) == date_str],
        key=_item_order,
    )
    prefixes = (("起始", start_prefix.strip()), ("结束", end_prefix.strip()))
    matched = []
    for label, prefix in prefixes:
        normalized_prefix = _normalize_title_prefix(prefix)
        indices = [
            i for i, it in enumerate(ordered)
            if _normalize_title_prefix(it.get("title")).startswith(normalized_prefix)
        ]
        if len(indices) != 1:
            return [], f"{label}前缀「{prefix}」匹配到 {len(indices)} 条, 请提供更多标题字"
        matched.append(indices[0])
    lo, hi = sorted(matched)
    return ordered[lo:hi + 1], ""

def _progress_date(progress, album_id):
    """取上次处理到的日期串. 兼容旧格式(int 毫秒时间戳)."""
    v = progress.get(album_id)
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d")
    if isinstance(v, dict):
        return str(v.get("cutoff_date") or "")
    return str(v)


def _item_version(it):
    """用条目内容和源更新时间生成稳定版本, 不再只依赖发布日期."""
    payload = {
        "title": it.get("title") or "",
        "imgsSrc": list(it.get("imgsSrc") or []),
        "videoUrl": it.get("videoUrl") or it.get("videoURL") or "",
        "time_stamp": it.get("time_stamp"),
        "update_time": it.get("update_time"),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()


def _new_progress_state(cutoff_date=""):
    return {
        "schema": 2,
        "cutoff_date": cutoff_date,
        "processed_ids": [],
        "processed_versions": {},
        "failed_versions": {},
        "read_versions": {},
    }


def _ensure_progress_state(album_id, raw_items, progress):
    """将旧日期/ID进度迁移到版本进度, 返回可继续使用的状态."""
    state = progress.get(album_id)
    by_id = {it.get("goods_id"): it for it in raw_items if it.get("goods_id")}
    if state is None:
        cutoff = max((_item_date(it) for it in raw_items), default="")
        state = _new_progress_state(cutoff)
        # 首次启用仍只把最新发布日期作为待选, 历史内容建立基线.
        state["processed_versions"] = {
            gid: _item_version(it)
            for gid, it in by_id.items()
            if _item_date(it) < cutoff
        }
    elif not isinstance(state, dict):
        cutoff = _progress_date(progress, album_id)
        state = _new_progress_state(cutoff)
        # 旧日期无法还原当天哪些已处理, 当天内容宁可多展示一次, 不静默漏掉.
        state["processed_versions"] = {
            gid: _item_version(it)
            for gid, it in by_id.items()
            if _item_date(it) < cutoff
        }
    else:
        state.setdefault("schema", 2)
        state.setdefault("cutoff_date", "")
        state.setdefault("processed_ids", [])
        state.setdefault("processed_versions", {})
        state.setdefault("failed_versions", {})
        state.setdefault("read_versions", {})
        processed_versions = state["processed_versions"]
        for gid in state["processed_ids"]:
            if gid not in processed_versions and gid in by_id:
                processed_versions[gid] = _item_version(by_id[gid])
        cutoff = str(state.get("cutoff_date") or "")
        for gid, it in by_id.items():
            if cutoff and _item_date(it) < cutoff and gid not in processed_versions:
                processed_versions[gid] = _item_version(it)
    state["processed_ids"] = sorted(state["processed_versions"])
    progress[album_id] = state
    return state


def _progress_item_status(it, state):
    """返回 new / updated / failed / done, 版本变化优先标为 updated."""
    gid = it.get("goods_id")
    version = _item_version(it)
    processed_versions = state.get("processed_versions") or {}
    failed_versions = state.get("failed_versions") or {}
    read_versions = state.get("read_versions") or {}
    if processed_versions.get(gid) == version:
        return "done"
    if read_versions.get(gid) == version:
        return "read"
    if failed_versions.get(gid) == version:
        return "failed"
    if gid in processed_versions or gid in failed_versions or gid in read_versions:
        return "updated"
    return "new"


def filter_new_items(album_id, raw_items, progress):
    """返回未成功处理或版本已变化的条目, 兼容旧日期进度."""
    state = _ensure_progress_state(album_id, raw_items, progress)
    return [
        it for it in raw_items
        if _progress_item_status(it, state) not in ("done", "read")
    ]


def _record_processed_ids(progress, album_id, groups, processed_ids):
    """记录实际成功落盘的帖子和当时版本, 避免部分失败被跳过."""
    with _PROGRESS_LOCK:
        raw_items = [it for group in groups for it in group]
        state = _ensure_progress_state(album_id, raw_items, progress)
        versions = state["processed_versions"]
        failed = state["failed_versions"]
        read = state["read_versions"]
        by_id = {it.get("goods_id"): it for it in raw_items}
        for gid in processed_ids:
            if gid in by_id:
                versions[gid] = _item_version(by_id[gid])
                failed.pop(gid, None)
                read.pop(gid, None)
        state["processed_ids"] = sorted(versions)
        save_json(PROGRESS_FILE, progress)


def _record_failed_ids(progress, album_id, items):
    """记录失败版本, 版本变化后会自动从 failed 变为 updated 重试."""
    with _PROGRESS_LOCK:
        state = _ensure_progress_state(album_id, items, progress)
        failed = state["failed_versions"]
        for it in items:
            gid = it.get("goods_id")
            if gid:
                failed[gid] = _item_version(it)
        save_json(PROGRESS_FILE, progress)


def _record_read_items(progress, reads):
    """记录用户明确看过的条目版本;内容变化后自动重新进入待处理."""
    with _PROGRESS_LOCK:
        for read in reads:
            album_id = read.get("albumId")
            items = read.get("items") or []
            if not album_id or not items:
                continue
            state = _ensure_progress_state(album_id, items, progress)
            versions = state["read_versions"]
            for item in items:
                gid = item.get("goods_id")
                if gid:
                    versions[gid] = _item_version(item)
        save_json(PROGRESS_FILE, progress)


def _items_for_goods_ids(groups, goods_ids):
    wanted = set(goods_ids)
    return [it for group in groups for it in group if it.get("goods_id") in wanted]


def _img_data_url(p):
    mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
    return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"


VIDEO_EXTS = (".mp4", ".mov", ".avi")


def _video_frame_data_url(path):
    """用 ffmpeg 提取视频首帧作为分类预览缩略图; 失败时返回空串."""
    import subprocess
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:v:0",
             "-frames:v", "1", "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, check=False,
        )
        if result.returncode == 0 and result.stdout:
            return "data:image/jpeg;base64," + base64.b64encode(result.stdout).decode()
    except (OSError, subprocess.TimeoutExpired):
        pass
    print(f"  ⚠ 视频首帧提取失败: {Path(path).name}")
    return ""


def build_classify_preview_html(
        supplier_name, prepared, out_path, review_label="", confirm_base="分类确认"):
    """图片预览: 按当前处理顺序展示每张图, 拖拽重排 + 删除 + 标记尺码表。
    导出 分类确认.json = {supplier, prods:[{order:[存活id新序], sizes:[标为尺码表的id]}]}。
    prepared 每项含 sorted_imgs:[(path,cat,score,color)] (已排好序)。"""
    prods = []
    for pr in prepared:
        imgs = []
        for j, t in enumerate(pr["sorted_imgs"]):
            is_vid = t[1] == "视频" or t[0].suffix.lower() in VIDEO_EXTS
            imgs.append({"id": j, "cat": t[1], "video": is_vid, "sz": t[1] == "尺码表",
                         "thumb": _video_frame_data_url(t[0]) if is_vid else _img_data_url(t[0]),
                         "src": t[0].resolve().as_uri() if is_vid else ""})
        prods.append({"gi": pr["gi"], "time": pr["latest_time"], "imgs": imgs})
    payload = json.dumps({
        "supplier": supplier_name,
        "label": review_label or supplier_name,
        "confirmName": f"{confirm_base}.json",
        "prods": prods,
    }, ensure_ascii=False).replace("<", "\\u003c")
    html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>排序预览</title>
<style>
body{font-family:-apple-system,sans-serif;margin:0;background:#f5f5f7;color:#1d1d1f}
header{position:sticky;top:0;background:#fff;padding:12px 16px;box-shadow:0 1px 4px rgba(0,0,0,.1);z-index:10;display:flex;justify-content:space-between;align-items:center}
h2{margin:0;font-size:16px}
#confirm{background:#0071e3;color:#fff;border:0;border-radius:20px;padding:8px 22px;font-size:14px;cursor:pointer}
.hint{font-size:12px;color:#888;margin:4px 16px;line-height:1.5}
.prod{background:#fff;border-radius:12px;margin:12px 16px;overflow:hidden;border:2px solid #e5e5ea}
.prodhead{padding:8px 12px;font-weight:600;font-size:13px;background:#f0f7ff;color:#0071e3}
.cards{display:flex;flex-wrap:wrap;gap:10px;padding:12px;min-height:80px}
.card{width:130px;position:relative;border:2px solid transparent;border-radius:10px}
.card.sz{border-color:#ff9500}
.card.selected{border-color:#0071e3!important;box-shadow:0 0 0 2px rgba(0,113,227,.2)}
.card .media{width:130px;height:130px;border-radius:8px;background:#eee;display:block;object-fit:cover;cursor:grab;overflow:hidden}
.card .vid{position:relative}
.card .vid img,.card .vid video{width:100%;height:100%;display:block;object-fit:cover}
.play{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);width:34px;height:34px;border-radius:50%;background:rgba(0,0,0,.58);color:#fff;display:flex;align-items:center;justify-content:center;font-size:16px;padding-left:2px;box-sizing:border-box}
.card.drag{opacity:.35}
.num{position:absolute;top:5px;left:5px;background:#0071e3;color:#fff;font-size:11px;border-radius:10px;padding:1px 7px;font-weight:600}
.del{position:absolute;top:4px;right:4px;z-index:3;width:22px;height:22px;border:0;border-radius:50%;background:rgba(0,0,0,.55);color:#fff;font-size:15px;line-height:22px;padding:0;cursor:pointer}
.del:hover{background:#d32f2f}
.preview-btn{position:absolute;right:5px;top:101px;z-index:2;width:24px;height:24px;border:0;border-radius:50%;background:rgba(0,0,0,.58);cursor:pointer}
.preview-btn:hover{background:#0071e3}
.preview-btn:before{content:'';position:absolute;left:6px;top:5px;width:7px;height:7px;border:2px solid #fff;border-radius:50%}
.preview-btn:after{content:'';position:absolute;left:14px;top:14px;width:6px;height:2px;background:#fff;transform:rotate(45deg);transform-origin:left center}
.cat{font-size:10px;color:#777;text-align:center;margin-top:3px}
.szbtn{display:block;width:100%;margin-top:3px;border:1px solid #ddd;border-radius:6px;background:#fafafa;font-size:11px;padding:2px;cursor:pointer}
.card.sz .szbtn{background:#ff9500;color:#fff;border-color:#ff9500}
#hover-preview{display:none;position:fixed;z-index:100;pointer-events:none;width:min(560px,calc(100vw - 16px));height:min(640px,70vh);padding:8px;box-sizing:border-box;border-radius:8px;background:#111;box-shadow:0 12px 36px rgba(0,0,0,.35);align-items:center;justify-content:center}
#hover-preview img,#hover-preview video{display:block;max-width:100%;max-height:100%;object-fit:contain}
</style></head><body>
<header><h2 id="preview-title">🖼️ 排序预览 — 拖动排序 · × 删图</h2><button id="confirm">✅ 完成并生成</button></header>
<div class="hint">图片按当前处理顺序展示。<b>拖动</b>调顺序,点 <b>×</b> 删掉不要的(不会存到本地)。<br><b>按住 Command 单击</b>可多选,选中一张后按 <b>Shift 单击</b>可连选,选中后可一起拖动。某张是<b>尺码表</b>但没识别出→点它下面的「尺码表」按钮标上(橙框=已标,会命名成"尺码表"); 视频显示为 🎬。改完点右上「完成并生成」。直接关网页=按现在顺序生成。</div>
<div id="app"></div>
<div id="hover-preview" aria-hidden="true"></div>
<script>
const D=__PAYLOAD__;
const app=document.getElementById('app');
const hoverPreview=document.getElementById('hover-preview');
document.title=D.label+' · 排序预览';
document.getElementById('preview-title').textContent='🖼️ '+D.label+' — 拖动排序 · × 删图';
let dragEl=null,dragEls=[],selected=new Set(),selectionAnchor=null;
function renumber(box){[...box.children].forEach((c,i)=>{const n=c.querySelector('.num');if(n)n.textContent=i+1;});}
function clearSelection(){selected.forEach(c=>c.classList.remove('selected'));selected.clear();selectionAnchor=null;}
function selectCard(c){selected.add(c);c.classList.add('selected');selectionAnchor=c;}
function selectRange(c){
  if(!selectionAnchor||selectionAnchor.parentNode!==c.parentNode){clearSelection();selectCard(c);return;}
  const cards=[...c.parentNode.children],a=cards.indexOf(selectionAnchor),b=cards.indexOf(c),lo=Math.min(a,b),hi=Math.max(a,b);
  cards.slice(lo,hi+1).forEach(selectCard);
  selectionAnchor=c;
}
function hidePreview(){
  const video=hoverPreview.querySelector('video');if(video)video.pause();
  hoverPreview.replaceChildren();hoverPreview.style.display='none';
}
function showPreview(im,target){
  hidePreview();
  const media=document.createElement(im.video?'video':'img');
  if(im.video){media.src=im.src;media.muted=true;media.loop=true;media.playsInline=true;media.autoplay=true;}
  else{media.src=im.thumb;}
  hoverPreview.appendChild(media);hoverPreview.style.display='flex';
  const r=target.getBoundingClientRect(),gap=12,w=hoverPreview.offsetWidth,h=hoverPreview.offsetHeight;
  let left=r.right+gap;if(left+w>innerWidth-8)left=r.left-w-gap;
  hoverPreview.style.left=Math.max(8,left)+'px';
  hoverPreview.style.top=Math.max(8,Math.min(r.top,innerHeight-h-8))+'px';
  if(im.video)media.play().catch(()=>{});
}
D.prods.forEach(pr=>{
  const box=document.createElement('div');box.className='prod';
  box.innerHTML='<div class="prodhead">产品 '+pr.gi+' · '+pr.time+'</div>';
  const cards=document.createElement('div');cards.className='cards';
  pr.imgs.forEach(im=>{
    const c=document.createElement('div');c.className='card'+(im.sz?' sz':'');c.draggable=true;c.dataset.id=im.id;
    const videoThumb=im.thumb?'<img draggable="false" src="'+im.thumb+'">':'<video muted preload="metadata" src="'+im.src+'"></video>';
    const media=im.video?'<div class="media vid">'+videoThumb+'<span class="play">▶</span></div>':'<img class="media" draggable="false" src="'+im.thumb+'">';
    c.innerHTML='<span class="num"></span><button class="del">×</button>'+media+'<button class="preview-btn" title="预览" aria-label="预览"></button><div class="cat">'+im.cat+'</div><button class="szbtn">尺码表</button>';
    c.addEventListener('click',e=>{
      if(e.target.closest('button'))return;
      const range=e.shiftKey,add=e.metaKey||e.ctrlKey;
      if(range){
        selectRange(c);
      }else if(add){
        if(selected.has(c)){selected.delete(c);c.classList.remove('selected');}
        else selectCard(c);
        selectionAnchor=c;
      }else{clearSelection();selectCard(c);}
    });
    c.querySelector('.del').onclick=()=>{
      const targets=selected.has(c)?[...selected]:[c],boxes=new Set(targets.map(card=>card.parentNode));
      targets.forEach(card=>{selected.delete(card);if(selectionAnchor===card)selectionAnchor=null;card.remove();});
      boxes.forEach(renumber);
    };
    c.querySelector('.szbtn').onclick=()=>{c.classList.toggle('sz');};
    const previewBtn=c.querySelector('.preview-btn');
    const showFromButton=()=>{c.draggable=false;showPreview(im,previewBtn);};
    const hideFromButton=()=>{hidePreview();c.draggable=true;};
    previewBtn.draggable=false;
    previewBtn.addEventListener('mouseenter',showFromButton);
    previewBtn.addEventListener('mouseleave',hideFromButton);
    previewBtn.addEventListener('focus',showFromButton);
    previewBtn.addEventListener('blur',hideFromButton);
    previewBtn.onmousedown=e=>e.stopPropagation();
    c.addEventListener('dragstart',e=>{
      hidePreview();
      if(!selected.has(c)){clearSelection();selectCard(c);}
      dragEl=c;dragEls=[...cards.children].filter(card=>selected.has(card));
      dragEls.forEach(card=>card.classList.add('drag'));
      e.dataTransfer.effectAllowed='move';e.dataTransfer.setData('text/plain','');
    });
    c.addEventListener('dragend',()=>{dragEls.forEach(card=>card.classList.remove('drag'));dragEl=null;dragEls=[];renumber(cards);});
    cards.appendChild(c);
  });
  // 容器级 dragover: 先锁定光标所在行(y 落在卡片上下范围内), 行内按 x 找插入点;
  // 光标在行间空隙时退回 2D 最近命中。按卡片中点左/右决定插到前面还是后面。
  // 适配 flex-wrap 网格, 可拖到任意位置(不再只落行首/行尾)。
  cards.addEventListener('dragover',e=>{
    e.preventDefault();
    if(!dragEl||dragEl.parentNode!==cards)return;
    const els=[...cards.querySelectorAll('.card:not(.drag)')];
    const row=els.filter(el=>{const r=el.getBoundingClientRect();return e.clientY>=r.top&&e.clientY<=r.bottom;});
    const pool=row.length?row:els, sameRow=row.length>0;
    let best=null,bd=Infinity,after=false;
    for(const el of pool){
      const r=el.getBoundingClientRect();
      const cx=r.left+r.width/2, cy=r.top+r.height/2;
      const d=sameRow?Math.abs(e.clientX-cx):Math.hypot(e.clientX-cx,e.clientY-cy);
      if(d<bd){bd=d;best=el;after=e.clientX>cx;}
    }
    const ref=best?(after?best.nextSibling:best):null;
    if(ref&&!dragEls.includes(ref)){
      const fragment=document.createDocumentFragment();dragEls.forEach(card=>fragment.appendChild(card));cards.insertBefore(fragment,ref);
    }else if(!ref){
      const fragment=document.createDocumentFragment();dragEls.forEach(card=>fragment.appendChild(card));cards.appendChild(fragment);
    }
    renumber(cards);
  });
  box.appendChild(cards);app.appendChild(box);renumber(cards);
});
document.getElementById('confirm').onclick=()=>{
  const prods=[...app.querySelectorAll('.cards')].map(box=>({
    order:[...box.children].map(c=>+c.dataset.id),
    sizes:[...box.children].filter(c=>c.classList.contains('sz')).map(c=>+c.dataset.id)
  }));
  const blob=new Blob([JSON.stringify({supplier:D.supplier,prods:prods})],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=D.confirmName;
  document.body.appendChild(a);a.click();
  alert('已下载 '+D.confirmName+' — 回到终端继续');
};
</script></body></html>"""
    out_path.write_text(html.replace("__PAYLOAD__", payload), encoding="utf-8")


def _open_in_browser(path):
    """尽量自动弹出本地 HTML: macOS 用 open, 其它退回 webbrowser。"""
    try:
        if sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", str(path)], check=False)
            return
    except Exception:
        pass
    try:
        import webbrowser
        webbrowser.open(Path(path).as_uri())
    except Exception:
        pass


def wait_for_classify_review(
        supplier_name, prepared, timeout=None, review_id="", review_label="",
        raise_interrupt=False, cancel_base=""):
    """弹排序预览, 等 分类确认.json。返回 orders(按 prepared 顺序的存活id列表)或 None。"""
    safe_id = re.sub(r"[^0-9A-Za-z_-]", "_", review_id).strip("_")
    suffix = f"_{safe_id}" if safe_id else ""
    out = SCRIPT_DIR / f"分类预览{suffix}.html"
    confirm_base = f"分类确认{suffix}"
    build_classify_preview_html(
        supplier_name, prepared, out,
        review_label=review_label, confirm_base=confirm_base,
    )
    # 清残留, 避免 Chrome 把新文件改名成 分类确认 (1).json
    for p in _chrome_download_paths(confirm_base):
        try: p.unlink()
        except Exception: pass
    _open_in_browser(out)
    print(f"\n🖼️  已弹出排序预览(自动打开): {out}")
    wait_hint = f"最长 {timeout} 秒" if timeout is not None else "一直等待"
    print(f"   拖动排序 / × 删图 → 点「完成并生成」({wait_hint}, Ctrl+C 取消)")
    print(f"   未点「完成并生成」不会生成文件夹")
    start = time.time() - 1
    end = time.time() + timeout if timeout is not None else None
    try:
        while end is None or time.time() < end:
            if cancel_base and _consume_cancel(cancel_base):
                print("  已取消此商品, 未生成产品文件夹")
                return None
            time.sleep(1)
            p = pick_newest_download(confirm_base)
            if p and p.stat().st_mtime > start:
                s1 = p.stat().st_size; time.sleep(0.5)
                if p.stat().st_size != s1:
                    continue
                try:
                    p = _archive_download(p)
                    prods = json.loads(p.read_text()).get("prods")
                    print("  ✓ 收到排序确认, 应用人工调整")
                    return prods
                except Exception as e:
                    print(f"  ⚠ 分类确认读失败({e}), 按当前顺序")
                    return None
    except KeyboardInterrupt:
        if raise_interrupt:
            raise
        print("\n  已取消, 未收到排序确认")
    print("  未收到排序确认, 本次不生成文件夹")
    return None


def process_groups(supplier_name, album_id, groups, progress, feishu, fs_cfg, ai_cfg,
                   advance_progress=True, review=None, order_key=_item_order,
                   review_id="", review_label="", raise_interrupt=False,
                   cancel_base=""):
    """按给定分组处理: 下载→去重→分类→排序→(可选拖拽/删图)→飞书→建文件夹. 返回产品数.
    advance_progress=False 时不推进按天进度(定向/批量下载用).
    review=None: 按环境变量 SKIP_REVIEW 决定, 默认弹排序预览。"""
    if MAX_PRODUCTS > 0:
        groups = groups[:MAX_PRODUCTS]
    if review is None:
        review = os.environ.get("SKIP_REVIEW", "") not in ("1", "true", "yes")

    # Phase A: 下载 → 去重 → 保持条目顺序, 暂存(临时目录先不删)
    prepared = []
    for gi, group in enumerate(groups, 1):
        group_asc = sorted(group, key=order_key)
        latest_item = max(group, key=lambda x: x["time_stamp"])
        latest_time = datetime.fromtimestamp(latest_item["time_stamp"] / 1000).strftime("%Y-%m-%d %H:%M")
        print(f"\n─ 产品 {gi}/{len(groups)} ({len(group)} 帖, {latest_time})")
        texts = [it["title"] for it in group_asc if it.get("title")]
        combined_text = "\n\n---\n\n".join(texts) if texts else "(无文案)"
        print(f"  文案: {combined_text[:60]}...")
        artifact_id = review_id or str(gi)
        tmp_dir = TMP_ROOT / f"tmp_{album_id[-8:]}_{artifact_id}"
        images = download_product_images(group_asc, tmp_dir)
        if not images:
            print("  图片未完整下载或没有图片, 跳过")
            if tmp_dir.exists():
                shutil.rmtree(tmp_dir, ignore_errors=True)
            if advance_progress:
                _record_failed_ids(progress, album_id, group)
            continue
        # 相同的图只保留1张(按内容哈希去重, 保持顺序)
        # ponytail: 精确字节去重; 需要"视觉近似"去重时再上感知哈希
        seen_h, uniq = set(), []
        for p in images:
            h = hashlib.md5(p.read_bytes()).hexdigest()
            if h in seen_h:
                continue
            seen_h.add(h); uniq.append(p)
        if len(uniq) < len(images):
            print(f"  去重: {len(images)} → {len(uniq)} 张")
        print(f"  下载 {len(uniq)} 张")
        # 默认按下载顺序保留图片; 图片分类函数保留为独立旧能力, 以后需要时再接回流程。
        sorted_imgs = [
            (p, "视频" if p.suffix.lower() in VIDEO_EXTS else "其他", 0, "")
            for p in uniq
        ]
        prepared.append({"gi": gi, "latest_time": latest_time, "combined_text": combined_text,
                         "goods_ids": [it["goods_id"] for it in group],
                         "tmp_dir": tmp_dir, "sorted_imgs": sorted_imgs, "final": sorted_imgs})
    if not prepared:
        return 0

    if cancel_base and _consume_cancel(cancel_base):
        for pr in prepared:
            if pr["tmp_dir"].exists():
                shutil.rmtree(pr["tmp_dir"], ignore_errors=True)
        print("  已取消此商品, 未生成产品文件夹")
        return 0

    # Phase A.5: 拖拽/删图/标尺码表 确认。edits 按 prepared 顺序, 每项 {order:[存活id], sizes:[标尺码表的id]}
    if review:
        if review_id or review_label or raise_interrupt:
            review_args = {
                "review_id": review_id,
                "review_label": review_label,
                "raise_interrupt": raise_interrupt,
            }
            if cancel_base:
                review_args["cancel_base"] = cancel_base
            edits = wait_for_classify_review(supplier_name, prepared, **review_args)
        else:
            edits = (wait_for_classify_review(
                supplier_name, prepared, cancel_base=cancel_base
            ) if cancel_base else wait_for_classify_review(supplier_name, prepared))
        if edits is None:
            print("  未收到人工排序确认, 不生成任何产品文件夹")
            for pr in prepared:
                if pr["tmp_dir"].exists():
                    shutil.rmtree(pr["tmp_dir"], ignore_errors=True)
            return 0
        for pr, ed in zip(prepared, edits):
            si = pr["sorted_imgs"]
            sizes = set(ed.get("sizes", []))
            final = []
            for i in ed.get("order", []):
                if not (0 <= i < len(si)):
                    continue
                path, cat, score, color = si[i]
                is_vid = cat == "视频" or path.suffix.lower() in VIDEO_EXTS
                # 命名只认 尺码表/视频; 标记优先, 取消标记的旧尺码表回落成普通图
                if i in sizes:
                    cat = "尺码表"
                elif is_vid:
                    cat = "视频"
                elif cat == "尺码表":
                    cat = "细节图"
                final.append((path, cat, score, color))
            pr["final"] = final

    # Phase B: 飞书 → 建文件夹
    n = 0
    feishu_pending = _load_feishu_pending() if feishu else {}
    for pr in prepared:
        if cancel_base and _consume_cancel(cancel_base):
            for pending in prepared:
                if pending["tmp_dir"].exists():
                    shutil.rmtree(pending["tmp_dir"], ignore_errors=True)
            print("  已取消此商品, 未生成产品文件夹")
            return 0
        final = pr["final"]
        if not final:
            print(f"\n─ 产品 {pr['gi']}: 图片被全部删除, 跳过")
            if pr["tmp_dir"].exists():
                shutil.rmtree(pr["tmp_dir"])
            continue
        print(f"\n─ 产品 {pr['gi']} 最终 {len(final)} 张:")
        for i, (p, cat, *_rest) in enumerate(final, 1):
            print(f"    {i:2d}. [{cat}{' 封面' if i == 1 and cat == '合图' else ''}] {p.name}")
        folder_suffix = review_id or str(pr["gi"])
        folder_name = (
            f"{supplier_name}_{pr['latest_time'].replace(' ', '_').replace(':', '')}"
            f"_{folder_suffix}"
        )
        pending_key = None
        if feishu:
            pending_key = _feishu_pending_key(album_id, pr["goods_ids"])
            try:
                pending_entry = feishu_pending.get(pending_key)
                if isinstance(pending_entry, dict):
                    raise RuntimeError(
                        "上次飞书记录创建结果未能落盘, 请先检查 feishu_pending.json"
                    )
                record_id = pending_entry
                if record_id:
                    print(f"  ↻ 复用待完成飞书记录: {record_id}")
                else:
                    feishu_pending[pending_key] = {"status": "creating"}
                    save_json(FEISHU_PENDING_FILE, feishu_pending)
                    try:
                        category_options = (
                            sync_category_field_options(feishu, fs_cfg)
                            ["options"]
                            if hasattr(feishu, "get_field_options") else None
                        )
                        auto_fields, matched_categories, cost = build_auto_fields(
                            pr["combined_text"], ai_cfg, fs_cfg, category_options
                        )
                        record_fields = {
                            fs_cfg.get("info_field", "信息"): pr["combined_text"],
                            fs_cfg.get("supplier_field", "相册供货商"): supplier_name,
                            **auto_fields,
                        }
                        record_id = feishu.create_record(record_fields)
                        if matched_categories:
                            print(f"  ✓ 分类: {'、'.join(matched_categories)}")
                        if cost is not None:
                            print(f"  ✓ 成本价: {cost} · 售价: {record_fields[fs_cfg.get('sale_field', '售价')]}")
                        model_value = record_fields.get(fs_cfg.get("model_field", "型号"))
                        if model_value:
                            print(f"  ✓ 型号: {model_value.replace(chr(10), ' | ')}")
                    except Exception:
                        feishu_pending.pop(pending_key, None)
                        save_json(FEISHU_PENDING_FILE, feishu_pending)
                        raise
                    feishu_pending[pending_key] = record_id
                    save_json(FEISHU_PENDING_FILE, feishu_pending)
                    print(f"  ✓ 飞书记录已创建: {record_id}")
                folder_name = feishu.wait_for_field(record_id, fs_cfg.get("img_name_field", "图片名"))
                print(f"  ✓ 图片名: {folder_name}")
            except Exception as e:
                print(f"  ⚠ 飞书失败, 本产品不生成且不记进度: {e}")
                if pr["tmp_dir"].exists():
                    shutil.rmtree(pr["tmp_dir"], ignore_errors=True)
                if advance_progress:
                    _record_failed_ids(
                        progress, album_id,
                        _items_for_goods_ids(groups, pr["goods_ids"]),
                    )
                continue
        try:
            create_product_folder(final, folder_name, OUTPUT_DIR)
        except Exception as e:
            print(f"  ⚠ 文件夹生成失败, 本产品不记进度: {e}")
            if pr["tmp_dir"].exists():
                shutil.rmtree(pr["tmp_dir"], ignore_errors=True)
            if advance_progress:
                _record_failed_ids(
                    progress, album_id,
                    _items_for_goods_ids(groups, pr["goods_ids"]),
                )
            continue
        if pr["tmp_dir"].exists():
            shutil.rmtree(pr["tmp_dir"])
        n += 1
        if advance_progress:
            _record_processed_ids(progress, album_id, groups, pr["goods_ids"])
        if pending_key:
            feishu_pending.pop(pending_key, None)
            try:
                save_json(FEISHU_PENDING_FILE, feishu_pending)
            except Exception as e:
                print(f"  ⚠ 飞书待处理状态清理失败, 产品已生成: {e}")
    return n


def apply_code_filter(items, code):
    """保留文案(title)包含 code 的条目."""
    return [it for it in items if code in (it.get("title") or "")]


def process_supplier(
        supplier_name, album_id, raw_items, progress, feishu, fs_cfg, ai_cfg,
        code="", review_id="", review_label="", raise_interrupt=False):
    """直接处理(不预览): 过滤→AI分组→处理.
    code 非空 = 定向模式: 在全部条目里按编码筛, 跳过按天进度过滤, 且不推进进度."""
    print(f"\n{'='*50}\n处理供货商: {supplier_name}")
    if code:
        items = apply_code_filter(raw_items, code)
        print(f"  定向: 文案含「{code}」的 {len(items)} 条 (不推进进度)")
    else:
        items = filter_new_items(album_id, raw_items, progress)
    if not items:
        print("  没有新内容")
        return 0
    if code:
        groups = [sorted(items, key=lambda x: x["time_stamp"])]
        print(f"  同一编码按 1 个产品处理 ({len(items)} 帖, 从旧到新)")
    else:
        print(f"  AI 分组中 ({len(items)} 帖)...")
        try:
            groups = group_products_ai(
                ai_cfg, items, TMP_ROOT / f"grpcache_{album_id[-8:]}"
            )
        except RuntimeError as e:
            print(f"  ❌ {e}; 自动模式停止, 请用默认交互流程人工确认分组")
            return 0
    print(f"  分为 {len(groups)} 个产品")
    return process_groups(
        supplier_name, album_id, groups, progress, feishu, fs_cfg, ai_cfg,
        advance_progress=not code,
        review=bool(code),
        order_key=(lambda it: it["time_stamp"]) if code else _item_order,
        review_id=review_id,
        review_label=review_label,
        raise_interrupt=raise_interrupt,
    )


# ── 预览确认界面 ──────────────────────────────────────
def thumb(url):
    """图片 URL 加缩略参数, 预览加载更快."""
    return url.split("?")[0] + "?imageMogr2/thumbnail/!200x200r/quality/80"


def video_thumb(url):
    """七牛视频首帧 URL, 用于工作台缩略图."""
    return url.split("?")[0] + "?vframe/jpg/offset/0"


def _workbench_item(item):
    """保留处理管线需要的原始条目, 另附工作台用的懒加载媒体缩略图."""
    value = dict(item)
    media = []
    seen = set()
    for url in item.get("imgsSrc") or []:
        if not url or url in seen:
            continue
        seen.add(url)
        path = url.split("?")[0].lower()
        is_video = path.endswith(VIDEO_EXTS) and "vframe" not in url.lower()
        media.append({
            "url": url,
            "type": "video" if is_video else "image",
            "thumb": video_thumb(url) if is_video else thumb(url),
        })
    video = item.get("videoUrl") or item.get("videoURL") or ""
    if video and video not in seen:
        media.append({"url": video, "type": "video", "thumb": video_thumb(video)})
    value["workbenchMedia"] = media
    return value


def _workbench_payload(data, progress):
    """工作台数据: 待处理内容与全部历史同时提供, 默认只展示待处理内容."""
    suppliers = []
    for aid, bucket in data.items():
        all_items = sorted(
            bucket.get("items") or [],
            key=_workbench_datetime,
            reverse=True,
        )
        capture_ok = bucket.get("capture_ok")
        if not all_items and capture_ok is not False:
            continue
        state = _ensure_progress_state(aid, all_items, progress)
        statuses = {
            item.get("goods_id"): _progress_item_status(item, state)
            for item in all_items
        }
        pending_statuses = {"new", "updated", "failed"}
        pending_ids = {
            gid for gid, status in statuses.items() if status in pending_statuses
        }
        status_counts = {
            status: sum(1 for value in statuses.values() if value == status)
            for status in ("new", "updated", "failed", "done")
        }
        workbench_items = []
        for item in all_items:
            value = _workbench_item(item)
            value["workbenchStatus"] = statuses.get(item.get("goods_id"), "new")
            value["_new"] = value["workbenchStatus"] in pending_statuses
            value["workbenchDate"] = _workbench_datetime(item).strftime("%m/%d")
            value["workbenchTime"] = _workbench_datetime(item).strftime("%m/%d %H:%M")
            workbench_items.append(value)
        suppliers.append({
            "supplier": bucket.get("supplier", aid[-8:]),
            "albumId": aid,
            "pendingCount": len(pending_ids),
            "statusCounts": status_counts,
            "captureOk": capture_ok,
            "captureAt": bucket.get("captured_at", ""),
            "captureError": bucket.get("capture_error", ""),
            "items": workbench_items,
        })
    return suppliers


def build_workbench_html(suppliers, out_path, capture_time=""):
    """生成日常选品工作台, 输出格式复用 confirmed_groups.json."""
    payload = json.dumps(suppliers, ensure_ascii=False).replace("</", "<\\/")
    capture_payload = json.dumps(capture_time, ensure_ascii=False)
    html = r'''<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>日常选品工作台</title>
<style>
:root{--top-height:60px;--tabs-height:48px;font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:#1d1d1f;background:#f5f5f7}
*{box-sizing:border-box}body{margin:0}.top{position:sticky;top:0;z-index:20;background:#fff;border-bottom:1px solid #e5e5ea;padding:12px 18px;box-shadow:0 2px 8px #0000000d}
.topline{display:flex;gap:12px;align-items:center;flex-wrap:wrap}.top h1{font-size:18px;margin:0 12px 0 0}.muted{color:#86868b;font-size:12px}.supplier-toggle{display:none}.date-filter{display:inline-flex;align-items:center;gap:6px;font-size:13px}.date-filter select{border:1px solid #d2d2d7;border-radius:8px;background:#fff;padding:6px 8px;color:#1d1d1f}
button{border:1px solid #d2d2d7;background:#fff;border-radius:8px;padding:7px 12px;cursor:pointer;color:#1d1d1f}button:hover{border-color:#0071e3;color:#0071e3}
button.primary{background:#0071e3;color:#fff;border-color:#0071e3;font-weight:600}button.primary:disabled{opacity:.45;cursor:not-allowed}
button.danger{color:#c62828}.switch{display:inline-flex;gap:6px;align-items:center;font-size:13px}.supplier-list{display:flex;gap:8px;flex-wrap:wrap;margin-top:10px;max-height:2000px;overflow:hidden;opacity:1}.top.collapsed .supplier-list{max-height:0;margin-top:0;opacity:0;pointer-events:none}.top.collapsed .supplier-toggle{display:inline-flex}
.supplier-list label{display:inline-flex;align-items:center;gap:5px;background:#f5f5f7;border-radius:18px;padding:6px 10px;font-size:13px;cursor:pointer}.supplier-list label.active{background:#e8f2ff;color:#0066cc}
.supplier-list small{color:#86868b}.layout{display:grid;grid-template-columns:minmax(0,1fr) 300px;gap:14px;max-width:1500px;margin:0 auto;padding:14px}.timeline{min-width:0}.tabs{position:sticky;top:var(--top-height);z-index:12;display:flex;gap:6px;overflow:auto;padding:8px 0;background:#f5f5f7}.tab{white-space:nowrap}.tab.active{border-color:#0071e3;color:#0071e3;background:#e8f2ff}
.day{margin:14px 0}.day-title{position:sticky;top:calc(var(--top-height) + var(--tabs-height));z-index:4;display:block;width:100%;border:0;border-radius:0;background:#f5f5f7;color:#555;padding:6px 2px;font-size:12px;font-weight:600;text-align:left}.day-title:hover{color:#0071e3}.entry{background:#fff;border:2px solid #e5e5ea;border-radius:12px;padding:10px;margin:8px 0;cursor:pointer}.entry.selected{border-color:#0071e3;background:#f5faff}.entry.locked{opacity:.45;background:#f0f0f2}.entry-head{display:flex;gap:10px;align-items:flex-start}.entry-info{min-width:180px;flex:1}.entry-time{font-size:12px;color:#86868b}.entry-title{font-size:13px;line-height:1.4;margin-top:4px;white-space:pre-wrap;max-height:54px;overflow:hidden}.entry-badge{font-size:11px;color:#86868b;white-space:nowrap}.status{font-weight:600}.status-new{color:#0071e3}.status-updated{color:#ff9500}.status-failed{color:#c62828}.status-read{color:#86868b}.status-done{color:#86868b}.media-strip{display:flex;flex-wrap:wrap;gap:7px;margin-top:9px}.media{width:78px;height:78px;border-radius:8px;overflow:hidden;position:relative;background:#eee;border:2px solid transparent;padding:0}.media:hover{border-color:#0071e3}.media img{width:100%;height:100%;object-fit:cover;display:block}.media.video{display:flex;align-items:center;justify-content:center;background:#222;color:#fff;font-size:12px}.media .mark{position:absolute;right:3px;bottom:3px;background:#000b;color:#fff;border-radius:4px;padding:1px 4px;font-size:10px}.media.anchor{border-color:#ff9500;box-shadow:0 0 0 2px #ff950044}.media.range{border-color:#0071e3}.entry-marker{font-size:11px;color:#0071e3;margin-top:7px}.entry-marker b{color:#ff9500}.compact .media:not(:first-child){display:none}.compact .media-strip{min-height:78px}
.side{position:sticky;top:calc(var(--top-height) + var(--tabs-height));align-self:start}.panel{background:#fff;border-radius:12px;padding:14px;margin-bottom:12px;border:1px solid #e5e5ea}.panel h2{font-size:14px;margin:0 0 9px}.selection{font-size:12px;line-height:1.6;color:#555;min-height:48px}.selection strong{color:#1d1d1f}.count{font-size:12px;color:#0071e3;margin:8px 0}.draft{border-top:1px solid #e5e5ea;padding:9px 0;font-size:12px}.draft:first-of-type{border-top:0}.draft-title{font-weight:600}.draft-meta{color:#86868b;margin-top:3px}.job-actions{margin-top:6px}.job-actions button{font-size:11px;padding:4px 8px}.empty{padding:40px 12px;text-align:center;color:#86868b;background:#fff;border-radius:12px}
#media-hover-preview{display:none;position:fixed;z-index:100;pointer-events:none;width:min(560px,calc(100vw - 16px));height:min(640px,70vh);padding:8px;box-sizing:border-box;border-radius:10px;background:#111;box-shadow:0 12px 36px rgba(0,0,0,.35);align-items:center;justify-content:center}#media-hover-preview img,#media-hover-preview video{display:block;max-width:100%;max-height:100%;object-fit:contain}.media.video{color:#fff}.media.video img{position:absolute;inset:0}.video-label{position:absolute;left:50%;top:50%;transform:translate(-50%,-50%);background:#000a;border-radius:16px;padding:5px 9px;font-size:11px;white-space:nowrap}
@media(max-width:900px){.layout{grid-template-columns:1fr}.side{position:static}.day-title{top:calc(var(--top-height) + var(--tabs-height))}}
</style></head><body>
<header class="top" id="top"><div class="topline"><h1>📦 日常选品工作台</h1><span class="muted" id="capture"></span><span class="muted" id="summary"></span><label class="switch"><input id="history" type="checkbox"> 查看全部历史</label><label class="date-filter">日期<select id="dateFilter"><option value="">全部日期</option></select></label><button id="compact">显示全部图片</button><button class="mark-read" type="button">本批已阅</button><button id="supplierToggle" class="supplier-toggle" type="button" aria-expanded="true">收起供货商</button></div><div class="supplier-list" id="supplierList"></div></header>
<main class="layout"><section class="timeline"><div class="tabs" id="tabs"></div><div id="entries"></div></section><aside class="side"><div class="panel"><h2>当前选择</h2><div class="selection" id="selection">先点击一个条目作为起点</div><div class="count" id="rangeCount"></div><button class="primary" id="create" disabled>创建商品</button><button id="clear" style="margin-left:6px">清除选择</button></div><div class="panel"><h2>商品队列 <span id="draftCount">0</span></h2><div id="drafts"><div class="muted">还没有创建商品</div></div><button class="danger" id="undo" disabled>撤销上一个草稿</button><button class="primary" id="process" disabled style="margin-top:8px;width:100%">确认并开始处理</button></div></aside></main>
<div id="media-hover-preview" aria-hidden="true"></div>
<script>
const DATA=__PAYLOAD__;
const CAPTURE_TIME=__CAPTURE_TIME__;
const state={active:null,history:false,compact:true,supplierOpen:true,date:'',selection:{start:null,end:null},drafts:[],submitted:[],locked:new Set()};
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const current=()=>DATA.find(s=>s.albumId===state.active);
const allCurrentItems=()=>{const s=current();if(!s)return [];return state.history?s.items:s.items.filter(i=>i._new)};
const items=()=>{const list=allCurrentItems();return state.date?list.filter(i=>i.workbenchDate===state.date):list};
function syncLayoutOffsets(){document.documentElement.style.setProperty('--top-height',`${$('top').offsetHeight}px`);document.documentElement.style.setProperty('--tabs-height',`${$('tabs').offsetHeight}px`)}
function init(){
  const first=DATA.find(s=>s.pendingCount>0)||DATA[0];state.active=first?.albumId||null;
  const captureTs=CAPTURE_TIME?Date.parse(CAPTURE_TIME.replace(' ','T')):NaN;const stale=captureTs&&!Number.isNaN(captureTs)&&Date.now()-captureTs>24*60*60*1000;$('capture').textContent=CAPTURE_TIME?(stale?`抓取状态：可能过期 · ${CAPTURE_TIME}`:`抓取状态：已成功 · ${CAPTURE_TIME}`):'抓取状态：未知';renderSuppliers();renderDateFilter();renderTabs();render();syncLayoutOffsets();
}
function renderDateFilter(){const dates=[...new Set(allCurrentItems().map(i=>i.workbenchDate).filter(Boolean))];$('dateFilter').innerHTML='<option value="">全部日期</option>'+dates.map(date=>`<option value="${esc(date)}">${esc(date)}</option>`).join('');$('dateFilter').value=state.date;}
function statusLabel(status){return {new:'新增',updated:'已更新',failed:'处理失败',read:'已阅',done:'已处理'}[status]||'待处理'}
function statusSummary(s){const c=s.statusCounts||{},parts=[['new','新增'],['updated','已更新'],['failed','处理失败']].filter(([key])=>c[key]).map(([key,label])=>`${label} ${c[key]}`),pending=(c.new||0)+(c.updated||0)+(c.failed||0);return pending?`待处理 ${pending}${parts.length?'（'+parts.join(' · ')+'）':''}`:'无待处理'}
function captureSummary(s){if(s.captureOk===false)return `抓取失败${s.captureError?' · '+esc(s.captureError):''}`;if(s.captureOk===true)return `${statusSummary(s)} · 本次抓取成功`;return statusSummary(s)}
function renderSuppliers(){
  $('supplierList').innerHTML=DATA.map(s=>`<label class="${s.pendingCount||s.captureOk===false?'active':''}"><input type="checkbox" data-aid="${esc(s.albumId)}" ${s.pendingCount||s.captureOk===false?'checked':''}><span>${esc(s.supplier)}</span><small>${captureSummary(s)}</small></label>`).join('');
  $('supplierList').querySelectorAll('input').forEach(input=>input.onchange=()=>{const selected=[...document.querySelectorAll('#supplierList input:checked')].map(i=>i.dataset.aid);if(!selected.includes(state.active))state.active=selected[0]||null;state.date='';renderDateFilter();renderTabs();render();});
}
function setSupplierOpen(open){if(state.supplierOpen===open)return;state.supplierOpen=open;$('top').classList.toggle('collapsed',!open);$('supplierToggle').textContent=open?'收起供货商':'展开供货商';$('supplierToggle').setAttribute('aria-expanded',String(open));syncLayoutOffsets();}
let lastScrollY=window.scrollY,scrollTick=false;
window.addEventListener('scroll',()=>{if(scrollTick)return;scrollTick=true;requestAnimationFrame(()=>{const y=window.scrollY;if(y>lastScrollY&&state.supplierOpen)setSupplierOpen(false);lastScrollY=y;scrollTick=false})},{passive:true});
function selectedSuppliers(){const ids=new Set([...document.querySelectorAll('#supplierList input:checked')].map(i=>i.dataset.aid));return DATA.filter(s=>ids.has(s.albumId))}
function renderTabs(){const chosen=selectedSuppliers();if(!chosen.some(s=>s.albumId===state.active))state.active=chosen[0]?.albumId||null;$('tabs').innerHTML=chosen.map(s=>`<button class="tab ${s.albumId===state.active?'active':''}" data-aid="${esc(s.albumId)}">${esc(s.supplier)} · ${state.history?s.items.length:s.pendingCount}</button>`).join('');$('tabs').querySelectorAll('button').forEach(b=>b.onclick=()=>{state.active=b.dataset.aid;state.date='';state.selection={start:null,end:null};renderDateFilter();renderTabs();render()});syncLayoutOffsets();}
function mediaFor(item){return item.workbenchMedia||[]}
function sameAnchor(a,b){return a&&b&&a.goodsId===b.goodsId}
function currentRange(){const list=items(),s=state.selection;if(!s.start||!s.end)return null;const a=list.findIndex(i=>i.goods_id===s.start.goodsId),b=list.findIndex(i=>i.goods_id===s.end.goodsId);if(a<0||b<0)return null;const lo=Math.min(a,b),hi=Math.max(a,b);return {lo,hi,items:list.slice(lo,hi+1)};}
function clickEntry(item){
  if(state.locked.has(item.goods_id))return;
  const a={goodsId:item.goods_id};
  if(!state.selection.start)state.selection.start=a;
  else if(!state.selection.end){if(sameAnchor(state.selection.start,a))state.selection={start:null,end:null};else state.selection.end=a}
  else if(sameAnchor(state.selection.start,a)||sameAnchor(state.selection.end,a))state.selection={start:null,end:null};
  else state.selection={start:a,end:null};
  render();
}
function mediaHTML(item,mi,m,range){const inRange=range&&range.items.some(i=>i.goods_id===item.goods_id);const cls=['media',m.type==='video'?'video':'',inRange?'range':''].filter(Boolean).join(' ');const thumbUrl=m.thumb||(m.type==='video'?videoThumbFallback(m.url):thumbFallback(m.url));if(m.type==='video')return `<button type="button" class="${cls}" data-goods="${esc(item.goods_id)}" data-media="${mi}" title="悬停预览视频"><img loading="lazy" decoding="async" src="${esc(thumbUrl)}"><span class="video-label">▶ 视频</span><span class="mark">${mi+1}</span></button>`;return `<button type="button" class="${cls}" data-goods="${esc(item.goods_id)}" data-media="${mi}" title="悬停查看大图"><img loading="lazy" decoding="async" src="${esc(thumbUrl)}"><span class="mark">${mi+1}</span></button>`}
function thumbFallback(url){const clean=String(url||'').split('?')[0];return clean+'?imageMogr2/thumbnail/!200x200r/quality/80'}
function videoThumbFallback(url){const clean=String(url||'').split('?')[0];return clean+'?vframe/jpg/offset/0'}
function hideMediaPreview(){const video=$('media-hover-preview').querySelector('video');if(video)video.pause();$('media-hover-preview').replaceChildren();$('media-hover-preview').style.display='none'}
function showMediaPreview(m,target){hideMediaPreview();if(!m||!m.url)return;const box=$('media-hover-preview'),media=document.createElement(m.type==='video'?'video':'img');if(m.type==='video'){media.src=m.url;media.poster=m.thumb||videoThumbFallback(m.url);media.muted=true;media.loop=true;media.autoplay=true;media.playsInline=true}else media.src=m.url;box.appendChild(media);box.style.display='flex';const r=target.getBoundingClientRect(),gap=12,w=box.offsetWidth,h=box.offsetHeight;let left=r.right+gap;if(left+w>innerWidth-8)left=r.left-w-gap;box.style.left=Math.max(8,left)+'px';box.style.top=Math.max(8,Math.min(r.top,innerHeight-h-8))+'px';if(m.type==='video')media.play().catch(()=>{})}
function bindMediaHover(list){$('entries').querySelectorAll('.media').forEach(button=>{const item=list.find(i=>i.goods_id===button.dataset.goods),m=item&&mediaFor(item)[Number(button.dataset.media)];const show=()=>showMediaPreview(m,button),hide=()=>hideMediaPreview();button.addEventListener('mouseenter',show);button.addEventListener('mouseleave',hide);button.addEventListener('focus',show);button.addEventListener('blur',hide)})}
function render(){
  hideMediaPreview();const s=current(),list=items(),range=currentRange();
  $('summary').textContent=s?`${s.supplier} · ${state.history?`${list.length} 条历史`:statusSummary(s)}`:'请先选择供货商';
  $('entries').className=state.compact?'compact':'';
  if(!s||!list.length){$('entries').innerHTML='<div class="empty">当前没有可展示的内容</div>';renderSide(null);return}
  let html='',lastDate='';list.forEach((item,index)=>{const date=item.workbenchDate||'';if(date!==lastDate){html+=`<div class="day"><button class="day-title" data-date="${esc(date)}" title="只看 ${esc(date)}">${esc(date)}</button>`;lastDate=date}const locked=state.locked.has(item.goods_id),inRange=range?.items.some(i=>i.goods_id===item.goods_id);const media=mediaFor(item),status=item.workbenchStatus||'new';html+=`<article class="entry ${locked?'locked ':''}${inRange?'selected':''}" data-goods="${esc(item.goods_id)}"><div class="entry-head"><div class="entry-info"><div class="entry-time">${esc(item.workbenchTime||'时间未知')}</div><div class="entry-title">${esc(item.title||'(无文案)')}</div></div><div class="entry-badge">${media.length} 个素材 · <span class="status status-${status}">${statusLabel(status)}</span>${locked?' · 已创建':''}</div></div><div class="media-strip">${media.map((m,mi)=>mediaHTML(item,mi,m,range)).join('')}</div>${inRange?'<div class="entry-marker">已在当前范围内</div>':''}</article>`;const nextDate=list[index+1]?.workbenchDate;if(index===list.length-1||date!==nextDate)html+='</div>'});
  $('entries').innerHTML=html;$('entries').querySelectorAll('.entry').forEach(article=>{const item=list.find(i=>i.goods_id===article.dataset.goods);article.onclick=()=>clickEntry(item)});$('entries').querySelectorAll('.media').forEach(b=>b.onclick=e=>e.stopPropagation());bindMediaHover(list);$('entries').querySelectorAll('.day-title').forEach(b=>b.onclick=()=>{state.date=b.dataset.date;state.selection={start:null,end:null};renderDateFilter();render()});renderSide(range);
}
 function renderSide(range){const s=current(),sel=state.selection;const fmt=a=>{if(!a)return'未选择';const i=(s?.items||[]).find(x=>x.goods_id===a.goodsId);return `${i?.workbenchTime||a.goodsId} · 条目`};$('selection').innerHTML=`起点：<strong>${esc(fmt(sel.start))}</strong><br>终点：<strong>${esc(fmt(sel.end))}</strong>`;const overlap=range&&range.items.some(i=>state.locked.has(i.goods_id));$('rangeCount').textContent=range?`${range.items.length} 个条目，${range.items.reduce((n,i)=>n+mediaFor(i).length,0)} 个素材${overlap?' · 包含已创建内容':''}`:'';$('create').disabled=!range||overlap;$('draftCount').textContent=state.drafts.length;$('undo').disabled=!state.drafts.length;$('process').disabled=!state.drafts.length;$('drafts').innerHTML=state.drafts.length?state.drafts.map((d,i)=>`<div class="draft"><div class="draft-title">${i+1}. ${esc(d.supplier)} · 商品 ${d.index}</div><div class="draft-meta">${d.items.length} 个条目 · ${d.items.reduce((n,x)=>n+mediaFor(x).length,0)} 个素材 · ${esc(d.label)}</div></div>`).join(''):'<div class="muted">还没有创建商品</div>'}
 $('supplierToggle').onclick=()=>{setSupplierOpen(!state.supplierOpen);lastScrollY=window.scrollY};$('dateFilter').onchange=()=>{state.date=$('dateFilter').value;state.selection={start:null,end:null};render()};$('history').onchange=()=>{state.history=$('history').checked;state.date='';state.selection={start:null,end:null};renderDateFilter();renderTabs();render()};$('compact').onclick=()=>{state.compact=!state.compact;$('compact').textContent=state.compact?'显示全部图片':'只显示首图';render()};$('clear').onclick=()=>{state.selection={start:null,end:null};render()};$('create').onclick=()=>{const r=currentRange(),s=current();if(!r||!s)return;const ids=r.items.map(i=>i.goods_id);if(ids.some(id=>state.locked.has(id)))return;const draft={supplier:s.supplier,albumId:s.albumId,items:r.items,index:state.drafts.filter(d=>d.albumId===s.albumId).length+1,label:`${ids[0]} → ${ids[ids.length-1]}`};state.drafts.push(draft);ids.forEach(id=>state.locked.add(id));state.selection={start:null,end:null};render()};$('undo').onclick=()=>{const d=state.drafts.pop();if(d)d.items.forEach(i=>state.locked.delete(i.goods_id));render()};$('process').onclick=()=>{const batch=state.drafts.slice();if(!batch.length)return;const grouped=[];const by=new Map();const cleanItem=i=>{const {workbenchMedia,_new,workbenchDate,workbenchTime,workbenchStatus,...raw}=i;return raw};batch.forEach(d=>{if(!by.has(d.albumId)){const value={supplier:d.supplier,albumId:d.albumId,groups:[]};by.set(d.albumId,value);grouped.push(value)}by.get(d.albumId).groups.push(d.items.map(cleanItem))});const blob=new Blob([JSON.stringify({suppliers:grouped},null,2)],{type:'application/json'});const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=`confirmed_groups_${Date.now()}.json`;a.click();state.drafts=[];state.selection={start:null,end:null};render();alert(`已提交 ${batch.length} 个商品，继续选择新商品即可提交下一批`);};init();
</script>
<script>
function downloadJson(name,value){const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(value,null,2)],{type:'application/json'}));a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(a.href),1000)}
function cancelSubmitted(jobId){const job=state.submitted.find(item=>item.jobId===jobId);if(!job)return;downloadJson(`cancelled_job_${job.jobId}.json`,{jobId:job.jobId});state.submitted=state.submitted.filter(item=>item.jobId!==jobId);job.items.forEach(item=>state.locked.delete(item.goods_id));state.selection={start:null,end:null};render();alert('已撤销该商品，可以重新选择');}
function renderSide(range){const s=current(),sel=state.selection;const fmt=a=>{if(!a)return'未选择';const i=(s?.items||[]).find(x=>x.goods_id===a.goodsId);return `${i?.workbenchTime||a.goodsId} · 条目`};const overlap=range&&range.items.some(i=>state.locked.has(i.goods_id));$('selection').innerHTML=`起点：<strong>${esc(fmt(sel.start))}</strong><br>终点：<strong>${esc(fmt(sel.end))}</strong>`;$('rangeCount').textContent=range?`${range.items.length} 个条目，${range.items.reduce((n,i)=>n+mediaFor(i).length,0)} 个素材${overlap?' · 包含已创建内容':''}`:'';$('create').disabled=!range||overlap;$('draftCount').textContent=state.drafts.length+state.submitted.length;$('undo').disabled=!state.drafts.length;$('process').disabled=!state.drafts.length;const drafts=state.drafts.map((d,i)=>`<div class="draft"><div class="draft-title">${i+1}. ${esc(d.supplier)} · 商品 ${d.index}</div><div class="draft-meta">待提交 · ${d.items.length} 个条目 · ${d.items.reduce((n,x)=>n+mediaFor(x).length,0)} 个素材 · ${esc(d.label)}</div></div>`).join('');const submitted=state.submitted.map(job=>`<div class="draft"><div class="draft-title">${esc(job.supplier)} · ${esc(job.label||'商品')}</div><div class="draft-meta">处理中/等待分类预览 · ${job.items.length} 个条目</div><div class="job-actions"><button class="danger cancel-job" data-job="${esc(job.jobId)}">撤销并重新选择</button></div></div>`).join('');$('drafts').innerHTML=drafts+submitted||( '<div class="muted">还没有创建商品</div>');$('drafts').querySelectorAll('.cancel-job').forEach(button=>button.onclick=()=>cancelSubmitted(button.dataset.job));}
$('process').onclick=()=>{const batch=state.drafts.slice();if(!batch.length)return;const stamp=Date.now();const cleanItem=i=>{const {workbenchMedia,_new,workbenchDate,workbenchTime,workbenchStatus,...raw}=i;return raw};const jobs=batch.map((d,index)=>({jobId:`${stamp}_${index+1}`,supplier:d.supplier,albumId:d.albumId,label:`${d.supplier} · 商品 ${d.index}`,items:d.items.map(cleanItem)}));downloadJson(`confirmed_groups_${stamp}.json`,{jobs});state.submitted.push(...jobs);state.drafts=[];state.selection={start:null,end:null};render();alert(`已提交 ${jobs.length} 个商品，分类预览可同时生成；如需重选，可在商品队列中撤销`);};
function refreshSupplierStatus(s){const counts={new:0,updated:0,failed:0,read:0,done:0};s.items.forEach(item=>{counts[item.workbenchStatus||'new']=(counts[item.workbenchStatus||'new']||0)+1});s.statusCounts=counts;s.pendingCount=(counts.new||0)+(counts.updated||0)+(counts.failed||0)}
function markCurrentRead(){const s=current(),pending=new Set(['new','updated','failed']),list=items().filter(item=>pending.has(item.workbenchStatus));if(!s||!list.length){alert('当前没有待阅条目');return}downloadJson(`read_items_${Date.now()}.json`,{reads:[{supplier:s.supplier,albumId:s.albumId,items:list.map(item=>({goods_id:item.goods_id,title:item.title||'',imgsSrc:item.imgsSrc||[],time_stamp:item.time_stamp,update_time:item.update_time,videoUrl:item.videoUrl||item.videoURL||''}))}]});list.forEach(item=>{item.workbenchStatus='read';item._new=false});refreshSupplierStatus(s);renderSuppliers();renderDateFilter();renderTabs();render();alert(`已阅 ${list.length} 个条目；内容以后发生变化时仍会重新进入待处理`)}
document.querySelectorAll('.mark-read').forEach(button=>button.onclick=markCurrentRead);
render();
</script></body></html>'''
    html = html.replace("__PAYLOAD__", payload).replace("__CAPTURE_TIME__", capture_payload)
    out_path.write_text(html, encoding="utf-8")


def build_preview_html(previews, out_path):
    """previews: [{supplier, albumId, posts:[...], boundaries:[bool]}]
    boundaries[i]=True 表示 posts[i] 与 posts[i+1] 之间是分界(不同产品).
    """
    payload = json.dumps(previews, ensure_ascii=False)
    html = """<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>分组预览确认</title>
<style>
body{font-family:-apple-system,sans-serif;margin:0;background:#f5f5f7;color:#1d1d1f}
header{position:sticky;top:0;background:#fff;padding:12px 16px;box-shadow:0 1px 4px rgba(0,0,0,.1);z-index:10;display:flex;justify-content:space-between;align-items:center}
h2{margin:0;font-size:16px}
#confirm{background:#0071e3;color:#fff;border:0;border-radius:20px;padding:8px 20px;font-size:14px;cursor:pointer}
#confirm:hover{background:#0077ed}
.supplier{margin:16px}
.supplier>h3{font-size:15px;margin:8px 0}
.prod{background:#fff;border-radius:12px;margin:10px 0;overflow:hidden;border:2px solid #e5e5ea}
.prodhead{padding:8px 12px;font-weight:600;font-size:13px;background:#f0f7ff;color:#0071e3;display:flex;justify-content:space-between}
.cards{display:flex;flex-wrap:wrap;gap:8px;padding:10px}
.card{width:120px;font-size:11px}
.card img{width:120px;height:120px;object-fit:cover;border-radius:8px;background:#eee;display:block}
.card .t{margin-top:4px;color:#555;line-height:1.3;max-height:44px;overflow:hidden}
.card .time{color:#aaa;font-size:10px}
.divider{text-align:center;padding:2px}
.divider button{border:0;border-radius:14px;padding:4px 14px;font-size:12px;cursor:pointer}
.same{background:#e8f5e9;color:#2e7d32}
.split{background:#ff0f0;color:#d32f2f;font-weight:600}
.hint{font-size:12px;color:#888;margin:4px 16px}
</style></head><body>
<header><h2>📦 分组预览 — 拖不了,点两帖之间的按钮改边界</h2>
<button id="confirm">✅ 确认并下载分组</button></header>
<div class="hint">绿色「同一产品」=合并 · 红色「✂分界」=拆成两个产品。点击切换。改完点右上角确认,会下载 confirmed_groups.json</div>
<div id="app"></div>
<script>
const DATA = __PAYLOAD__;
function render(){
  const app=document.getElementById('app');app.innerHTML='';
  DATA.forEach((sup,si)=>{
    const sec=document.createElement('div');sec.className='supplier';
    sec.innerHTML='<h3>🏪 '+sup.supplier+' ('+sup.posts.length+' 帖)</h3>';
    // 依据 boundaries 切分成产品块
    let groups=[[0]];
    for(let i=0;i<sup.boundaries.length;i++){
      if(sup.boundaries[i]) groups.push([i+1]); else groups[groups.length-1].push(i+1);
    }
    groups.forEach((g,gi)=>{
      const prod=document.createElement('div');prod.className='prod';
      prod.innerHTML='<div class="prodhead"><span>产品 '+(gi+1)+'</span><span>'+g.length+' 帖</span></div>';
      const cards=document.createElement('div');cards.className='cards';
      g.forEach(pi=>{
        const p=sup.posts[pi];
        const c=document.createElement('div');c.className='card';
        c.innerHTML='<img loading="lazy" src="'+(p.thumb||'')+'"><div class="t">'+(p.title||'(无文案)').slice(0,40)+'</div><div class="time">'+p.time+' · '+p.imgs+'图</div>';
        cards.appendChild(c);
      });
      prod.appendChild(cards);sec.appendChild(prod);
      // 产品块之间的分界按钮 (最后一帖 与 下一产品第一帖)
      const lastPi=g[g.length-1];
      if(lastPi<sup.posts.length-1){
        const div=document.createElement('div');div.className='divider';
        const b=document.createElement('button');
        b.className='split';b.textContent='✂ 分界 (点击合并)';
        b.onclick=()=>{sup.boundaries[lastPi]=false;render();};
        div.appendChild(b);sec.appendChild(div);
      }
    });
    // 产品块内部相邻帖的"合并中"按钮 — 允许再拆开
    app.appendChild(sec);
    // 补充: 在每个产品内部帖之间加"同一产品(点击拆开)"按钮
    // 简化实现: 重画时用扁平方式在卡片间插分界更直观 -> 见 renderFlat
  });
}
// 扁平渲染: 卡片顺序排列, 每两帖间一个按钮, 更好操作
function renderFlat(){
  const app=document.getElementById('app');app.innerHTML='';
  DATA.forEach((sup,si)=>{
    const sec=document.createElement('div');sec.className='supplier';
    // 计算每帖的产品号
    let prod=1;const prodOf=[1];
    for(let i=0;i<sup.boundaries.length;i++){if(sup.boundaries[i])prod++;prodOf.push(prod);}
    sec.innerHTML='<h3>🏪 '+sup.supplier+' ('+sup.posts.length+' 帖 → '+prod+' 个产品)</h3>';
    const wrap=document.createElement('div');
    sup.posts.forEach((p,pi)=>{
      const card=document.createElement('div');card.className='prod';
      card.style.borderColor=['#e5e5ea','#c9e3ff','#ffe0b2','#d7f5d7','#f5d7f0'][prodOf[pi]%5];
      card.innerHTML='<div class="prodhead"><span>产品 '+prodOf[pi]+'</span><span>'+p.time+' · '+p.imgs+'图</span></div>'+
        '<div class="cards"><div class="card"><img loading="lazy" src="'+(p.thumb||'')+'"><div class="t">'+(p.title||'(无文案)').slice(0,50)+'</div></div></div>';
      wrap.appendChild(card);
      if(pi<sup.posts.length-1){
        const div=document.createElement('div');div.className='divider';
        const b=document.createElement('button');
        const split=sup.boundaries[pi];
        b.className=split?'split':'same';
        b.textContent=split?'✂ 分界':'— 同一产品 —';
        b.onclick=()=>{sup.boundaries[pi]=!sup.boundaries[pi];renderFlat();};
        div.appendChild(b);wrap.appendChild(div);
      }
    });
    sec.appendChild(wrap);app.appendChild(sec);
  });
}
document.getElementById('confirm').onclick=()=>{
  const out={suppliers:DATA.map(sup=>{
    let groups=[[sup.posts[0]]];
    for(let i=0;i<sup.boundaries.length;i++){
      if(sup.boundaries[i])groups.push([sup.posts[i+1]]);else groups[groups.length-1].push(sup.posts[i+1]);
    }
    return {supplier:sup.supplier,albumId:sup.albumId,groups};
  })};
  const blob=new Blob([JSON.stringify(out,null,2)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='confirmed_groups.json';
  document.body.appendChild(a);a.click();
  alert('已下载 confirmed_groups.json — 回到终端继续处理');
};
renderFlat();
</script></body></html>"""
    html = html.replace("__PAYLOAD__", payload)
    out_path.write_text(html, encoding="utf-8")


def list_available(data, progress, code=""):
    """返回真实待处理的供货商: [(name, aid, 新增条数), ...]
    定向模式(code 非空)按编码筛全量; 否则按进度筛新的. 只列有内容的.
    """
    out = []
    for aid, b in data.items():
        items = b.get("items", [])
        if not items:
            continue
        if code:
            n = sum(1 for it in items if code in (it.get("title") or ""))
        else:
            n = len(filter_new_items(aid, items, progress))
        if n > 0:
            out.append((b.get("supplier", aid[-8:]), aid, n))
    return out


def cmd_preview(config, progress, data, code=""):
    """生成分组预览 HTML. code 非空 = 定向: 按编码筛全部条目, 不看进度."""
    available = list_available(data, progress, code)
    if not available:
        print("没有新内容"); return
    chosen = select_suppliers(available)
    if not chosen:
        print("未选择"); return
    ai_cfg = config.get("ai_vision") or {}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    previews = []
    for name, aid, _ in chosen:
        if code:
            items = apply_code_filter(data[aid]["items"], code)
            print(f"  {name}: 定向文案含「{code}」 {len(items)} 条")
        else:
            items = filter_new_items(aid, data[aid]["items"], progress)
        if not items:
            print(f"  {name}: 无新内容, 跳过"); continue
        print(f"  {name}: AI 分组中 ({len(items)} 帖)...")
        posts = sorted(items, key=_item_order)
        try:
            groups = group_products_ai(
                ai_cfg, posts, TMP_ROOT / f"grpcache_{aid[-8:]}"
            )
        except RuntimeError as e:
            print(f"  ⚠ {name}: {e}, 已改为每帖一个产品, 请在预览页人工调整边界")
            groups = [[it] for it in posts]
        # 由 groups 反推 boundaries
        gid = {}
        for i, g in enumerate(groups):
            for it in g:
                gid[it["goods_id"]] = i
        boundaries = [gid.get(posts[i]["goods_id"]) != gid.get(posts[i+1]["goods_id"])
                      for i in range(len(posts) - 1)]
        previews.append({
            "supplier": name, "albumId": aid,
            "posts": [{"goods_id": p["goods_id"], "title": p.get("title", ""),
                       "imgsSrc": p.get("imgsSrc", []), "time_stamp": p["time_stamp"],
                       "update_time": p.get("update_time"),
                       "videoUrl": p.get("videoUrl", ""),
                       "thumb": thumb(p["imgsSrc"][0]) if p.get("imgsSrc") else "",
                       "imgs": len(p.get("imgsSrc", [])),
                       "time": datetime.fromtimestamp(p["time_stamp"]/1000).strftime("%m-%d %H:%M")}
                      for p in posts],
            "boundaries": boundaries,
        })
    if not previews:
        print("没有可预览的内容"); return
    out = SCRIPT_DIR / "分组预览.html"
    build_preview_html(previews, out)
    print(f"\n✓ 预览已生成: {out}")
    try:
        import webbrowser
        webbrowser.open(out.as_uri())
        print("  已在浏览器打开. 调整分组后点'确认并下载', 得到 confirmed_groups.json")
    except Exception:
        print(f"  请手动打开: {out}")
    print(f"  然后运行: python3 pick_products.py process data/confirmed_groups.json")


def cmd_workbench(config, progress, data, scrape_path=""):
    """生成日常选品工作台: 默认新增, 可切换全部历史, 输出现有确认格式."""
    before = json.dumps(progress, ensure_ascii=False, sort_keys=True)
    suppliers = _workbench_payload(data, progress)
    if json.dumps(progress, ensure_ascii=False, sort_keys=True) != before:
        save_json(PROGRESS_FILE, progress)
    if not suppliers:
        print("没有可展示的内容"); return False
    out = SCRIPT_DIR / "日常选品工作台.html"
    capture_time = ""
    if scrape_path and Path(scrape_path).exists():
        capture_time = datetime.fromtimestamp(
            Path(scrape_path).stat().st_mtime
        ).strftime("%Y-%m-%d %H:%M:%S")
    build_workbench_html(suppliers, out, capture_time=capture_time)
    print(f"\n✓ 工作台已生成: {out}")
    print("  默认展示新增内容; 可切换供货商和全部历史, 选择起止素材后创建商品")
    try:
        _open_in_browser(out)
        print("  已在浏览器打开, 全部商品选完后点‘确认并开始处理’")
    except Exception:
        print(f"  请手动打开: {out}")
    return True


def _normalize_date(date_str):
    """各种简写 → 'YYYY-MM-DD'; 月日允许不补零, 短日期补当前年份."""
    value = str(date_str).strip()
    separated = re.fullmatch(
        r"(?:(?P<year>\d{4})-)?(?P<month>\d{1,2})-(?P<day>\d{1,2})",
        value,
    )
    compact = re.fullmatch(r"\d{4}|\d{8}", value)
    if separated:
        year = int(separated.group("year") or datetime.now().year)
        month = int(separated.group("month"))
        day = int(separated.group("day"))
    elif compact and len(value) == 4:
        year = datetime.now().year
        month, day = int(value[:2]), int(value[2:])
    elif compact:
        year, month, day = int(value[:4]), int(value[4:6]), int(value[6:])
    else:
        raise ValueError(
            f"日期格式无效「{value}」, 请使用 7-21、07-21 或 2026-07-21"
        )
    try:
        return datetime(year, month, day).strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError(
            f"日期格式无效「{value}」, 请检查年月日是否存在"
        )

def _data_has_date(scrape_path, config, supplier_name, date_str, require_order=True):
    """检查 scrape 里是否有目标发布日期, 默认同时要求页面排序字段."""
    p = Path(scrape_path)
    if not p.exists():
        return False
    try:
        d = load_scrape(str(p), config)
    except Exception:
        return False
    # 供货商模糊匹配
    aid = config.get("suppliers", {}).get(supplier_name)
    if not aid:
        for n, a in config["suppliers"].items():
            if supplier_name in n:
                aid = a; break
    if not aid or aid not in d:
        return False
    matches = [it for it in d[aid].get("items", []) if _item_display_date(it) == date_str]
    return bool(matches) and (not require_order or all(it.get("update_time") for it in matches))


def _data_has_code(scrape_path, config, supplier_name, code):
    """检查指定供货商的抓取数据里是否已有目标编码."""
    p = Path(scrape_path)
    if not p.exists():
        return False
    try:
        d = load_scrape(str(p), config)
    except Exception:
        return False
    aid = config.get("suppliers", {}).get(supplier_name)
    if not aid:
        for n, a in config.get("suppliers", {}).items():
            if supplier_name in n:
                aid = a; break
    return bool(aid and aid in d and apply_code_filter(d[aid].get("items", []), code))

def _merge_anchor_into_scrape(scrape_path, anchor_json_path):
    """深挖来的单供货商 JSON merge 到主 scrape_all.json.
    anchor 格式: {"supplier":.., "albumId":.., "items":[..]}
    """
    anchor = json.loads(Path(anchor_json_path).read_text())
    aid = anchor.get("albumId"); items = anchor.get("items", [])
    if not aid:
        return False
    if Path(scrape_path).exists():
        raw = json.loads(Path(scrape_path).read_text())
        if not (isinstance(raw, dict) and "data" in raw):
            raw = {"data": {}}
    else:
        raw = {"data": {}}
    existing = raw["data"].get(aid, {"supplier": anchor.get("supplier", ""), "items": []})
    meta = anchor.get("anchor") or {}
    if meta.get("fullScan") is True:
        merged_items = list(items)
    elif meta.get("dateScan") is True:
        scan_date = meta.get("rangeDate")
        merged_items = [
            it for it in existing["items"]
            if _item_display_date(it) != scan_date
        ] + list(items)
    else:
        base_items = existing["items"]
        if meta.get("dateWindow") is True and items:
            oldest_captured_order = min(_item_order(it) for it in items)
            base_items = [
                it for it in base_items
                if _item_order(it) < oldest_captured_order
            ]
        incoming = {it["goods_id"]: it for it in items}
        existing_ids = {it["goods_id"] for it in base_items}
        merged_items = [incoming.get(it["goods_id"], it) for it in base_items]
        merged_items.extend(it for gid, it in incoming.items() if gid not in existing_ids)
    existing = {
        **existing,
        "supplier": anchor.get("supplier") or existing.get("supplier", ""),
        "items": merged_items,
    }
    raw["data"][aid] = existing
    Path(scrape_path).write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    return True


def _code_anchor_problem(anchor_json_path, expected_aid, code):
    """返回本次编码深挖的具体问题; 空串表示可用."""
    try:
        anchor = json.loads(Path(anchor_json_path).read_text())
    except Exception:
        return "下载文件无法读取"
    if anchor.get("albumId") != expected_aid:
        return "下载文件与目标相册不符"
    meta = anchor.get("anchor") or {}
    if "incomplete" not in meta:
        return "仍在使用旧版书签, 请从 install_bookmark.html 重新替换"
    if meta.get("code") != code:
        return "下载文件与本次请求编码不符"
    if meta.get("incomplete"):
        pages = meta.get("pages", 0)
        if meta.get("stopReason") == "limit":
            return f"达到 {pages} 页上限仍未定位完整编码区段"
        return f"第 {pages} 页附近网络请求中断"
    if not apply_code_filter(anchor.get("items", []), code):
        return f"本次深挖没有找到编码「{code}」"
    return ""


def _date_anchor_problem(anchor_json_path, expected_aid, date_str):
    """返回本次日期深挖的具体问题; 空串表示可用."""
    try:
        anchor = json.loads(Path(anchor_json_path).read_text())
    except Exception:
        return "下载文件无法读取"
    if anchor.get("albumId") != expected_aid:
        return "下载文件与目标相册不符"
    meta = anchor.get("anchor") or {}
    if meta.get("date") != date_str:
        return "下载文件与本次请求日期不符"
    if (
        "incomplete" not in meta
        or (
            meta.get("dateWindow") is not True
            and meta.get("fullScan") is not True
        )
    ):
        return "仍在使用旧版书签, 请从 install_bookmark.html 重新替换"
    if meta.get("incomplete"):
        return (f"深挖未完成: {meta.get('pages', 0)} 页, "
                f"停止原因 {meta.get('stopReason', 'unknown')}")
    items = anchor.get("items", [])
    if any(it.get("update_time") is None for it in items):
        return "深挖数据缺少 update_time, 无法还原真实排序"
    if not any(_item_display_date(it) == date_str for it in items):
        return (f"未抓到 {date_str}: 原始 {meta.get('rawCount', '?')} 条, "
                f"清洗后 {len(items)} 条, {meta.get('pages', 0)} 页, "
                f"停止原因 {meta.get('stopReason', 'unknown')}")
    return ""

def _range_anchor_problem(anchor_json_path, expected_aid, date_str, start_prefix, end_prefix):
    """返回本次标题范围深挖的具体问题; 空串表示可用."""
    try:
        anchor = json.loads(Path(anchor_json_path).read_text())
    except Exception:
        return "下载文件无法读取"
    if anchor.get("albumId") != expected_aid:
        return "下载文件与目标相册不符"
    meta = anchor.get("anchor") or {}
    if (
        meta.get("rangeDate") != date_str
        or meta.get("rangeStart") != start_prefix
        or meta.get("rangeEnd") != end_prefix
    ):
        return "下载文件与本次日期或首尾前缀不符"
    if meta.get("dateScan") is not True:
        return "仍在使用旧版书签, 请从 install_bookmark.html 重新替换"
    if meta.get("incomplete"):
        return (f"深挖未完成: {meta.get('pages', 0)} 页, "
                f"停止原因 {meta.get('stopReason', 'unknown')}")
    items = anchor.get("items", [])
    if not items:
        return (f"未抓到 {date_str}: 原始 {meta.get('rawCount', '?')} 条, "
                f"{meta.get('pages', 0)} 页, "
                f"停止原因 {meta.get('stopReason', 'unknown')}")
    if any(it.get("update_time") is None for it in items):
        return "深挖数据缺少 update_time, 无法还原真实排序"
    _, problem = find_title_range(items, start_prefix, end_prefix, date_str)
    return problem


def _chrome_download_paths(base):
    downloads = Path.home() / "Downloads"
    chrome_name = re.compile(rf"^{re.escape(base)}(?: \(\d+\))?\.json$")
    return [
        p for p in downloads.iterdir() if chrome_name.fullmatch(p.name)
    ] if downloads.exists() else []


def pick_newest_download(base):
    """非破坏性选取 Downloads 里最新的 base.json / base (N).json."""
    dls = sorted(_chrome_download_paths(base), key=lambda p: p.stat().st_mtime)
    if not dls:
        return None
    return dls[-1]


def _project_json_paths(base):
    data_dir = _project_data_dir()
    if not data_dir.exists():
        return []
    return [
        p for p in data_dir.iterdir()
        if p.is_file() and p.suffix == ".json" and (
            p.stem == base
            or p.stem.startswith(f"{base} (")
            or p.stem.startswith(f"{base}_")
        )
    ]


def _latest_project_json(base):
    paths = sorted(_project_json_paths(base), key=lambda p: p.stat().st_mtime)
    return paths[-1] if paths else None


def _archive_download(path):
    """把 Chrome Downloads 中的 JSON 移入项目 data, 保留同名旧文件."""
    source = Path(path)
    data_dir = _project_data_dir()
    if source.parent.resolve() == data_dir.resolve():
        return source
    if source.parent.resolve() != (Path.home() / "Downloads").resolve():
        return source
    data_dir.mkdir(parents=True, exist_ok=True)
    target = data_dir / source.name
    if target.exists():
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        target = data_dir / f"{source.stem}_{stamp}{source.suffix}"
        index = 2
        while target.exists():
            target = data_dir / f"{source.stem}_{stamp}_{index}{source.suffix}"
            index += 1
    shutil.move(str(source), str(target))
    return target


def _latest_scrape_path():
    """优先使用项目归档; 若旧文件仍在 Downloads, 首次使用时一并归档."""
    project = _latest_project_json("scrape_all")
    download = pick_newest_download("scrape_all")
    if download and (project is None or download.stat().st_mtime > project.stat().st_mtime):
        return _archive_download(download)
    return project


def _confirmed_download_paths(watch_dir):
    """找出工作台提交的 confirmed_groups 文件, 兼容旧名和带时间戳的新名."""
    pattern = re.compile(r"^confirmed_groups(?:_\d+)?(?: \(\d+\))?\.json$")
    return [p for p in watch_dir.iterdir() if pattern.fullmatch(p.name)] if watch_dir.exists() else []


def _read_download_paths(watch_dir):
    """找出工作台的已阅记录文件."""
    pattern = re.compile(r"^read_items(?:_\d+)?(?: \(\d+\))?\.json$")
    return [p for p in watch_dir.iterdir() if pattern.fullmatch(p.name)] if watch_dir.exists() else []


def _apply_read_file(progress, path):
    """应用一份工作台已阅记录并归档, 让下次抓取不再重复显示同一版本."""
    source = Path(path)
    payload = json.loads(source.read_text(encoding="utf-8"))
    _record_read_items(progress, payload.get("reads") or [])
    archived = _archive_download(source)
    try:
        archived.rename(archived.with_suffix(".done.json"))
    except OSError:
        pass


def _consume_pending_read_files(progress):
    for path in sorted(_read_download_paths(Path.home() / "Downloads"), key=lambda p: p.stat().st_mtime):
        try:
            _apply_read_file(progress, path)
        except Exception as e:
            print(f"⚠ 已阅记录处理失败 {path.name}: {e}")


def ensure_data_for_date(
        scrape_path, config, supplier_name, date_str, timeout=240, code="",
        force_fetch=True, range_start="", range_end="", raise_interrupt=False):
    """前置: 确保 scrape 里有指定日期、编码或标题范围的完整数据. 必要时打开带 anchor 参数的 szwego,
    让书签深挖. 监听 Downloads 中转文件,收到后归档到项目 data/,
    merge 到本地 scrape_all*.json 后再验证.
    """
    range_mode = bool(range_start and range_end)
    has_target = (lambda: _data_has_code(scrape_path, config, supplier_name, code)) if code else (
        lambda: _data_has_date(scrape_path, config, supplier_name, date_str))
    target = (
        f"{date_str} 标题范围「{range_start}」到「{range_end}」" if range_mode
        else (f"编码「{code}」" if code else date_str)
    )
    target_exists = False if range_mode else has_target()
    if target_exists and not force_fetch:
        return True
    if range_mode:
        print(f"⚠ 为确保 「{supplier_name}」 {target} 完整, 重新深挖目标日期窗口...")
    elif target_exists:
        print(f"⚠ 为避免 「{supplier_name}」 {target} 的素材不完整, 重新深挖目标日期窗口...")
    else:
        print(f"⚠ 本地数据没有 「{supplier_name}」 {target} 的内容, 帮你去深挖...")
    # 先记录已存在的诊断文件。批量上一项失败时会保留它，下一项必须忽略，
    # 但同一路径被浏览器覆盖并更新修改时间时仍应识别为新下载。
    anchor_before = pick_newest_download("scrape_anchor")
    anchor_before_path = anchor_before.resolve() if anchor_before else None
    anchor_before_mtime_ns = (
        anchor_before.stat().st_mtime_ns if anchor_before else 0
    )
    all_dl = Path(scrape_path)
    start_ts = time.time() - 1
    all_dl_orig_mtime = all_dl.stat().st_mtime if all_dl.exists() else 0

    # 打开带 anchor 参数的 szwego, 书签会读 URL 参数决定行为
    # 参数放 hash 前 (search), 否则 vue router 匹配失败白屏
    anchor_url = (
        "https://www.szwego.com/static/index.html"
        + f"?anchor_supplier={urllib.parse.quote(supplier_name)}"
        + (
            f"&anchor_code={urllib.parse.quote(code)}" if code
            else (
                f"&range_start={urllib.parse.quote(range_start)}"
                f"&range_end={urllib.parse.quote(range_end)}"
                f"&range_date={date_str}"
                if range_mode else f"&anchor_date={date_str}"
            )
        )
        + "#/album_home"
    )
    try:
        import webbrowser
        webbrowser.open(anchor_url)
    except Exception:
        pass
    print(f"  🌐 已打开微购相册 (URL 含 anchor 参数)")
    print(f"  👆 请点浏览器书签栏的「🛒 抓挑品数据」")
    print(f"     还没装/需要更新书签: 另开终端跑 python3 pick_products.py bookmark")
    print(f"  ⏳ 监听下载... (最长 {timeout} 秒, Ctrl+C 取消)")

    try:
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(1)
            # 优先看 anchor 深挖 (认 Chrome 改名的 scrape_anchor (1).json)
            anchor_dl = pick_newest_download("scrape_anchor")
            anchor_is_new = False
            if anchor_dl:
                same_path = (
                    anchor_before_path is not None
                    and anchor_dl.resolve() == anchor_before_path
                )
                anchor_is_new = (
                    (not same_path or anchor_dl.stat().st_mtime_ns > anchor_before_mtime_ns)
                    and anchor_dl.stat().st_mtime > start_ts
                )
            if anchor_dl and anchor_is_new:
                s1 = anchor_dl.stat().st_size; time.sleep(0.5)
                if anchor_dl.stat().st_size != s1: continue
                anchor_dl = _archive_download(anchor_dl)
                print(f"  ⇣ 收到深挖数据并归档: {anchor_dl}")
                expected_aid = config.get("suppliers", {}).get(supplier_name)
                if not expected_aid:
                    expected_aid = next((a for n, a in config.get("suppliers", {}).items()
                                         if supplier_name in n), None)
                if range_mode:
                    problem = _range_anchor_problem(
                        anchor_dl, expected_aid, date_str, range_start, range_end
                    )
                    if problem:
                        print(f"⚠ 本次深挖不可用: {problem}")
                        print(f"  已保留诊断文件: {anchor_dl}")
                        return False
                elif code:
                    problem = _code_anchor_problem(anchor_dl, expected_aid, code)
                    if problem:
                        print(f"⚠ 本次深挖不可用: {problem}")
                        print(f"  已保留诊断文件: {anchor_dl}")
                        return False
                else:
                    problem = _date_anchor_problem(anchor_dl, expected_aid, date_str)
                    if problem:
                        print(f"⚠ 本次深挖不可用: {problem}")
                        print(f"  已保留诊断文件: {anchor_dl}")
                        return False
                if _merge_anchor_into_scrape(scrape_path, anchor_dl):
                    if range_mode:
                        print(f"✓ merge 后已找到 {target}")
                        return True
                    if has_target():
                        print(f"✓ merge 后已找到 {target}")
                        return True
                    elif not code and _data_has_date(scrape_path, config, supplier_name, date_str,
                                        require_order=False):
                        print("⚠ 深挖数据缺少 update_time, 请重新运行 bookmark 并替换旧书签")
                        return False
                    else:
                        print(f"⚠ 深挖翻完了仍没找到 {target}")
                        return False
            # 兼容: 用户点了普通书签(不带 anchor 参数抓的全量)
            all_new = pick_newest_download("scrape_all")
            if all_new and all_new.stat().st_mtime > all_dl_orig_mtime and all_new.stat().st_mtime > start_ts:
                s1 = all_new.stat().st_size; time.sleep(0.5)
                if all_new.stat().st_size != s1: continue
                all_new = _archive_download(all_new)
                scrape_path = str(all_new)
                if code or range_mode:
                    label = "编码模式" if code else "标题范围模式"
                    print(f"⚠ {label}不接受普通单页抓取, 请点击当前深挖页面上的新版书签")
                    all_dl_orig_mtime = all_new.stat().st_mtime
                    continue
                if has_target():
                    print(f"✓ 全量数据里已找到 {target}")
                    return True
                elif not code and _data_has_date(scrape_path, config, supplier_name, date_str,
                                    require_order=False):
                    print("⚠ 抓取数据缺少 update_time, 请重新运行 bookmark 并替换旧书签")
                    all_dl_orig_mtime = all_new.stat().st_mtime
                else:
                    print(f"⚠ 全量抓完仍没找到 {target}, 请点带深挖的书签(URL 里带 anchor 参数)")
                    all_dl_orig_mtime = all_new.stat().st_mtime  # 记录已看过
    except KeyboardInterrupt:
        print("\n已取消")
        if raise_interrupt:
            raise
    print("超时"); return False


def ensure_data_for_code(
        scrape_path, config, supplier_name, code, timeout=240,
        raise_interrupt=False):
    """按供货商逐页刷新目标编码, 避免本地只有部分命中帖."""
    return ensure_data_for_date(
        scrape_path, config, supplier_name, "", timeout=timeout, code=code,
        force_fetch=True, raise_interrupt=raise_interrupt)

def ensure_data_for_range(
        scrape_path, config, supplier_name, date_str, start_prefix, end_prefix,
        timeout=240, raise_interrupt=False):
    """刷新指定日期, 保证标题首尾之间的帖子完整."""
    return ensure_data_for_date(
        scrape_path, config, supplier_name, date_str, timeout=timeout,
        range_start=start_prefix, range_end=end_prefix, force_fetch=True,
        raise_interrupt=raise_interrupt)


def cmd_anchor(
        config, progress, data, supplier_name, keyword, date_str,
        review_prefix="", review_label="", raise_interrupt=False):
    """锚点定向: 供货商 + 日期 + title 含关键词 → 锚点前后最近占位图间的素材 → 处理.
    不推进进度.
    date_str 支持 'MM-DD' 或 'YYYY-MM-DD'. 前者补当前年份.
    """
    # 1. 定位供货商
    suppliers = config.get("suppliers", {})
    aid = suppliers.get(supplier_name)
    if not aid:
        # 尝试模糊匹配
        for n, a in suppliers.items():
            if supplier_name in n:
                supplier_name, aid = n, a; break
    if not aid:
        print(f"❌ 供货商 「{supplier_name}」 未在 config.json 里配置"); return
    if aid not in data or not data[aid].get("items"):
        print(f"❌ 抓取数据里没有 「{supplier_name}」 的内容"); return

    # 2. 日期规范化
    date_str = _normalize_date(date_str)
    print(f"锚点定位: 供货商={supplier_name} 日期={date_str} 关键词=「{keyword}」")

    # 3. 日期只定位锚点; 占位图边界必须在供应商完整页面顺序中查找.
    ordered_items = sorted(data[aid]["items"], key=_item_order)
    date_items = [it for it in ordered_items if _item_display_date(it) == date_str]
    if not date_items:
        print(f"❌ 该日期无内容"); return
    print(f"  当日 {len(date_items)} 帖")

    # 4. 找匹配锚点
    matched_ids = {it["goods_id"] for it in date_items if keyword in (it.get("title") or "")}
    if not matched_ids:
        print(f"❌ 没有 title 含「{keyword}」的条目"); return
    print(f"  匹配锚点 {len(matched_ids)} 条")

    # 5. 占位图 = 只有 1 张图、无任何文案且不是视频帖.
    #    锚点前后最近的占位图就是该产品边界.
    ai_cfg = config.get("ai_vision") or {}
    matched_indices = [i for i, it in enumerate(ordered_items) if it["goods_id"] in matched_ids]

    def _is_struct_placeholder(item):
        imgs = item.get("imgsSrc") or []
        has_video = bool(item.get("videoUrl") or item.get("videoURL"))
        return (
            len(imgs) == 1
            and not (item.get("title") or "").strip()
            and not has_video
        )

    placeholder_indices = [i for i, it in enumerate(ordered_items) if _is_struct_placeholder(it)]
    print(f"  完整排序识别占位图 {len(placeholder_indices)} 张")

    # 6. 找锚点前后最近的占位图; 同一边界的多个锚点只处理一次.
    groups_by_bounds = {}
    for anchor_idx in matched_indices:
        before = [i for i in placeholder_indices if i < anchor_idx]
        after = [i for i in placeholder_indices if i > anchor_idx]
        if not before or not after:
            print(f"  ⚠ 锚点 #{anchor_idx} 前后找不到完整占位图, 跳过")
            continue
        opening, closing = before[-1], after[0]
        groups_by_bounds.setdefault((opening, closing), []).append(anchor_idx)

    all_groups = []
    for (opening, closing), anchor_indices in groups_by_bounds.items():
        selected = list(range(opening + 1, closing))
        all_groups.append([ordered_items[i] for i in selected])
        print(f"    边界 #{opening}..#{closing}: 锚点 {len(anchor_indices)} 帖, 素材 {len(selected)} 帖")

    if not all_groups:
        print("❌ 没有找到被两张占位图包住的锚点素材"); return

    print(f"  → 分成 {len(all_groups)} 个产品:")
    for gi, g in enumerate(all_groups, 1):
        titles = " / ".join((it.get("title") or "").replace("\n", " ")[:15] for it in g)
        anchor_mark = " 🎯" if any(it["goods_id"] in matched_ids for it in g) else ""
        print(f"    产品{gi}{anchor_mark}: {len(g)}帖 — {titles[:60]}")

    # 7. 处理
    fs_cfg = config.get("feishu") or {}
    feishu = Feishu(fs_cfg) if fs_cfg.get("app_id") and fs_cfg.get("base_id") else None
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    n = 0
    for group_index, group in enumerate(all_groups, 1):
        review_id = (
            f"{review_prefix}_{group_index:02d}" if review_prefix
            else (f"{group_index:02d}" if len(all_groups) > 1 else "")
        )
        product_label = review_label
        if len(all_groups) > 1:
            product_label = (
                f"{review_label} · 商品 {group_index}/{len(all_groups)}"
                if review_label else f"{supplier_name} · 商品 {group_index}/{len(all_groups)}"
            )
        n += process_groups(
            supplier_name, aid, [group], progress, feishu, fs_cfg, ai_cfg,
            advance_progress=False, review_id=review_id,
            review_label=product_label, raise_interrupt=raise_interrupt,
        )
    print(f"\n{'='*50}\n✓ 锚点定向完成, 处理 {n} 个产品 (未推进进度)")
    return n

def cmd_title_range(
        config, progress, data, supplier_name, date_str, start_prefix, end_prefix,
        review_id="", review_label="", raise_interrupt=False):
    """按标题首尾前缀选择连续帖子, 作为一个产品处理."""
    suppliers = config.get("suppliers", {})
    aid = suppliers.get(supplier_name)
    if not aid:
        for name, album_id in suppliers.items():
            if supplier_name in name:
                supplier_name, aid = name, album_id
                break
    if not aid:
        print(f"❌ 供货商 「{supplier_name}」 未在 config.json 里配置"); return
    if aid not in data or not data[aid].get("items"):
        print(f"❌ 抓取数据里没有 「{supplier_name}」 的内容"); return

    date_str = _normalize_date(date_str)
    group, problem = find_title_range(
        data[aid]["items"], start_prefix, end_prefix, date_str)
    if problem:
        print(f"❌ {problem}"); return

    print(
        f"首尾定向: 供货商={supplier_name} "
        f"日期={date_str} 起始=「{start_prefix}」 结束=「{end_prefix}」"
    )
    print(f"  选中 {len(group)} 帖 (包含首尾)")
    fs_cfg = config.get("feishu") or {}
    feishu = Feishu(fs_cfg) if fs_cfg.get("app_id") and fs_cfg.get("base_id") else None
    ai_cfg = config.get("ai_vision") or {}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    n = process_groups(
        supplier_name, aid, [group], progress, feishu, fs_cfg, ai_cfg,
        advance_progress=False,
        review_id=review_id, review_label=review_label,
        raise_interrupt=raise_interrupt,
    )
    print(f"\n{'='*50}\n✓ 首尾定向完成, 处理 {n} 个产品 (未推进进度)")
    return n


def _safe_job_id(job_id):
    return re.sub(r"[^0-9A-Za-z_-]", "_", str(job_id)).strip("_")


def _consume_cancel(cancel_base):
    """消费一个 Chrome 取消文件; 每个任务 ID 唯一, 不会误取消旧任务."""
    path = pick_newest_download(cancel_base)
    if not path:
        return False
    try:
        _archive_download(path)
    except Exception:
        pass
    return True


def _confirmed_jobs(confirmed):
    """兼容旧 suppliers 格式, 新格式按商品拆成独立任务."""
    jobs = confirmed.get("jobs")
    if jobs:
        return jobs
    result = []
    stamp = time.time_ns()
    for sup in confirmed.get("suppliers", []):
        for index, group in enumerate(sup.get("groups", []), 1):
            result.append({
                "jobId": f"legacy_{stamp}_{len(result)}",
                "supplier": sup["supplier"],
                "albumId": sup["albumId"],
                "items": group,
                "label": f"{sup['supplier']} · 商品 {index}",
            })
    return result


def _process_confirmed_job(config, progress, job):
    supplier = job["supplier"]
    album_id = job["albumId"]
    job_id = _safe_job_id(job.get("jobId") or f"{album_id}_{time.time_ns()}")
    label = job.get("label") or f"{supplier} · 商品"
    fs_cfg = config.get("feishu") or {}
    feishu = Feishu(fs_cfg) if fs_cfg.get("app_id") and fs_cfg.get("base_id") else None
    ai_cfg = config.get("ai_vision") or {}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"\n{'='*50}\n处理任务: {label}")
    return process_groups(
        supplier, album_id, [job["items"]], progress, feishu, fs_cfg, ai_cfg,
        review_id=job_id, review_label=label,
        cancel_base=f"cancelled_job_{job_id}",
    )


def cmd_process_confirmed(config, progress, confirmed_path):
    """按独立商品任务并行处理, 每个任务拥有自己的分类预览和取消文件."""
    confirmed = json.loads(Path(confirmed_path).read_text())
    jobs = _confirmed_jobs(confirmed)
    if not jobs:
        print("没有可处理的商品任务")
        return
    total = 0
    workers = min(8, len(jobs))
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="product") as pool:
        futures = [pool.submit(_process_confirmed_job, config, progress, job) for job in jobs]
        for future in as_completed(futures):
            try:
                total += future.result()
            except Exception as e:
                print(f"⚠ 商品任务失败: {e}")
    print(f"\n{'='*50}\n✓ 全部完成, 共处理 {total} 个产品")


def _open_szwego_in_chrome(config=None):
    url = "https://www.szwego.com/static/index.html"
    if config:
        suppliers = json.dumps(config.get("suppliers", {}), ensure_ascii=False)
        url += "?" + urllib.parse.urlencode({"suppliers": suppliers})
    try:
        if sys.platform == "darwin":
            import subprocess
            subprocess.run(["open", "-a", "Google Chrome", url], check=False)
        else:
            import webbrowser
            webbrowser.open(url)
    except Exception:
        print(f"  请手动打开: {url}")


def wait_for_fresh_download(base, before_mtime=0, timeout=600):
    """等待 Chrome 扩展生成新的下载文件, 兼容 Chrome 自动加 (1) 后缀."""
    print(f"\n⏳ 等待 Chrome 扩展抓取最新数据... (最长 {timeout} 秒)")
    deadline = time.time() + timeout
    try:
        while time.time() < deadline:
            path = pick_newest_download(base)
            if path and path.stat().st_mtime > before_mtime:
                size = path.stat().st_size
                time.sleep(0.5)
                if path.stat().st_size == size and size > 0:
                    return path
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n已取消等待"); return None
    print("\n⚠ 未收到新的抓取文件")
    return None


def refresh_daily_scrape(timeout=600, config=None):
    """打开已登录的微购相册, 由扩展抓取并返回最新 scrape_all 文件."""
    previous = pick_newest_download("scrape_all")
    before_mtime = previous.stat().st_mtime if previous else 0
    _open_szwego_in_chrome(config)
    print("  已打开微购相册, Chrome 扩展应自动开始抓取")
    path = wait_for_fresh_download("scrape_all", before_mtime, timeout)
    if path:
        path = _archive_download(path)
        captured = datetime.fromtimestamp(path.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        print(f"  ✓ 数据抓取完成并归档: {captured} ({path})")
    else:
        print("  请确认扩展已加载、账号已登录, 或使用旧书签抓取")
    return path


def wait_for_confirmed(watch_dir=None, timeout=None):
    """监听 Downloads 目录, 等新的 confirmed_groups 文件出现. 返回路径或 None(超时/取消)."""
    watch_dir = watch_dir or (Path.home() / "Downloads")
    # 忽略比启动更早的旧文件
    start_ts = time.time() - 1
    wait_hint = f"最长 {timeout} 秒" if timeout is not None else "一直等待"
    print(f"\n⏳ 等浏览器里点「确认并下载」... ({wait_hint}, Ctrl+C 退出)")
    print(f"   监听: {watch_dir}/confirmed_groups*.json")
    end = time.time() + timeout if timeout is not None else None
    try:
        while end is None or time.time() < end:
            candidates = [
                path for path in _confirmed_download_paths(watch_dir)
                if path.stat().st_mtime > start_ts
            ]
            if candidates:
                target = max(candidates, key=lambda path: path.stat().st_mtime)
                # 稳定性: 等文件写完 (大小连续 2 次一致)
                s1 = target.stat().st_size
                time.sleep(0.5)
                if target.stat().st_size == s1 and s1 > 0:
                    return target
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n已取消等待"); return None
    print("\n⚠ 超时未收到确认文件"); return None


def wait_for_workbench_file(watch_dir=None, timeout=None):
    """同时等待商品提交或已阅记录, 避免已阅要等到下次启动才生效."""
    watch_dir = watch_dir or (Path.home() / "Downloads")
    start_ts = time.time() - 1
    wait_hint = f"最长 {timeout} 秒" if timeout is not None else "一直等待"
    print(f"\n⏳ 等浏览器里提交商品或点击「本批已阅」... ({wait_hint}, Ctrl+C 退出)")
    print(f"   监听: {watch_dir}/confirmed_groups*.json, read_items*.json")
    end = time.time() + timeout if timeout is not None else None
    try:
        while end is None or time.time() < end:
            candidates = [
                (path, "confirmed") for path in _confirmed_download_paths(watch_dir)
                if path.stat().st_mtime > start_ts
            ] + [
                (path, "read") for path in _read_download_paths(watch_dir)
                if path.stat().st_mtime > start_ts
            ]
            if candidates:
                target, kind = max(candidates, key=lambda pair: pair[0].stat().st_mtime)
                size = target.stat().st_size
                time.sleep(0.5)
                if target.stat().st_size == size and size > 0:
                    return kind, target
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n已取消等待"); return None
    print("\n⚠ 超时未收到工作台文件")
    return None


def _process_confirmed_file(config, progress, confirmed):
    try:
        cmd_process_confirmed(config, progress, str(confirmed))
    finally:
        try:
            confirmed.rename(confirmed.with_suffix(".done.json"))
        except Exception:
            pass


def cmd_wechat_note(config, progress):
    """读取当前微信笔记缓存, 复用排序预览后写飞书并建产品文件夹."""
    fs_cfg = config.get("feishu") or {}
    required = ("app_id", "app_secret", "base_id", "table_id")
    if not all(fs_cfg.get(key) for key in required):
        raise RuntimeError("微信笔记流程需要完整的 feishu 配置")
    note = load_wechat_note()
    item = note["item"]
    album_id = f"wechat:{note['html_path'].parents[3].name}"
    state = _ensure_progress_state(album_id, [item], progress)
    if _progress_item_status(item, state) == "done":
        print(f"✓ 当前微信笔记已处理过: {note['html_path'].name}")
        return 0
    print(f"✓ 微信笔记缓存: {note['html_path']}")
    print(f"  文案 {len(item['title'])} 字 · 图片 {len(item['imgsSrc'])} 张（含子页面素材）")
    feishu = Feishu(fs_cfg)
    ai_cfg = config.get("ai_vision") or {}
    return process_groups(
        "微信笔记", album_id, [[item]], progress, feishu, fs_cfg, ai_cfg,
        advance_progress=True, review=True, order_key=lambda value: value["time_stamp"],
        review_id=item["goods_id"], review_label="微信笔记",
        raise_interrupt=True,
    )


def _load_all_supplier_ids(config):
    """把 config.suppliers 展开成 JS 字面量字符串, 用于生成书签."""
    return json.dumps(config.get("suppliers", {}), ensure_ascii=False)


def _write_chrome_extension(config, capture_script):
    """把当前供应商配置和同一份抓取脚本写成可加载的 Chrome 扩展."""
    CHROME_EXTENSION_DIR.mkdir(parents=True, exist_ok=True)
    manifest = {
        "manifest_version": 3,
        "name": "微购相册自动抓取",
        "version": "1.0.0",
        "description": "打开微购相册上新页后自动抓取供应商上新数据",
        "content_scripts": [{
            "matches": ["https://www.szwego.com/static/index.html*"],
            "js": ["content.js"],
            "run_at": "document_idle",
        }],
        "host_permissions": ["https://www.szwego.com/*"],
    }
    (CHROME_EXTENSION_DIR / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (CHROME_EXTENSION_DIR / "content.js").write_text(
        "// 自动生成, 供应商配置来自 config.json\n"
        + capture_script.replace("alert(", "console.log("),
        encoding="utf-8",
    )
    (CHROME_EXTENSION_DIR / "README.md").write_text(
        """# 微购相册自动抓取扩展

1. 打开 Chrome `chrome://extensions/`。
2. 打开右上角「开发者模式」。
3. 点击「加载已解压的扩展程序」，选择本目录。
4. 登录微购相册后打开 `https://www.szwego.com/static/index.html`。

扩展会自动抓取 `config.json` 中的供货商，并下载 `scrape_all.json` 到 Chrome 的 Downloads 中转目录。运行挑品脚本时会自动归档到项目 `data/`。
运行 `python3 pick_products.py` 时会把当前 config.json 的供货商配置自动传给扩展,无需重复生成扩展。
""",
        encoding="utf-8",
    )
    print(f"✓ Chrome 扩展已生成: {CHROME_EXTENSION_DIR}")


def cmd_install_bookmark(config, open_install=True):
    """生成一个 HTML, 你拖里面的按钮到浏览器书签栏即可."""
    suppliers_js = _load_all_supplier_ids(config)
    # 书签的 javascript: URL — 编码为 URI 保证在 href 里合法
    bookmarklet_body = r"""(async()=>{
const SUP=Object.assign(__SUPPLIERS__, JSON.parse(new URLSearchParams(location.search).get('suppliers')||'{}'));
const clean=it=>({goods_id:it.goods_id,title:it.title||'',imgsSrc:it.imgsSrc||[],time_stamp:it.time_stamp,update_time:it.update_time,videoUrl:it.videoUrl||it.videoURL||''});
const filt=arr=>arr.filter(i=>!i.isTop&&!i.forwardTime&&i.parent_goods_id===i.goods_id).map(clean);
const dailyClean=arr=>arr.filter(i=>!i.isTop).map(clean);
const anchorClean=arr=>arr.filter(i=>!i.isTop).map(clean);
const itemDate=it=>{const d=new Date(it.time_stamp),p=n=>String(n).padStart(2,'0');return d.getFullYear()+'-'+p(d.getMonth()+1)+'-'+p(d.getDate());};
const fetchOne=async(aid,pageTs)=>{
  const u='https://www.szwego.com/album/personal/new?&albumId='+aid+'&searchValue=&searchImg=&startDate=&endDate=&sourceId=&requestDataType='+(pageTs?'&slipType=1&timestamp='+pageTs:'');
  const r=await fetch(u,{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded; charset=UTF-8'},body:''});return await r.json();
};
const fetchDaily=async aid=>{
  const all=[],seen=new Set();let pageTs='',pages=0,latestDate='',foundLatest=false,misses=0,hasMore=false;const DAILY_MAX=50;
  while(pages<DAILY_MAX){
    const d=await fetchOne(aid,pageTs);
    const raw=d.result&&d.result.items?d.result.items:[];
    if(raw.length===0)break;
    const items=dailyClean(raw);
    const rawLatestDate=raw.map(itemDate).sort().pop();
    if(!latestDate&&rawLatestDate)latestDate=rawLatestDate;
    const hasLatest=!!latestDate&&raw.some(i=>itemDate(i)===latestDate);
    if(hasLatest){foundLatest=true;misses=0;}else if(foundLatest){misses++;}
    for(const it of items){if(!seen.has(it.goods_id)){seen.add(it.goods_id);all.push(it);}}
    pages++;
    hasMore=!!(d.result.pagination&&d.result.pagination.isLoadMore);
    if(foundLatest&&misses>=2)break;
    if(!hasMore)break;
    pageTs=d.result.pagination.pageTimestamp;
    await new Promise(r=>setTimeout(r,150));
  }
  if(pages===DAILY_MAX&&hasMore&&misses<2)throw new Error('日常抓取达到分页上限');
  return all;
};
const dl=(data,fname)=>{
  const blob=new Blob([JSON.stringify(data)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=fname;
  document.body.appendChild(a);a.click();document.body.removeChild(a);
};
const params=new URLSearchParams(location.search);
const anchorSup=params.get('anchor_supplier');
const anchorDate=params.get('anchor_date');
const anchorCode=params.get('anchor_code');
const rangeStart=params.get('range_start');
const rangeEnd=params.get('range_end');
const rangeDate=params.get('range_date');
if(anchorSup&&(anchorDate||anchorCode||(rangeStart&&rangeEnd&&rangeDate))){
  let aid=null,name=anchorSup;
  for(const [n,a] of Object.entries(SUP)){if(n===anchorSup||n.includes(anchorSup)){aid=a;name=n;break;}}
  if(!aid){alert('未找到供货商: '+anchorSup);return;}
  const all=[];let pageTs='',pages=0,rawCount=0,hasMore=false,incomplete=false,foundCode=false,missesAfterCode=0,foundDate=false,missesAfterDate=0,foundRangeDate=false,missesAfterRangeDate=0,stopReason='end';const MAX=50;const RANGE_MAX=50;const pageLimit=rangeDate?RANGE_MAX:MAX;
  while(pages<pageLimit){
    let d;try{d=await fetchOne(aid,pageTs);}catch(e){incomplete=true;stopReason='network';break;}
    const rawItems=d.result&&d.result.items?d.result.items:[];
    rawCount+=rawItems.length;
    const items=(anchorDate||rangeStart)?anchorClean(rawItems):filt(rawItems);
    if(rawItems.length===0)break;
    const rangeItems=rangeDate?items.filter(i=>itemDate(i)===rangeDate):items;
    const pageHasCode=anchorCode&&items.some(i=>(i.title||'').includes(anchorCode));
    const pageHasDate=anchorDate&&items.some(i=>itemDate(i)===anchorDate);
    const pageHasRangeDate=rangeDate&&items.some(i=>itemDate(i)===rangeDate);
    if(pageHasCode){foundCode=true;missesAfterCode=0;}else if(anchorCode&&foundCode){missesAfterCode++;}
    if(pageHasDate){foundDate=true;missesAfterDate=0;}else if(anchorDate&&foundDate){missesAfterDate++;}
    if(pageHasRangeDate){foundRangeDate=true;missesAfterRangeDate=0;}else if(rangeDate&&foundRangeDate){missesAfterRangeDate++;}
    all.push(...rangeItems);pages++;
    hasMore=!!(d.result.pagination&&d.result.pagination.isLoadMore);
    if(anchorCode&&foundCode&&missesAfterCode>=2){stopReason='boundary';break;}
    if(anchorDate&&foundDate&&missesAfterDate>=2){stopReason='date-boundary';break;}
    if(rangeDate&&foundRangeDate&&missesAfterRangeDate>=2){stopReason='date-boundary';break;}
    if(!hasMore)break;
    pageTs=d.result.pagination.pageTimestamp;
    await new Promise(r=>setTimeout(r,150));
  }
  if(pages===pageLimit&&hasMore&&stopReason==='end'){incomplete=true;stopReason='limit';}
  dl({supplier:name,albumId:aid,items:all,anchor:{supplier:anchorSup,date:anchorDate,code:anchorCode,rangeDate,rangeStart,rangeEnd,pages,rawCount,foundDate,incomplete,stopReason,fullScan:false,dateWindow:!!anchorDate,dateScan:!!rangeDate}},'scrape_anchor.json');
  const target=anchorCode||anchorDate||(rangeDate+' '+rangeStart+' → '+rangeEnd);
  alert((incomplete?'深挖未完成':'深挖完成')+': '+name+' '+target+' 共 '+all.length+' 条 ('+pages+' 页)\n下载 scrape_anchor.json');
  return;
}
const out={data:{}};const capturedAt=new Date().toISOString();let ok=0,err=0;
for(const [name,aid] of Object.entries(SUP)){
  try{out.data[aid]={supplier:name,items:await fetchDaily(aid),captured_at:capturedAt,capture_ok:true};ok++;}
  catch(e){out.data[aid]={supplier:name,items:[],captured_at:capturedAt,capture_ok:false,capture_error:String(e)};err++;}
  await new Promise(r=>setTimeout(r,200));
}
dl(out,'scrape_all.json');
alert('抓取完成: '+ok+' 成功, '+err+' 失败\n下载 scrape_all.json');
})();"""
    # 展开成一行; 不剥除 //, 否则会截断 https:// 这类字符串。
    _body = bookmarklet_body.replace("__SUPPLIERS__", suppliers_js)
    bookmarklet = "javascript:" + urllib.parse.quote(
        _body.replace("\n", " "),
        safe="():;,{}[]=/|+.*!$?~"   # 不含 " ' &，保证 HTML 属性安全
    )
    html = """<!DOCTYPE html><meta charset="utf-8">
<title>安装挑品抓取书签</title>
<style>
body{font-family:-apple-system,sans-serif;max-width:640px;margin:40px auto;padding:0 20px;color:#1d1d1f;line-height:1.6}
h1{font-size:20px}
.btn{display:inline-block;padding:10px 24px;background:#0071e3;color:#fff!important;border-radius:20px;text-decoration:none;font-weight:600;font-size:16px}
.steps{background:#f5f5f7;padding:16px 20px;border-radius:12px;margin:16px 0}
.steps ol{margin:0;padding-left:20px}
code{background:#f0f0f0;padding:2px 6px;border-radius:4px;font-size:13px}
</style>
<h1>📦 挑品抓取书签</h1>
<p><b>把下面这个蓝色按钮 <em style="color:#d32f2f">拖</em>(不是点)到书签栏</b>:</p>
<p><a class="btn" href="__HREF__">🛒 抓挑品数据</a></p>
<div class="steps">
<b>使用方法:</b>
<ol>
<li>登录微购相册 <code>https://www.szwego.com/static/index.html</code></li>
<li>在书签栏点「🛒 抓挑品数据」</li>
<li>先自动下载 <code>scrape_all.json</code> 到 Chrome 的 Downloads 中转目录,脚本会归档到项目 <code>data/</code></li>
<li>回终端跑 <code>python3 pick_products.py</code></li>
</ol>
</div>
<div class="steps" style="background:#fff3e0">
<b>🆘 兜底方案(书签点了没反应):</b>
<ol>
<li>登录 szwego 页面</li>
<li>按 <code>F12</code> 打开开发者工具 → 切到 Console 标签</li>
<li>打开 <code>paste_to_console.js</code>(在挑品脚本目录),全选复制内容</li>
<li>粘贴到 console,按回车 → 自动下载</li>
</ol>
<p style="font-size:13px;color:#666">兜底方案不需要装书签,一次性用完</p>
</div>
<p style="color:#666;font-size:13px">书签内置了 config.json 里配的 __N__ 个供货商。加了新供货商,重跑一次 <code>bookmark</code>。</p>
"""
    html = html.replace("__HREF__", bookmarklet).replace("__N__", str(len(config.get("suppliers", {}))))
    BOOKMARKLET_FILE.write_text(html, encoding="utf-8")
    # 兜底: 生成一段可直接粘贴 console 的 JS
    console_js = (
        "// === 粘贴到 szwego 页面的 devtools console 里跑 ===\n"
        + bookmarklet_body.replace("__SUPPLIERS__", suppliers_js).replace("alert(", "console.log(")
    )
    (SCRIPT_DIR / "paste_to_console.js").write_text(console_js, encoding="utf-8")
    _write_chrome_extension(config, console_js)
    print(f"✓ 书签安装页已生成: {BOOKMARKLET_FILE}")
    print(f"✓ 兜底 console 脚本: {SCRIPT_DIR / 'paste_to_console.js'}")
    if not open_install:
        return
    try:
        import webbrowser
        webbrowser.open(BOOKMARKLET_FILE.as_uri())
        print("  已在浏览器打开, 拖蓝色按钮到书签栏即可")
    except Exception:
        print(f"  请手动打开: {BOOKMARKLET_FILE}")


def cmd_install_extension(config):
    """生成 Chrome 扩展, 不打开旧书签安装页."""
    cmd_install_bookmark(config, open_install=False)
    print("  Chrome: chrome://extensions/ → 开发者模式 → 加载已解压的扩展程序")


def _apply_defaults(config):
    """把 config.defaults 里的默认值应用到环境变量(允许命令行/环境覆盖)."""
    global MAX_PRODUCTS
    d = config.get("defaults") or {}
    if not MAX_PRODUCTS and d.get("max_products"):
        MAX_PRODUCTS = int(d["max_products"])


def cmd_refresh_keywords(config):
    fs_cfg = config.get("feishu") or {}
    if not all(fs_cfg.get(key) for key in ("app_id", "app_secret", "base_id", "table_id")):
        raise RuntimeError("刷新本地关键词需要完整的 feishu 配置")
    days = int(os.environ.get("KEYWORD_DAYS", "30"))
    payload = refresh_local_category_keywords(Feishu(fs_cfg), fs_cfg, days=days)
    print(
        f"✓ 已读取飞书 {payload['records']} 条记录, "
        f"生成 {sum(len(words) for words in payload['keywords'].values())} 个本地分类关键词"
    )
    print(f"  缓存: {LOCAL_CATEGORY_KEYWORDS_FILE}")


def cmd_sync_categories(config):
    fs_cfg = config.get("feishu") or {}
    if not all(fs_cfg.get(key) for key in ("app_id", "app_secret", "base_id", "table_id")):
        raise RuntimeError("同步飞书分类选项需要完整的 feishu 配置")
    result = sync_category_field_options(Feishu(fs_cfg), fs_cfg)
    print(
        f"✓ 飞书「{result['field']}」已同步 {len(result['options'])} 个分类选项"
    )
    if result["added"]:
        print(f"  新增 {len(result['added'])} 个")
    if result["removed"]:
        print(f"  移除 {len(result['removed'])} 个不在分类树中的旧选项")


def cmd_fill_fields(config):
    fs_cfg = config.get("feishu") or {}
    if not all(fs_cfg.get(key) for key in ("app_id", "app_secret", "base_id", "table_id")):
        raise RuntimeError("填充飞书分类/型号需要完整的 feishu 配置")
    result = fill_missing_feishu_fields(
        Feishu(fs_cfg), fs_cfg, config.get("ai_vision") or {}
    )
    print(
        f"✓ 扫描 {result['scanned']} 条, 命中 {result['matched']} 条, "
        f"更新 {result['updated']} 条, 无可靠匹配 {result['no_match']} 条"
    )
    for failure in result["failed"]:
        print(f"  ❌ {failure}")
    if result["failed"]:
        raise RuntimeError(f"{len(result['failed'])} 条记录更新失败")


def _parse_batch_targets(mode, args):
    """把高级模式位置参数解析为去重后的目标元组, 保留首次出现顺序."""
    if mode == "run":
        if len(args) < 2:
            return []
        supplier = args[0]
        targets = [(supplier, code) for code in args[1:]]
    elif mode == "anchor":
        if not args or len(args) % 3:
            raise ValueError(
                "anchor 每 3 个参数一组: <供货商> <关键词> <日期>"
            )
        targets = [tuple(args[i:i + 3]) for i in range(0, len(args), 3)]
    elif mode == "range":
        if not args or len(args) % 4:
            raise ValueError(
                "range 每 4 个参数一组: <供货商> <日期> <起始标题> <结束标题>"
            )
        targets = [tuple(args[i:i + 4]) for i in range(0, len(args), 4)]
    else:
        return []

    seen = set()
    unique = []
    for target in targets:
        if target in seen:
            continue
        seen.add(target)
        unique.append(target)
    return unique


def _resolve_supplier(config, supplier_name):
    """返回 config 中的规范供应商名和 album id."""
    suppliers = config.get("suppliers", {})
    if supplier_name in suppliers:
        return supplier_name, suppliers[supplier_name]
    for name, album_id in suppliers.items():
        if supplier_name in name:
            return name, album_id
    return "", ""


def _print_batch_summary(results, heading="批量完成"):
    success = [result for result in results if result["count"] > 0]
    failed = [result for result in results if result["count"] <= 0]
    product_count = sum(result["count"] for result in success)
    print(
        f"\n{'='*50}\n{heading}: {len(results)} 项条件, "
        f"成功 {len(success)} 项, 生成 {product_count} 个商品, "
        f"失败 {len(failed)} 项"
    )
    for result in results:
        mark = "✓" if result["count"] > 0 else "❌"
        detail = (
            f"{result['count']} 个商品"
            if result["count"] > 0 else result.get("reason", "未生成商品")
        )
        print(f"  {mark} {result['label']} — {detail}")


def main():
    # 位置参数: [mode] [模式参数...]  (config.json 之类的 .json 不算位置参数)
    positional = [a for a in sys.argv[1:] if not a.endswith(".json")]
    mode = ""
    if positional and positional[0] in ("preview", "workbench", "process", "run", "bookmark", "extension", "anchor", "range", "refresh_keywords", "sync_categories", "fill_fields", "wx_note"):
        mode = positional.pop(0)
    mode_args = list(positional)
    supplier_arg = positional[0] if len(positional) >= 1 else ""
    code_arg = positional[1] if len(positional) >= 2 else ""

    supplier = supplier_arg or os.environ.get("SUPPLIERS", "")
    code = code_arg or os.environ.get("CODE", "")
    if supplier:
        os.environ["SUPPLIERS"] = supplier

    config = load_json_or(CONFIG_FILE, None)
    if not config:
        print(f"配置文件不存在: {CONFIG_FILE}"); sys.exit(1)
    _apply_defaults(config)
    progress = load_json_or(PROGRESS_FILE, {})
    _consume_pending_read_files(progress)

    # bookmark: 生成书签安装页
    if mode == "bookmark":
        cmd_install_bookmark(config); return

    if mode == "extension":
        cmd_install_extension(config); return

    if mode == "refresh_keywords":
        try:
            cmd_refresh_keywords(config)
        except Exception as e:
            print(f"❌ 刷新本地关键词失败: {e}")
            sys.exit(1)
        return

    if mode == "sync_categories":
        try:
            cmd_sync_categories(config)
        except Exception as e:
            print(f"❌ 同步飞书分类失败: {e}")
            sys.exit(1)
        return

    if mode == "fill_fields":
        try:
            cmd_fill_fields(config)
        except Exception as e:
            print(f"❌ 填充飞书分类/型号失败: {e}")
            sys.exit(1)
        return

    if mode == "wx_note":
        try:
            count = cmd_wechat_note(config, progress)
        except KeyboardInterrupt:
            print("\n已取消, 未创建飞书记录或产品文件夹")
            return
        except Exception as e:
            print(f"❌ 微信笔记处理失败: {e}")
            sys.exit(1)
        print(f"\n{'='*50}\n✓ 微信笔记完成, 共处理 {count} 个产品")
        return

    # process: 老入口, 自动化脚本用. 交互模式默认走合并流程, 不再需要用户手动 process
    if mode == "process":
        paths = [a for a in sys.argv[1:] if a.endswith(".json") and "config" not in a]
        if not paths:
            print("用法: python3 pick_products.py process <confirmed_groups.json>"); sys.exit(1)
        confirmed_path = Path(paths[0])
        if confirmed_path.parent.resolve() == (Path.home() / "Downloads").resolve():
            confirmed_path = _archive_download(confirmed_path)
        cmd_process_confirmed(config, progress, confirmed_path); return

    default_scrape = _project_data_dir() / "scrape_all.json"
    env_scrape = os.environ.get("SCRAPE_JSON")
    if mode == "range":
        try:
            targets = _parse_batch_targets("range", mode_args)
        except ValueError as e:
            print(f"用法错误: {e}"); sys.exit(1)
        scrape_path = env_scrape or str(_latest_scrape_path() or default_scrape)
        is_batch = len(targets) > 1
        results = []
        fetched_dates = set()
        for index, (target_supplier, target_date, start_prefix, end_prefix) in enumerate(targets, 1):
            try:
                norm_date = _normalize_date(target_date)
            except ValueError as e:
                label = (
                    f"{target_supplier} · 首尾 {start_prefix} → {end_prefix} "
                    f"· {target_date}"
                )
                print(f"\n[{index}/{len(targets)}] {label}\n❌ {e}")
                results.append({
                    "label": label, "count": 0, "reason": "日期格式无效",
                })
                continue
            canonical_name, album_id = _resolve_supplier(config, target_supplier)
            fetch_key = (album_id or target_supplier, norm_date)
            label = (
                f"{target_supplier} · 首尾 {start_prefix} → {end_prefix} · {norm_date}"
            )
            review_label = f"第 {index}/{len(targets)} 项 · {label}"
            print(f"\n[{index}/{len(targets)}] {label}")
            try:
                if fetch_key in fetched_dates:
                    print(
                        f"  ↻ 复用已深挖数据: "
                        f"{canonical_name or target_supplier} {norm_date}"
                    )
                    ready = True
                elif is_batch:
                    ready = ensure_data_for_range(
                        scrape_path, config, target_supplier, norm_date,
                        start_prefix, end_prefix, raise_interrupt=True,
                    )
                else:
                    ready = ensure_data_for_range(
                        scrape_path, config, target_supplier, norm_date,
                        start_prefix, end_prefix,
                    )
                if not ready:
                    print("❌ 未获取到完整的首尾标题范围, 跳过")
                    results.append({
                        "label": label, "count": 0, "reason": "深挖失败",
                    })
                    continue
                fetched_dates.add(fetch_key)
                data = load_scrape(scrape_path, config)
                kwargs = {}
                if is_batch:
                    kwargs = {
                        "review_id": f"{index:02d}_01",
                        "review_label": review_label,
                        "raise_interrupt": True,
                    }
                count = cmd_title_range(
                    config, progress, data, target_supplier, norm_date,
                    start_prefix, end_prefix, **kwargs,
                ) or 0
                results.append({
                    "label": label, "count": count,
                    "reason": "未找到或未生成商品",
                })
            except KeyboardInterrupt:
                if not is_batch:
                    raise
                print("\n已取消批量任务")
                if results:
                    _print_batch_summary(results, heading="取消前汇总")
                return
            except Exception as e:
                print(f"❌ 本项处理失败: {e}")
                results.append({
                    "label": label, "count": 0, "reason": str(e),
                })
        if is_batch:
            _print_batch_summary(results)
        return

    # anchor 模式即使本地没数据也继续 (ensure_data_for_date 会深挖创建)
    if mode == "anchor":
        try:
            targets = _parse_batch_targets("anchor", mode_args)
        except ValueError as e:
            print(f"用法错误: {e}"); sys.exit(1)
        scrape_path = env_scrape or str(_latest_scrape_path() or default_scrape)
        is_batch = len(targets) > 1
        results = []
        fetched_dates = set()
        for index, (target_supplier, keyword, target_date) in enumerate(targets, 1):
            try:
                norm_date = _normalize_date(target_date)
            except ValueError as e:
                label = f"{target_supplier} · 锚点 {keyword} · {target_date}"
                print(f"\n[{index}/{len(targets)}] {label}\n❌ {e}")
                results.append({
                    "label": label, "count": 0, "reason": "日期格式无效",
                })
                continue
            canonical_name, album_id = _resolve_supplier(config, target_supplier)
            fetch_key = (album_id or target_supplier, norm_date)
            label = f"{target_supplier} · 锚点 {keyword} · {norm_date}"
            review_label = f"第 {index}/{len(targets)} 项 · {label}"
            print(f"\n[{index}/{len(targets)}] {label}")
            try:
                if fetch_key in fetched_dates:
                    print(
                        f"  ↻ 复用已深挖数据: "
                        f"{canonical_name or target_supplier} {norm_date}"
                    )
                    ready = True
                elif is_batch:
                    ready = ensure_data_for_date(
                        scrape_path, config, target_supplier, norm_date,
                        raise_interrupt=True,
                    )
                else:
                    ready = ensure_data_for_date(
                        scrape_path, config, target_supplier, norm_date,
                    )
                if not ready:
                    print(f"❌ 未获取到 {norm_date} 的数据, 跳过")
                    results.append({
                        "label": label, "count": 0, "reason": "深挖失败",
                    })
                    continue
                fetched_dates.add(fetch_key)
                data = load_scrape(scrape_path, config)
                kwargs = {}
                if is_batch:
                    kwargs = {
                        "review_prefix": f"{index:02d}",
                        "review_label": review_label,
                        "raise_interrupt": True,
                    }
                count = cmd_anchor(
                    config, progress, data, target_supplier, keyword,
                    norm_date, **kwargs,
                ) or 0
                results.append({
                    "label": label, "count": count,
                    "reason": "未找到或未生成商品",
                })
            except KeyboardInterrupt:
                if not is_batch:
                    raise
                print("\n已取消批量任务")
                if results:
                    _print_batch_summary(results, heading="取消前汇总")
                return
            except Exception as e:
                print(f"❌ 本项处理失败: {e}")
                results.append({
                    "label": label, "count": 0, "reason": str(e),
                })
        if is_batch:
            _print_batch_summary(results)
        return

    if mode in ("", "workbench") and not env_scrape:
        fresh = refresh_daily_scrape(config=config)
        scrape_path = str(fresh or "")
    else:
        latest_scrape = _latest_scrape_path()
        scrape_path = env_scrape or (str(latest_scrape) if latest_scrape else "")
    run_targets = []
    if mode == "run":
        run_targets = _parse_batch_targets("run", mode_args)
        if not run_targets and supplier and code:
            run_targets = [(supplier, code)]
    if mode == "run" and run_targets:
        scrape_path = scrape_path or str(default_scrape)
        is_batch = len(run_targets) > 1
        fs_cfg = config.get("feishu") or {}
        feishu = Feishu(fs_cfg) if fs_cfg.get("app_id") and fs_cfg.get("base_id") else None
        if feishu:
            print("✓ 飞书已配置")
        ai_cfg = config.get("ai_vision") or {}
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        results = []
        for index, (target_supplier, target_code) in enumerate(run_targets, 1):
            label = f"{target_supplier} · 编码 {target_code}"
            review_label = f"第 {index}/{len(run_targets)} 项 · {label}"
            print(f"\n[{index}/{len(run_targets)}] {label}")
            try:
                if is_batch:
                    ready = ensure_data_for_code(
                        scrape_path, config, target_supplier, target_code,
                        raise_interrupt=True,
                    )
                else:
                    ready = ensure_data_for_code(
                        scrape_path, config, target_supplier, target_code,
                    )
                if not ready:
                    print(
                        f"❌ 深挖后仍未找到 「{target_supplier}」"
                        f" 编码「{target_code}」的素材"
                    )
                    results.append({
                        "label": label, "count": 0, "reason": "深挖失败",
                    })
                    continue
                canonical_name, aid = _resolve_supplier(config, target_supplier)
                data = load_scrape(scrape_path, config)
                if not aid or aid not in data:
                    print(f"❌ 抓取数据里没有 「{target_supplier}」 的内容")
                    results.append({
                        "label": label, "count": 0, "reason": "供应商数据缺失",
                    })
                    continue
                kwargs = {"code": target_code}
                if is_batch:
                    kwargs.update({
                        "review_id": f"{index:02d}_01",
                        "review_label": review_label,
                        "raise_interrupt": True,
                    })
                count = process_supplier(
                    canonical_name, aid, data[aid]["items"], progress,
                    feishu, fs_cfg, ai_cfg, **kwargs,
                )
                results.append({
                    "label": label, "count": count,
                    "reason": "未找到或未生成商品",
                })
            except KeyboardInterrupt:
                if not is_batch:
                    raise
                print("\n已取消批量任务")
                if results:
                    _print_batch_summary(results, heading="取消前汇总")
                return
            except Exception as e:
                print(f"❌ 本项处理失败: {e}")
                results.append({
                    "label": label, "count": 0, "reason": str(e),
                })
        if is_batch:
            _print_batch_summary(results)
        else:
            total = sum(result["count"] for result in results)
            print(f"\n{'='*50}\n✓ 全部完成, 共处理 {total} 个产品")
        return
    if not scrape_path:
        print(f"❌ 没找到抓取数据: {default_scrape}")
        print(f"   请先在浏览器点「🛒 抓挑品数据」书签抓一次数据。")
        print(f"   还没装书签? 运行: python3 pick_products.py bookmark"); sys.exit(1)
    print(f"读取抓取数据: {scrape_path}")
    data = load_scrape(scrape_path, config)

    # run: 显式跳过预览(自动化用)
    if mode == "run":
        available = list_available(data, progress, code)
        if not available:
            if code:
                print(f"❌ 没有找到编码「{code}」的素材")
            else:
                print("✓ 没有新内容(所有供货商都已处理到最新)")
            return
        chosen = select_suppliers(available)
        if not chosen:
            print("未选择任何供货商, 退出"); return
        print(f"\n将处理 {len(chosen)} 个供货商: {', '.join(c[0] for c in chosen)}"
              + (f"  [定向编码: {code}]" if code else ""))
        fs_cfg = config.get("feishu") or {}
        feishu = Feishu(fs_cfg) if fs_cfg.get("app_id") and fs_cfg.get("base_id") else None
        if feishu: print("✓ 飞书已配置")
        ai_cfg = config.get("ai_vision") or {}
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        total = 0
        for name, aid, _ in chosen:
            total += process_supplier(name, aid, data[aid]["items"], progress, feishu, fs_cfg, ai_cfg, code=code)
        print(f"\n{'='*50}\n✓ 全部完成, 共处理 {total} 个产品"); return

    # 默认: 可视化选品工作台; preview 保留旧的分组预览入口
    if mode == "workbench" or not mode:
        if not cmd_workbench(config, progress, data, scrape_path):
            return
    else:
        cmd_preview(config, progress, data, code=code)
    if mode == "preview":
        return   # 显式 preview: 只生成预览, 不等待

    # 合并流程: 处理任务在后台运行, 主线程继续监听后续提交批次.
    executor = ThreadPoolExecutor(max_workers=8, thread_name_prefix="batch")
    try:
        while True:
            workbench_file = wait_for_workbench_file()
            if not workbench_file:
                print("未收到确认文件, 退出。稍后可手动: python3 pick_products.py process <confirmed_groups.json>")
                return
            kind, path = workbench_file
            if kind == "read":
                _apply_read_file(progress, path)
                print(f"✓ 已阅记录已保存: {path.name}")
                continue
            confirmed = _archive_download(path)
            print(f"\n✓ 收到确认文件: {confirmed}, 已加入并行处理队列...")
            executor.submit(_process_confirmed_file, config, progress, confirmed)
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


if __name__ == "__main__":
    main()
