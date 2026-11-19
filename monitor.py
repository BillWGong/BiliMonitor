#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""B站 UP 主更新监控。

每 60 秒检查一次指定 UP 主的视频与动态，发现新内容立即通过 SMTP 发送高优先级邮件。
仅依赖 Python 标准库，macOS 自带 python3 即可运行。
"""

import argparse
import html
import json
import os
import re
import shutil
import smtplib
import ssl
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
SUPPORT_DIR = Path.home() / "Library" / "Application Support" / "BiliMonitor"


def resolve_data_dir():
    """优先使用环境变量；源码目录存在 config.json 时沿用源码目录，否则用系统应用支持目录。"""
    custom = os.environ.get("BILIMONITOR_HOME")
    if custom:
        return Path(custom).expanduser()
    if (APP_DIR / "config.json").exists():
        return APP_DIR
    return SUPPORT_DIR


DATA_DIR = resolve_data_dir()
DATA_DIR.mkdir(parents=True, exist_ok=True)
CONFIG_PATH = DATA_DIR / "config.json"
STATE_PATH = DATA_DIR / "state.json"
LOG_PATH = DATA_DIR / "monitor.log"

BEIJING = timezone(timedelta(hours=8))
UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
RISK_CODES = {-352, -412, -509, -799}

TYPE_LABELS = {
    "DYNAMIC_TYPE_AV": "视频",
    "DYNAMIC_TYPE_DRAW": "图文",
    "DYNAMIC_TYPE_WORD": "文字",
    "DYNAMIC_TYPE_ARTICLE": "专栏",
    "DYNAMIC_TYPE_FORWARD": "转发",
    "DYNAMIC_TYPE_PGC": "番剧",
    "DYNAMIC_TYPE_LIVE_RCMD": "直播",
    "DYNAMIC_TYPE_MUSIC": "音频",
    "DYNAMIC_TYPE_COMMON_SQUARE": "装扮",
}


def log(message):
    line = "[{}] {}".format(datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S"), message)
    print(line, flush=True)
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > 5 * 1024 * 1024:
            LOG_PATH.replace(LOG_PATH.with_name("monitor.log.1"))
        with LOG_PATH.open("a", encoding="utf-8") as fp:
            fp.write(line + "\n")
    except OSError:
        pass


def load_json(path, default):
    try:
        with path.open("r", encoding="utf-8") as fp:
            return json.load(fp)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)
    tmp.replace(path)


def load_config():
    if not CONFIG_PATH.exists():
        example = APP_DIR / "config.example.json"
        if example.exists():
            shutil.copyfile(example, CONFIG_PATH)
            sys.exit("已生成配置文件：{}\n请填写 SMTP 账号与授权码后重新运行。".format(CONFIG_PATH))
        sys.exit("缺少配置文件：{}".format(CONFIG_PATH))
    cfg = load_json(CONFIG_PATH, None)
    if not cfg or not cfg.get("uid"):
        sys.exit("config.json 中未填写 uid")
    cfg["uid"] = str(cfg["uid"]).strip()
    cfg.setdefault("interval_seconds", 60)
    cfg.setdefault("smtp", {})
    smtp = cfg["smtp"]
    to = smtp.get("to")
    if isinstance(to, str):
        to = [to]
    smtp["to"] = [addr.strip() for addr in (to or []) if addr and addr.strip()]
    return cfg


def smtp_ready(smtp):
    return bool(
        smtp.get("host")
        and smtp.get("username")
        and smtp.get("password")
        and smtp.get("to")
    )


def fmt_time(ts):
    return datetime.fromtimestamp(ts, BEIJING).strftime("%Y-%m-%d %H:%M:%S")


def rel_time(ts):
    delta = int(time.time()) - int(ts)
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return "{} 分钟前".format(delta // 60)
    if delta < 86400:
        return "{} 小时前".format(delta // 3600)
    return "{} 天前".format(delta // 86400)


def clean_text(text, limit=800):
    if not text:
        return ""
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def https_url(url):
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    if url.startswith("http://"):
        return "https://" + url[len("http://"):]
    return url


class BiliClient:
    def __init__(self, cookie=""):
        self.cookie = cookie or ""

    def _headers(self, referer):
        headers = {
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
        }
        if referer:
            headers["Referer"] = referer
        if self.cookie:
            headers["Cookie"] = self.cookie
        return headers

    def _get_json(self, url, referer=""):
        req = urllib.request.Request(url, headers=self._headers(referer))
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8", "replace"))

    def refresh_cookie(self):
        data = self._get_json(
            "https://api.bilibili.com/x/frontend/finger/spi",
            "https://www.bilibili.com/",
        )
        spi = data.get("data") or {}
        if not spi.get("b_3"):
            raise RuntimeError("获取 buvid 失败：{}".format(data.get("message")))
        self.cookie = "buvid3={}; buvid4={}; b_nut={}".format(
            spi["b_3"], spi.get("b_4", ""), int(time.time())
        )

    def fetch_feed(self, uid):
        """返回 (items, up_name)。items 同时包含视频投稿与图文动态。"""
        url = (
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
            "?host_mid={}&features=itemOpusStyle".format(uid)
        )
        referer = "https://space.bilibili.com/{}/dynamic".format(uid)
        last_error = None
        for attempt in range(3):
            if not self.cookie:
                try:
                    self.refresh_cookie()
                except Exception as exc:
                    last_error = exc
                    time.sleep(3 + attempt * 4)
                    continue
            try:
                data = self._get_json(url, referer)
                code = data.get("code")
                if code == 0:
                    items = (data.get("data") or {}).get("items") or []
                    if items:
                        up_name = ""
                        for raw in items:
                            author = (raw.get("modules") or {}).get("module_author") or {}
                            if author.get("name"):
                                up_name = author["name"]
                                break
                        return items, up_name
                    last_error = RuntimeError("接口返回空列表")
                else:
                    last_error = RuntimeError("code={} message={}".format(code, data.get("message")))
                    if code not in RISK_CODES:
                        raise last_error
            except Exception as exc:
                last_error = exc
            time.sleep(4 + attempt * 6)
        raise RuntimeError("动态接口请求失败：{}".format(last_error))

    def fetch_opus_feed(self, uid):
        url = (
            "https://api.bilibili.com/x/polymer/web-dynamic/v1/opus/feed/space"
            "?host_mid={}&page=1".format(uid)
        )
        data = self._get_json(url, "https://space.bilibili.com/{}/article".format(uid))
        if data.get("code") != 0:
            raise RuntimeError("图文接口失败：code={} message={}".format(data.get("code"), data.get("message")))
        return (data.get("data") or {}).get("items") or []

    def fetch_opus_pub_ts(self, opus_id):
        url = "https://www.bilibili.com/opus/{}".format(opus_id)
        req = urllib.request.Request(url, headers=self._headers("https://www.bilibili.com/"))
        with urllib.request.urlopen(req, timeout=20) as resp:
            page = resp.read().decode("utf-8", "replace")
        matched = re.search(r'"pub_ts":"(\d+)"', page)
        if not matched:
            raise RuntimeError("未能从图文页解析发布时间")
        return int(matched.group(1))


def normalize_feed_item(raw):
    modules = raw.get("modules") or {}
    author = modules.get("module_author") or {}
    dynamic = modules.get("module_dynamic") or {}
    major = dynamic.get("major") or {}
    desc_text = (dynamic.get("desc") or {}).get("text") or ""
    item_type = raw.get("type") or ""
    pub_ts = int(author.get("pub_ts") or 0)
    if not pub_ts:
        return None

    item = {
        "key": "",
        "kind": "dynamic",
        "type_label": TYPE_LABELS.get(item_type, "动态"),
        "title": "",
        "text": "",
        "url": "",
        "pub_ts": pub_ts,
        "cover": "",
        "extra": "",
    }

    if item_type == "DYNAMIC_TYPE_AV" and major.get("archive"):
        archive = major["archive"]
        bvid = archive.get("bvid") or ""
        item.update(
            key="video:{}".format(bvid or raw.get("id_str")),
            kind="video",
            type_label="视频",
            title=archive.get("title") or "未命名视频",
            text=desc_text or archive.get("desc") or "",
            url=https_url(archive.get("jump_url")) or "https://www.bilibili.com/video/{}".format(bvid),
            cover=https_url(archive.get("cover")),
            extra="时长 {}｜播放 {}".format(
                archive.get("duration_text") or "--",
                (archive.get("stat") or {}).get("play") or "--",
            ),
        )
    elif item_type == "DYNAMIC_TYPE_ARTICLE" and major.get("article"):
        article = major["article"]
        item.update(
            key="dynamic:{}".format(raw.get("id_str")),
            type_label="专栏",
            title=article.get("title") or "新专栏",
            text=article.get("desc") or desc_text,
            url=https_url(article.get("jump_url")),
            cover=https_url((article.get("covers") or [""])[0]),
        )
    else:
        opus = major.get("opus") or {}
        summary = (opus.get("summary") or {}).get("text") or ""
        title = opus.get("title") or ""
        text = summary or desc_text
        jump = opus.get("jump_url") or ((raw.get("basic") or {}).get("jump_url") or "")
        if not text and item_type == "DYNAMIC_TYPE_FORWARD":
            text = "转发了一条动态"
        if not text:
            text = "[{}]".format(item["type_label"])
        item.update(
            key="dynamic:{}".format(raw.get("id_str")),
            title=title or clean_text(text, 40),
            text=text,
            url=https_url(jump) or "https://t.bilibili.com/{}".format(raw.get("id_str")),
            cover=https_url(((opus.get("pics") or [{}])[0] or {}).get("url")),
        )
    if not item["title"]:
        item["title"] = "未命名内容"
    return item


def build_subject(up_name, items):
    videos = sum(1 for item in items if item["kind"] == "video")
    if len(items) == 1:
        item = items[0]
        if item["kind"] == "video":
            return "【重要】B站更新｜{} 发布新视频：{}".format(up_name, clean_text(item["title"], 60))
        return "【重要】B站更新｜{} 发布新动态：{}".format(up_name, clean_text(item["title"], 40))
    if videos == len(items):
        return "【重要】B站更新｜{} 发布 {} 个新视频".format(up_name, len(items))
    return "【重要】B站更新｜{} 发布 {} 条新内容".format(up_name, len(items))


def build_text(up_name, items, interval=60):
    lines = [
        "B站更新提醒",
        "",
        "UP主：{}".format(up_name),
        "检测时间：{}（北京时间）".format(datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S")),
        "本次共 {} 条新内容".format(len(items)),
        "",
    ]
    for index, item in enumerate(items, 1):
        lines.append("{}. 【{}】{}".format(index, item["type_label"], item["title"]))
        lines.append("   发布时间：{}（北京时间，{}）".format(fmt_time(item["pub_ts"]), rel_time(item["pub_ts"])))
        if item.get("extra"):
            lines.append("   {}".format(item["extra"]))
        if item.get("text"):
            lines.append("   内容：{}".format(clean_text(item["text"], 300)))
        lines.append("   链接：{}".format(item["url"]))
        lines.append("")
    lines.append("本邮件由 B站更新监控 自动发送，每 {} 秒检测一次。".format(interval))
    return "\n".join(lines)


def build_html(up_name, items, interval):
    cards = []
    for item in items:
        cover = ""
        if item.get("cover"):
            cover = (
                '<img src="{}" width="140" style="display:block;border-radius:6px;'
                'margin:0 0 10px;" alt="">'.format(html.escape(item["cover"]))
            )
        text = item.get("text") or ""
        text_block = ""
        if text:
            text_block = (
                '<div style="font-size:13px;line-height:1.7;color:#5c6470;margin-top:6px;">'
                "{}</div>".format(html.escape(clean_text(text, 500)))
            )
        extra = ""
        if item.get("extra"):
            extra = (
                '<div style="font-size:12px;color:#8a919f;margin-top:6px;">{}</div>'.format(
                    html.escape(item["extra"])
                )
            )
        cards.append(
            """
            <div style="padding:18px 0;border-bottom:1px solid #f0f1f3;">
              {cover}
              <span style="display:inline-block;background:#fff0f5;color:#fb7299;border-radius:4px;
                           padding:2px 8px;font-size:12px;font-weight:600;">{label}</span>
              <span style="font-size:12px;color:#8a919f;margin-left:8px;">{pub}</span>
              <div style="font-size:16px;font-weight:600;color:#1f2329;margin-top:10px;">{title}</div>
              {text}
              {extra}
              <div style="margin-top:12px;">
                <a href="{url}" style="display:inline-block;background:#fb7299;color:#ffffff;
                       text-decoration:none;padding:8px 18px;border-radius:6px;font-size:13px;">
                  立即查看
                </a>
              </div>
            </div>
            """.format(
                cover=cover,
                label=html.escape(item["type_label"]),
                pub=html.escape("{}（{}）".format(fmt_time(item["pub_ts"]), rel_time(item["pub_ts"]))),
                title=html.escape(item["title"]),
                text=text_block,
                extra=extra,
                url=html.escape(item["url"]),
            )
        )
    return """\
