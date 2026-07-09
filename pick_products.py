"""
微购相册挑品脚本 — 从供货商拉取产品、下载图片、AI分类排序、建文件夹、写飞书
用法:
  # 让 Claude Code 用浏览器抓当天上新, 保存为 scrape.json (无需登录)
  # 然后运行:
  SCRAPE_JSON=~/Downloads/scrape.json python3 pick_products.py
"""
import json, os, sys, time, shutil, base64, hashlib, tempfile, re
from pathlib import Path
from datetime import datetime
import urllib.request, urllib.parse, urllib.error

SCRIPT_DIR = Path(__file__).parent
CONFIG_FILE = SCRIPT_DIR / (sys.argv[1] if len(sys.argv) > 1 and sys.argv[1].endswith(".json") else "config.json")
PROGRESS_FILE = SCRIPT_DIR / "progress.json"
OUTPUT_DIR = Path("/Users/nick/Downloads/weidian_products-main/商品图")
TMP_ROOT = Path(tempfile.gettempdir()) / "weidian_pick"  # 临时/缓存, 不落在商品图里
MAX_PRODUCTS = int(os.environ.get("MAX_PRODUCTS", "0"))  # 0 = 不限, 也可 config.json 里配 defaults.max_products
BOOKMARKLET_FILE = SCRIPT_DIR / "install_bookmark.html"
GROUP_TIME_GAP = 120  # 秒: 帖子时间间隔 < 此值归为同一产品


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
    """AI 判断两帖是否同一件商品. 失败时保守返回 False (拆开)."""
    base_url = (ai_cfg or {}).get("base_url", "").rstrip("/")
    api_key = (ai_cfg or {}).get("api_key", "")
    model = (ai_cfg or {}).get("model", "qwen3-vl-flash")
    if not (base_url and api_key):
        return False
    ia, ib = _ensure_first_image(a, cache_dir), _ensure_first_image(b, cache_dir)
    if not ia or not ib:
        return False
    def durl(p):
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        return f"data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}"
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
    payload = {"model": model, "max_tokens": 10, "messages": [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": durl(ia)}},
        {"type": "image_url", "image_url": {"url": durl(ib)}},
        {"type": "text", "text": prompt},
    ]}]}
    try:
        resp = http_post_json(f"{base_url}/chat/completions", payload,
                              headers={"Authorization": f"Bearer {api_key}"})
        ans = resp["choices"][0]["message"]["content"].strip()
        return ans.startswith("是") or "是" in ans[:3]
    except Exception:
        return False

def _is_placeholder(ai_cfg, item, cache_dir):
    """判断某帖是否'与服装无关的占位/分割图'.
    结构性前置: 必须 1 图 + 空文案. 不满足 → 一定不是占位图, 直接 False (免 AI 调用).
    满足后再用 AI 视觉确认是不是"与服装无关".
    三态返回: True 占位 / False 商品 / None 调用失败.
    """
    imgs = item.get("imgsSrc") or []
    title = (item.get("title") or "").strip()
    if len(imgs) != 1 or title:
        return False  # 结构不符 → 一定不是占位, 免 AI
    base_url = (ai_cfg or {}).get("base_url", "").rstrip("/")
    api_key = (ai_cfg or {}).get("api_key", "")
    model = (ai_cfg or {}).get("model", "qwen3-vl-flash")
    if not (base_url and api_key):
        return None
    ip = _ensure_first_image(item, cache_dir)
    if not ip:
        return None
    mime = "image/png" if ip.suffix.lower() == ".png" else "image/jpeg"
    durl = f"data:{mime};base64,{base64.b64encode(ip.read_bytes()).decode()}"
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
    items = sorted(items, key=lambda x: x["time_stamp"])
    if len(items) <= 1:
        if cache_dir.exists():
            shutil.rmtree(cache_dir, ignore_errors=True)
        return [list(items)] if items else []

    # 第一遍: 占位判定, 按占位帖切成若干段(占位帖本身丢弃, 不下载)
    # ponytail: 每帖一次占位判定, 若调用量成问题再合并进分组调用
    segments = [[]]
    for it in items:
        if _is_placeholder(ai_cfg, it, cache_dir):
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
            gap = (cur["time_stamp"] - prev["time_stamp"]) / 1000
            if gap <= GROUP_MAX_GAP and _ai_same_product(ai_cfg, prev, cur, cache_dir):
                cur_groups[-1].append(cur)
            else:
                cur_groups.append([cur])
        groups.extend(cur_groups)

    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)  # 用完删首图缓存
    return groups


