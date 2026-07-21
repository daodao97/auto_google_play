#!/usr/bin/env python3
"""Extract and filter HAR entries from multiple HAR files for Claude registration reference doc."""

import json
import re
import os

OUTPUT_PATH = "/Users/xiaochuang/Desktop/文件/Claude/Claude-register/docs/all-requests.md"

# HAR files to process
HAR_FILES = [
    "/Users/xiaochuang/Desktop/文件/Claude/pro-max/1.har",
    "/Users/xiaochuang/Desktop/文件/Claude/Claude-register/claude.ai.har",
]

# Filter patterns - entries matching ANY of these are SKIPPED
SKIP_PATTERNS = [
    # Static assets - extensions
    r'\.js(\?|$)', r'\.css(\?|$)', r'\.woff2(\?|$)', r'\.ttf(\?|$)',
    r'\.png(\?|$)', r'\.jpg(\?|$)', r'\.jpeg(\?|$)', r'\.gif(\?|$)',
    r'\.svg(\?|$)', r'\.webp(\?|$)', r'\.webm(\?|$)', r'\.ico(\?|$)',
    r'\.mp4(\?|$)',
    # Analytics/tracking
    r'(google|doubleclick|googletagmanager|googleadservices|adservice)',
    r'(datadoghq|browser-intake)',
    r'/metrics/ui', r'/metrics/', r'/rum',
    r'event_logging', r'isolated-segment\.html',
    # CDN cache / images
    r's-cdn\.anthropic\.com/images',
    r'/params/sri',
    # i18n/manifest/chunks
    r'i18n.*\.json', r'manifest\.json',
    r'/_next/static/chunks',
    # Cloudflare challenge
    r'cdn-cgi/challenge-platform',
    # assets-proxy with js/css
    r'assets-proxy.*\.(js|css)',
    # Online-metrix fingerprinting
    r'online-metrix\.net',
    # Favicon
    r'favicon\.ico',
    # Stripe JS/CSS/fonts (but keep API calls)
    r'js\.stripe\.com/',
    r'b\.stripecdn\.com',
    r'q\.stripe\.com',
    r'r\.stripe\.com/',
    r'm\.stripe\.com/',
    r'm\.stripe\.network/',
    # hCaptcha JS assets
    r'newassets\.hcaptcha\.com',
    r'imgs\.hcaptcha\.com',
    # Sentry
    r'sentry',
    # Fonts
    r'fonts\.(googleapis|gstatic)',
    # Google pay js
    r'pay\.google\.com/',
    r'play\.google\.com/',
    r'www\.gstatic\.com/',
    # Statsig
    r'statsig\.com',
    r'featureassets\.org',
    # Growthbook
    r'cdn\.growthbook\.io',
    # Sentry
    r'js\.sentry-cdn\.com',
    # Stripe elements inner HTML (frames)
    r'elements-inner-',
    r'fingerprinted/',
    # Google tag manager
    r'googletagmanager',
]

# But always KEEP (overrides skip) if URL contains these
KEEP_PATTERNS = [
    r'claude\.ai/api/',
    r'claude\.ai/edge-api/',
    r'a-cdn\.claude\.ai/',
    r'a-api\.anthropic\.com/',
    r'api\.anthropic\.com/',
    r'a-cdn\.anthropic\.com/v1/projects/',
    r'api\.stripe\.com/v1/',
    r'api\.stripe\.com/edge-internal/',
    r'api\.hcaptcha\.com/',
    r'acs\.apata\.io/',
    r'acs-challenge\.apata\.io/',
    r'merchant-ui-api\.stripe\.com/',
]

# Even with keep/match, explicitly SKIP if URL matches these
ALWAYS_SKIP = [
    r'event_logging',
    r'browser-intake',
    r's-cdn\.anthropic\.com/images',
    r'favicon\.ico',
]


def should_keep(entry):
    url = entry['request']['url']

    # Always skip these no matter what
    for pattern in ALWAYS_SKIP:
        if re.search(pattern, url, re.IGNORECASE):
            return False

    # Check skip patterns
    for pattern in SKIP_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            # But check if keep pattern overrides
            for kp in KEEP_PATTERNS:
                if re.search(kp, url, re.IGNORECASE):
                    return True
            return False

    # Check keep patterns
    for pattern in KEEP_PATTERNS:
        if re.search(pattern, url, re.IGNORECASE):
            return True

    return False


