# 每日市场雷达

财联社电报抓取 + 飞书卡片推送：读取 `index_products.txt` 与「今日投资舆情热点」，组装飞书交互卡片定时/手动推送。

## 本地运行

```bash
# 复制配置并填写 Webhook
cp .env.example .env
# 编辑 .env，必填 FEISHU_WEBHOOK_URL

pip install requests
python daily_push_bot.py
```

## GitHub Actions 部署

### 1. 配置

- **Secrets**（必填）：`FEISHU_WEBHOOK_URL` = 飞书机器人 Webhook 完整 URL（与本地 .env 里一致即可）。
- **Variables**（可选）：若希望线上和本地 .env 一致，可在 Variables 里添加同名项：`FEISHU_CARD_TITLE`、`FEISHU_CARD_HEADER_TEMPLATE`、`CLS_KEYWORD`、`INDEX_PRODUCTS_TXT`、`PRODUCT_NAME_WRAP_WIDTH`。不设则用脚本默认值。
- 本地继续用 `.env` 即可，无需挪出或改配置方式。

### 2. 触发方式

- **定时**：UTC+8 周一到周五早上 9:00 自动执行（工作流内为 UTC 1:00）
- **手动**：仓库 **Actions** 页选择 **Daily Push** → **Run workflow** → **Run workflow**

### 3. 仓库内容

- 需提交 `index_products.txt`（每行一条产品，格式：`产品名 代码 6位数字 发行日 YYYY-MM-DD`），否则仅推送行业热点部分。