# ── 图片下载 ─────────────────────────────────────────
def download_product_images(product_items, tmp_dir):
    tmp_dir.mkdir(parents=True, exist_ok=True)
    paths = []
    for item in product_items:
        for i, url in enumerate(item["imgsSrc"]):
            clean_url = url.split("?")[0]
            ext = Path(clean_url).suffix or ".jpg"
            fname = f"{item['goods_id'][-8:]}_{i:02d}{ext}"
            dest = tmp_dir / fname
            if not dest.exists():
                try:
                    dest.write_bytes(http_get_bytes(clean_url))
                except Exception as e:
                    print(f"  下载失败 {clean_url}: {e}")
                    continue
            paths.append(dest)
        if item.get("videoUrl"):
            vurl = item["videoUrl"].split("?")[0]
            ext = Path(vurl).suffix or ".mp4"
            dest = tmp_dir / f"{item['goods_id'][-8:]}_video{ext}"
            if not dest.exists():
                try:
                    dest.write_bytes(http_get_bytes(vurl))
                except Exception as e:
                    print(f"  视频下载失败: {e}")
                    continue
            paths.append(dest)
    return paths


# ── AI 分类 ───────────────────────────────────────────
CATEGORIES = ["合图", "价格图", "模特图", "细节图", "尺码表", "其他"]

def classify_images_ai(ai_config, image_paths):
    """OpenAI 兼容视觉 API: 返回 [(path, category, cover_score), ...]
    cover_score 只对 "合图" 有意义, 数字 1-5, 用于选封面; 其他类为 0.
    """
    base_url = (ai_config or {}).get("base_url", "").rstrip("/")
    api_key = (ai_config or {}).get("api_key", "")
    model = (ai_config or {}).get("model", "qwen3-vl-flash")
    if not api_key or not base_url:
        print("  ⚠ 未配置 ai_vision, 跳过 AI 分类")
        return [(p, "其他", 0) for p in image_paths]

    prompt = (
        "这是服装产品图,请严格分类。只回复 JSON,格式: {\"cat\":\"类别\",\"score\":数字}\n"
        "类别按优先级判断:\n"
        "1) 尺码表: 数字表格/尺寸数据表(即使背景有商品)\n"
        "2) 价格图: 明显的价格数字或价格牌标注\n"
        "3) 模特图: 画面中有真人的身体部位(手/脚/腿/上身/全身),不论露不露脸\n"
        "4) 合图: 无真人,拍到完整的商品整体(能看到整条裤/整件衣的大部分长度),且画面里有2件以上的相同款不同色/不同版本平铺或挂拍\n"
        "5) 细节图: 无真人,局部特写(腰头/口袋/拉链/logo/标签/面料/走线/裤脚等),即使画面里有多个颜色的局部也算细节图\n"
        "6) 其他: 都不符合(如封面海报/纯背景图)\n"
        "重要:只要不是完整商品的多色平铺挂拍,就不算合图;局部特写永远是细节图。\n"
        "score: 仅当 cat=合图 时给 1-5 分(视角好/清晰/整件可见/信息量大越高);其它类均为 0"
    )

    results = []
    for img_path in image_paths:
        if img_path.suffix.lower() in (".mp4", ".mov", ".avi"):
            results.append((img_path, "视频", 0))
            continue
        img_b64 = base64.b64encode(img_path.read_bytes()).decode()
        mime = "image/png" if img_path.suffix.lower() == ".png" else "image/jpeg"
        payload = {
            "model": model,
            "max_tokens": 60,
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
            results.append((img_path, cat, score))
            tag = f"[{cat}" + (f" 分{score}" if cat == "合图" else "") + "]"
            print(f"    {img_path.name} → {tag}")
        except Exception as e:
            print(f"    {img_path.name} → 分类失败({e}), 归为其他")
            results.append((img_path, "其他", 0))
        time.sleep(0.15)

    return results


def sort_by_new_rule(classified):
    """新顺序: 合图(最佳1张) → 价格图 → 模特图 → 合图(其余) → 细节图 → 尺码表 → 视频 → 其他"""
    by_cat = {c: [] for c in CATEGORIES + ["视频"]}
    for triple in classified:
        by_cat.setdefault(triple[1], []).append(triple)

    # 合图按 score 降序; 挑最高作为封面, 其余排后面
    hetu = sorted(by_cat.get("合图", []), key=lambda x: -x[2])
    cover = [hetu[0]] if hetu else []
    hetu_rest = hetu[1:]

    ordered = []
    ordered.extend(cover)
    ordered.extend(by_cat.get("价格图", []))
    ordered.extend(by_cat.get("模特图", []))
    ordered.extend(hetu_rest)
    ordered.extend(by_cat.get("细节图", []))
    ordered.extend(by_cat.get("尺码表", []))
    ordered.extend(by_cat.get("视频", []))
    ordered.extend(by_cat.get("其他", []))
    return ordered


# ── 飞书多维表格 ──────────────────────────────────────
class Feishu:
    def __init__(self, cfg):
        self.cfg = cfg
        self.token = None
        self.token_expire = 0

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
    folder = output_dir / folder_name
    folder.mkdir(parents=True, exist_ok=True)
    img_idx = 0
    vid_idx = 0
    size_idx = 0
    size_count = sum(1 for t in sorted_images if t[1] == "尺码表")
    for src, cat, _ in sorted_images:
        if cat == "尺码表":
            size_idx += 1
            name = "尺码表" if size_count == 1 else f"尺码表{size_idx:02d}"
        elif cat == "视频":
            vid_idx += 1
            name = f"视频{vid_idx:02d}"
        else:
            img_idx += 1
            name = f"{folder_name}{img_idx:02d}"
        dest = folder / f"{name}{src.suffix}"
        shutil.copy2(src, dest)
    print(f"  ✓ 文件夹: {folder} ({len(sorted_images)} 个文件)")
    return folder


# ── 进度/配置 ────────────────────────────────────────
def load_json_or(path, default):
    return json.loads(path.read_text()) if path.exists() else default

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))


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

