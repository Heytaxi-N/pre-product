# 微购相册挑品自动化

从微购相册(szwego)供货商的「上新」批量挑品,自动完成:抓数据 → AI 分组成产品 → 下载图片 → AI 分类排序 → 写飞书多维表格 → 按图片名建产品文件夹。产出的文件夹供下游上架脚本读取。

## 它做什么

1. **抓上新** — 从供货商的「上新」接口取当天新品(帖子含文案 + 多张图 + 可选视频)。
2. **AI 分组** — 同一件商品常被拆成 3~4 个帖子(正视图/价格图/模特图/尺码表分开发)。用 AI 看主图+文案,把相邻帖子判定为同款并合并成一个产品。
3. **人工兜底** — 生成「分组预览」网页,缩略图 + 文案一目了然,点两帖之间的按钮改边界,确认后再处理。应对「专场」这类 AI 难分干净的情况。
4. **AI 分类排图** — 每张图分类(合图/价格图/模特图/细节图/尺码表/视频),按规则排序:
   `合图(最清晰美观的一张作封面) → 价格图 → 模特图 → 其余合图 → 细节图 → 尺码表 → 视频`
5. **写飞书** — 合并后的文案写入多维表格「信息」字段;飞书自动化据此生成「图片名」。
6. **建文件夹** — 以图片名建文件夹,图片按 `图片名+序号` 命名(如 `凯速干裤01.jpg`);尺码表命名为 `尺码表`,视频命名为 `视频01`。

## 快速开始

### 1. 配置

复制 `config.example.json` 为 `config.json` 并填写:

```json
{
  "suppliers": { "供货商名": "albumId(从网页版URL #/shop_detail/后面那段取)" },
  "ai_vision": { "base_url": "...", "api_key": "...", "model": "qwen3-vl-flash" },
  "feishu": { "app_id": "...", "app_secret": "...", "base_id": "...", "table_id": "...",
              "info_field": "信息", "img_name_field": "图片名" }
}
```

> `config.json` 含密码/密钥,已被 `.gitignore` 排除,不会进 git。

飞书需在「开放平台」给应用开多维表格读写权限,并把应用**加入到具体那张多维表格**(可编辑)。

### 2. 抓数据

在浏览器里登录微购相册网页版,抓取全部供货商的上新数据存成 `scrape_all.json`(结构见下)。目前这一步由 Claude Code 在浏览器执行。

```json
{ "data": { "albumId": { "supplier": "供货商名", "items": [ {"goods_id","title","imgsSrc","time_stamp","videoUrl"} ] } } }
```

### 3. 预览分组(推荐)

```bash
SCRAPE_JSON=~/Downloads/scrape_all.json python3 pick_products.py preview
```

按提示多选供货商(`1,3,5` / `1-4` / `all`),自动打开「分组预览.html」。调整边界后点「确认并下载」,得到 `confirmed_groups.json`。

### 4. 处理

```bash
python3 pick_products.py process ~/Downloads/confirmed_groups.json
```

下载图片、AI 分类排序、写飞书、建文件夹一气呵成。

## 运行模式

| 命令 | 说明 |
|------|------|
| `pick_products.py preview` | 生成分组预览网页(人工确认前) |
| `pick_products.py process <confirmed.json>` | 按确认的分组处理 |
| `pick_products.py run` | 直接处理(AI 分组,不预览),适合以后自动化 |

多选供货商:交互菜单,或用环境变量 `SUPPLIERS="供货商A,供货商B"`(或 `all`)跳过菜单。
限制产品数(调试用):`MAX_PRODUCTS=1`。

## 进度记录

`progress.json` 记录每个供货商上次处理到的时间戳。首次只处理最新日期的产品,之后只处理更新的部分。

## 输出

产品文件夹默认建在 `/Users/nick/Downloads/weidian_products-main/商品图/`(在 `pick_products.py` 顶部 `OUTPUT_DIR` 改)。每个文件夹含编号好的图片 + `文案.txt`。临时/缓存文件放系统临时目录,不污染输出目录。

## 文件

- `pick_products.py` — 主脚本
- `capture_szwego.py` — mitmproxy 抓包辅助(备用,当前用浏览器抓)
- `config.example.json` — 配置模板
- `上架SKILLS.md` — 原始需求
