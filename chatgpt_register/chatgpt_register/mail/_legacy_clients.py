#!/usr/bin/env python3
"""Mail clients for ChatGPT login OTP — MoEmail (temp mail API) + MailCom (mail.com via self-hosted frontend)."""

import json
import random
import re
import string
import time

import requests as _req_lib
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


def _is_local_host_url(url: str) -> bool:
    """True if `url` points to a local address (loopback / link-local / RFC1918)."""
    try:
        from urllib.parse import urlsplit
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in ("localhost", "ip6-localhost", "ip6-loopback"):
        return True
    if "." not in host:
        return True
    if host.endswith(".local") or host.endswith(".internal"):
        return True
    if host.startswith("127.") or host == "::1":
        return True
    if host.startswith("10.") or host.startswith("192.168."):
        return True
    if host.startswith("172."):
        try:
            second = int(host.split(".")[1])
            if 16 <= second <= 31:
                return True
        except Exception:
            pass
    if host.startswith("169.254."):
        return True
    return False


# ═══════════════════════════════════════════════════════════════════════
# MoEmail — API-based temporary email
# ═══════════════════════════════════════════════════════════════════════

class MoEmail:
    """Temporary email inbox via a MoEmail-compatible API.

    The API supports:
      POST /api/emails/generate  → create a random mailbox
      GET  /api/emails/{id}      → list messages
      GET  /api/emails/{id}/{mid} → get message detail
    """

    def __init__(self, api_base, api_key, domains, expiry=3600000):
        self.base = api_base.rstrip("/")
        self.key = api_key
        self.domains = domains
        self._di = 0
        self.expiry = expiry
        self._last_eid = ""
        self._last_addr = ""

    def _api(self, method, path, **kw):
        kw.setdefault("timeout", 30)
        kw.setdefault("verify", False)
        h = kw.pop("headers", {})
        h.update({"X-API-Key": self.key, "Content-Type": "application/json"})
        full_url = f"{self.base}{path}"
        if _is_local_host_url(full_url) and "proxies" not in kw:
            kw["proxies"] = {"http": None, "https": None}
        r = _req_lib.request(method, full_url, headers=h, **kw)
        if r.status_code not in (200, 201):
            raise RuntimeError(
                f"MoEmail {method} {path}: {r.status_code} {r.text[:200]}")
        return r.json()

    def create_mailbox(self, prefix=None, max_retries=6):
        """Create a new mailbox. `prefix` optionally sets the local-part."""
        base_prefix = prefix or (
            "".join(random.choices(string.ascii_lowercase, k=6))
            + "".join(random.choices(string.digits, k=random.randint(1, 3)))
        )
        alnum = string.ascii_lowercase + string.digits
        last_err = None
        for attempt in range(max_retries):
            if attempt == 0:
                name = base_prefix
            else:
                if attempt < 4:
                    suffix_len = 2 + (attempt - 1)
                    name = (base_prefix + "".join(
                        random.choices(alnum, k=suffix_len)))[:22]
                else:
                    name = ("".join(random.choices(string.ascii_lowercase, k=6))
                            + "".join(random.choices(string.digits,
                                                     k=random.randint(2, 4))))
            dom = self.domains[self._di % len(self.domains)]
            self._di += 1
            try:
                d = self._api("POST", "/api/emails/generate",
                              json={"name": name, "expiryTime": self.expiry,
                                    "domain": dom})
            except RuntimeError as e:
                msg = str(e)
                if ("409" in msg and
                    ("已被使用" in msg or "already" in msg.lower()
                     or "in use" in msg.lower())):
                    last_err = e
                    continue
                raise
            addr = d.get("email", "")
            eid = str(d.get("id") or "")
            if not addr or not eid:
                raise RuntimeError(f"MoEmail create: {d}")
            self._last_eid = eid
            self._last_addr = addr
            return {"address": addr, "email_id": eid}
        raise last_err or RuntimeError("MoEmail create: exceeded max retries")

    def prime_seen(self, seen):
        """Pre-fill `seen` with all current mailbox message IDs for dedup."""
        eid = getattr(self, "_last_eid", "") or ""
        if not eid:
            return 0
        try:
            d = self._api("GET", f"/api/emails/{eid}")
        except Exception:
            return 0
        n = 0
        for msg in (d.get("messages") or []):
            mid = str(msg.get("id") or "")
            if mid and mid not in seen:
                seen.add(mid)
                n += 1
        return n

    def wait_for_code(self, email_id=None, timeout=60, interval=3,
                      seen=None, log=None):
        """Poll mailbox for a 6-digit OTP code. Returns code string or None."""
        if email_id is None:
            email_id = getattr(self, "_last_eid", "") or ""
        if not email_id:
            raise RuntimeError("MoEmail.wait_for_code: no email_id")
        if seen is None:
            seen = set()
        deadline = time.time() + timeout
        t0 = time.time()
        polls = 0
        while time.time() < deadline:
            polls += 1
            try:
                d = self._api("GET", f"/api/emails/{email_id}")
                msgs = d.get("messages") or []
                if log and polls % 4 == 1:
                    log(f"  [mail] poll #{polls} ({int(time.time()-t0)}s) "
                        f"inbox={len(msgs)}")
                for msg in msgs:
                    mid = str(msg.get("id") or "")
                    if mid in seen:
                        continue
                    code = self._extract(msg, email_id)
                    if code:
                        seen.add(mid)
                        if log:
                            log(f"  [mail] OTP hit (poll #{polls}, "
                                f"{int(time.time()-t0)}s)")
                        return code
            except Exception as e:
                if log:
                    log(f"  [mail] poll error: {type(e).__name__}: "
                        f"{str(e)[:80]}")
            time.sleep(interval)
        if log:
            log(f"  [mail] OTP timeout ({timeout}s, polled {polls})")
        return None

    def _extract(self, msg, eid):
        mid = str(msg.get("id") or "")
        if mid:
            try:
                detail = self._api("GET", f"/api/emails/{eid}/{mid}")
                msg = (detail.get("message", detail)
                       if isinstance(detail.get("message"), dict)
                       else detail)
            except Exception:
                pass
        txt = "\n".join(
            str(msg.get(k, ""))
            for k in ("subject", "text", "text_content", "html",
                      "html_content", "body", "content")
        )
        if not txt.strip():
            return None
        m = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})",
                      txt, re.I)
        if m:
            return m.group(1)
        m = re.search(r"(?:verification code|code is)[:\s]*(\d{6})",
                      txt, re.I)
        if m and m.group(1) != "177010":
            return m.group(1)
        for c in re.findall(r">\s*(\d{6})\s*<|(?<![#&])\b(\d{6})\b", txt):
            v = c[0] or c[1]
            if v and v != "177010":
                return v
        return None