def _progress_date(progress, album_id):
    """取上次处理到的日期串. 兼容旧格式(int 毫秒时间戳)."""
    v = progress.get(album_id)
    if v is None:
        return ""
    if isinstance(v, (int, float)):
        return datetime.fromtimestamp(v / 1000).strftime("%Y-%m-%d")
    return str(v)

def filter_new_items(album_id, raw_items, progress):
    """按天进度过滤: 首次只取最新日期; 之后取日期严格晚于上次处理日期的."""
    last_date = _progress_date(progress, album_id)
    if not last_date and raw_items:
        max_date = max(_item_date(it) for it in raw_items)
        return [it for it in raw_items if _item_date(it) == max_date]
    return [it for it in raw_items if _item_date(it) > last_date]


def process_groups(supplier_name, album_id, groups, progress, feishu, fs_cfg, ai_cfg,
                   advance_progress=True):
    """按给定分组处理: 下载→分类→排序→飞书→建文件夹. 返回产品数.
    advance_progress=False 时不推进按天进度(定向/批量下载用)."""
    if MAX_PRODUCTS > 0:
        groups = groups[:MAX_PRODUCTS]
    for gi, group in enumerate(groups, 1):
        group_asc = sorted(group, key=lambda x: x["time_stamp"])
        latest_time = datetime.fromtimestamp(group[-1]["time_stamp"] / 1000).strftime("%Y-%m-%d %H:%M")
        print(f"\n─ 产品 {gi}/{len(groups)} ({len(group)} 帖, {latest_time})")

        texts = [it["title"] for it in group_asc if it.get("title")]
        combined_text = "\n\n---\n\n".join(texts) if texts else "(无文案)"
        print(f"  文案: {combined_text[:60]}...")

        tmp_dir = TMP_ROOT / f"tmp_{album_id[-8:]}_{gi}"
        images = download_product_images(group_asc, tmp_dir)
        if not images:
            print("  无图片, 跳过")
            continue
        print(f"  下载 {len(images)} 张")

        classified = classify_images_ai(ai_cfg, images)
        sorted_imgs = sort_by_new_rule(classified)
        print("  排序:")
        for i, (p, cat, sc) in enumerate(sorted_imgs, 1):
            print(f"    {i:2d}. [{cat}{' 封面' if i == 1 and cat == '合图' else ''}] {p.name}")

        folder_name = f"{supplier_name}_{latest_time.replace(' ', '_').replace(':', '')}_{gi}"
        if feishu:
            try:
                record_id = feishu.create_record({fs_cfg.get("info_field", "信息"): combined_text})
                print(f"  ✓ 飞书记录已创建: {record_id}")
                folder_name = feishu.wait_for_field(record_id, fs_cfg.get("img_name_field", "图片名"))
                print(f"  ✓ 图片名: {folder_name}")
            except Exception as e:
                print(f"  ⚠ 飞书失败, 用默认文件夹名: {e}")

        folder = create_product_folder(sorted_imgs, folder_name, OUTPUT_DIR)
        (folder / "文案.txt").write_text(combined_text, encoding="utf-8")
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir)

    if groups and advance_progress:
        progress[album_id] = max(_item_date(it) for group in groups for it in group)
        save_json(PROGRESS_FILE, progress)
    return len(groups)


