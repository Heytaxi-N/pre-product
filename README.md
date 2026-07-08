# 微购相册挑品自动化

从微购相册(szwego)供货商的「上新」批量挑品:抓数据 → AI 分组成产品 → 下载图片 → AI 分类排序 → 写飞书多维表格 → 按图片名建产品文件夹。产出的文件夹供下游上架脚本读取。

## 它做什么

1. **抓上新** — 从供货商的「上新」接口取新品(帖子含文案 + 多张图 + 可选视频)。
2. **占位图切边界** — 识别与服装无关的占位/分割图,作为产品和批的硬边界,不下载。
3. **AI 分组** — 同一件商品常被拆成 3~4 个帖子(正视图/价格图/模特图/尺码表分开发)。AI 看主图+文案,把相邻帖子判定为同款并合并成一个产品。
4. **人工兜底** — 生成「分组预览」网页,缩略图 + 文案一目了然,点两帖之间的按钮改边界。
5. **AI 分类排图** — 每张图分类,按规则排序:
   `合图(最清晰美观的一张作封面) → 价格图 → 模特图 → 其余合图 → 细节图 → 尺码表 → 视频`
6. **写飞书** — 合并后的文案写入多维表格「信息」字段;飞书自动化据此生成「图片名」。
7. **建文件夹** — 以图片名建文件夹,图片按 `图片名+序号` 命名(如 `凯速干裤01.jpg`);尺码表命名为 `尺码表`,视频命名为 `视频01`。

## 一次性配置(只做一次)

### ① 填 config.json

复制 `config.example.json` 为 `config.json`,填 `suppliers`、`ai_vision.api_key`、`feishu` 四个字段。飞书还需要在开放平台开表格读写权限,并把应用**加入到那张多维表格**(可编辑)。

### ② 装抓取书签

```bash
python3 pick_products.py bookmark
```

浏览器会自动打开一个安装页,**把里面的蓝色按钮拖到书签栏**——完事。以后加了新供货商,重新运行这条命令重装书签即可。

## 日常挑品(每天)

**只有 2 步**:

**① 抓数据** — 浏览器登录 [www.szwego.com](https://www.szwego.com/static/index.html) → 点书签栏的「🛒 抓挑品数据」→ `scrape_all.json` 自动下载到 Downloads(几秒)。

**② 跑脚本** — 一条命令跑完:

```bash
cd ~/Downloads/上架前准备
python3 pick_products.py
```

脚本会自己:
1. 读 `~/Downloads/scrape_all.json`
2. 交互菜单让你选供货商
3. AI 分组、弹「分组预览.html」到浏览器
4. 你在网页里调整分组边界(可选)→ 点右上角「✅ 确认并下载」
5. 脚本**自动接收** `confirmed_groups.json` → 下载图 → AI 分类 → 写飞书 → 建文件夹

整个过程零环境变量、零路径粘贴。

## 高级用法

### 定向下载(按编码筛)

只处理某供货商、文案含某编码的素材:

```bash
python3 pick_products.py run 晨星外贸06 0708d
```

定向模式**不推进进度**,下次跑仍能拿到这批。

### 锚点定向(找某天某关键词素材,前后到占位图为止)

```bash
python3 pick_products.py anchor 南在南方 凯乐石女款裙裤 04-30
```

流程:
1. 检查本地是否有 南在南方 04-30 的数据
2. **没有**就自动打开 szwego 页面(URL 里带 anchor 参数),提示你点书签
3. 书签识别 URL 参数,进入**深挖模式**:用接口的 `startDate`/`endDate` 直接查询该天,自动翻页拿全,下载 `scrape_anchor.json`
4. 脚本自动 merge 到本地 `scrape_all.json`
5. 从匹配"凯乐石女款裙裤"的锚点条目,**向前/向后扩展直到遇到占位图**(只对锚点邻域调 AI,不全扫;单边最多 25 帖兜底)
6. 段内 AI 分组成产品 → 下载 → 分类 → 写飞书 → 建文件夹

不推进进度。

**常见问题**

- `❌ AI 视觉调用失败, 无法进行占位图判定`:说明 `ai_vision.api_key` 无效或额度耗尽。锚点定向依赖占位图判定,AI 不可用会直接终止(避免误处理海量数据)。请检查 [token.xiaorongtech.com](https://token.xiaorongtech.com) 的额度或换 key。
- 扩展到 25 帖仍未遇占位图时,单边会强制停(硬边界)。产出结果里"命中段"最多约 50 帖。

### 单独入口(给自动化脚本用)

- `python3 pick_products.py bookmark` — 生成/更新书签安装页 + 兜底 console 脚本
- `python3 pick_products.py run [供货商] [编码]` — 直接跑,不弹预览
- `python3 pick_products.py anchor <供货商> <关键词> <日期MM-DD>` — 锚点定向
- `python3 pick_products.py process <confirmed.json>` — 只处理已确认的分组文件

环境变量:`SUPPLIERS=`、`CODE=`、`MAX_PRODUCTS=`、`SCRAPE_JSON=`。

### 书签点了没反应?兜底方案

`bookmark` 命令会同时生成 `paste_to_console.js`。打开 szwego 页面 → 按 F12 → Console 标签 → 粘贴脚本内容 → 回车,一次性抓完,不用装书签。

## 进度记录

`progress.json` 记录每个供货商上次处理到的**日期**。首次只处理最新日期的产品,之后只处理更晚日期的。定向下载不推进进度。

## 输出

产品文件夹默认建在 `/Users/nick/Downloads/weidian_products-main/商品图/`(在 `pick_products.py` 顶部 `OUTPUT_DIR` 改)。每个文件夹含编号好的图片 + `文案.txt`。临时/缓存文件放系统临时目录,不污染输出目录。

## 文件

- `pick_products.py` — 主脚本
- `config.example.json` — 配置模板
- `capture_szwego.py` — mitmproxy 抓包辅助(备用,当前用书签抓)
- `上架SKILLS.md` — 原始需求
