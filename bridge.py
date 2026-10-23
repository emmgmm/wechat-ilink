#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信 iLink 桥接机器人（个人微信 1v1 私聊）

走腾讯官方 iLink 机器人网关（https://ilinkai.weixin.qq.com）：
扫码授权 -> 长轮询收消息 -> 按配置回复文本。

接口字段参考腾讯官方 npm 包 @tencent-weixin/openclaw-weixin（MIT 协议）
的实际实现；仅使用官方网关，不使用 itchat / wechaty 等非官方方案。

依赖：pip install -r requirements.txt   （仅 requests）
运行：python bridge.py
"""

import base64
import json
import os
import random
import signal
import struct
import sys
import time
import uuid

try:
    import requests
except ImportError:
    print("缺少依赖 requests，请先执行：pip install -r requirements.txt")
    sys.exit(1)

# ---------------------------------------------------------------------------
# 协议常量（来自官方实现）
# ---------------------------------------------------------------------------
BASE_URL = "https://ilinkai.weixin.qq.com"
BOT_TYPE = 3                      # 扫码登录用的 bot 类型
ILINK_APP_ID = "bot"              # 官方包 package.json 中的 ilink_appid
CLIENT_VERSION_STR = "1.0.0"      # 映射为 iLink-App-ClientVersion
BRIDGE_VERSION = "1.0.0"

MSG_TYPE_USER = 1
MSG_TYPE_BOT = 2
ITEM_TEXT = 1
ITEM_IMAGE = 2
ITEM_VOICE = 3
ITEM_FILE = 4
ITEM_VIDEO = 5
STATE_FINISH = 2
TYPING_ON, TYPING_OFF = 1, 2
ERR_SESSION_EXPIRED = -14

CREDS_FILE = "creds.json"         # 登录凭证（bot_token 等），请勿外传/提交
CURSOR_FILE = "cursor.txt"        # 长轮询游标，重启续传不丢消息
CONFIG_FILE = "config.json"

MEDIA_LABEL = {ITEM_IMAGE: "[图片]", ITEM_VIDEO: "[视频]",
               ITEM_FILE: "[文件]", ITEM_VOICE: "[语音]"}

running = True


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}]", *a, flush=True)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def encode_client_version(v: str) -> int:
    """'1.0.0' -> 0x00010000 风格的 uint32（官方算法）"""
    p = [int(x) for x in v.split(".")]
    while len(p) < 3:
        p.append(0)
    return ((p[0] & 0xFF) << 16) | ((p[1] & 0xFF) << 8) | (p[2] & 0xFF)


def random_wechat_uin() -> str:
    """X-WECHAT-UIN：随机 uint32 -> 十进制字符串 -> base64（官方算法）"""
    n = struct.unpack(">I", random.randbytes(4))[0]
    return base64.b64encode(str(n).encode()).decode()


def load_config() -> dict:
    if not os.path.exists(CONFIG_FILE):
        if os.path.exists("config.example.json"):
            with open("config.example.json", encoding="utf-8") as f:
                cfg = json.load(f)
            with open(CONFIG_FILE, "w", encoding="utf-8") as f:
                json.dump(cfg, f, ensure_ascii=False, indent=2)
            log(f"已从示例生成 {CONFIG_FILE}，可按需修改后重启")
        else:
            cfg = {}
    else:
        with open(CONFIG_FILE, encoding="utf-8") as f:
            cfg = json.load(f)
    cfg.setdefault("reply_template", "收到：{text}")
    cfg.setdefault("allowed_users", [])   # 空 = 允许任何人；填入 user id 则只回复名单内
    cfg.setdefault("media_reply", "收到{label}，我目前只能回复文字消息。")
    return cfg


# ---------------------------------------------------------------------------
# iLink 客户端
# ---------------------------------------------------------------------------
class ILinkClient:
    def __init__(self, base_url=BASE_URL):
        self.base_url = base_url.rstrip("/")
        self.token = None
        self.session = requests.Session()

    # -- 底层请求 -----------------------------------------------------------
    def _headers(self):
        h = {
            "Content-Type": "application/json",
            "AuthorizationType": "ilink_bot_token",
            "X-WECHAT-UIN": random_wechat_uin(),
            "iLink-App-Id": ILINK_APP_ID,
            "iLink-App-ClientVersion": str(encode_client_version(CLIENT_VERSION_STR)),
        }
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _get(self, endpoint, timeout=20):
        r = self.session.get(self.base_url + "/" + endpoint,
                             headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json()

    def _post(self, endpoint, body, timeout=15):
        body = dict(body)
        body["base_info"] = {"channel_version": BRIDGE_VERSION,
                             "bot_agent": f"wechat-ilink-bridge/{BRIDGE_VERSION}"}
        r = self.session.post(self.base_url + "/" + endpoint, json=body,
                              headers=self._headers(), timeout=timeout)
        r.raise_for_status()
        return r.json()

    # -- 扫码登录 ------------------------------------------------------------
    def login(self):
        """扫码登录，返回 (bot_token, bot_id, user_id)。需用户在手机微信上扫码确认。"""
        log("正在获取登录二维码…")
        qr = self._get(f"ilink/bot/get_bot_qrcode?bot_type={BOT_TYPE}")
        qrcode, qr_url = qr.get("qrcode"), qr.get("qrcode_img_content")
        if not qrcode or not qr_url:
            raise RuntimeError(f"获取二维码失败：{qr}")

        print("\n" + "=" * 60)
        print("请用微信 App 扫描下方二维码完成授权（机器人身份登录）：")
        print(qr_url)
        try:
            import qrcode as qr_lib  # 可选：终端直接显示二维码
            qr_lib.print_ascii(qr_url)
        except ImportError:
            print("（在手机浏览器打开上面的链接也可看到二维码，再用微信扫）")
        print("=" * 60 + "\n")

        log("等待扫码确认…（二维码会过期，过期后会自动刷新）")
        while True:
            st = self._get(f"ilink/bot/get_qrcode_status?qrcode={qrcode}", timeout=35)
            status = st.get("status")
            if status == "confirmed":
                self.token = st["bot_token"]
                log(f"登录成功，机器人 id：{st.get('ilink_bot_id')}")
                if st.get("redirect_host"):
                    self.base_url = "https://" + st["redirect_host"]
                    log(f"网关已重定向到：{self.base_url}")
                return {"bot_token": st["bot_token"],
                        "ilink_bot_id": st.get("ilink_bot_id"),
                        "ilink_user_id": st.get("ilink_user_id"),
                        "base_url": self.base_url}
            if status == "expired":
                log("二维码已过期，重新获取…")
                return self.login()
            if status == "scanned":
                log("已扫码，请在手机上点确认…")
            time.sleep(2)

    # -- 收消息（长轮询） ------------------------------------------------------
    def get_updates(self, cursor: str):
        return self._post("ilink/bot/getupdates",
                          {"get_updates_buf": cursor or ""}, timeout=40)

    # -- 发文本消息 ------------------------------------------------------------
    def send_text(self, to_user_id: str, text: str, context_token: str):
        body = {
            "msg": {
                "from_user_id": "",
                "to_user_id": to_user_id,
                "client_id": f"bridge-{uuid.uuid4().hex[:12]}",
                "message_type": MSG_TYPE_BOT,
                "message_state": STATE_FINISH,
                "item_list": [{"type": ITEM_TEXT, "text_item": {"text": text}}],
                "context_token": context_token,
            }
        }
        resp = self._post("ilink/bot/sendmessage", body)
        if resp.get("ret"):
            raise RuntimeError(f"发送失败 ret={resp.get('ret')} errmsg={resp.get('errmsg')}")
        return resp

    # -- 输入状态（"对方正在输入…"，尽力而为） -----------------------------------
    def send_typing(self, to_user_id: str, context_token: str, on: bool = True):
        try:
            cfg = self._post("ilink/bot/getconfig",
                             {"ilink_user_id": to_user_id,
                              "context_token": context_token}, timeout=10)
            ticket = cfg.get("typing_ticket")
            if not ticket:
                return
            self._post("ilink/bot/sendtyping",
                       {"ilink_user_id": to_user_id,
                        "typing_ticket": ticket,
                        "status": TYPING_ON if on else TYPING_OFF}, timeout=10)
        except Exception:
            pass  # 输入状态失败不影响主流程


# ---------------------------------------------------------------------------
# 消息解析
# ---------------------------------------------------------------------------
def extract_text(msg: dict):
    """从一条 inbound 消息提取文本；返回 (text, media_label)。"""
    for item in msg.get("item_list") or []:
        t = item.get("type")
        if t == ITEM_TEXT and (item.get("text_item") or {}).get("text") is not None:
            return str(item["text_item"]["text"]), None
        if t == ITEM_VOICE and (item.get("voice_item") or {}).get("text"):
            return str(item["voice_item"]["text"]), None  # 语音转写文字
        if t in MEDIA_LABEL:
            return "", MEDIA_LABEL[t]
    return "", None


def load_cursor() -> str:
    try:
        with open(CURSOR_FILE, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def save_cursor(cursor: str):
    with open(CURSOR_FILE, "w", encoding="utf-8") as f:
        f.write(cursor or "")


def load_creds():
    try:
        with open(CREDS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except OSError:
        return None


def save_creds(creds: dict):
    with open(CREDS_FILE, "w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False, indent=2)
    try:
        os.chmod(CREDS_FILE, 0o600)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------------------
def ensure_login(client: ILinkClient):
    creds = load_creds()
    if creds and creds.get("bot_token"):
        client.token = creds["bot_token"]
        client.base_url = creds.get("base_url", BASE_URL)
        log(f"已载入保存的登录凭证（机器人 {creds.get('ilink_bot_id')}），跳过扫码")
        return
    creds = client.login()
    save_creds(creds)


def handle_message(client: ILinkClient, cfg: dict, msg: dict):
    from_id = msg.get("from_user_id", "")
    context_token = msg.get("context_token", "")
    if not from_id or not context_token:
        return

    allowed = cfg.get("allowed_users") or []
    if allowed and from_id not in allowed:
        log(f"忽略非名单用户 {from_id}")
        return

    text, media_label = extract_text(msg)
    if media_label and not text:
        reply = cfg["media_reply"].format(label=media_label)
    elif text.strip() == "/ping":
        reply = "pong"
    elif text.strip() == "/help":
        reply = ("我是你的微信机器人（iLink 网关）。\n"
                 "直接发文字跟我聊天；/ping 测试连通。\n"
                 "暂只支持私聊文字，群聊不可用。")
    else:
        reply = cfg["reply_template"].format(text=text)

    # 微信只支持纯文本：简单过滤 markdown 符号
    for ch in ("*", "`", "#"):
        reply = reply.replace(ch, "")
    reply = reply[:1500]

    log(f"收到来自 {from_id}：{text or media_label}")
    client.send_typing(from_id, context_token, True)
    try:
        client.send_text(from_id, reply, context_token)
        log(f"已回复 {from_id}")
    finally:
        client.send_typing(from_id, context_token, False)


def main():
    global running

    def stop(*_):
        global running
        running = False
        log("正在退出…")

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    cfg = load_config()
    if not cfg.get("allowed_users"):
        log("提醒：allowed_users 为空，任何人给机器人发消息都会收到回复；"
            "建议在 config.json 中填入允许的用户 id")

    client = ILinkClient()
    ensure_login(client)
    cursor = load_cursor()
    seen_ids = set()
    log("桥接运行中，等待微信私聊消息…（Ctrl+C 退出）")

    backoff = 1
    while running:
        try:
            resp = client.get_updates(cursor)
        except requests.RequestException as e:
            log(f"网络异常：{e}，{backoff}s 后重试")
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
            continue
        backoff = 1

        ret = resp.get("ret", 0)
        if ret == ERR_SESSION_EXPIRED:
            log("登录会话已过期，重新扫码登录…")
            try:
                os.remove(CREDS_FILE)
            except OSError:
                pass
            ensure_login(client)
            continue
        if ret:
            log(f"网关返回错误 ret={ret} errmsg={resp.get('errmsg')}，5s 后重试")
            time.sleep(5)
            continue

        cursor = resp.get("get_updates_buf", cursor)
        save_cursor(cursor)

        for msg in resp.get("msgs") or []:
            if msg.get("message_type") != MSG_TYPE_USER:
                continue  # 只处理用户发来的消息
            mid = msg.get("message_id") or json.dumps(msg.get("item_list"),
                                                      sort_keys=True)[:64]
            if mid in seen_ids:
                continue
            seen_ids.add(mid)
            if len(seen_ids) > 5000:
                seen_ids.clear()
            try:
                handle_message(client, cfg, msg)
            except Exception as e:
                log(f"处理消息失败：{e}")

    log("已退出")


if __name__ == "__main__":
    main()