def apply_code_filter(items, code):
    """保留文案(title)包含 code 的条目."""
    return [it for it in items if code in (it.get("title") or "")]


def process_supplier(supplier_name, album_id, raw_items, progress, feishu, fs_cfg, ai_cfg, code=""):
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
    print(f"  AI 分组中 ({len(items)} 帖)...")
    groups = group_products_ai(ai_cfg, items, TMP_ROOT / f"grpcache_{album_id[-8:]}")
    print(f"  分为 {len(groups)} 个产品")
    return process_groups(supplier_name, album_id, groups, progress, feishu, fs_cfg, ai_cfg,
                          advance_progress=not code)


# ── 预览确认界面 ──────────────────────────────────────
def thumb(url):
    """图片 URL 加缩略参数, 预览加载更快."""
    return url.split("?")[0] + "?imageMogr2/thumbnail/!200x200r/quality/80"

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
        groups = group_products_ai(ai_cfg, items, TMP_ROOT / f"grpcache_{aid[-8:]}")
        posts = sorted(items, key=lambda x: x["time_stamp"])
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
    print(f"  然后运行: python3 pick_products.py process ~/Downloads/confirmed_groups.json")


def _normalize_date(date_str):
    """各种简写 → 'YYYY-MM-DD'. 支持 MM-DD / MMDD / YYYY-MM-DD / YYYYMMDD, MM-DD 补当前年份."""
    digits = date_str.strip().replace("-", "")
    if len(digits) == 4:  # MMDD / MM-DD
        return f"{datetime.now():%Y}-{digits[:2]}-{digits[2:]}"
    if len(digits) == 8:  # YYYYMMDD / YYYY-MM-DD
        return f"{digits[:4]}-{digits[4:6]}-{digits[6:]}"
    return date_str.strip()  # 无法识别, 原样返回

def _data_has_date(scrape_path, config, supplier_name, date_str):
    """检查 scrape 里指定供货商指定日期是否有内容."""
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
    for it in d[aid].get("items", []):
        if _item_date(it) == date_str:
            return True
    return False

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
    # 已有该 aid 的 items 按 goods_id 去重合并
    existing = raw["data"].get(aid, {"supplier": anchor.get("supplier", ""), "items": []})
    seen = {it["goods_id"] for it in existing["items"]}
    for it in items:
        if it["goods_id"] not in seen:
            existing["items"].append(it); seen.add(it["goods_id"])
    existing["supplier"] = anchor.get("supplier") or existing.get("supplier", "")
    raw["data"][aid] = existing
    Path(scrape_path).write_text(json.dumps(raw, ensure_ascii=False, indent=2))
    return True


def pick_newest_download(base):
    """Downloads 里 base*.json 取 mtime 最新的一个, 删掉其余旧副本,
    把最新的规范化成 base.json 返回其路径; 没有则 None。
    解决 Chrome 去重改名 (scrape_all (1).json) + 历史副本堆积。"""
    dls = sorted((Path.home() / "Downloads").glob(f"{base}*.json"),
                 key=lambda p: p.stat().st_mtime)
    if not dls:
        return None
    newest = dls[-1]
    for p in dls[:-1]:
        try:
            p.unlink()
        except Exception:
            pass
    canon = newest.parent / f"{base}.json"
    if newest != canon:
        try:
            newest.replace(canon); newest = canon
        except Exception:
            pass
    return newest