def truncate(text, max_len=8000):
    if not text:
        return "(empty)"
    if len(text) > max_len:
        return text[:max_len] + f"\n\n... [TRUNCATED at {max_len} chars, total {len(text)} chars] ..."
    return text


def extract_entry(entry, index):
    """Extract full details from a HAR entry."""
    req = entry['request']
    resp = entry['response']

    method = req['method']
    url = req['url']
    status = resp['status']
    started = entry['startedDateTime']

    # Request headers
    headers_str = ""
    for h in req['headers']:
        headers_str += f"  {h['name']}: {h['value']}\n"

    # Request body
    post_data = "(none)"
    if 'postData' in req and 'text' in req.get('postData', {}):
        post_data = req['postData']['text']

    # Response body
    resp_body = "(empty)"
    resp_mime = resp.get('content', {}).get('mimeType', '')
    if 'text' in resp.get('content', {}) and resp['content'].get('text'):
        resp_text = resp['content']['text']
        if len(resp_text) > 50000:
            resp_body = f"(response too large: {len(resp_text)} chars, {resp_mime})"
        elif 'application/octet-stream' in resp_mime:
            resp_body = f"(binary response, {len(resp_text)} bytes, {resp_mime})"
        else:
            resp_body = resp_text

    # Truncate long URLs in the heading
    display_url = url if len(url) < 200 else url[:197] + "..."

    out = f"### Entry {index}: {method} {display_url} -> {status}\n\n"
    out += f"**Full URL**: `{url}`\n\n"
    out += f"**Started**: {started}\n\n"
    out += f"**Response MIME**: {resp_mime}\n\n"
    out += "**Request Headers**:\n```\n" + headers_str + "```\n\n"
    out += "**Request Body**:\n```json\n" + post_data + "\n```\n\n"
    out += "**Response Body**:\n```json\n" + truncate(resp_body, 8000) + "\n```\n\n"
    out += "---\n\n"

    return out


def process_har(har_path, start_index=1):
    """Process a single HAR file and return kept entries with global index."""
    print(f"\nLoading HAR: {os.path.basename(har_path)}")
    with open(har_path, 'r', encoding='utf-8') as f:
        har = json.load(f)

    entries = har['log']['entries']
    total = len(entries)
    print(f"  Total entries: {total}")

    kept = []
    skipped_count = 0

    for entry in entries:
        if should_keep(entry):
            kept.append(entry)
        else:
            skipped_count += 1

    print(f"  Kept: {len(kept)}, Skipped: {skipped_count}")

    if kept:
        times = [e['startedDateTime'] for e in kept]
        print(f"  Time range: {min(times)} to {max(times)}")

    return kept


