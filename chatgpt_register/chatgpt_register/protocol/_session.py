#!/usr/bin/env python3
"""ChatGPT Login & Session Token — mit curl_cffi NextAuth login und /api/auth/session accessToken.

Nur der Login-Flow:
  1. GET /auth/login (warm cookies)
  2. GET /api/auth/csrf → csrfToken
  3. POST /api/auth/signin/openai → auth_url
  4. GET auth_url chain → OTP oder Password
  5. Follow continue_url redirect chain
  6. GET /api/auth/session → accessToken

Usage (CLI):
  python3 -m core.session --email user@example.com --password xxx
  python3 -m core.session --email user@example.com --otp --config config.json
"""

import base64
import hashlib
import json
import os
import random
import re
import secrets
import string
import time
import uuid
from urllib.parse import parse_qs, quote, urlencode, urljoin, urlparse, urlsplit

import urllib3
from curl_cffi import requests as _curl

TLS_VERIFY = os.environ.get("CHATGPT_LOGIN_TLS_VERIFY", "1").strip() != "0"
if not TLS_VERIFY:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ── Constants ──
AUTH = "https://auth.openai.com"
CHATGPT = "https://chatgpt.com"
SENTINEL_API = "https://sentinel.openai.com"

# ── curl_cffi supported impersonate versions ──
_SUPPORTED_CHROME = (124, 131, 136, 142, 145, 146)
_SUPPORTED_FIREFOX = (133, 135, 144, 147)


# ── Country profiles (timezone / locale) ──
COUNTRY_PROFILES = {
    "US": {"tz_offset": -300, "tz_label": "Eastern Standard Time",
           "tz_short": "America/New_York", "lang": "en-US", "accept_lang": "en-US,en;q=0.9"},
    "GB": {"tz_offset": 60, "tz_label": "Greenwich Mean Time",
           "tz_short": "Europe/London", "lang": "en-GB", "accept_lang": "en-GB,en;q=0.9"},
    "DE": {"tz_offset": 120, "tz_label": "Central European Summer Time",
           "tz_short": "Europe/Berlin", "lang": "de-DE", "accept_lang": "de-DE,de;q=0.9,en;q=0.8"},
    "JP": {"tz_offset": 540, "tz_label": "Japan Standard Time",
           "tz_short": "Asia/Tokyo", "lang": "ja-JP", "accept_lang": "ja-JP,ja;q=0.9,en;q=0.8"},
    "BR": {"tz_offset": -180, "tz_label": "Brasilia Standard Time",
           "tz_short": "America/Sao_Paulo", "lang": "pt-BR", "accept_lang": "pt-BR,pt;q=0.9,en;q=0.8"},
    "ID": {"tz_offset": 420, "tz_label": "Western Indonesia Time",
           "tz_short": "Asia/Jakarta", "lang": "id-ID", "accept_lang": "id-ID,id;q=0.9,en;q=0.8"},
    "IN": {"tz_offset": 330, "tz_label": "India Standard Time",
           "tz_short": "Asia/Kolkata", "lang": "hi-IN", "accept_lang": "hi-IN,hi;q=0.9,en;q=0.8"},
    "FR": {"tz_offset": 120, "tz_label": "Central European Summer Time",
           "tz_short": "Europe/Paris", "lang": "fr-FR", "accept_lang": "fr-FR,fr;q=0.9,en;q=0.8"},
    "KR": {"tz_offset": 540, "tz_label": "Korean Standard Time",
           "tz_short": "Asia/Seoul", "lang": "ko-KR", "accept_lang": "ko-KR,ko;q=0.9,en;q=0.8"},
    "AU": {"tz_offset": 600, "tz_label": "Australian Eastern Standard Time",
           "tz_short": "Australia/Sydney", "lang": "en-AU", "accept_lang": "en-AU,en;q=0.9"},
}


# ── Screen resolution pools ──
SCREEN_POOLS = {
    "Windows": [(1920, 1080), (2560, 1440), (1366, 768), (1536, 864), (3840, 2160)],
    "macOS": [(1680, 1050), (2560, 1600), (1440, 900), (3024, 1964), (3456, 2234)],
    "Linux": [(1920, 1080), (2560, 1440), (1366, 768)],
}


# ═══════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════

def _chrome_imp(v):
    return f"chrome{v}"


def _firefox_imp(v):
    return f"firefox{v}"


def _build_fp_pool():
    pool = []
    win_versions = ("15.0.0", "19.0.0")
    mac_versions = ("14.6.1", "14.7.1", "15.0.0", "15.2.0")
    for v in _SUPPORTED_CHROME:
        grease_label = random.choice([
            '"Not(A:Brand";v="99"', '"Not_A Brand";v="99"',
            '"Not.A/Brand";v="99"', '"Not?A_Brand";v="8"',
            '"Not-A.Brand";v="24"',
        ])
        ch_parts = [grease_label, f'"Chromium";v="{v}"', f'"Google Chrome";v="{v}"']
        random.shuffle(ch_parts)
        ch = ", ".join(ch_parts)
        full_ver = random.choice(["0.0.0", "0.0.7339.207", "0.0.7204.157", "0.0.7103.114"])
        ua_w = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{v}.{full_ver} Safari/537.36")
        ua_m = (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                f"AppleWebKit/537.36 (KHTML, like Gecko) "
                f"Chrome/{v}.{full_ver} Safari/537.36")
        imp = _chrome_imp(v)
        pool.append({"ua": ua_w, "ch_ua": ch, "platform": '"Windows"',
                     "platform_version": f'"{random.choice(win_versions)}"',
                     "arch": '"x86_64"', "bitness": '"64"', "model": '""',
                     "mobile": "?0", "imp": imp, "browser": "chrome",
                     "os": "Windows", "chrome": v, "lang": "en-US,en;q=0.9"})
        pool.append({"ua": ua_m, "ch_ua": ch, "platform": '"macOS"',
                     "platform_version": f'"{random.choice(mac_versions)}"',
                     "arch": '"arm"', "bitness": '"64"', "model": '""',
                     "mobile": "?0", "imp": imp, "browser": "chrome",
                     "os": "macOS", "chrome": v, "lang": "en-US,en;q=0.9"})
    for v in _SUPPORTED_FIREFOX:
        imp = _firefox_imp(v)
        ua_w = (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:{v}.0) "
                f"Gecko/20100101 Firefox/{v}.0")
        ua_m = (f"Mozilla/5.0 (Macintosh; Intel Mac OS X 10.15; rv:{v}.0) "
                f"Gecko/20100101 Firefox/{v}.0")
        pool.append({"ua": ua_w, "ch_ua": "", "platform": '"Windows"',
                     "platform_version": f'"{random.choice(win_versions)}"',
                     "arch": '"x86_64"', "bitness": '"64"', "model": '""',
                     "mobile": "?0", "imp": imp, "browser": "firefox",
                     "os": "Windows", "firefox": v, "lang": "en-US,en;q=0.9"})
        pool.append({"ua": ua_m, "ch_ua": "", "platform": '"macOS"',
                     "platform_version": f'"{random.choice(mac_versions)}"',
                     "arch": '"arm"', "bitness": '"64"', "model": '""',
                     "mobile": "?0", "imp": imp, "browser": "firefox",
                     "os": "macOS", "firefox": v, "lang": "en-US,en;q=0.9"})
    return pool