# ═══════════════════════════════════════════════════════════════════════
# MailComClient — mail.com 自部署前端 HTTP API
# ═══════════════════════════════════════════════════════════════════════

class MailComMailboxError(RuntimeError):
    """mail.com 前端拒绝登录该邮箱 (密码错/提供商不支持/被风控等)."""


class MailComClient:
    """mail.com 本地取件客户端。

    跟你发过来的 frontend/server.mjs 对讲:
      POST /api/fetch    — 登录 + 读收件箱列表
      POST /api/message  — 读单封邮件详情 (bodyText / links / attachments)

    鉴权: x-app-token header (对应 frontend 的 APP_TOKEN 环境变量)

    调用方式跟 JuMail 一样:
      mail = MailComClient(api_base="http://127.0.0.1:8787", app_token="<APP_TOKEN>",
                           email="xxx@mail.com", password="mail-password")
      code = mail.wait_for_code(seen=seen, timeout=60, log=print)
    """

    _MAILBOX_FATAL_PATTERNS = (
        "无法找到", "登录表单", "登录失败", "密码错误", "凭证错误",
        "captcha", "needs_verification", "unsupported", "not supported",
        "invalid credential", "invalid_credential", "auth failed",
        "authentication failed", "邮箱格式不正确",
    )

    def __init__(self, api_base, app_token, email, password, proxy="", control_callback=None):
        self.base = api_base.rstrip("/") if api_base else "http://127.0.0.1:8787"
        self.token = app_token or ""
        self.email = email
        self.password = password
        self.credential = f"{email}:{password}"
        self.session_id = None
        self.proxy = proxy or ""
        self.control_callback = control_callback

    def _should_stop(self):
        return bool(self.control_callback and self.control_callback())

    def _h(self):
        h = {"content-type": "application/json", "accept": "application/json"}
        if self.token:
            h["x-app-token"] = self.token
        return h

    def _post(self, path, body, timeout=60):
        payload = dict(body or {})
        if self.proxy:
            payload["proxy"] = self.proxy
        kw = {"json": payload, "headers": self._h(),
              "timeout": timeout, "verify": False}
        full_url = f"{self.base}{path}"
        if _is_local_host_url(full_url):
            kw["proxies"] = {"http": None, "https": None}
        elif self.proxy:
            kw["proxies"] = {"http": self.proxy, "https": self.proxy}
        r = _req_lib.request("POST", full_url, **kw)
        if r.status_code != 200:
            body_lc = (r.text or "").lower()
            if any(p in r.text for p in self._MAILBOX_FATAL_PATTERNS) or \
               any(p in body_lc for p in self._MAILBOX_FATAL_PATTERNS):
                raise MailComMailboxError(
                    f"mail.com 前端拒绝邮箱 ({r.status_code}): "
                    f"{r.text[:200]}")
            raise RuntimeError(
                f"MailCom POST {path}: {r.status_code} {r.text[:200]}")
        try:
            return r.json()
        except Exception:
            return {}

    def fetch(self, limit=20):
        """登录 mail.com 并读收件箱列表。
        返回: (messages, session_id)
        messages 每项: {mailId, sender, fromAddress, subjectPreview, dateOrTime, unread, ...}
        """
        d = self._post("/api/fetch",
                       {"credential": self.credential, "limit": limit})
        msgs = d.get("messages") or []
        sid = d.get("sessionId") or ""
        if sid:
            self.session_id = sid
        return msgs, sid

    def fetch_message(self, mail_id, session_id=None):
        """读单封邮件详情 (bodyText / links / attachments).
        返回 dict: {mailId, sender, fromAddress, subjectPreview, bodyText, links, attachments, ...}
        """
        sid = session_id or self.session_id
        if not sid:
            raise RuntimeError("MailComClient.fetch_message: 没有 sessionId, 先调 fetch")
        return self._post("/api/message",
                          {"sessionId": sid, "mailId": mail_id})

    @staticmethod
    def _is_openai(msg):
        """这封邮件是不是 OpenAI/ChatGPT 寄出的."""
        haystack = " ".join(str(msg.get(k, "")) for k in
                            ("sender", "fromAddress", "subjectPreview",
                             "subject", "bodyText")).lower()
        if not haystack.strip():
            return None
        return any(k in haystack for k in
                   ("openai", "chatgpt", "noreply@tm.openai.com",
                    "noreply@openai.com"))

    @staticmethod
    def _extract_otp(text):
        """从文本里抽 6 位 OTP."""
        if not text:
            return None
        for pat in (
            r"(?:verification code|code is|your code is|您的验证码|您的安全代码|code:)[\s:：]*?(\d{6})",
            r"\b(\d{6})\b(?=[\s\S]{0,200}(?:OpenAI|ChatGPT|verification|sign|log)\b)",
            r"(?<![\d])(\d{6})(?![\d])",
        ):
            m = re.search(pat, text, re.I)
            if m and m.group(1) != "177010":
                return m.group(1)
        return None

    def prime_seen(self, seen, fresh_window_min=25):
        """把历史邮件灌进 seen；过去 fresh_window_min 分钟内的邮件保留，供 wait_for_code 立即使用。
        原理：新OTP从OpenAI到mail.com延迟5-7分钟，与其等新的，不如直接使用收件箱里最近到达的有效码。"""
        import datetime as _dt
        try:
            msgs, _sid = self.fetch(limit=30)
        except MailComMailboxError:
            raise
        except Exception:
            return 0
        now = time.time()
        cutoff = now - fresh_window_min * 60
        n = 0
        for m in msgs:
            mid = str(m.get("mailId") or "")
            if not mid or mid in seen:
                continue
            dt_str = m.get("dateOrTime") or ""
            if dt_str:
                try:
                    msg_ts = _dt.datetime.fromisoformat(
                        dt_str.replace("Z", "+00:00")).timestamp()
                    if msg_ts > cutoff:
                        continue  # recent — leave unprimed for wait_for_code
                except Exception:
                    pass
            seen.add(mid)
            n += 1
        return n

    def create_mailbox(self, prefix=None):
        """兼容 MoEmail 接口: 邮箱是固定的, 直接返回自己."""
        return {"address": self.email, "email_id": self.email}

    def wait_for_code(self, email_id=None, timeout=60, interval=4,
                      seen=None, log=None):
        """轮询 mail.com 收件箱等 OpenAI 发来的 6 位验证码。

        email_id 参数为了签名兼容保留 (不用传).
        返回: 6 位 OTP 字符串, 超时返回 None.
        """
        _ = email_id
        # Respect the caller's timeout so account-login can fail fast and retry
        # the OTP send path instead of hanging for several minutes.
        timeout = max(int(timeout or 60), 10)
        interval = min(interval, 8)
        if seen is None:
            seen = set()
        deadline = time.time() + timeout
        t0 = time.time()
        polls = 0
        connect_errors = 0
        while time.time() < deadline:
            if self._should_stop():
                raise RuntimeError("登录提取任务已终止")
            polls += 1
            try:
                msgs, sid = self.fetch(limit=20)
                connect_errors = 0
                if log and polls % 3 == 1:
                    new_cnt = sum(
                        1 for m in msgs
                        if str(m.get("mailId") or "") not in seen)
                    log(f"  [mailcom] poll #{polls} ({int(time.time()-t0)}s) "
                        f"inbox={len(msgs)} new={new_cnt}")
                for m in msgs:
                    mid = str(m.get("mailId") or "")
                    if not mid or mid in seen:
                        continue
                    # 先看预览里有没有 OTP
                    is_oai = self._is_openai(m)
                    if is_oai is False:
                        seen.add(mid)
                        continue
                    snippet = " ".join(str(m.get(k, "")) for k in
                                       ("subjectPreview", "sender",
                                        "fromAddress", "bodyText"))
                    code = self._extract_otp(snippet)
                    if not code:
                        # 读邮件正文
                        try:
                            full = self.fetch_message(mid, session_id=sid)
                            if is_oai is None and isinstance(full, dict):
                                if self._is_openai(full) is False:
                                    seen.add(mid)
                                    continue
                            blob = " ".join(str(full.get(k, "")) for k in
                                            ("subjectPreview", "bodyText",
                                             "sender", "fromAddress"))
                            if not blob.strip() and isinstance(full, dict):
                                blob = json.dumps(full)
                            code = self._extract_otp(blob)
                        except Exception as e:
                            if log:
                                log(f"  [mailcom] 读详情失败: "
                                    f"{type(e).__name__} {str(e)[:80]}")
                    seen.add(mid)
                    if code:
                        if log:
                            log(f"  [mailcom] OTP 命中 mid={mid[:10]} "
                                f"(poll #{polls}, {int(time.time()-t0)}s)")
                        return code
            except MailComMailboxError:
                raise
            except Exception as e:
                error_text = str(e)
                if (
                    "Connection refused" in error_text
                    or "NewConnectionError" in error_text
                    or "Max retries exceeded" in error_text
                    or "Failed to establish a new connection" in error_text
                ):
                    connect_errors += 1
                    if connect_errors >= 3:
                        raise RuntimeError(
                            "mail.com 接码服务不可达，请确认 helper 已启动并且 "
                            f"api_base={self.base} 可从当前容器访问"
                        ) from e
                else:
                    connect_errors = 0
                if log:
                    log(f"  [mailcom] poll error: {type(e).__name__}: "
                        f"{str(e)[:120]}")
            sleep_until = time.time() + interval
            while time.time() < sleep_until:
                if self._should_stop():
                    raise RuntimeError("登录提取任务已终止")
                time.sleep(min(0.5, max(0, sleep_until - time.time())))
        if log:
            log(f"  [mailcom] OTP 超时 ({timeout}s, polled {polls})")
        return None
