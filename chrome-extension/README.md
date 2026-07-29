# 微购相册自动抓取扩展

1. 打开 Chrome `chrome://extensions/`。
2. 打开右上角「开发者模式」。
3. 点击「加载已解压的扩展程序」，选择本目录。
4. 登录微购相册后打开 `https://www.szwego.com/static/index.html`。

扩展会自动抓取 `config.json` 中的供货商，并下载 `scrape_all.json`。
供应商配置变化后，重新运行 `python3 pick_products.py extension`，再在扩展页点刷新。