_FP_POOL = _build_fp_pool()


def _country_profile(country_code):
    cc = (country_code or "US").upper()
    return dict(COUNTRY_PROFILES.get(cc, COUNTRY_PROFILES["US"]))


def _pick_fp(country_code="US", impersonate=""):
    candidates = [fp for fp in _FP_POOL if fp.get("browser") == "chrome"] or _FP_POOL
    if impersonate:
        matched = [fp for fp in _FP_POOL if fp.get("imp") == impersonate]
        if matched:
            candidates = matched
    fp = dict(random.choice(candidates))
    prof = _country_profile(country_code)
    fp["lang"] = prof["accept_lang"]
    fp["country"] = (country_code or "US").upper()
    fp["nav_lang"] = prof["lang"]
    fp["tz_short"] = prof.get("tz_short", "America/New_York")
    fp["tz_offset"] = prof.get("tz_offset", -300)
    return fp


def _locale_cookie_value(locale_tag):
    raw = json.dumps(
        {"computedPreferredLocale": locale_tag or "en-US", "v": 1},
        separators=(",", ":"),
    )
    return quote(raw, safe="")


def _json_h_for(fp):
    return {
        "user-agent": fp["ua"],
        "accept": "application/json",
        "accept-language": fp["lang"],
        "content-type": "application/json",
        "origin": AUTH,
    }


def _nav_h_for(fp):
    return {
        "user-agent": fp["ua"],
        "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "accept-language": fp["lang"],
    }


def _pkce():
    v = base64.urlsafe_b64encode(secrets.token_bytes(64)).rstrip(b"=").decode()
    c = base64.urlsafe_b64encode(hashlib.sha256(v.encode()).digest()).rstrip(b"=").decode()
    return v, c


def _rj(r):
    try:
        d = r.json()
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _trace_h():
    return {
        "traceparent": f"00-{uuid.uuid4().hex}-{format(random.getrandbits(64), '016x')}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": str(random.getrandbits(64)),
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": str(random.getrandbits(64)),
    }


def _resolve_proxy(proxy_template):
    """替换代理模板里的 {SESSION} 占位符为随机 session id，实现每次调用换 IP。
    同时自动将 http:// 转为 socks5h://（kookeey 等住宅代理的 HTTP 端口实际走 SOCKS5）。"""
    if not proxy_template:
        return proxy_template
    if "{SESSION}" in proxy_template:
        sess = "".join(random.choices(string.ascii_lowercase + string.digits, k=10))
        proxy_template = proxy_template.replace("{SESSION}", sess)
    # kookeey / iproyal 等住宅代理的标称 "HTTP" 端口实际跑 SOCKS5；
    # 走 HTTP CONNECT 会 400。兼容 http:// → socks5h://。
    if proxy_template.startswith("http://"):
        proxy_template = "socks5h://" + proxy_template[len("http://"):]
    return proxy_template


def _session(proxy="", imp="chrome"):
    kw = {"impersonate": imp, "verify": TLS_VERIFY}
    if proxy:
        kw["proxy"] = proxy
    return _curl.Session(**kw)


def _promote_oai_sc_cookie(session, resp):
    if session is None or resp is None:
        return
    sc = ""
    try:
        sc = resp.cookies.get("oai-sc") or ""
    except Exception:
        sc = ""
    if not sc:
        try:
            raw = ""
            if hasattr(resp.headers, "get"):
                raw = (resp.headers.get("set-cookie")
                       or resp.headers.get("Set-Cookie") or "")
            m = re.search(r"(?:^|,\s*)oai-sc=([^;,\s]+)", raw)
            if m:
                sc = m.group(1)
        except Exception:
            sc = ""
    if not sc:
        return
    _clear_cookie_names(session, {"oai-sc"})
    try:
        session.cookies.set("oai-sc", sc, domain=".openai.com", path="/")
    except Exception:
        pass


def _clear_cookie_names(session, names, domain_suffixes=None):
    if session is None:
        return
    suffixes = tuple(domain_suffixes or ())
    try:
        jar = getattr(session.cookies, "jar", None)
        cookies = list(jar) if jar is not None else []
    except Exception:
        cookies = []
    for c in cookies:
        try:
            name = getattr(c, "name", "")
            domain = (getattr(c, "domain", "") or "").lstrip(".")
            if name not in names:
                continue
            if suffixes and not any(domain.endswith(s.lstrip(".")) for s in suffixes):
                continue
            session.cookies.clear(
                domain=getattr(c, "domain", None),
                path=getattr(c, "path", None),
                name=name,
            )
        except Exception:
            pass


