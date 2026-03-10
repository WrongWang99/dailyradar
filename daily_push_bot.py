"""
财联社电报抓取 + 飞书卡片推送。

- 从配置文件 .env 读取各项配置（见 .env.example）
- 从 index_products.txt 读取待发行/上市产品
- 抓取财联社关键词下最新一条电报正文，组装飞书交互卡片并推送
"""
import json
import os
import re
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# 配置（从 .env 读取，缺省用默认值）
# ---------------------------------------------------------------------------
_SCRIPT_DIR = Path(__file__).resolve().parent


def _load_env() -> None:
    """从脚本同目录的 .env 加载 KEY=VALUE 到 os.environ（不覆盖已存在变量）。"""
    env_path = _SCRIPT_DIR / ".env"
    if not env_path.exists():
        return
    with open(env_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip()
            if k and v.startswith('"') and v.endswith('"'):
                v = v[1:-1].replace('\\"', '"')
            elif k and v.startswith("'") and v.endswith("'"):
                v = v[1:-1].replace("\\'", "'")
            if k and k not in os.environ:
                os.environ[k] = v


_load_env()

UTC8 = timezone(timedelta(hours=8))
API_SW_URL = "https://www.cls.cn/api/sw"
DETAIL_URL_TEMPLATE = "https://www.cls.cn/detail/{id}"

# 以下从环境变量读取，未设置或空串则用默认值（便于 GitHub Actions 只配部分 Variables）
def _env(key: str, default: str) -> str:
    return (os.environ.get(key) or default).strip()


FEISHU_WEBHOOK = _env("FEISHU_WEBHOOK_URL", "") or _env("FEISHU_WEBHOOK", "")
FEISHU_CARD_TITLE = _env("FEISHU_CARD_TITLE", "每日市场雷达")
FEISHU_CARD_HEADER_TEMPLATE = _env("FEISHU_CARD_HEADER_TEMPLATE", "blue")
CLS_KEYWORD = _env("CLS_KEYWORD", "今日投资舆情热点")
SECTION_HEADER_HOTSPOT = "【今日投资舆情热点】"  # 与 CLS_KEYWORD 对应的小节标题

_txt_path = _env("INDEX_PRODUCTS_TXT", "index_products.txt")
DEFAULT_TXT_PATH = Path(_txt_path) if os.path.isabs(_txt_path) else _SCRIPT_DIR / _txt_path

_non_trading_path = _env("NON_TRADING_DAYS_TXT", "non_trading_days.txt")
NON_TRADING_DAYS_PATH = Path(_non_trading_path) if os.path.isabs(_non_trading_path) else _SCRIPT_DIR / _non_trading_path

try:
    PRODUCT_NAME_WRAP_WIDTH = int(_env("PRODUCT_NAME_WRAP_WIDTH", "18"))
except ValueError:
    PRODUCT_NAME_WRAP_WIDTH = 18


# ---------------------------------------------------------------------------
# 非交易日（跳过推送）
# ---------------------------------------------------------------------------


def _load_non_trading_days(filepath: Path) -> set[date]:
    """从 txt 读取非交易日，每行一个 YYYY-MM-DD，返回 date 集合。"""
    out: set[date] = set()
    if not filepath.exists():
        return out
    try:
        for line in filepath.read_text(encoding="utf-8").splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
            if m:
                try:
                    out.add(datetime.strptime(m.group(1), "%Y-%m-%d").date())
                except ValueError:
                    pass
    except Exception as e:
        print(f"[non_trading_days] 读取 {filepath} 失败: {e}")
    return out


def is_today_non_trading(filepath: Path) -> bool:
    """当前日期(UTC+8)是否在非交易日列表中。"""
    today = _today_utc8()
    non_trading = _load_non_trading_days(filepath)
    return today in non_trading


# ---------------------------------------------------------------------------
# 财联社 API 抓取
# ---------------------------------------------------------------------------


def fetch_latest_id_via_api_sw(keyword: str) -> Optional[str]:
    """调用 cls.cn api/sw 获取关键字下最新一条电报的 id。"""
    params = {
        "app": "CailianpressWeb",
        "os": "web",
        "sv": "8.4.6",
        "sign": "9f8797a1f4de66c2370f7a03990d2737",
    }
    payload = {
        "type": "telegram",
        "keyword": keyword,
        "page": 0,
        "rn": 20,
        "app": "CailianpressWeb",
        "os": "web",
        "sv": "8.4.6",
    }
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
        "Content-Type": "application/json;charset=UTF-8",
    }

    try:
        resp = requests.post(API_SW_URL, params=params, json=payload, headers=headers, timeout=10)
        resp.raise_for_status()
    except Exception as e:
        print(f"[api/sw] 请求失败: {e}")
        return None

    try:
        data = resp.json()
    except Exception as e:
        print(f"[api/sw] 解析 JSON 失败: {e}")
        return None

    # 递归在 JSON 中寻找第一个带 id 的电报列表
    def find_item_list_with_id(obj):
        target_keys = ("id", "news_id", "article_id", "detail_id")

        if isinstance(obj, list):
            if obj and isinstance(obj[0], dict):
                for k in target_keys:
                    if k in obj[0]:
                        return obj
            for elem in obj:
                found = find_item_list_with_id(elem)
                if found is not None:
                    return found
            return None

        if isinstance(obj, dict):
            if isinstance(obj.get("data"), dict):
                found = find_item_list_with_id(obj["data"])
                if found is not None:
                    return found
            if isinstance(obj.get("items"), list):
                found = find_item_list_with_id(obj["items"])
                if found is not None:
                    return found
            for v in obj.values():
                found = find_item_list_with_id(v)
                if found is not None:
                    return found

        return None

    items = find_item_list_with_id(data)
    if not items:
        print("[api/sw] 未在返回中找到电报列表")
        return None

    first = items[0]
    for key in ("id", "news_id", "article_id", "detail_id"):
        if key in first:
            return str(first[key])

    print("[api/sw] 找不到电报 id 字段")
    return None


