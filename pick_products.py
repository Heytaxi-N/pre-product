"""
微购相册挑品脚本 — 从供货商拉取产品、下载图片、AI分类排序、建文件夹、写飞书
用法:
  # 让 Claude Code 用浏览器抓当天上新, 保存为 scrape.json (无需登录)
  # 然后运行:
  SCRAPE_JSON=~/Downloads/scrape.json python3 pick_products.py
"""
import json, os, sys, time, shutil, base64, hashlib, tempfile
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

def _ensure_first_image(item, cache_dir):
    """下载帖子第一张图到缓存, 返回路径 (按 goods_id 缓存, 避免重复下)."""
    imgs = item.get("imgsSrc") or []
    if not imgs:
        return None
    cache_dir.mkdir(parents=True, exist_ok=True)
    url = imgs[0].split("?")[0]
    dest = cache_dir / f"{item['goods_id'][-10:]}{Path(url).suffix or '.jpg'}"
    if not dest.exists():
        try:
            dest.write_bytes(http_get_bytes(url))
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
        "下面是微商相册里两个相邻帖子的主图和文案。判断它们是否属于【同一件商品】。\n"
        "同一件商品的判定: 同一款(可不同颜色/不同角度)、或其中一个是这件商品的价格图/尺码表/模特图/细节图。\n"
        "不同商品的判定: 明显是两种不同品类或不同款式的货。\n"
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
    """AI 判断某帖主图是否为'与服装无关的占位/分割图'. 失败保守返回 False."""
    base_url = (ai_cfg or {}).get("base_url", "").rstrip("/")
    api_key = (ai_cfg or {}).get("api_key", "")
    model = (ai_cfg or {}).get("model", "qwen3-vl-flash")
    if not (base_url and api_key):
        return False
    ip = _ensure_first_image(item, cache_dir)
    if not ip:
        return False
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
    try:
        resp = http_post_json(f"{base_url}/chat/completions", payload,
                              headers={"Authorization": f"Bearer {api_key}"})
        ans = resp["choices"][0]["message"]["content"].strip()
        return "占位" in ans[:4]
    except Exception:
        return False

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


def cmd_preview(config, progress, data, code=""):
    """生成分组预览 HTML. code 非空 = 定向: 按编码筛全部条目, 不看进度."""
    available = [(b.get("supplier", aid[-8:]), aid, len(b.get("items", [])))
                 for aid, b in data.items() if b.get("items")]
    if not available:
        print("没有内容"); return
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
const out={data:{}};let ok=0,err=0;
for(const [name,aid] of Object.entries(SUP)){
  try{
    const r=await fetch('https://www.szwego.com/album/personal/new?&albumId='+aid+'&searchValue=&searchImg=&startDate=&endDate=&sourceId=&requestDataType=',{method:'POST'});
    const d=await r.json();
    const items=(d.result&&d.result.items?d.result.items:[])
      .filter(i=>!i.isTop&&!i.forwardTime&&i.parent_goods_id===i.goods_id)
      .map(it=>({goods_id:it.goods_id,title:it.title||'',imgsSrc:it.imgsSrc||[],time_stamp:it.time_stamp,videoUrl:it.videoUrl||it.videoURL||''}));
    out.data[aid]={supplier:name,items};ok++;
  }catch(e){out.data[aid]={supplier:name,items:[]};err++;}
  await new Promise(r=>setTimeout(r,200));
}
const blob=new Blob([JSON.stringify(out)],{type:'application/json'});
const a=document.createElement('a');a.href=URL.createObjectURL(blob);a.download='scrape_all.json';
document.body.appendChild(a);a.click();document.body.removeChild(a);
alert('抓取完成: '+ok+' 个成功, '+err+' 个失败\n已下载 scrape_all.json 到 Downloads');
})();"""
    bookmarklet = "javascript:" + urllib.parse.quote(
        bookmarklet_body.replace("__SUPPLIERS__", suppliers_js).replace("\n", " "),
        safe="():;,{}[]=/|&+.*!$?~"   # 不含 " ' 让它们变 %22 %27, 保证 HTML 属性安全
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
<p><b>拖下面这个蓝色按钮到浏览器书签栏</b>(不要点它):</p>
<p><a class="btn" href="__HREF__">🛒 抓挑品数据</a></p>
<div class="steps">
<b>使用方法:</b>
<ol>
<li>登录微购相册网页版 <code>https://www.szwego.com/static/index.html</code></li>
<li>随便进一个页面(相册动态即可)</li>
<li>点书签栏里的「🛒 抓挑品数据」</li>
<li>等几秒 → 自动下载 <code>scrape_all.json</code> 到 Downloads</li>
<li>回终端跑 <code>python3 pick_products.py</code></li>
</ol>
</div>
<p style="color:#666;font-size:13px">书签内置了 config.json 里配的 __N__ 个供货商。以后加了新供货商,重新生成书签即可。</p>
"""
    html = html.replace("__HREF__", bookmarklet).replace("__N__", str(len(config.get("suppliers", {}))))
    BOOKMARKLET_FILE.write_text(html, encoding="utf-8")
    print(f"✓ 书签安装页已生成: {BOOKMARKLET_FILE}")
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
    if positional and positional[0] in ("preview", "process", "run", "bookmark"):
        mode = positional.pop(0)
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
    scrape_path = os.environ.get("SCRAPE_JSON") or (str(default_scrape) if default_scrape.exists() else "")
    if not scrape_path:
        print(f"❌ 没找到抓取数据: {default_scrape}")
        print(f"   请先在浏览器点「🛒 抓挑品数据」书签抓一次数据。")
        print(f"   还没装书签? 运行: python3 pick_products.py bookmark"); sys.exit(1)
    print(f"读取抓取数据: {scrape_path}")
    data = load_scrape(scrape_path, config)

    # run: 显式跳过预览(自动化用)
    if mode == "run":
        available = [(b.get("supplier", aid[-8:]), aid, len(b.get("items", [])))
                     for aid, b in data.items() if b.get("items")]
        if not available:
            print("抓取数据里没有任何供货商内容"); return
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