def _do(session, method, url, retries=3, **kw):
    kw.setdefault("timeout", 30)
    last = None
    for i in range(retries):
        try:
            resp = session.request(method, url, **kw)
            _promote_oai_sc_cookie(session, resp)
            return resp
        except Exception as e:
            last = e
            if i < retries - 1:
                time.sleep(1)
    raise last


def _callback_code(url):
    if not url:
        return None
    try:
        code = parse_qs(urlparse(url).query).get("code", [""])[0]
        return code or None
    except Exception:
        return None


def _is_local_host_url(url):
    try:
        host = (urlsplit(url).hostname or "").lower()
    except Exception:
        return False
    if not host:
        return False
    if host in ("localhost", "ip6-localhost", "ip6-loopback"):
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


def _tz_date_string(country_code):
    prof = _country_profile(country_code)
    offset_min = int(prof.get("tz_offset", 0))
    sign = "+" if offset_min >= 0 else "-"
    off_h = abs(offset_min) // 60
    off_m = abs(offset_min) % 60
    off_str = f"GMT{sign}{off_h:02d}{off_m:02d}"
    local_secs = time.time() + offset_min * 60
    base = time.strftime("%a %b %d %Y %H:%M:%S", time.gmtime(local_secs))
    return f"{base} {off_str} ({prof.get('tz_label', 'Coordinated Universal Time')})"


# ═══════════════════════════════════════════════════════════════════════
# Sentinel (Pure Python PoW + Remote fallback)
# ═══════════════════════════════════════════════════════════════════════

class _Sentinel:
    """Python 仿造 sentinel token: PoW (proof-of-work) 字段。
    不含 t (Turnstile dx) 和 so (session observer), 那两个需要真浏览器 WASM。
    """

    def __init__(self, device_id, ua, platform="Windows", country_code="US",
                 nav_lang="en-US", hwc=None):
        self.did = device_id
        self.sid = str(uuid.uuid4())
        self.ua = ua
        self.platform = platform if platform in SCREEN_POOLS else "Windows"
        self.country = country_code or "US"
        self.nav_lang = nav_lang or "en-US"
        if hwc is None:
            hwc = random.choice([8, 10, 12]) if self.platform == "macOS" \
                  else random.choice([4, 8, 12, 16])
        self.hwc = hwc
        self.screen = random.choice(SCREEN_POOLS[self.platform])

    @staticmethod
    def _fnv(text):
        h = 2166136261
        for c in text:
            h ^= ord(c)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= h >> 16
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= h >> 13
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= h >> 16
        return format(h & 0xFFFFFFFF, "08x")

    def _cfg(self):
        pn = random.uniform(1000, 50000)
        sw, sh = self.screen
        return [
            f"{sw}x{sh}",
            _tz_date_string(self.country),
            random.choice([4294705152, 4294705152, 4294705152, 2197815296]),
            random.random(), self.ua,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None, None, self.nav_lang, random.random(),
            random.choice(["vendorSub-undefined", "plugins-undefined",
                           "mimeTypes-undefined", "hardwareConcurrency-undefined"]),
            random.choice(["location", "implementation", "URL", "documentURI", "compatMode"]),
            random.choice(["Object", "Function", "Array", "Number", "parseFloat", "undefined"]),
            pn, self.sid, "", self.hwc,
            time.time() * 1000 - pn,
        ]

    def _b64(self, data):
        return base64.b64encode(json.dumps(data, separators=(",", ":"),
                                           ensure_ascii=False).encode()).decode()

    def req_token(self):
        d = self._cfg()
        d[3] = 1
        d[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._b64(d)

    def solve(self, seed, difficulty):
        d = self._cfg()
        difficulty = str(difficulty or "0")
        t0 = time.time()
        for i in range(500000):
            d[3] = i
            d[9] = round((time.time() - t0) * 1000)
            payload = self._b64(d)
            if self._fnv(seed + payload)[:len(difficulty)] <= difficulty:
                return "gAAAAAB" + payload + "~S"
        return "gAAAAAB" + "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D" + self._b64(str(None))


def _build_sentinel(session, device_id, flow, fp=None):
    """Pure Python sentinel token with PoW. No Turnstile dx / SO (needs real browser)."""
    fp = fp or _FP_POOL[0]
    plat = fp.get("os") or ("macOS" if fp.get("platform") == '"macOS"' else "Windows")
    gen = _Sentinel(
        device_id, ua=fp["ua"],
        platform=plat,
        country_code=fp.get("country", "US"),
        nav_lang=fp.get("nav_lang", "en-US"),
    )
    h = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "Origin": "https://sentinel.openai.com",
        "User-Agent": fp["ua"],
        "Accept": "*/*",
        "Accept-Language": fp.get("lang", "en-US,en;q=0.9"),
    }
    if fp.get("browser") != "firefox":
        h.update({
            "sec-ch-ua": fp["ch_ua"],
            "sec-ch-ua-mobile": fp["mobile"],
            "sec-ch-ua-platform": fp["platform"],
        })
    r = session.post(
        f"{SENTINEL_API}/backend-api/sentinel/req",
        data=json.dumps({"p": gen.req_token(), "id": device_id, "flow": flow}),
        headers=h, timeout=20, verify=TLS_VERIFY,
    )
    _promote_oai_sc_cookie(session, r)
    d = _rj(r)
    token = str(d.get("token") or "")
    if r.status_code != 200 or not token:
        raise RuntimeError(f"sentinel failed: {r.status_code}")
    pw = d.get("proofofwork") or {}
    p = (gen.solve(str(pw.get("seed") or ""), str(pw.get("difficulty") or "0"))
         if pw.get("required") and pw.get("seed")
         else gen.req_token())
    return json.dumps({"p": p, "t": "", "c": token, "id": device_id, "flow": flow},
                      separators=(",", ":"))