def fetch_detail_page_text(detail_id: str) -> str:
    """抓取 detail 页面 HTML。"""
    url = DETAIL_URL_TEMPLATE.format(id=detail_id)
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/121.0.0.0 Safari/537.36"
        ),
    }
    resp = requests.get(url, headers=headers, timeout=10)
    resp.raise_for_status()
    return resp.text


def extract_main_telegram_text(raw_html: str) -> str:
    """
    从 detail 页 HTML 中提取电报正文内容（articleDetail.content）。
    """
    m = re.search(
        r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>',
        raw_html,
        re.S,
    )
    if not m:
        raise RuntimeError("未找到 __NEXT_DATA__ 脚本块，无法解析正文")

    data = json.loads(m.group(1))
    content = (
        data.get("props", {})
        .get("initialState", {})
        .get("detail", {})
        .get("articleDetail", {})
        .get("content")
    )
    if not isinstance(content, str) or not content.strip():
        raise RuntimeError("在 __NEXT_DATA__ 中未找到正文 content 字段")
    return content.strip()


def get_latest_telegram(keyword: str = "今日投资舆情热点") -> str:
    """获取关键字下最新一条电报的正文。"""
    print(f"尝试通过 api/sw 获取最新电报 id，关键字：{keyword}")
    detail_id = fetch_latest_id_via_api_sw(keyword)

    if not detail_id:
        raise RuntimeError("无法通过 api/sw 解析出电报 id。")

    print(f"解析到最新电报 id：{detail_id}，开始抓取详情页 ...")
    detail_raw = fetch_detail_page_text(detail_id)
    return extract_main_telegram_text(detail_raw)


# ---------------------------------------------------------------------------
# 指数产品 txt 解析与倒计时
# ---------------------------------------------------------------------------


def read_txt_content(filepath: Path) -> str:
    """
    读取目录下 txt 文件内容，用于「指数产品待发行/上市」部分。
    文件不存在或为空时返回空字符串。
    """
    try:
        if not filepath.exists():
            return ""
        raw = filepath.read_text(encoding="utf-8")
        return raw.strip()
    except Exception as e:
        print(f"[read_txt] 读取 {filepath} 失败: {e}")
        return ""


def _today_utc8() -> date:
    """当前日期（UTC+8）。"""
    return datetime.now(UTC8).date()


def _countdown_text(issue_date: date) -> str:
    """根据发行日与今日（UTC+8）计算倒计时文案。"""
    today = _today_utc8()
    delta = (issue_date - today).days
    if delta == 0:
        return " 倒计时今日"
    if delta > 0:
        return f" 倒计时{delta}天"
    return " 已过"


