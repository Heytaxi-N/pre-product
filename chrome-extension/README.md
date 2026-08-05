# 微购相册自动抓取扩展

1. 打开 Chrome `chrome://extensions/`。
2. 打开右上角「开发者模式」。
3. 点击「加载已解压的扩展程序」，选择本目录。
4. 登录微购相册后打开 `https://www.szwego.com/static/index.html`。

扩展会自动抓取 `config.json` 中的供货商，并下载 `scrape_all.json` 到 Chrome 的 Downloads 中转目录。运行挑品脚本时会自动归档到项目 `data/`。
运行 `python3 pick_products.py` 时会把当前 config.json 的供货商配置自动传给扩展,无需重复生成扩展。