def _solve_sentinel_remote(flow, fp=None, proxy="", session=None):
    """Remote flow-only sentinel shortcut (fast, no PoW/Turnstile)."""
    ua = (fp or {}).get("ua", _FP_POOL[0]["ua"]) if isinstance(fp, dict) else _FP_POOL[0]["ua"]
    try:
        r = _curl.post(
            f"{SENTINEL_API}/backend-api/sentinel/req",
            json={"flow": flow},
            headers={"User-Agent": ua, "Content-Type": "application/json",
                     "Origin": AUTH},
            impersonate="chrome", timeout=20,
        )
        _promote_oai_sc_cookie(session, r)
        sc = ""
        try:
            sc = r.cookies.get("oai-sc") or ""
        except Exception:
            sc = ""
        if not sc:
            try:
                set_cookie = (r.headers.get("set-cookie")
                              or r.headers.get("Set-Cookie") or "")
                m = re.search(r"(?:^|,\s*)oai-sc=([^;]+)", set_cookie)
                if m:
                    sc = m.group(1)
            except Exception:
                sc = ""
        if sc and session is not None:
            _clear_cookie_names(session, {"oai-sc"}, domain_suffixes=("openai.com",))
            try:
                session.cookies.set("oai-sc", sc, domain=".openai.com", path="/")
            except Exception:
                pass
        tok = r.json().get("token", "")
        if tok and len(tok) > 20:
            return tok
    except Exception:
        pass
    return None


def _solve_sentinel_pair(session, device_id, flow, fp=None, proxy=""):
    """Get enforcement token. Tries remote first, falls back to Python PoW."""
    remote = _solve_sentinel_remote(flow, fp=fp, proxy=proxy, session=session)
    if remote:
        return remote, None
    enf = _build_sentinel(session, device_id, flow, fp=fp)
    return enf, None


def _inject_sentinel(h, session, device_id, flow, fp=None, proxy=""):
    """Inject openai-sentinel-token header into h dict."""
    enf, so = _solve_sentinel_pair(session, device_id, flow, fp=fp, proxy=proxy)
    h["openai-sentinel-token"] = enf
    if so:
        h["openai-sentinel-so-token"] = so
    return enf, so


# ═══════════════════════════════════════════════════════════════════════
# ChatGPTSession — Login + /api/auth/session
# ═══════════════════════════════════════════════════════════════════════

class ChatGPTLoginError(RuntimeError):
    """Login failed for a reason that should not be retried."""
    pass


class ChatGPTOTPTimeout(RuntimeError):
    """OTP wait timed out."""
    pass