def _parse_product_line(line: str) -> Optional[tuple[str, str, str, str, str]]:
    """
    从一行解析：产品名称、代码、状态(发行/上市)、日期、倒计时。
    返回 (name, code, status, date_str, countdown_str)，解析失败返回 None。
    状态：行内含「上市日」或「状态 上市」则为「上市」，否则为「发行」。
    """
    line = line.strip()
    if not line:
        return None
    # 状态：显式「状态 上市」或含「上市日」为上市，否则发行
    if re.search(r"状态\s*上市|上市日", line):
        status = "上市"
    else:
        status = "发行"

    code_m = re.search(r"代码\s*(\d+)", line)
    date_m = re.search(r"发行日\s*(\d{4}-\d{2}-\d{2})", line)
    if not date_m:
        date_m = re.search(r"上市日\s*(\d{4})年(\d{1,2})月(\d{1,2})日", line)
        if date_m:
            y, m, d = date_m.group(1), date_m.group(2).zfill(2), date_m.group(3).zfill(2)
            issue_date_str = f"{y}-{m}-{d}"
        else:
            issue_date_str = ""
    else:
        issue_date_str = date_m.group(1)

    if not issue_date_str:
        return None
    if code_m:
        name = line[: code_m.start()].strip()
        code = code_m.group(1)
    else:
        name = re.sub(r"\s*(无代码|上市日|发行日).*", "", line).strip()
        code = "-"
    if not issue_date_str:
        return None
    try:
        issue_date = datetime.strptime(issue_date_str, "%Y-%m-%d").date()
        cnt = _countdown_text(issue_date).strip()
    except ValueError:
        cnt = "-"
    return (name or "-", code, status, issue_date_str, cnt)


def _wrap_text(s: str, width: int = 18) -> str:
    """按指定字数自动换行（每 width 字插入换行）。"""
    if not s or width <= 0:
        return s or ""
    return "\n".join(s[i : i + width] for i in range(0, len(s), width))


def _build_products_column_set_elements(content: str, name_wrap_width: int = 18) -> list[dict]:
    """
    用 column_set 构建「产品名称、代码、状态、日期、倒计时」表格元素列表。
    只保留发行日/上市日 >= 今日(UTC+8) 的产品，已过期的不展示。
    产品名称超过 name_wrap_width 字时自动换行。
    """
    lines = [ln.strip() for ln in content.strip().splitlines() if ln.strip()]
    rows = []
    for ln in lines:
        parsed = _parse_product_line(ln)
        if parsed:
            rows.append(parsed)
    # 只保留今日及以后的产品，已过期的不显示
    today = _today_utc8()
    def _not_past(date_str: str) -> bool:
        try:
            d = datetime.strptime(date_str, "%Y-%m-%d").date()
            return d >= today
        except ValueError:
            return True
    rows = [r for r in rows if _not_past(r[3])]
    if not rows:
        return []

    def col_md(text: str) -> dict:
        return {"tag": "markdown", "content": text or "-"}

    elements = []

    # 表头：产品名称、代码、状态、日期、倒计时
    elements.append({
        "tag": "column_set",
        "columns": [
            {"tag": "column", "width": "weighted", "weight": 3, "elements": [col_md("**产品名称**")]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [col_md("**代码**")]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [col_md("**发行/上市**")]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [col_md("**日期**")]},
            {"tag": "column", "width": "weighted", "weight": 1, "elements": [col_md("**倒计时**")]},
        ],
    })

    # 数据行
    for name, code, status, date, cnt in rows:
        wrapped_name = _wrap_text(name, name_wrap_width)
        elements.append({
            "tag": "column_set",
            "columns": [
                {"tag": "column", "width": "weighted", "weight": 3, "elements": [col_md(wrapped_name)]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [col_md(code)]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [col_md(status)]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [col_md(date)]},
                {"tag": "column", "width": "weighted", "weight": 1, "elements": [col_md(cnt)]},
            ],
        })

    return elements


# ---------------------------------------------------------------------------
# 行业热点（爬取正文）清洗与编号
# ---------------------------------------------------------------------------


def _strip_leading_bullet(s: str) -> str:
    """去掉段首的「·」或「・」及紧跟的空格（爬取内容常带此前缀）。"""
    return re.sub(r"^\s*[·．・]\s*", "", s).strip()


def _strip_leading_number(s: str) -> str:
    """去掉段首已有的「1)」「1）」等编号（含半角/全角括号），避免与后续统一编号重复。"""
    return re.sub(r"^\s*\d+[).)\）\.．]\s*", "", s).strip()