<div style="background:#f5f6f8;padding:24px 12px;">
  <div style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC','Microsoft YaHei',Arial,sans-serif;
              max-width:640px;margin:0 auto;background:#ffffff;border-radius:10px;overflow:hidden;">
    <div style="background:#fb7299;color:#ffffff;padding:20px;">
      <div style="font-size:12px;letter-spacing:2px;opacity:.9;">B站更新监控 · 即时提醒</div>
      <div style="font-size:22px;font-weight:700;margin-top:6px;">{up} 有新内容发布</div>
      <div style="font-size:13px;margin-top:6px;opacity:.95;">
        {detect} 检测到 {count} 条更新
      </div>
    </div>
    <div style="padding:4px 20px 20px;">{cards}</div>
  </div>
  <div style="font-family:-apple-system,BlinkMacSystemFont,'PingFang SC',Arial,sans-serif;
              max-width:640px;margin:14px auto 0;text-align:center;font-size:12px;color:#8a919f;">
    本邮件由 B站更新监控 自动发送 · 每 {interval} 秒检测一次
  </div>
</div>""".format(
        up=html.escape(up_name),
        detect=datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M:%S"),
        count=len(items),
        cards="".join(cards),
        interval=interval,
    )


def compose_message(cfg, subject, text_body, html_body):
    smtp = cfg["smtp"]
    sender = smtp.get("username")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((smtp.get("sender_name") or "B站更新监控", sender))
    msg["To"] = ", ".join(smtp["to"])
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    msg["X-Priority"] = "1 (Highest)"
    msg["X-MSMail-Priority"] = "High"
    msg["Importance"] = "High"
    msg["Priority"] = "urgent"
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    return msg


def send_email(cfg, subject, text_body, html_body):
    smtp = cfg["smtp"]
    msg = compose_message(cfg, subject, text_body, html_body)

    host = smtp["host"]
    port = int(smtp.get("port") or 465)
    context = ssl.create_default_context()
    if smtp.get("use_ssl", True):
        server = smtplib.SMTP_SSL(host, port, timeout=30, context=context)
    else:
        server = smtplib.SMTP(host, port, timeout=30)
        server.ehlo()
        server.starttls(context=context)
        server.ehlo()
    try:
        server.login(smtp["username"], smtp["password"])
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def normalize_opus_item(raw, pub_ts):
    opus_id = raw.get("opus_id") or ""
    content = raw.get("content") or ""
    return {
        "key": "dynamic:{}".format(opus_id),
        "kind": "dynamic",
        "type_label": "图文",
        "title": clean_text(content, 40) or "新图文动态",
        "text": content,
        "url": https_url(raw.get("jump_url")) or "https://www.bilibili.com/opus/{}".format(opus_id),
        "pub_ts": pub_ts,
        "cover": https_url(((raw.get("cover") or {}) or {}).get("url")),
        "extra": "",
    }


def collect_items(client, cfg, state):
    uid = cfg["uid"]
    up_name = state.get("up_name") or ""
    try:
        raw_items, name = client.fetch_feed(uid)
        if name:
            up_name = name
        items = [normalize_feed_item(raw) for raw in raw_items]
        items = [item for item in items if item and item["pub_ts"]]
        items.sort(key=lambda item: item["pub_ts"])
        return items, up_name or "UP主 {}".format(uid), True
    except Exception as exc:
        log("动态接口不可用：{}".format(exc))

    # 兜底：图文接口 + 图文详情页（视频动态无法从该接口获取）
    try:
        raw_items = client.fetch_opus_feed(uid)
        seen = set(state.get("seen") or [])
        items = []
        for raw in raw_items[:10]:
            key = "dynamic:{}".format(raw.get("opus_id"))
            if key in seen:
                continue
            try:
                pub_ts = client.fetch_opus_pub_ts(raw["opus_id"])
            except Exception as exc:
                log("获取图文发布时间失败：{}".format(exc))
                continue
            items.append(normalize_opus_item(raw, pub_ts))
        items.sort(key=lambda item: item["pub_ts"])
        log("已切换到图文兜底接口（本轮可检测图文动态，视频动态暂不可见）")
        return items, up_name or "UP主 {}".format(uid), False
    except Exception as exc:
        log("图文兜底接口同样不可用：{}".format(exc))
        return None, up_name or "UP主 {}".format(uid), False


def check_once(client, cfg, state):
    items, up_name, feed_complete = collect_items(client, cfg, state)
    state["up_name"] = up_name
    state["last_check"] = int(time.time())
    if items is None:
        log("本轮未获取到数据，将在下一轮重试")
        return 0

    if not state.get("initialized"):
        state["seen"] = [item["key"] for item in items][-5000:]
        state["initialized"] = True
        state["initialized_at"] = int(time.time())
        state["baseline_complete"] = feed_complete
        state["pending"] = []
        save_json(STATE_PATH, state)
        log("已建立基线：{} 条已有内容（不会重复提醒），后续每分钟检测更新".format(len(items)))
        for item in items[-3:]:
            log("已有内容｜{}｜{}｜{}".format(item["type_label"], item["title"], fmt_time(item["pub_ts"])))
        if cfg.get("notify_on_start") and smtp_ready(cfg["smtp"]):
            try:
                send_email(
                    cfg,
                    "【重要】B站更新监控已启动｜{}".format(up_name),
                    "监控已启动。\nUP主：{}\n每 {} 秒检测一次。\n发现新视频或动态会立即发送提醒。".format(
                        up_name, cfg["interval_seconds"]
                    ),
                    build_html(up_name, [], cfg["interval_seconds"]),
                )
                log("启动通知邮件已发送")
            except Exception as exc:
                log("启动通知邮件发送失败：{}".format(exc))
        return 0

    seen = set(state.get("seen") or [])
    seen_changed = False
    new_items = []
    for item in items:
        if item["key"] in seen:
            continue
        # 基线建立时若接口不全，首次补全后只提醒基线之后的新内容
        if not state.get("baseline_complete") and item["pub_ts"] <= state.get("initialized_at", 0):
            seen.add(item["key"])
            seen_changed = True
            continue
        new_items.append(item)
    if feed_complete and not state.get("baseline_complete"):
        state["baseline_complete"] = True

    if not new_items:
        if seen_changed:
            state["seen"] = list(seen)[-5000:]
            save_json(STATE_PATH, state)
        log("无更新（当前可见 {} 条）".format(len(items)))
        return 0

    for item in new_items:
        seen.add(item["key"])
        log("发现新内容｜{}｜{}｜{}｜{}".format(
            item["type_label"], item["title"], fmt_time(item["pub_ts"]), item["url"]))
    state["seen"] = list(seen)[-5000:]
    save_json(STATE_PATH, state)

    if not smtp_ready(cfg["smtp"]):
        log("SMTP 未配置，跳过邮件；请填写 config.json 后运行 --test-email 验证")
        return len(new_items)

    pending = state.get("pending") or []
    pending_keys = {item["key"] for item in pending}
    pending.extend(item for item in new_items if item["key"] not in pending_keys)
    pending = pending[-100:]
    state["pending"] = pending
    save_json(STATE_PATH, state)

    try:
        send_email(
            cfg,
            build_subject(up_name, pending),
            build_text(up_name, pending, cfg["interval_seconds"]),
            build_html(up_name, pending, cfg["interval_seconds"]),
        )
        state["pending"] = []
        save_json(STATE_PATH, state)
        log("提醒邮件已发送（{} 条）".format(len(pending)))
    except Exception as exc:
        log("邮件发送失败，将于下一轮重试：{}".format(exc))
    return len(new_items)


def send_test_email(cfg):
    if not smtp_ready(cfg["smtp"]):
        sys.exit("SMTP 未配置完整：请在 config.json 中填写 host/port/username/password/to")
    now = int(time.time())
    items = [
        {
            "key": "test:video",
            "kind": "video",
            "type_label": "视频",
            "title": "测试视频：这是一封监控测试邮件",
            "text": "如果你收到这封邮件，说明 SMTP 配置正确，发现新内容时会立刻发送同格式的高优先级邮件。",
            "url": "https://www.bilibili.com/video/BV1hUsg62Ej6",
            "pub_ts": now,
            "cover": "",
            "extra": "时长 00:30｜播放 72.8万",
        }
    ]
    send_email(
        cfg,
        "【重要】B站更新监控测试邮件｜配置成功",
        build_text("测试 UP 主", items, cfg["interval_seconds"]),
        build_html("测试 UP 主", items, cfg["interval_seconds"]),
    )
    log("测试邮件已发送至：{}".format(", ".join(cfg["smtp"]["to"])))


def main():
    parser = argparse.ArgumentParser(description="B站 UP 主更新监控")
    parser.add_argument("--once", action="store_true", help="只检测一次后退出")
    parser.add_argument("--test-email", action="store_true", help="发送一封测试邮件")
    parser.add_argument("--reset", action="store_true", help="清空基线，重新开始监控")
    args = parser.parse_args()

    cfg = load_config()
    state = load_json(STATE_PATH, {})
    if args.reset:
        state = {}
        save_json(STATE_PATH, state)
        log("已重置监控基线")
    if args.test_email:
        send_test_email(cfg)
        return

    client = BiliClient(state.get("cookie") or "")
    log("开始监控 UP 主 {}，每 {} 秒检测一次（视频 + 动态）".format(cfg["uid"], cfg["interval_seconds"]))
    if not smtp_ready(cfg["smtp"]):
        log("提示：SMTP 尚未配置，目前只会输出到终端和 monitor.log")

    if args.once:
        check_once(client, cfg, state)
        state["cookie"] = client.cookie
        save_json(STATE_PATH, state)
        return

    while True:
        started = time.monotonic()
        try:
            check_once(client, cfg, state)
        except Exception as exc:
            log("本轮检测异常：{}".format(exc))
        state["cookie"] = client.cookie
        save_json(STATE_PATH, state)
        elapsed = time.monotonic() - started
        time.sleep(max(1, cfg["interval_seconds"] - elapsed))


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已停止监控")