def main():
    # Process each HAR file
    all_entries = []
    for har_path in HAR_FILES:
        if not os.path.exists(har_path):
            print(f"SKIP: {har_path} does not exist")
            continue
        entries = process_har(har_path)
        all_entries.extend(entries)

    # Deduplicate entries by (method, url, status)
    seen = set()
    unique_entries = []
    for entry in all_entries:
        key = (entry['request']['method'], entry['request']['url'], entry['response']['status'])
        if key not in seen:
            seen.add(key)
            unique_entries.append(entry)

    print(f"\nTotal unique entries after dedup: {len(unique_entries)} (from {len(all_entries)} total)")

    # Sort by timestamp
    unique_entries.sort(key=lambda e: e['startedDateTime'])

    # Generate markdown
    out_lines = []
    out_lines.append("# Claude 注册全量请求参考\n\n")
    out_lines.append(f"> 数据来源: 1.har (221 entries) + claude.ai.har (460 entries)\n")
    out_lines.append(f"> 过滤后业务相关条目: {len(unique_entries)}\n")
    out_lines.append(f"> 时间范围: {unique_entries[0]['startedDateTime'] if unique_entries else 'N/A'} to {unique_entries[-1]['startedDateTime'] if unique_entries else 'N/A'}\n\n")
    out_lines.append("> **注意**: `claudeaizhuce.har` (324 entries, 注册全流程) 文件在当前系统中未找到。本文档使用可获取的 HAR 文件生成，覆盖了登录后会话和支付升级流程。如需完整的 magic link -> onboarding 流程，需要重新抓取。\n\n")
    out_lines.append("---\n\n")

    out_lines.append("## 阶段 0 — 发送 magic link\n\n")
    out_lines.append("> 注: 当前 HAR 文件中未包含发送 magic link 阶段的请求（需单独抓取 `POST /api/auth/send_magic_link`）。\n")
    out_lines.append("> 该请求的预期格式:\n")
    out_lines.append("> ```\n")
    out_lines.append("> POST https://claude.ai/api/auth/send_magic_link\n")
    out_lines.append("> Body: {\"email\": \"user@example.com\"}\n")
    out_lines.append("> ```\n\n")
    out_lines.append("---\n\n")

    out_lines.append("## 阶段 1-3 — 落地->验证->Onboarding->升级支付\n\n")

    # Organize entries by URL pattern for readability
    # Group similar URLs together
    groups = {}

    for i, entry in enumerate(unique_entries, 1):
        url = entry['request']['url']
        # Determine group
        if 'claude.ai/edge-api/bootstrap' in url:
            group = 'Bootstrap'
        elif 'claude.ai/api/account' in url:
            group = 'Account'
        elif 'claude.ai/api/organizations' in url and 'subscription' in url:
            group = 'Subscription'
        elif 'claude.ai/api/organizations' in url:
            group = 'Organizations'
        elif 'claude.ai/api/billing' in url:
            group = 'Billing'
        elif 'claude.ai/api/team-signup' in url:
            group = 'Team Signup'
        elif 'claude.ai/api/referral' in url:
            group = 'Referral'
        elif 'a-api.anthropic.com' in url:
            group = 'Anthropic API'
        elif 'a-cdn.anthropic.com' in url:
            group = 'Anthropic CDN'
        elif 'api.anthropic.com' in url:
            group = 'Anthropic API (other)'
        elif 'api.stripe.com' in url:
            group = 'Stripe API'
        elif 'api.hcaptcha.com' in url:
            group = 'hCaptcha'
        elif 'acs.apata.io' in url or 'acs-challenge.apata.io' in url:
            group = 'ACS (3DS)'
        elif 'merchant-ui-api.stripe.com' in url:
            group = 'Stripe Merchant'
        else:
            group = 'Other'

        if group not in groups:
            groups[group] = []
        groups[group].append((i, entry))

    # Output entries grouped
    global_entry_num = 0
    group_order = [
        'Bootstrap', 'Account', 'Organizations', 'Team Signup', 'Referral',
        'Billing', 'Subscription', 'Anthropic API', 'Anthropic CDN',
        'Anthropic API (other)', 'Stripe API', 'Stripe Merchant',
        'hCaptcha', 'ACS (3DS)', 'Other'
    ]

    for group_name in group_order:
        if group_name not in groups:
            continue

        entries_in_group = groups[group_name]
        out_lines.append(f"### {group_name} ({len(entries_in_group)} entries)\n\n")

        for _, entry in entries_in_group:
            global_entry_num += 1
            out_lines.append(extract_entry(entry, global_entry_num))

    # Write output
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write(''.join(out_lines))

    print(f"\nOutput written to: {OUTPUT_PATH}")
    print(f"Total entries written: {global_entry_num}")

    # List all kept entries by group for verification
    print("\n" + "="*80)
    print("KEPT ENTRIES BY GROUP:")
    print("="*80)
    for group_name in group_order:
        if group_name not in groups:
            continue
        entries_in_group = groups[group_name]
        print(f"\n--- {group_name} ---")
        for _, entry in entries_in_group:
            url = entry['request']['url']
            method = entry['request']['method']
            status = entry['response']['status']
            print(f"  {method} {url[:150]} [{status}]")


if __name__ == '__main__':
    main()