def _normalize_scraped_line(line: str) -> str:
    """对爬取的一行：先去掉开头的 ·，再去掉已有序号，得到纯正文。"""
    s = line.strip()
    s = _strip_leading_bullet(s)
    s = _strip_leading_number(s)
    return s.strip()


def _format_scraped_as_numbered(content: str) -> str:
    """
    将爬取的行业热点格式化为有序列表 1. 2. 3.；
    先从整段文本中彻底删除「【今日投资舆情热点】」再解析；文本自带的 1）2） 等会在 _normalize_scraped_line 中删除。
    """
    if not content.strip():
        return ""
    # 从整段中删除该标题（含常见变体：全角括号、前后空白、单独成行）
    content = re.sub(r"\s*【今日投资舆情热点】\s*", "\n", content)
    content = re.sub(r"\n{2,}", "\n", content).strip()

    parts = re.split(r"\n+", content)
    parts = [p.strip() for p in parts if p.strip()]
    if len(parts) == 1 and len(parts[0]) > 80:
        parts = [p.strip() for p in re.split(r"[。；]\s*", parts[0]) if p.strip()]
    parts = [_normalize_scraped_line(p) for p in parts]
    parts = [p for p in parts if p]
    if not parts:
        return ""
    return "\n".join(f"{i}. {p}" for i, p in enumerate(parts, 1))


# ---------------------------------------------------------------------------
# 飞书卡片构建与推送
# ---------------------------------------------------------------------------


def _build_feishu_card(txt_content: str, scraped_content: str) -> dict:
    """
    构建飞书交互卡片 payload。上部分用 column_set 表格（产品名称超 18 字自动换行），下部分用 markdown。
    """
    elements = []

    # 上部分：指数产品待发行/上市（column_set 表格）
    top_title = "【📊 哪些指数产品待发行/上市?】"
    column_set_elements = _build_products_column_set_elements(txt_content, name_wrap_width=PRODUCT_NAME_WRAP_WIDTH)
    if column_set_elements:
        elements.append({"tag": "markdown", "content": f"**{top_title}**"})
        elements.extend(column_set_elements)

    # 下部分：行业热点
    bottom_title = "【🔥 哪些行业火?】"
    bottom_body = _format_scraped_as_numbered(scraped_content)
    if bottom_body:
        elements.append({
            "tag": "markdown",
            "content": f"**{bottom_title}**\n\n{bottom_body}",
        })

    if not elements:
        elements.append({"tag": "markdown", "content": "（暂无内容）"})

    card = {
        "header": {
            "title": {"tag": "plain_text", "content": FEISHU_CARD_TITLE},
            "template": FEISHU_CARD_HEADER_TEMPLATE,
        },
        "elements": elements,
    }
    return {"msg_type": "interactive", "card": card}


def send_card_to_feishu(txt_content: str, scraped_content: str) -> None:
    """
    使用飞书交互卡片推送（若机器人设了安全关键词，请改为「签名校验」否则可能发送失败）。
    """
    if not FEISHU_WEBHOOK:
        raise ValueError("未配置 FEISHU_WEBHOOK_URL，请在 .env 中填写飞书机器人 Webhook")
    payload = _build_feishu_card(txt_content, scraped_content)
    resp = requests.post(FEISHU_WEBHOOK, json=payload, timeout=10)
    resp.raise_for_status()
    print("飞书卡片推送完成，返回：", resp.text)


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        if not FEISHU_WEBHOOK:
            print("错误：未配置 FEISHU_WEBHOOK_URL。请复制 .env.example 为 .env 并填写 Webhook。")
            raise SystemExit(1)

        if is_today_non_trading(NON_TRADING_DAYS_PATH):
            print(f"今日({_today_utc8().isoformat()})在非交易日列表中，跳过推送。")
            raise SystemExit(0)

        txt_content = read_txt_content(DEFAULT_TXT_PATH)
        if txt_content:
            print(f"已读取 {DEFAULT_TXT_PATH.name}，共 {len(txt_content.splitlines())} 行")
        else:
            print(f"未找到或为空: {DEFAULT_TXT_PATH}，仅推送爬取内容")

        scraped = get_latest_telegram(CLS_KEYWORD)
        print(f"\n=== 爬取的电报（关键词: {CLS_KEYWORD}）===")
        print(scraped[:500] + ("..." if len(scraped) > 500 else ""))

        print("\n=== 推送飞书交互卡片 ===")
        send_card_to_feishu(txt_content, scraped)
    except Exception as e:
        print(f"执行失败：{e}")
        raise SystemExit(1)