class ChatGPTSession:
    """Login to ChatGPT via NextAuth and get the /api/auth/session accessToken.

    Supports two auth modes:
      - 'password': Use OpenAI account password (traditional)
      - 'otp': Use email OTP (recommended, more reliable)
    """

    def __init__(self, proxy="", country_code="US", impersonate=""):
        self.proxy = _resolve_proxy(proxy) if proxy else ""
        self.country_code = (country_code or "US").upper()
        self.fp = _pick_fp(self.country_code, impersonate=impersonate)
        self.s = _session(self.proxy, imp=self.fp["imp"])
        self.did = str(uuid.uuid4())
        self.json_h = _json_h_for(self.fp)
        self.nav_h = _nav_h_for(self.fp)
        self._seed_browser_cookies()

    def close(self):
        self.s.close()

    def _seed_browser_cookies(self):
        now = int(time.time())
        locale = _locale_cookie_value(self.fp.get("nav_lang", "en-US"))
        ga = random.randint(100000000, 1999999999)
        anonymous = str(uuid.uuid4())

        # openai.com cookies
        _clear_cookie_names(self.s, {"oai-did", "country", "locale"},
                            domain_suffixes=("openai.com",))
        self.s.cookies.set("oai-did", self.did, domain=".openai.com", path="/")
        self.s.cookies.set("country", self.country_code, domain=".openai.com", path="/")
        self.s.cookies.set("locale", locale, domain=".openai.com", path="/")

        # chatgpt.com cookies
        _clear_cookie_names(self.s,
                            {"oai-did", "_ga", "_ga_9SHBSK2D9J", "_dd_s",
                             "oai-hm", "oai-asli"},
                            domain_suffixes=("chatgpt.com",))
        self.s.cookies.set("oai-did", self.did, domain=".chatgpt.com", path="/")
        self.s.cookies.set("_ga", f"GA1.1.{ga}.{now}", domain=".chatgpt.com", path="/")
        self.s.cookies.set("_ga_9SHBSK2D9J",
                           f"GS2.1.s{now}$o1$g1$t{now+30}$j31$l0$h0",
                           domain=".chatgpt.com", path="/")
        self.s.cookies.set(
            "_dd_s",
            f"aid={anonymous}&rum=0&expire={(now+900)*1000}"
            f"&logs=1&id={self.did}&created={now*1000}",
            domain=".chatgpt.com", path="/")
        self.s.cookies.set("oai-hm",
                           "AGENDA_TODAY%20%7C%20GOOD_TO_SEE_YOU",
                           domain=".chatgpt.com", path="/")
        self.s.cookies.set("oai-asli", str(uuid.uuid4()),
                           domain=".chatgpt.com", path="/")

    def _normalize_openai_context_cookies(self):
        sc = ""
        try:
            jar = getattr(self.s.cookies, "jar", None)
            for c in list(jar) if jar is not None else []:
                if getattr(c, "name", "") == "oai-sc":
                    sc = getattr(c, "value", "") or sc
        except Exception:
            sc = ""
        _clear_cookie_names(self.s, {"oai-did", "country", "locale", "oai-sc"},
                            domain_suffixes=("openai.com",))
        try:
            self.s.cookies.set("oai-did", self.did, domain=".openai.com", path="/")
            self.s.cookies.set("country", self.country_code, domain=".openai.com", path="/")
            self.s.cookies.set("locale",
                               _locale_cookie_value(self.fp.get("nav_lang", "en-US")),
                               domain=".openai.com", path="/")
            if sc:
                self.s.cookies.set("oai-sc", sc, domain=".openai.com", path="/")
        except Exception:
            pass

    def _jh(self, referer=None):
        return dict(self.json_h)

    # ── Public API ──

    def login(self, email, password=None, mail_client=None, auth_mode="otp",
              log=print):
        """Login to ChatGPT and return session tokens.

        Args:
            email: ChatGPT account email
            password: OpenAI password (required for auth_mode='password')
            mail_client: MoEmail/MailComClient instance (required for auth_mode='otp')
            auth_mode: 'otp' (default) or 'password'
            log: logger function

        Returns:
            dict with keys: accessToken, expires, email, nextauth_cookie_present
        """
        t0 = time.time()
        def _ts():
            return f"{time.time()-t0:5.1f}s"

        self.email = email
        self._normalize_openai_context_cookies()
        ua = self.fp["ua"]
        is_firefox = self.fp.get("browser") == "firefox"

        log(f"[login] {_ts()} email={email} country={self.country_code} "
            f"browser={self.fp.get('browser')} imp={self.fp.get('imp')} "
            f"auth={auth_mode}")

        # Step 1: Warm cookies with /auth/login
        log(f"[login] {_ts()} Step 1: GET /auth/login (warm cookies)")
        try:
            r0 = _do(self.s, "GET", f"{CHATGPT}/auth/login",
                     headers={**self.nav_h, "referer": f"{CHATGPT}/"},
                     allow_redirects=True)
            log(f"[login] {_ts()} login page {r0.status_code} "
                f"final={str(getattr(r0, 'url', ''))[:100]}")
        except Exception as e:
            log(f"[login] {_ts()} login page warm-up failed (ignored): {e}")

        # Step 2: Get CSRF token
        log(f"[login] {_ts()} Step 2: GET /api/auth/csrf")
        csrf_headers = {
            "accept": "application/json",
            "user-agent": ua,
            "referer": f"{CHATGPT}/auth/login",
        }
        if is_firefox:
            csrf_headers.update({
                "accept-language": self.fp["lang"],
                "sec-fetch-site": "same-origin",
                "sec-fetch-mode": "cors",
                "sec-fetch-dest": "empty",
                "priority": "u=4",
            })
        r = _do(self.s, "GET", f"{CHATGPT}/api/auth/csrf",
                headers=csrf_headers)
        if r.status_code != 200:
            raise ChatGPTLoginError(f"csrf: {r.status_code} {_rj(r)}")
        csrf = (_rj(r) or {}).get("csrfToken", "")
        if not csrf:
            raise ChatGPTLoginError(f"csrf empty: {_rj(r)}")
        log(f"[login] {_ts()} csrf={csrf[:16]}...")

        # Step 3: POST /api/auth/signin/openai → auth_url
        signin_qs = urlencode({
            "prompt": "login",
            "screen_hint": "login_or_signup",
            "login_hint": email,
        })
        signin_body = urlencode({
            "callbackUrl": f"{CHATGPT}/",
            "csrfToken": csrf,
            "json": "true",
        })
        log(f"[login] {_ts()} Step 3: POST /api/auth/signin/openai")
        r = _do(self.s, "POST",
                f"{CHATGPT}/api/auth/signin/openai?{signin_qs}",
                headers={
                    "accept": "application/json",
                    "accept-language": self.fp["lang"],
                    "content-type": "application/x-www-form-urlencoded",
                    "user-agent": ua,
                    "referer": f"{CHATGPT}/",
                    "origin": CHATGPT,
                },
                data=signin_body,
                allow_redirects=False)
        try:
            _body = r.text[:400] if hasattr(r, "text") else ""
        except Exception:
            _body = ""
        auth_url = ""
        j = _rj(r)
        if isinstance(j, dict) and j.get("url"):
            auth_url = j["url"]
        if not auth_url:
            auth_url = (r.headers.get("location")
                        or r.headers.get("Location") or "")
        log(f"[login] {_ts()} signin {r.status_code} auth_url={auth_url[:140]!r}")

        bad = (not auth_url
               or auth_url.rstrip("/") == CHATGPT
               or "/auth/login" in auth_url
               or "/auth/error" in auth_url)
        if bad:
            raise ChatGPTLoginError(
                f"signin/openai didn't return auth0 URL: status={r.status_code} "
                f"got={auth_url[:200]!r} body={_body[:200]!r}")

        # Step 4: GET auth_url chain
        log(f"[login] {_ts()} Step 4: GET auth_url chain {auth_url[:120]}")
        if is_firefox:
            nav_h = dict(self.nav_h)
            nav_h.update({
                "referer": f"{CHATGPT}/",
                "sec-fetch-site": "cross-site",
                "sec-fetch-mode": "navigate",
                "sec-fetch-dest": "document",
                "sec-fetch-user": "?1",
                "upgrade-insecure-requests": "1",
                "priority": "u=0, i",
            })
        else:
            nav_h = {**self.nav_h, "referer": f"{CHATGPT}/"}
        # Inject sentinel token for the auth0 chain — without it Cloudflare may block
        _inject_sentinel(nav_h, self.s, self.did, "authorize_continue",
                         fp=self.fp, proxy=self.proxy)
        r3 = _do(self.s, "GET", auth_url, headers=nav_h,
                 allow_redirects=True)
        final_url = str(getattr(r3, "url", "") or "")
        log(f"[login] {_ts()} chain {r3.status_code} final={final_url[:140]}")

        # Check for silent auth (already logged in)
        try:
            _final_parsed = urlparse(final_url or "")
            silent_ok = (_final_parsed.netloc == "chatgpt.com")
        except Exception:
            silent_ok = False

        if auth_mode == "otp" and not silent_ok:
            if mail_client is None:
                raise ChatGPTLoginError(
                    "auth_mode='otp' requires a mail_client (MoEmail or MailComClient). "
                    "Use --password mode or provide --config with mail settings.")

            # Handle OTP flow
            cont = ""
            if "/log-in-or-create-account" in final_url:
                log(f"[login] {_ts()} Step 4a: /log-in-or-create-account, "
                    f"check email...")
                try:
                    self._ios_check_email(email, self.did, log=log)
                except Exception as e:
                    log(f"[login] {_ts()} check email failed (ignored): {e}")

            if "/log-in/password" in final_url:
                log(f"[login] {_ts()} Step 4b: OTP from /log-in/password...")
                cont, _ = self._login_via_otp_from_login_page(
                    email, "login", mail_client, device_id=self.did,
                    referer_login=final_url, log=log)
            elif "/email-verification" in final_url:
                log(f"[login] {_ts()} Step 4b: email-verification, "
                    f"waiting for OTP...")
                cont = self._handle_otp_verification(mail_client, log=log, ts=_ts)
            elif "/log-in/password" in final_url:
                log(f"[login] {_ts()} Step 4b: /log-in/password, "
                    f"walking OTP path...")
                cont, _ = self._login_via_otp_from_login_page(
                    email, "login", mail_client, device_id=self.did,
                    referer_login=final_url, log=log)
            elif "/about-you" in final_url:
                log(f"[login] {_ts()} Step 4b: /about-you, completing "
                    f"create_account...")
                cont = self._handle_about_you(log=log, ts=_ts)
            else:
                raise ChatGPTLoginError(
                    f"auth0 chain landed on unexpected page: "
                    f"final={final_url[:200]}")

            if not cont:
                raise ChatGPTLoginError(
                    f"no continue_url after OTP verification, "
                    f"final={final_url[:200]}")

            # Step 5: Follow continue_url chain
            log(f"[login] {_ts()} Step 5: follow continue_url {cont[:120]}")
            url = cont
            referer = final_url
            for hop in range(12):
                try:
                    rh = _do(self.s, "GET", url,
                             headers={**self.nav_h, "referer": referer},
                             allow_redirects=False)
                except Exception as e:
                    log(f"[login] {_ts()} hop#{hop} failed (ignored): {e}")
                    break
                if rh.status_code in (301, 302, 303, 307, 308):
                    loc = rh.headers.get("Location") or rh.headers.get("location") or ""
                    if not loc:
                        log(f"[login] {_ts()} hop#{hop} {rh.status_code} no Location")
                        break
                    referer = url
                    url = urljoin(url, loc)
                    log(f"[login] {_ts()} hop#{hop} -> {url[:140]}")
                else:
                    # Handle /about-you mid-chain
                    if "/about-you" in url:
                        log(f"[login] {_ts()} hop#{hop} /about-you, POST "
                            f"create_account...")
                        cont2 = self._handle_about_you(log=log, ts=_ts)
                        if cont2:
                            referer = url
                            url = cont2
                            continue
                    log(f"[login] {_ts()} hop#{hop} {rh.status_code} "
                        f"final={url[:140]}")
                    break

            # Ensure we land on chatgpt.com
            try:
                parsed = urlparse(url or "")
            except Exception:
                parsed = None
            if not (parsed and parsed.netloc == "chatgpt.com"
                    and parsed.path in ("", "/")):
                log(f"[login] {_ts()} extra GET chatgpt.com/")
                try:
                    _do(self.s, "GET", f"{CHATGPT}/",
                        headers={**self.nav_h, "referer": url or cont},
                        allow_redirects=True)
                except Exception as e:
                    log(f"[login] {_ts()} extra GET failed (ignored): {e}")

        elif auth_mode == "password" and not silent_ok:
            # Password auth
            if not password:
                raise ChatGPTLoginError(
                    "auth_mode='password' requires a password.")

            if "/log-in-or-create-account" in final_url:
                log(f"[login] {_ts()} Step 4a: check email...")
                try:
                    self._ios_check_email(email, self.did, log=log)
                except Exception:
                    pass

            if "/log-in/password" in final_url:
                log(f"[login] {_ts()} Step 4b: password verify...")
                d = self._password_verify(password, self.did,
                                          referer=final_url, log=log)
                cont = str(d.get("continue_url") or "")
                pt = str((d.get("page") or {}).get("type") or "")
                if pt in ("add_phone", "phone_verification") or "/add-phone" in cont:
                    log(f"[login] {_ts()} WARNING: add-phone required, "
                        f"login may fail")
                    raise ChatGPTLoginError("add-phone required: account needs phone binding")

                if cont:
                    log(f"[login] {_ts()} follow continue_url {cont[:120]}")
                    r = _do(self.s, "GET", cont if cont.startswith("http")
                            else AUTH + cont,
                            headers={**self.nav_h, "referer": final_url},
                            allow_redirects=True)
                    log(f"[login] {_ts()} follow -> "
                        f"{str(getattr(r, 'url', ''))[:120]}")
            elif "/email-verification" in final_url:
                # password mode 但 OpenAI 决定走 OTP（常见于可疑登录）
                log(f"[login] {_ts()} Step 4b: /email-verification "
                    f"(password mode → OTP required)")
                if not mail_client:
                    raise ChatGPTLoginError(
                        "OpenAI requires email OTP verification. "
                        "Use --otp mode with --config or add mail settings.")
                cont = self._handle_otp_verification(mail_client, log=log, ts=_ts)
            elif "/about-you" in final_url:
                log(f"[login] {_ts()} Step 4b: /about-you, completing...")
                cont = self._handle_about_you(log=log, ts=_ts)
                if cont:
                    r = _do(self.s, "GET", cont,
                            headers={**self.nav_h, "referer": final_url},
                            allow_redirects=True)
                    log(f"[login] {_ts()} follow -> "
                        f"{str(getattr(r, 'url', ''))[:120]}")
            else:
                raise ChatGPTLoginError(
                    f"password auth landed on unexpected page: "
                    f"final={final_url[:200]}")

        # Step 6: GET /api/auth/session
        log(f"[login] {_ts()} Step 6: GET /api/auth/session")
        r = _do(self.s, "GET", f"{CHATGPT}/api/auth/session",
                headers={
                    "accept": "application/json",
                    "user-agent": ua,
                    "referer": f"{CHATGPT}/",
                })
        if r.status_code != 200:
            raise ChatGPTLoginError(
                f"/api/auth/session: {r.status_code} {_rj(r)}")
        d = _rj(r) or {}
        at = d.get("accessToken", "") or ""
        expires = d.get("expires", "")

        if not at:
            raise ChatGPTLoginError(
                f"/api/auth/session returned no accessToken "
                f"(not logged in to chatgpt.com): keys={list(d.keys())}")

        log(f"[login] {_ts()} SUCCESS at={at[:30]}... expires={expires} "
            f"keys={list(d.keys())}")

        return {
            "accessToken": at,
            "expires": expires,
            "email": email,
            "session_data": d,
        }

    # ── Internal helpers ──

    def _handle_otp_verification(self, mail_client, log=print, ts=None):
        """Wait for OTP email on email-verification page, then validate."""
        _ts = ts or (lambda: "")
        seen = set()

        if hasattr(mail_client, "prime_seen"):
            try:
                primed = mail_client.prime_seen(seen)
                log(f"  [otp] {_ts()} prime mailbox: {primed} old messages ignored")
            except Exception as e:
                log(f"  [otp] {_ts()} prime_seen failed (ignored): {e}")

        log(f"  [otp] {_ts()} waiting for OTP (timeout=60s)...")
        code = mail_client.wait_for_code(timeout=60, interval=3,
                                         seen=seen, log=log)
        if not code:
            # Retry: send OTP again
            log(f"  [otp] {_ts()} no OTP received, retrying send...")
            try:
                h_send = self._jh(f"{AUTH}/email-verification")
                _inject_sentinel(h_send, self.s, self.did,
                                 "passwordless_send_otp", fp=self.fp,
                                 proxy=self.proxy)
                sr = _do(self.s, "POST",
                         f"{AUTH}/api/accounts/passwordless/send-otp",
                         json={}, headers=h_send, allow_redirects=False)
                log(f"  [otp] {_ts()} passwordless/send-otp {sr.status_code}")
            except Exception as e:
                log(f"  [otp] {_ts()} retry send failed (ignored): {e}")

            log(f"  [otp] {_ts()} waiting for OTP again (timeout=40s)...")
            code = mail_client.wait_for_code(timeout=40, interval=3,
                                             seen=seen, log=log)

        if not code:
            raise ChatGPTOTPTimeout("OTP wait timed out")
        log(f"  [otp] {_ts()} OTP={code}")

        h_v = self._jh(f"{AUTH}/email-verification")
        vr = _do(self.s, "POST", f"{AUTH}/api/accounts/email-otp/validate",
                 json={"code": code}, headers=h_v, allow_redirects=False)
        if vr.status_code != 200:
            _inject_sentinel(h_v, self.s, self.did, "authorize_continue",
                             fp=self.fp, proxy=self.proxy)
            vr = _do(self.s, "POST",
                     f"{AUTH}/api/accounts/email-otp/validate",
                     json={"code": code}, headers=h_v, allow_redirects=False)
        d = _rj(vr)
        cont = str(d.get("continue_url") or "")
        pt = str((d.get("page") or {}).get("type") or "")
        log(f"  [otp] {_ts()} validated page_type={pt!r} cont={cont[:100]}")
        if pt in ("add_phone", "phone_verification") or "/add-phone" in cont:
            log(f"  [otp] {_ts()} WARNING: add-phone required")
        return cont

    def _login_via_otp_from_login_page(self, email, tag, mail_client,
                                       device_id=None, referer_login="",
                                       parent_seen=None, skip_check=False,
                                       log=print):
        """Complete OTP login from /log-in/password page."""
        seen = set()
        if parent_seen:
            seen.update(parent_seen)
        if hasattr(mail_client, "prime_seen"):
            try:
                primed = mail_client.prime_seen(seen)
                log(f"  [{tag}] prime mailbox: {primed} old messages ignored")
            except Exception:
                pass

        device_id = device_id or self.did

        # Send OTP
        log(f"  [{tag}] POST /passwordless/send-otp...")
        h_send = self._jh(f"{AUTH}/log-in/password")
        _inject_sentinel(h_send, self.s, device_id, "passwordless_send_otp",
                         fp=self.fp, proxy=self.proxy)
        sr = _do(self.s, "POST", f"{AUTH}/api/accounts/passwordless/send-otp",
                 json={}, headers=h_send, allow_redirects=False)
        if sr.status_code != 200:
            log(f"  [{tag}] send-otp {sr.status_code}, "
                f"fallback GET /email-otp/send")
            _do(self.s, "GET", f"{AUTH}/api/accounts/email-otp/send",
                headers={"User-Agent": self.fp["ua"]})
        else:
            log(f"  [{tag}] send-otp 200")

        # Wait for OTP
        log(f"  [{tag}] waiting for OTP (timeout=60s)...")
        code = mail_client.wait_for_code(timeout=60, interval=3,
                                         seen=seen, log=log)
        if not code:
            return "", seen

        log(f"  [{tag}] OTP={code}")
        h_v = self._jh(f"{AUTH}/email-verification")
        vr = _do(self.s, "POST", f"{AUTH}/api/accounts/email-otp/validate",
                 json={"code": code}, headers=h_v, allow_redirects=False)
        d = _rj(vr)
        cont = str(d.get("continue_url") or "")
        pt = str((d.get("page") or {}).get("type") or "")
        log(f"  [{tag}] validated page_type={pt!r} cont={cont[:100]}")
        if pt in ("add_phone", "phone_verification") or "/add-phone" in cont:
            log(f"  [{tag}] WARNING: add-phone required")
        return cont, seen

    def _password_verify(self, password, device_id, referer="", log=print):
        """POST /api/accounts/password/verify for password-based login."""
        ref = referer or f"{AUTH}/log-in/password"
        h = self._jh(ref)
        _inject_sentinel(h, self.s, device_id, "password_verify",
                         fp=self.fp, proxy=self.proxy)
        r = _do(self.s, "POST", f"{AUTH}/api/accounts/password/verify",
                json={"password": password}, headers=h,
                allow_redirects=False)
        if r.status_code != 200:
            raise ChatGPTLoginError(
                f"password/verify: {r.status_code} {_rj(r)}")
        return _rj(r)

    def _ios_check_email(self, email, device_id, log=print):
        """POST /api/accounts/check to tell auth0 which email is logging in."""
        h = self._jh(f"{AUTH}/log-in-or-create-account")
        _inject_sentinel(h, self.s, device_id, "log_in_or_create_account",
                         fp=self.fp, proxy=self.proxy)
        r = _do(self.s, "POST", f"{AUTH}/api/accounts/check",
                json={"email": email}, headers=h, allow_redirects=False)
        d = _rj(r)
        return str(d.get("continue_url") or "")

    def _handle_about_you(self, log=print, ts=None):
        """Complete /about-you step (create_account with random name/birthdate)."""
        _ts = ts or (lambda: "")
        fn = (secrets.choice(string.ascii_uppercase)
              + "".join(secrets.choice(string.ascii_lowercase)
                        for _ in range(random.randint(4, 8))))
        ln = (secrets.choice(string.ascii_uppercase)
              + "".join(secrets.choice(string.ascii_lowercase)
                        for _ in range(random.randint(4, 8))))
        birthdate = (
            f"{random.randint(1985, 2003)}-"
            f"{random.randint(1,12):02d}-"
            f"{random.randint(1,28):02d}"
        )
        h2 = self._jh(f"{AUTH}/about-you")
        _inject_sentinel(h2, self.s, self.did, "oauth_create_account",
                         fp=self.fp, proxy=self.proxy)
        log(f"  [login] {_ts()} POST create_account name={fn} {ln} "
            f"birth={birthdate}")
        r = _do(self.s, "POST", f"{AUTH}/api/accounts/create_account",
                json={"name": f"{fn} {ln}", "birthdate": birthdate},
                headers=h2)
        d = _rj(r)
        cont = str(d.get("continue_url") or "")
        log(f"  [login] {_ts()} create_account {r.status_code} "
            f"cont={cont[:100]}")
        return cont