def ensure_data_for_date(scrape_path, config, supplier_name, date_str, timeout=240):
    """前置: 确保 scrape 里有 supplier_name 在 date_str 的数据. 缺失则打开带 anchor 参数的 szwego,
    让书签深挖. 监听 ~/Downloads 里 scrape_anchor.json (单供货商深挖) 或 scrape_all.json (全量) 出现,
    merge 到本地 scrape_all.json 后再验证.
    """
    if _data_has_date(scrape_path, config, supplier_name, date_str):
        return True
    print(f"⚠ 本地数据没有 「{supplier_name}」 {date_str} 的内容, 帮你去深挖...")
    # 打开带 anchor 参数的 szwego, 书签会读 URL 参数决定行为
    # 参数放 hash 前 (search), 否则 vue router 匹配失败白屏
    anchor_url = (
        "https://www.szwego.com/static/index.html"
        f"?anchor_supplier={urllib.parse.quote(supplier_name)}"
        f"&anchor_date={date_str}"
        "#/album_home"
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

    all_dl = Path(scrape_path)
    # 清掉残留的深挖文件, 让 Chrome 写出干净的 scrape_anchor.json 而不是 (1)
    for p in (Path.home() / "Downloads").glob("scrape_anchor*.json"):
        try: p.unlink()
        except Exception: pass
    # 记录开始时刻, 忽略更早的旧文件
    start_ts = time.time() - 1
    all_dl_orig_mtime = all_dl.stat().st_mtime if all_dl.exists() else 0

    try:
        end = time.time() + timeout
        while time.time() < end:
            time.sleep(1)
            # 优先看 anchor 深挖 (认 Chrome 改名的 scrape_anchor (1).json)
            anchor_dl = pick_newest_download("scrape_anchor")
            if anchor_dl and anchor_dl.stat().st_mtime > start_ts:
                s1 = anchor_dl.stat().st_size; time.sleep(0.5)
                if anchor_dl.stat().st_size != s1: continue
                print(f"  ⇣ 收到深挖数据: {anchor_dl}")
                if _merge_anchor_into_scrape(scrape_path, anchor_dl):
                    anchor_dl.unlink()  # 用完删掉
                    if _data_has_date(scrape_path, config, supplier_name, date_str):
                        print("✓ merge 后已含目标日期数据")
                        return True
                    else:
                        print(f"⚠ 深挖翻完了仍没抓到 {date_str}(供货商可能真没在这天上新)")
                        return False
            # 兼容: 用户点了普通书签(不带 anchor 参数抓的全量)
            all_new = pick_newest_download("scrape_all")
            if all_new and all_new.stat().st_mtime > all_dl_orig_mtime and all_new.stat().st_mtime > start_ts:
                s1 = all_new.stat().st_size; time.sleep(0.5)
                if all_new.stat().st_size != s1: continue
                if _data_has_date(scrape_path, config, supplier_name, date_str):
                    print("✓ 全量数据里有目标日期")
                    return True
                else:
                    print(f"⚠ 全量抓完仍没有 {date_str}, 请点带深挖的书签(URL 里带 anchor 参数)")
                    all_dl_orig_mtime = all_new.stat().st_mtime  # 记录已看过
    except KeyboardInterrupt:
        print("\n已取消")
    print("超时"); return False


def cmd_anchor(config, progress, data, supplier_name, keyword, date_str):
    """锚点定向: 供货商 + 日期 + title 含关键词 → 找到匹配条目 → 前后到占位图为止 → 处理.
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

    # 3. 该日期条目, 按时间升序
    day_items = sorted(
        [it for it in data[aid]["items"] if _item_date(it) == date_str],
        key=lambda x: x["time_stamp"])
    if not day_items:
        print(f"❌ 该日期无内容"); return
    print(f"  当日 {len(day_items)} 帖")

    # 4. 找匹配锚点
    matched_ids = {it["goods_id"] for it in day_items if keyword in (it.get("title") or "")}
    if not matched_ids:
        print(f"❌ 没有 title 含「{keyword}」的条目"); return
    print(f"  匹配锚点 {len(matched_ids)} 条")

    # 5. 白图切段 + 从锚点段合并后续同款段.
    #    用户模型: 一个产品被两张白色占位图(1图+空文案)包起来. 锚点是产品引流预告帖,
    #    后面白图分隔了引流与正片, 再后面段是详情/展示. 只要下一段的代表帖跟锚点同款,
    #    就合并; 直到某段代表帖不同款为止.
    ai_cfg = config.get("ai_vision") or {}
    if not (ai_cfg.get("base_url") and ai_cfg.get("api_key")):
        print("❌ 未配置 ai_vision, 无法进行 AI 同款判定"); return
    cache_dir = TMP_ROOT / f"anchor_{aid[-8:]}"
    matched_indices = [i for i, it in enumerate(day_items) if it["goods_id"] in matched_ids]

    MAX_MERGE_SEGS = 5  # 最多向后合并的段数(每段 1 次 AI 判定)

    def _is_struct_placeholder(item):
        """结构性占位: 1 图 + 空文案. 用户明确指出这是白色占位图特征."""
        imgs = item.get("imgsSrc") or []
        title = (item.get("title") or "").strip()
        return len(imgs) == 1 and not title

    # 5a. 用结构性白图切当日成段(白图本身不属于任何段)
    placeholders = {i for i, it in enumerate(day_items) if _is_struct_placeholder(it)}
    segments = []
    cur = []
    for i in range(len(day_items)):
        if i in placeholders:
            if cur: segments.append(cur); cur = []
        else:
            cur.append(i)
    if cur: segments.append(cur)
    print(f"  当日按白图切成 {len(segments)} 段, 白图 {len(placeholders)} 张")

    # 5b. 找每个锚点所在段, 合并锚点 + 后续同款段
    visited = set()
    target_segs = []
    ai_calls = [0]
    for anchor_idx in matched_indices:
        if anchor_idx in visited: continue
        anchor_it = day_items[anchor_idx]
        # 找锚点所在段的编号
        seg_i = next((i for i, seg in enumerate(segments) if anchor_idx in seg), None)
        if seg_i is None:
            # 锚点自己是白图? 罕见, 跳过
            target_segs.append([anchor_it])
            visited.add(anchor_idx)
            continue

        collected_idxs = [anchor_idx]  # 先只放锚点自己(锚点段里其他帖可能是别的产品)
        merged_segs = [seg_i]

        # 向后合并: Sk+1, Sk+2, ..., 每段用第一帖跟锚点比是否同款
        for next_i in range(seg_i + 1, min(seg_i + 1 + MAX_MERGE_SEGS, len(segments))):
            next_seg = segments[next_i]
            first_it = day_items[next_seg[0]]
            ai_calls[0] += 1
            time.sleep(0.15)
            if _ai_same_product(ai_cfg, anchor_it, first_it, cache_dir):
                collected_idxs.extend(next_seg)
                merged_segs.append(next_i)
            else:
                break  # 不同款: 停止合并

        collected_idxs.sort()
        seg_items = [day_items[i] for i in collected_idxs]
        target_segs.append(seg_items)
        for i in collected_idxs: visited.add(i)
        print(f"    锚点 #{anchor_idx} (段 S{seg_i}) → 合并段 {merged_segs}, 共 {len(collected_idxs)} 帖 idxs={collected_idxs}")

    total_posts = sum(len(s) for s in target_segs)
    print(f"  合并完: {len(target_segs)} 段, 共 {total_posts} 帖 (AI 调用 {ai_calls[0]} 次, 对比全扫 {len(day_items)} 次)")

    # 6. 每段 = 1 个产品 (白图切段 + 同款合并已确认段内全是同一件商品的素材, 不再拆)
    all_groups = target_segs
    if cache_dir.exists():
        shutil.rmtree(cache_dir, ignore_errors=True)

    print(f"  → 分成 {len(all_groups)} 个产品:")
    for gi, g in enumerate(all_groups, 1):
        titles = " / ".join((it.get("title") or "").replace("\n", " ")[:15] for it in g)
        anchor_mark = " 🎯" if any(it["goods_id"] in matched_ids for it in g) else ""
        print(f"    产品{gi}{anchor_mark}: {len(g)}帖 — {titles[:60]}")

    # 7. 处理
    fs_cfg = config.get("feishu") or {}
    feishu = Feishu(fs_cfg) if fs_cfg.get("app_id") and fs_cfg.get("base_id") else None
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    n = process_groups(supplier_name, aid, all_groups, progress, feishu, fs_cfg, ai_cfg,
                       advance_progress=False)
    print(f"\n{'='*50}\n✓ 锚点定向完成, 处理 {n} 个产品 (未推进进度)")


def cmd_process_confirmed(config, progress, confirmed_path):
    """按确认后的分组处理."""
    confirmed = json.loads(Path(confirmed_path).read_text())
    fs_cfg = config.get("feishu") or {}
    feishu = Feishu(fs_cfg) if fs_cfg.get("app_id") and fs_cfg.get("base_id") else None
    ai_cfg = config.get("ai_vision") or {}
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    total = 0
    for sup in confirmed["suppliers"]:
        print(f"\n{'='*50}\n处理供货商: {sup['supplier']} ({len(sup['groups'])} 个产品)")
        total += process_groups(sup["supplier"], sup["albumId"], sup["groups"],
                                progress, feishu, fs_cfg, ai_cfg)
    print(f"\n{'='*50}\n✓ 全部完成, 共处理 {total} 个产品")


def wait_for_confirmed(watch_dir=None, timeout=600):
    """监听 Downloads 目录, 等 confirmed_groups.json 出现. 返回路径或 None(超时/取消)."""
    watch_dir = watch_dir or (Path.home() / "Downloads")
    target = watch_dir / "confirmed_groups.json"
    # 忽略比启动更早的旧文件
    start_ts = time.time() - 1
    print(f"\n⏳ 等浏览器里点「确认并下载」... (最长 {timeout} 秒, Ctrl+C 退出)")
    print(f"   监听: {target}")
    end = time.time() + timeout
    try:
        while time.time() < end:
            if target.exists() and target.stat().st_mtime > start_ts:
                # 稳定性: 等文件写完 (大小连续 2 次一致)
                s1 = target.stat().st_size
                time.sleep(0.5)
                if target.stat().st_size == s1 and s1 > 0:
                    return target
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n已取消等待"); return None
    print("\n⚠ 超时未收到确认文件"); return None


def _load_all_supplier_ids(config):
    """把 config.suppliers 展开成 JS 字面量字符串, 用于生成书签."""
    return json.dumps(config.get("suppliers", {}), ensure_ascii=False)


def cmd_install_bookmark(config):
    """生成一个 HTML, 你拖里面的按钮到浏览器书签栏即可."""
    suppliers_js = _load_all_supplier_ids(config)
    # 书签的 javascript: URL — 编码为 URI 保证在 href 里合法
    bookmarklet_body = r"""(async()=>{
const SUP=__SUPPLIERS__;
const clean=it=>({goods_id:it.goods_id,title:it.title||'',imgsSrc:it.imgsSrc||[],time_stamp:it.time_stamp,videoUrl:it.videoUrl||it.videoURL||''});
const filt=arr=>arr.filter(i=>!i.isTop&&!i.forwardTime&&i.parent_goods_id===i.goods_id).map(clean);
const fetchOne=async(aid,pageTs,dateFilter)=>{
  const ds=dateFilter||'';const de=dateFilter||'';
  const u='https://www.szwego.com/album/personal/new?&albumId='+aid+'&searchValue=&searchImg=&startDate='+ds+'&endDate='+de+'&sourceId=&requestDataType='+(pageTs?'&slipType=1&timestamp='+pageTs:'');
  const r=await fetch(u,{method:'POST'});return await r.json();
};
const dl=(data,fname)=>{
  const blob=new Blob([JSON.stringify(data)],{type:'application/json'});
  const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download=fname;
  document.body.appendChild(a);a.click();document.body.removeChild(a);
};
const params=new URLSearchParams(location.search);
const anchorSup=params.get('anchor_supplier');
const anchorDate=params.get('anchor_date');
if(anchorSup&&anchorDate){
  let aid=null,name=anchorSup;
  for(const [n,a] of Object.entries(SUP)){if(n===anchorSup||n.includes(anchorSup)){aid=a;name=n;break;}}
  if(!aid){alert('未找到供货商: '+anchorSup);return;}
  const all=[];let pageTs='',pages=0;const MAX=15;
  while(pages<MAX){
    let d;try{d=await fetchOne(aid,pageTs,anchorDate);}catch(e){break;}
    const items=filt(d.result&&d.result.items?d.result.items:[]);
    if(items.length===0)break;
    all.push(...items);pages++;
    if(!d.result.pagination||!d.result.pagination.isLoadMore)break;
    pageTs=d.result.pagination.pageTimestamp;
    await new Promise(r=>setTimeout(r,150));
  }
  dl({supplier:name,albumId:aid,items:all,anchor:{supplier:anchorSup,date:anchorDate,pages}},'scrape_anchor.json');
  alert('深挖完成: '+name+' '+anchorDate+' 共 '+all.length+' 条 ('+pages+' 页)\n下载 scrape_anchor.json');
  return;
}
const out={data:{}};let ok=0,err=0;
for(const [name,aid] of Object.entries(SUP)){
  try{const d=await fetchOne(aid,'','');out.data[aid]={supplier:name,items:filt(d.result&&d.result.items?d.result.items:[])};ok++;}
  catch(e){out.data[aid]={supplier:name,items:[]};err++;}
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
<li>自动下载 <code>scrape_all.json</code> 到 Downloads</li>
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
    print(f"✓ 书签安装页已生成: {BOOKMARKLET_FILE}")
    print(f"✓ 兜底 console 脚本: {SCRIPT_DIR / 'paste_to_console.js'}")
    try:
        import webbrowser
        webbrowser.open(BOOKMARKLET_FILE.as_uri())
        print("  已在浏览器打开, 拖蓝色按钮到书签栏即可")
    except Exception:
        print(f"  请手动打开: {BOOKMARKLET_FILE}")


def _apply_defaults(config):
    """把 config.defaults 里的默认值应用到环境变量(允许命令行/环境覆盖)."""
    global MAX_PRODUCTS
    d = config.get("defaults") or {}
    if not MAX_PRODUCTS and d.get("max_products"):
        MAX_PRODUCTS = int(d["max_products"])


def main():
    # 位置参数: [mode] [供货商] [编码]  (config.json 之类的 .json 不算位置参数)
    positional = [a for a in sys.argv[1:] if not a.endswith(".json")]
    mode = ""
    if positional and positional[0] in ("preview", "process", "run", "bookmark", "anchor"):
        mode = positional.pop(0)
    supplier_arg = positional[0] if len(positional) >= 1 else ""
    code_arg = positional[1] if len(positional) >= 2 else ""
    date_arg = positional[2] if len(positional) >= 3 else ""  # anchor 专用

    supplier = supplier_arg or os.environ.get("SUPPLIERS", "")
    code = code_arg or os.environ.get("CODE", "")
    if supplier:
        os.environ["SUPPLIERS"] = supplier

    config = load_json_or(CONFIG_FILE, None)
    if not config:
        print(f"配置文件不存在: {CONFIG_FILE}"); sys.exit(1)
    _apply_defaults(config)
    progress = load_json_or(PROGRESS_FILE, {})

    # bookmark: 生成书签安装页
    if mode == "bookmark":
        cmd_install_bookmark(config); return

    # process: 老入口, 自动化脚本用. 交互模式默认走合并流程, 不再需要用户手动 process
    if mode == "process":
        paths = [a for a in sys.argv[1:] if a.endswith(".json") and "config" not in a]
        if not paths:
            print("用法: python3 pick_products.py process <confirmed_groups.json>"); sys.exit(1)
        cmd_process_confirmed(config, progress, paths[0]); return

    default_scrape = Path.home() / "Downloads" / "scrape_all.json"
    env_scrape = os.environ.get("SCRAPE_JSON")
    # anchor 模式即使本地没数据也继续 (ensure_data_for_date 会深挖创建)
    if mode == "anchor":
        if not (supplier_arg and code_arg and date_arg):
            print("用法: python3 pick_products.py anchor <供货商> <关键词> <日期MM-DD或YYYY-MM-DD>"); sys.exit(1)
        scrape_path = env_scrape or str(pick_newest_download("scrape_all") or default_scrape)
        norm_date = _normalize_date(date_arg)
        if not ensure_data_for_date(scrape_path, config, supplier_arg, norm_date):
            print(f"❌ 未获取到 {norm_date} 的数据, 无法继续"); return
        data = load_scrape(scrape_path, config)  # 深挖 merge 后加载
        cmd_anchor(config, progress, data, supplier_arg, code_arg, date_arg); return

    scrape_path = env_scrape or (str(pick_newest_download("scrape_all") or ""))
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
            print("✓ 没有新内容(所有供货商都已处理到最新)"); return
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

    # 默认: 交互全流程 (preview 单独也走这个, 但不等待/不处理)
    cmd_preview(config, progress, data, code=code)
    if mode == "preview":
        return   # 显式 preview: 只生成预览, 不等待

    # 合并流程: 等 confirmed_groups.json 出现后自动接着 process
    confirmed = wait_for_confirmed()
    if not confirmed:
        print("未收到确认文件, 退出。稍后可手动: python3 pick_products.py process <confirmed_groups.json>")
        return
    print(f"\n✓ 收到确认文件: {confirmed}, 开始处理...")
    cmd_process_confirmed(config, progress, str(confirmed))
    # 处理完把 confirmed 挪走, 避免下次误触
    try:
        confirmed.rename(confirmed.with_suffix(".done.json"))
    except Exception:
        pass


if __name__ == "__main__":
    main()
