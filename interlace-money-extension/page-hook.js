(function () {
  "use strict";

  if (window.__INTERLACE_MONEY_TOKEN_HOOKED__) return;
  window.__INTERLACE_MONEY_TOKEN_HOOKED__ = true;

  const LOG_PREFIX = "[Interlace Token Hook]";
  const cachedHeaders = {
    authorization: "",
    fingerprint: "",
    lang: "",
    systemtype: "",
    websiteVersion: ""
  };
  const MIN_PROBE_INTERVAL_SECONDS = 15;
  const PROBE_BODY = { status: "PROCESSING", size: 99 };
  const REAUTH_COOLDOWN_MS = 5 * 60 * 1000;
  const REAUTH_STORAGE_KEY = "interlace-probe-reauth-at";
  let probeConfig = {
    enabled: false,
    url: "",
    intervalSeconds: 30
  };
  let probeTimer = null;
  let probeInFlight = false;
  let reauthTriggered = false;

  function normalizeAuth(value) {
    if (!value) return "";
    const trimmed = String(value).trim();
    return /^Bearer\s+/i.test(trimmed) ? trimmed : `Bearer ${trimmed}`;
  }

  function readHeader(headersLike, name) {
    if (!headersLike) return "";

    try {
      if (headersLike instanceof Headers) {
        return headersLike.get(name) || "";
      }
    } catch (_) {
      // Ignore cross-realm Headers checks.
    }

    const lower = name.toLowerCase();
    const upper = name.toUpperCase();
    return headersLike[name] || headersLike[lower] || headersLike[upper] || "";
  }

  function cacheHeaders(headersLike) {
    if (!headersLike) return;

    const authorization = normalizeAuth(readHeader(headersLike, "authorization"));
    const fingerprint = readHeader(headersLike, "fingerprint");
    const lang = readHeader(headersLike, "lang");
    const systemtype = readHeader(headersLike, "systemtype");
    const websiteVersion = readHeader(headersLike, "website-version");

    if (authorization) cachedHeaders.authorization = authorization;
    if (fingerprint) cachedHeaders.fingerprint = String(fingerprint).trim();
    if (lang) cachedHeaders.lang = String(lang).trim();
    if (systemtype) cachedHeaders.systemtype = String(systemtype).trim();
    if (websiteVersion) cachedHeaders.websiteVersion = String(websiteVersion).trim();
  }

  function emitToken(rawAuth, meta) {
    const authorization = normalizeAuth(rawAuth);
    const token = authorization.replace(/^Bearer\s+/i, "").trim();
    if (!token) return;

    cachedHeaders.authorization = authorization;
    window.postMessage({
      type: "INTERLACE_MONEY_ACCESS_TOKEN",
      authorization,
      url: meta && meta.url || "",
      via: meta && meta.via || "",
      mode: meta && meta.mode || "capture",
      force: Boolean(meta && meta.force),
      cachedHeaders: { ...cachedHeaders }
    }, window.location.origin);
  }

  function isAppPage() {
    return location.pathname.includes("/app");
  }

  function emitProbeEvent(kind, details) {
    window.postMessage({
      type: "INTERLACE_MONEY_PROBE_EVENT",
      kind,
      url: probeConfig.url,
      ...details
    }, window.location.origin);
  }

  function buildProbeHeaders() {
    const headers = {
      accept: "application/json, text/plain, */*",
      "content-type": "application/json",
      authorization: cachedHeaders.authorization
    };

    if (cachedHeaders.fingerprint) headers.fingerprint = cachedHeaders.fingerprint;
    if (cachedHeaders.lang) headers.lang = cachedHeaders.lang;
    if (cachedHeaders.systemtype) headers.systemtype = cachedHeaders.systemtype;
    if (cachedHeaders.websiteVersion) headers["website-version"] = cachedHeaders.websiteVersion;
    return headers;
  }

  function tryReadTokenFromStorage() {
    const likelyKeyPattern = /(access[_-]?token|auth[_-]?token|token|authorization)/i;
    const stores = [window.localStorage, window.sessionStorage].filter(Boolean);

    for (const store of stores) {
      for (let index = 0; index < store.length; index += 1) {
        const key = store.key(index);
        if (!key || !likelyKeyPattern.test(key)) continue;

        const value = store.getItem(key);
        const token = extractToken(value);
        if (token) return token;
      }
    }

    for (const store of stores) {
      for (let index = 0; index < store.length; index += 1) {
        const value = store.getItem(store.key(index));
        const token = extractToken(value);
        if (token) return token;
      }
    }

    return "";
  }

  function extractToken(value) {
    if (!value) return "";

    const raw = String(value).trim();
    if (/^Bearer\s+/i.test(raw)) return raw;
    if (/^[A-Za-z0-9._~+/=-]{24,}$/.test(raw) && raw.includes(".")) return raw;

    try {
      const parsed = JSON.parse(raw);
      return findTokenInObject(parsed);
    } catch (_) {
      const match = raw.match(/Bearer\s+([A-Za-z0-9._~+/=-]{24,})/i)
        || raw.match(/"access[_-]?token"\s*:\s*"([^"]{24,})"/i)
        || raw.match(/"token"\s*:\s*"([^"]{24,})"/i);
      return match ? match[1] : "";
    }
  }

  function findTokenInObject(value) {
    if (!value || typeof value !== "object") return "";

    const preferredKeys = ["accessToken", "access_token", "authToken", "auth_token", "token", "authorization"];
    for (const key of preferredKeys) {
      if (typeof value[key] === "string") {
        const token = extractToken(value[key]);
        if (token) return token;
      }
    }

    for (const child of Object.values(value)) {
      if (typeof child === "string") {
        const token = extractToken(child);
        if (token) return token;
      }
      if (child && typeof child === "object") {
        const token = findTokenInObject(child);
        if (token) return token;
      }
    }

    return "";
  }

  function collectHeaders(headersLike, meta) {
    if (!headersLike) return;
    cacheHeaders(headersLike);
    emitToken(readHeader(headersLike, "authorization"), meta);
  }

  function parseFetchArgs(args) {
    const input = args[0];
    const init = args[1] || {};
    let url = "";
    let headers = init.headers;

    if (typeof input === "string" || input instanceof URL) {
      url = String(input);
    } else if (input instanceof Request) {
      url = input.url;
      headers = headers || input.headers;
    } else if (input && typeof input.url === "string") {
      url = input.url;
    }

    return { url, headers };
  }

  if (typeof window.fetch === "function") {
    const originalFetch = window.fetch;

    async function probeSession() {
      if (!probeConfig.enabled || !probeConfig.url || !isAppPage() || probeInFlight || reauthTriggered) return;
      if (!cachedHeaders.authorization) {
        emitProbeEvent("skipped", { status: 0, error: "尚未捕获 accessToken" });
        return;
      }

      probeInFlight = true;
      try {
        const response = await originalFetch.call(window, probeConfig.url, {
          method: "POST",
          headers: buildProbeHeaders(),
          body: JSON.stringify(PROBE_BODY),
          credentials: "omit"
        });

        // This tab may have lost leader status while the request was in flight.
        if (!probeConfig.enabled) return;

        if (response.status === 401) {
          const now = Date.now();
          const lastReauthAt = Number(sessionStorage.getItem(REAUTH_STORAGE_KEY) || 0);
          reauthTriggered = true;
          if (now - lastReauthAt < REAUTH_COOLDOWN_MS) {
            emitProbeEvent("reauth-suppressed", {
              status: response.status,
              error: "5 分钟内已触发过重新登录，本次不再跳转"
            });
            return;
          }

          sessionStorage.setItem(REAUTH_STORAGE_KEY, String(now));
          emitProbeEvent("result", { status: response.status });
          window.setTimeout(() => {
            location.replace(`${location.origin}/sign-in`);
          }, 800);
          return;
        }

        sessionStorage.removeItem(REAUTH_STORAGE_KEY);
        emitProbeEvent("result", { status: response.status });
      } catch (error) {
        emitProbeEvent("error", {
          status: 0,
          error: String(error && error.message ? error.message : error)
        });
      } finally {
        probeInFlight = false;
      }
    }

    function restartProbeLoop() {
      if (probeTimer) {
        window.clearInterval(probeTimer);
        probeTimer = null;
      }
      if (!probeConfig.enabled || !probeConfig.url || !isAppPage()) return;

      const seconds = Number(probeConfig.intervalSeconds || 30);
      const intervalMs = Math.max(
        MIN_PROBE_INTERVAL_SECONDS,
        Number.isFinite(seconds) ? seconds : 30
      ) * 1000;
      probeTimer = window.setInterval(probeSession, intervalMs);
      probeSession();
    }

    window.fetch = async function interlaceFetchHook() {
      const args = Array.from(arguments);
      const { url, headers } = parseFetchArgs(args);
      collectHeaders(headers, { url, via: "fetch" });
      return originalFetch.apply(this, args);
    };

    window.addEventListener("message", (event) => {
      if (event.source !== window) return;
      if (event.origin !== window.location.origin) return;
      if (!event.data || event.data.type !== "INTERLACE_MONEY_PROBE_CONFIG") return;

      probeConfig = {
        enabled: Boolean(event.data.enabled),
        url: String(event.data.url || ""),
        intervalSeconds: Number(event.data.intervalSeconds || 30)
      };
      reauthTriggered = false;
      restartProbeLoop();
    });
  }

  if (window.XMLHttpRequest && window.XMLHttpRequest.prototype) {
    const originalOpen = XMLHttpRequest.prototype.open;
    const originalSetRequestHeader = XMLHttpRequest.prototype.setRequestHeader;

    XMLHttpRequest.prototype.open = function interlaceOpenHook(method, url) {
      this.__interlaceRequestUrl = String(url || "");
      return originalOpen.apply(this, arguments);
    };

    XMLHttpRequest.prototype.setRequestHeader = function interlaceSetHeaderHook(header, value) {
      if (!this.__interlaceHeaders) this.__interlaceHeaders = {};
      this.__interlaceHeaders[String(header).toLowerCase()] = value;
      collectHeaders(this.__interlaceHeaders, {
        url: this.__interlaceRequestUrl || "",
        via: "xhr"
      });
      return originalSetRequestHeader.apply(this, arguments);
    };
  }

  window.addEventListener("message", (event) => {
    if (event.source !== window) return;
    if (event.origin !== window.location.origin) return;
    if (!event.data || event.data.type !== "INTERLACE_MONEY_GET_LATEST_TOKEN") return;

    const authorization = cachedHeaders.authorization || normalizeAuth(tryReadTokenFromStorage());
    if (!authorization) return;

    emitToken(authorization, {
      url: location.href,
      via: cachedHeaders.authorization ? "periodic-page-cache" : "periodic-storage",
      mode: "periodic",
      force: true
    });
  });

  console.info(`${LOG_PREFIX} ready`);
})();