# ═══════════════════════════════════════════════════════════════════════
# CLI
# ═══════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    import argparse

    p = argparse.ArgumentParser(
        description="ChatGPT Login & Session Token")
    p.add_argument("--email", required=True, help="ChatGPT account email")
    p.add_argument("--password", help="OpenAI password (for password auth mode)")
    p.add_argument("--otp", action="store_true",
                   help="Use OTP-based login (default if --password not given)")
    p.add_argument("--config", default="config.json",
                   help="Path to config.json (for proxy + mail settings)")
    p.add_argument("--proxy", help="Override proxy from config")
    p.add_argument("--country", default="US", help="Country code for fingerprint")
    p.add_argument("--impersonate", default="",
                   help="curl_cffi impersonate target (e.g. chrome136)")
    args = p.parse_args()

    # Load config
    cfg = {}
    if os.path.exists(args.config):
        with open(args.config) as f:
            cfg = json.load(f)

    proxy = args.proxy or cfg.get("proxy", "")
    auth_mode = "password" if args.password else "otp"

    # Initialize mail client for OTP mode
    mail_client = None
    if auth_mode == "otp":
        mail_provider = cfg.get("mail_provider", "moemail")
        if mail_provider == "moemail":
            from .mail_clients import MoEmail  # noqa: F811
            mail_client = MoEmail(
                api_base=cfg.get("mail_api_base", "https://mail.example.com"),
                api_key=cfg.get("mail_api_key", ""),
                domains=cfg.get("mail_domains", ["example.com"]),
                expiry=cfg.get("mail_expiry", 3600000),
            )
        elif mail_provider in ("mailcom", "auto"):
            from chatgpt_register.mail._legacy_clients import MailComClient
            mail_client = MailComClient(
                api_base=cfg.get("mailcom_api_base", "http://127.0.0.1:8787"),
                app_token=cfg.get("mailcom_app_token", ""),
                email=args.email,
                password=cfg.get("mail_password", ""),
                proxy=proxy,
            )
        else:
            print(f"Unknown mail_provider: {mail_provider!r}")
            exit(1)

    # Login
    sess = ChatGPTSession(
        proxy=proxy,
        country_code=args.country,
        impersonate=args.impersonate or cfg.get("impersonate", ""),
    )
    try:
        result = sess.login(
            email=args.email,
            password=args.password,
            mail_client=mail_client,
            auth_mode=auth_mode,
        )
        print("\n✅ Login successful!")
        print(f"   Email:     {result['email']}")
        print(f"   Expires:   {result.get('expires', 'unknown')}")
        print(f"\n   accessToken: {result['accessToken']}")
    except ChatGPTLoginError as e:
        print(f"\n❌ Login failed: {e}")
        exit(1)
    except ChatGPTOTPTimeout as e:
        print(f"\n❌ OTP timeout: {e}")
        exit(1)
    finally:
        sess.close()
