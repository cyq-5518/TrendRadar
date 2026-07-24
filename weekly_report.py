"""Build and deliver a weekly TrendRadar report from saved TXT snapshots."""

from __future__ import annotations

import argparse
import html
import os
import re
import smtplib
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from email.header import Header
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
from pathlib import Path
from typing import Any

import requests

try:
    import yaml
except ImportError:  # Allows an offline, no-notify local preview without dependencies.
    yaml = None


ROOT = Path(__file__).resolve().parent
OUTPUT_DIR = ROOT / "output"
SHANGHAI = timezone(timedelta(hours=8), name="Asia/Shanghai")
TITLE_LINE = re.compile(r"^\d+\.\s*(.*?)(?:\s+\[URL:([^\]]+)\])?(?:\s+\[MOBILE:[^\]]+\])?$")
CHINESE_CHUNK = re.compile(r"[\u4e00-\u9fff]{2,}")
STOP_PHRASES = {
    "今天", "最新", "回应", "发布", "宣布", "表示", "事件", "相关", "我们", "中国",
    "美国", "网友", "官方", "记者", "新闻", "视频", "情况", "正在", "已经", "可能",
    "热搜", "热点", "一周", "本周", "全球", "市场", "公司", "平台", "地区", "时间",
    "为什么", "如何看", "上半年", "有什么", "怎么样", "这一次", "这些", "一个", "是否",
    "近日", "目前", "关注的", "值得关", "怎么看", "怎么办",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate the TrendRadar weekly report")
    parser.add_argument("--end-date", help="Report end date in YYYY-MM-DD (default: today in Asia/Shanghai)")
    parser.add_argument("--no-notify", action="store_true", help="Create files without sending notifications")
    return parser.parse_args()


def report_end_date(value: str | None) -> date:
    return date.fromisoformat(value) if value else datetime.now(SHANGHAI).date()


def read_snapshots(end: date) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for offset in range(6, -1, -1):
        day = end - timedelta(days=offset)
        folder = OUTPUT_DIR / day.isoformat() / "txt"
        for path in sorted(folder.glob("*.txt")) if folder.exists() else []:
            source = ""
            for raw_line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                if " | " in raw_line and not raw_line[:1].isdigit():
                    source = raw_line.split(" | ", 1)[0].strip()
                    continue
                match = TITLE_LINE.match(raw_line.strip())
                if not match or not source:
                    continue
                title, url = match.groups()
                title = title.strip()
                if title:
                    items.append({"date": day, "source": source, "title": title, "url": url or ""})
    return items


def title_phrases(title: str) -> set[str]:
    phrases: set[str] = set()
    for chunk in CHINESE_CHUNK.findall(title):
        # Three- and four-character phrases avoid generic two-character words
        # such as “什么” and “如何”, which otherwise dominate a weekly ranking.
        for size in (3, 4):
            for start in range(max(0, len(chunk) - size + 1)):
                phrase = chunk[start : start + size]
                if phrase not in STOP_PHRASES:
                    phrases.add(phrase)
    return phrases


def build_topics(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    phrase_items: dict[str, set[int]] = defaultdict(set)
    for index, item in enumerate(items):
        for phrase in title_phrases(item["title"]):
            phrase_items[phrase].add(index)

    candidates = []
    for phrase, indexes in phrase_items.items():
        days = {items[index]["date"] for index in indexes}
        sources = {items[index]["source"] for index in indexes}
        if len(indexes) >= 3 and (len(days) >= 2 or len(sources) >= 3):
            score = len(indexes) + len(days) * 3 + len(sources) * 2
            candidates.append((score, phrase, indexes))

    topics: list[dict[str, Any]] = []
    used: set[int] = set()
    for _, phrase, indexes in sorted(candidates, key=lambda row: (-row[0], row[1])):
        fresh = indexes - used
        if len(fresh) < 3:
            continue
        topic_items = [items[index] for index in sorted(fresh)]
        days = {item["date"] for item in topic_items}
        sources = {item["source"] for item in topic_items}
        examples = sorted(topic_items, key=lambda item: (item["date"], item["source"]))[:3]
        topics.append({
            "name": phrase,
            "count": len(topic_items),
            "days": len(days),
            "sources": len(sources),
            "examples": examples,
            "must_watch": len(days) >= 3 or (len(sources) >= 5 and len(topic_items) >= 8),
        })
        used.update(fresh)
        if len(topics) == 10:
            break
    return topics


def report_markdown(end: date, items: list[dict[str, Any]], topics: list[dict[str, Any]], report_url: str) -> str:
    start = end - timedelta(days=6)
    snapshots = len({(item["date"], item["source"]) for item in items})
    lines = [
        f"# TrendRadar 一周资讯报告（{start:%Y-%m-%d} ～ {end:%Y-%m-%d}）",
        "",
        f"本周共汇总 {len(items)} 条榜单记录，覆盖 {len({item['source'] for item in items})} 个来源和 {snapshots} 份来源快照。",
        "",
        "## 必须关注",
        "",
    ]
    watch_list = [topic for topic in topics if topic["must_watch"]][:3]
    if watch_list:
        for topic in watch_list:
            lines.append(
                f"- **⚠️ {topic['name']}**：连续/反复出现在 {topic['days']} 天、{topic['sources']} 个来源，共 {topic['count']} 次；建议优先跟进。"
            )
    else:
        lines.append("- 本周没有达到连续 3 天或多来源反复出现阈值的主题。")

    lines.extend(["", "## 本周热点", ""])
    if not topics:
        lines.append("近 7 天可用快照不足，暂不能识别跨日热点。")
    for position, topic in enumerate(topics, 1):
        marker = " ⚠️ **重点关注**" if topic["must_watch"] else ""
        lines.append(
            f"### {position}. {topic['name']}{marker}\n"
            f"本周在 {topic['days']} 天、{topic['sources']} 个来源中出现 {topic['count']} 次，"
            "说明它是持续受到关注的议题。"
        )
        for example in topic["examples"]:
            link = f" ([原文]({example['url']}))" if example["url"] else ""
            lines.append(f"- {example['title']} — {example['source']}{link}")
        lines.append("")

    lines.extend([
        "## 阅读说明",
        "",
        "- 热点按标题中的重复短语、跨日出现次数和来源数自动聚合；它反映关注度，不等同于事实重要性。",
        "- “必须关注”阈值：至少连续/跨 3 天出现，或在至少 5 个来源出现 8 次以上。",
        f"- 完整 HTML 报告：{report_url}" if report_url else "",
    ])
    return "\n".join(line for line in lines if line is not None).strip() + "\n"


def write_html(markdown: str, path: Path) -> None:
    escaped = html.escape(markdown)
    document = f"""<!doctype html><html lang=\"zh-CN\"><meta charset=\"utf-8\"><title>TrendRadar 一周资讯报告</title>
<style>body{{max-width:900px;margin:40px auto;padding:0 20px;color:#1f2937;font:16px/1.7 system-ui,sans-serif}}pre{{white-space:pre-wrap;word-break:break-word}}h1{{color:#0f766e}}</style>
<body><pre>{escaped}</pre></body></html>"""
    path.write_text(document, encoding="utf-8")


def config_value(config: dict, env_name: str, *keys: str, default: str = "") -> str:
    if os.getenv(env_name):
        return os.environ[env_name]
    value: Any = config
    for key in keys:
        value = value.get(key, {}) if isinstance(value, dict) else {}
    return str(value or default)


def post_json(url: str, payload: dict) -> bool:
    try:
        response = requests.post(url, json=payload, timeout=30)
        response.raise_for_status()
        return True
    except requests.RequestException as error:
        print(f"通知发送失败: {error}")
        return False


def send_notifications(config: dict, text: str, html_path: Path) -> None:
    if not config.get("notification", {}).get("enable_notification", True):
        print("通知功能已禁用，跳过周报推送")
        return
    summary = text[:3500]
    feishu = config_value(config, "FEISHU_WEBHOOK_URL", "notification", "webhooks", "feishu_url")
    for url in filter(None, feishu.split(";")):
        post_json(url.strip(), {"msg_type": "text", "content": {"text": summary}})
    dingtalk = config_value(config, "DINGTALK_WEBHOOK_URL", "notification", "webhooks", "dingtalk_url")
    for url in filter(None, dingtalk.split(";")):
        post_json(url.strip(), {"msgtype": "markdown", "markdown": {"title": "TrendRadar 一周资讯报告", "text": summary}})
    wework = config_value(config, "WEWORK_WEBHOOK_URL", "notification", "webhooks", "wework_url")
    for url in filter(None, wework.split(";")):
        post_json(url.strip(), {"msgtype": "markdown", "markdown": {"content": summary}})
    slack = config_value(config, "SLACK_WEBHOOK_URL", "notification", "webhooks", "slack_webhook_url")
    for url in filter(None, slack.split(";")):
        post_json(url.strip(), {"text": summary})
    ntfy_topic = config_value(config, "NTFY_TOPIC", "notification", "webhooks", "ntfy_topic")
    if ntfy_topic:
        server = config_value(config, "NTFY_SERVER_URL", "notification", "webhooks", "ntfy_server_url", default="https://ntfy.sh")
        post_json(server.rstrip("/") + "/", {"topic": ntfy_topic, "title": "TrendRadar 一周资讯报告", "message": summary})
    bark = config_value(config, "BARK_URL", "notification", "webhooks", "bark_url")
    for bark_url in filter(None, bark.split(";")):
        parsed = re.match(r"(https?://[^/]+)/([^/?]+)", bark_url.strip())
        if parsed:
            endpoint, device_key = parsed.groups()
            post_json(f"{endpoint}/push", {"title": "TrendRadar 一周资讯报告", "markdown": summary[:3500], "device_key": device_key, "group": "TrendRadar"})

    token = config_value(config, "TELEGRAM_BOT_TOKEN", "notification", "webhooks", "telegram_bot_token")
    chat_id = config_value(config, "TELEGRAM_CHAT_ID", "notification", "webhooks", "telegram_chat_id")
    for bot, chat in zip(filter(None, token.split(";")), filter(None, chat_id.split(";"))):
        post_json(f"https://api.telegram.org/bot{bot}/sendMessage", {"chat_id": chat, "text": summary})

    sender = config_value(config, "EMAIL_FROM", "notification", "webhooks", "email_from")
    password = config_value(config, "EMAIL_PASSWORD", "notification", "webhooks", "email_password")
    recipients = config_value(config, "EMAIL_TO", "notification", "webhooks", "email_to")
    if sender and password and recipients:
        message = MIMEMultipart("alternative")
        message["Subject"] = Header("TrendRadar 一周资讯报告", "utf-8")
        message["From"] = formataddr(("TrendRadar", sender))
        message["To"] = recipients
        message.attach(MIMEText(summary, "plain", "utf-8"))
        message.attach(MIMEText(html_path.read_text(encoding="utf-8"), "html", "utf-8"))
        try:
            with smtplib.SMTP_SSL("smtp." + sender.split("@")[-1], 465, timeout=30) as smtp:
                smtp.login(sender, password)
                smtp.sendmail(sender, [value.strip() for value in recipients.split(",")], message.as_string())
        except Exception as error:
            print(f"邮件周报发送失败: {error}")


def main() -> None:
    args = parse_args()
    end = report_end_date(args.end_date)
    if yaml is None and not args.no_notify:
        raise RuntimeError("PyYAML is required to read notification settings; run `pip install -r requirements.txt`.")
    config = (
        yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8")) or {}
        if yaml is not None
        else {}
    )
    items = read_snapshots(end)
    topics = build_topics(items)
    week = f"{end.isocalendar().year}-W{end.isocalendar().week:02d}"
    report_dir = OUTPUT_DIR / "weekly"
    report_dir.mkdir(parents=True, exist_ok=True)
    report_url_base = config.get("notification", {}).get("report_url", "").rstrip("/")
    report_url = f"{report_url_base}/output/weekly/{week}.html" if report_url_base else ""
    markdown = report_markdown(end, items, topics, report_url)
    markdown_path = report_dir / f"{week}.md"
    html_path = report_dir / f"{week}.html"
    markdown_path.write_text(markdown, encoding="utf-8")
    write_html(markdown, html_path)
    print(f"周报已生成: {markdown_path} / {html_path}（读取 {len(items)} 条记录）")
    if not args.no_notify:
        send_notifications(config, markdown, html_path)


if __name__ == "__main__":
    main()
