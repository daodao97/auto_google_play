<script setup lang="ts">
import { computed, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";

type EndpointKey =
  | "add"
  | "dispatch"
  | "release"
  | "upgrade"
  | "cardDispatch"
  | "cardReport"
  | "credential"
  | "verify"
  | "googleDispatch"
  | "googleReport"
  | "mailDispatch"
  | "mailReport"
  | "cdkCheck"
  | "cdkRedeem";
interface Endpoint {
  key: EndpointKey;
  title: string;
  method: "POST";
  path: string;
  summary: string;
  sideEffect: boolean;
  headers: Array<{ name: string; required: boolean; description: string }>;
  fields: Array<{
    name: string;
    type: string;
    required: boolean;
    description: string;
  }>;
  request: Record<string, unknown>;
  response: Record<string, unknown>;
}

const endpoints: Endpoint[] = [
  {
    key: "add",
    title: "添加 Claude 普号",
    method: "POST",
    path: "/api/claude_account/add",
    summary:
      "通过 API 添加一个或一批 Claude 普号。接口强制保存为 Free 计划，重复邮箱或 Session Key 会计入 duplicates。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "在“API Key”菜单创建的密钥",
      },
    ],
    fields: [
      {
        name: "accounts",
        type: "array",
        required: false,
        description: "批量账号列表，最多 100 个；与顶层单账号字段二选一",
      },
      {
        name: "mail",
        type: "string",
        required: true,
        description: "Claude 账号邮箱",
      },
      {
        name: "password",
        type: "string",
        required: true,
        description: "Claude 账号密码",
      },
      {
        name: "sessionKey",
        type: "string",
        required: true,
        description: "Claude Session Key",
      },
    ],
    request: {
      accounts: [
        {
          mail: "user@example.com",
          password: "password",
          sessionKey: "session-key",
        },
      ],
    },
    response: { data: { created: 1, duplicates: 0, errors: [], ids: [1] } },
  },
  {
    key: "dispatch",
    title: "下发 Claude Free 账号",
    method: "POST",
    path: "/api/claude_account",
    summary:
      "按指定数量下发可用 Free 账号并创建租约，默认锁定 30 分钟（以响应中的 leaseExpiresAt 为准）。升级成功时调用升级同步接口；处理失败可主动释放。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "在“API Key”菜单创建的密钥",
      },
      {
        name: "Idempotency-Key",
        required: false,
        description: "强烈建议传入；每个业务请求使用唯一值，网络重试时保持不变",
      },
    ],
    fields: [
      {
        name: "count",
        type: "integer",
        required: false,
        description: "下发数量，默认 1，范围 1～100",
      },
      {
        name: "plan",
        type: "string",
        required: false,
        description: "固定为 free，可省略",
      },
    ],
    request: { count: 1, plan: "free" },
    response: {
      data: {
        requestId: "request-001",
        leaseExpiresAt: "2026-07-18T12:30:00+08:00",
        count: 1,
        accounts: [
          {
            mail: "user@example.com",
            password: "password",
            sessionKey: "session-key",
            plan: "free",
          },
        ],
      },
    },
  },
  {
    key: "release",
    title: "释放处理失败的 Claude 账号",
    method: "POST",
    path: "/api/claude_account/release",
    summary:
      "升级或后续处理失败时，主动释放本次下发的一个或多个账号，使其立即回到可用库存。不调用时也会在租约到期后自动释放。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "必须与下发账号时使用同一个 API Key",
      },
    ],
    fields: [
      {
        name: "requestId",
        type: "string",
        required: true,
        description: "下发接口响应中的 requestId",
      },
      {
        name: "mails",
        type: "string[]",
        required: true,
        description: "本次 requestId 下需要释放的账号邮箱，可批量提交",
      },
    ],
    request: { requestId: "request-001", mails: ["user@example.com"] },
    response: { data: { released: 1, errors: [] } },
  },
  {
    key: "upgrade",
    title: "同步 Claude 账号升级结果",
    method: "POST",
    path: "/api/claude_account/upgrade",
    summary:
      "将指定邮箱账号同步为 Max 20x，给本次使用的 Card Pool 卡增加一次使用次数，并将该卡冷却 5 小时；冷却期内不会再次下发。重复提交相同账号和卡不会重复增加次数，但会重新计算冷却时间。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "在“API Key”菜单创建的密钥",
      },
    ],
    fields: [
      {
        name: "mail",
        type: "string",
        required: true,
        description: "已存在的 Claude 账号邮箱",
      },
      {
        name: "plan",
        type: "string",
        required: true,
        description: "必须为 max_20x",
      },
      {
        name: "cardPoolId",
        type: "integer",
        required: true,
        description:
          "本次升级使用的 Card 下发响应中的 cardPoolId；仅用于关联统计，不校验下发归属",
      },
      {
        name: "upgradedAt",
        type: "RFC3339",
        required: false,
        description: "升级时间；省略时使用服务器当前时间",
      },
    ],
    request: { mail: "user@example.com", plan: "max_20x", cardPoolId: 12 },
    response: {
      data: {
        id: 1,
        mail: "user@example.com",
        plan: "max_20x",
        cardPoolId: 12,
        deliveryStatus: "upgraded",
        upgradedAt: "2026-07-18T12:00:00+08:00",
      },
    },
  },
  {
    key: "cardDispatch",
    title: "下发 Card",
    method: "POST",
    path: "/api/card",
    summary:
      "从 Card Pool 下发一张或多张启用且不在冷却期的卡。下发本身不增加使用次数；升级成功上报后，对应卡的使用次数加一并冷却 5 小时。系统按使用次数和最近下发时间均衡选择。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "在“API Key”菜单创建的密钥",
      },
      {
        name: "Idempotency-Key",
        required: false,
        description: "强烈建议传入；相同业务请求重试时保持不变",
      },
    ],
    fields: [
      {
        name: "count",
        type: "integer",
        required: false,
        description: "下发数量，默认 1，范围 1～100",
      },
      {
        name: "source",
        type: "string",
        required: false,
        description: "指定卡来源，例如 qbit；省略时从所有来源选择",
      },
    ],
    request: { count: 1, source: "qbit" },
    response: {
      data: {
        requestId: "card-request-001",
        count: 1,
        cards: [
          {
            cardPoolId: 12,
            source: "qbit",
            cardId: "channel-card-id",
            cardNo: "4111111111111111",
            expireMmyy: "1228",
            ccv: "123",
          },
        ],
      },
    },
  },
  {
    key: "cardReport",
    title: "上报 Card 不可用",
    method: "POST",
    path: "/api/card/report",
    summary:
      "调用方发现卡片失效、拒付或无法继续使用时，将本次下发中的卡标记为不可用。支持批量并可重复提交；不可用卡不会再次下发。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "必须与下发 Card 时使用同一个 API Key",
      },
    ],
    fields: [
      {
        name: "requestId",
        type: "string",
        required: true,
        description: "Card 下发响应中的 requestId",
      },
      {
        name: "cards",
        type: "array",
        required: true,
        description: "需要标记为不可用的卡列表",
      },
      {
        name: "cards[].cardPoolId",
        type: "integer",
        required: true,
        description: "Card 下发响应中的 cardPoolId",
      },
      {
        name: "cards[].status",
        type: "string",
        required: true,
        description: "固定为 unavailable",
      },
      {
        name: "cards[].reason",
        type: "string",
        required: false,
        description: "不可用原因，写入审计日志",
      },
    ],
    request: {
      requestId: "card-request-001",
      cards: [{ cardPoolId: 12, status: "unavailable", reason: "declined" }],
    },
    response: { data: { reported: 1, errors: [] } },
  },
  {
    key: "credential",
    title: "上传验证码渠道凭证",
    method: "POST",
    path: "/api/card/verify-code/token",
    summary:
      "上传 Qbit Bearer Token 或 Slash API Key。使用统一的 X-API-Key 鉴权；Token 会覆盖对应来源的现有凭证且不会在响应中回显。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "在“API Key”菜单创建的密钥",
      },
    ],
    fields: [
      {
        name: "source",
        type: "string",
        required: true,
        description: "渠道名称，例如 qbit、qbit_main 或 slash",
      },
      {
        name: "token",
        type: "string",
        required: true,
        description: "Qbit Bearer Token（不含 Bearer 前缀）或 Slash API Key",
      },
    ],
    request: { source: "qbit", token: "qbit-access-token" },
    response: { data: { source: "qbit", updated: true } },
  },
  {
    key: "verify",
    title: "查询 Card 验证码",
    method: "POST",
    path: "/api/card/verify-code",
    summary:
      "使用 Card 下发接口返回的 cardPoolId 和 Google Reference 查询 Qbit 交易记录或 Slash 卡验证事件，并提取匹配的六位验证码。当前 API Key 必须曾经下发过这张卡。",
    sideEffect: false,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "在“API Key”菜单创建的密钥",
      },
      {
        name: "Fingerprint",
        required: false,
        description: "调用方浏览器指纹，存在时透传给 Qbit",
      },
    ],
    fields: [
      {
        name: "cardPoolId",
        type: "integer",
        required: true,
        description: "Card 下发响应中的 cardPoolId，不是渠道 cardId",
      },
      {
        name: "googleRef",
        type: "string",
        required: true,
        description: "Google 交易引用，例如 BMR",
      },
    ],
    request: { cardPoolId: 12, googleRef: "BMR" },
    response: { data: { status: "ok", code: "123456" } },
  },
  {
    key: "googleDispatch",
    title: "下发 Google 账号",
    method: "POST",
    path: "/api/google_account",
    summary:
      "下发一个未使用的 Google 账号并创建临时租约。已使用账号不会再次下发；相同 requestId 可在租约期内安全重试。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "在“API Key”菜单创建的密钥",
      },
      {
        name: "Idempotency-Key",
        required: false,
        description: "可替代 JSON 中的 requestId；业务请求应使用唯一值",
      },
    ],
    fields: [
      {
        name: "requestId",
        type: "string",
        required: false,
        description: "幂等请求 ID；省略时读取 Idempotency-Key 或由服务端生成",
      },
    ],
    request: { requestId: "google-request-001" },
    response: {
      data: {
        requestId: "google-request-001",
        leaseExpiresAt: "2026-07-19T12:30:00+08:00",
        account: {
          googleAccountId: 21,
          mail: "google@example.com",
          password: "password",
        },
      },
    },
  },
  {
    key: "googleReport",
    title: "上报 Google 账号已使用",
    method: "POST",
    path: "/api/google_account/report",
    summary:
      "将本次下发的 Google 账号永久标记为已使用，并关联已存在的 Claude 账号邮箱。相同关联重复上报是幂等的。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "必须与下发 Google 账号时使用同一个 API Key",
      },
    ],
    fields: [
      {
        name: "requestId",
        type: "string",
        required: true,
        description: "Google 账号下发响应中的 requestId",
      },
      {
        name: "googleAccountId",
        type: "integer",
        required: true,
        description: "下发响应中的 googleAccountId",
      },
      {
        name: "claudeAccountMail",
        type: "string",
        required: true,
        description: "需要关联的已入库 Claude 账号邮箱",
      },
    ],
    request: {
      requestId: "google-request-001",
      googleAccountId: 21,
      claudeAccountMail: "claude@example.com",
    },
    response: {
      data: {
        googleAccountId: 21,
        status: "used",
        claudeAccountId: 10,
        claudeAccountMail: "claude@example.com",
        usedAt: "2026-07-19T12:05:00+08:00",
      },
    },
  },
  {
    key: "mailDispatch",
    title: "下发邮箱账号",
    method: "POST",
    path: "/api/mail_account",
    summary:
      "下发一个未使用的邮箱账号并创建临时租约。可按平台筛选；已使用账号不会再次下发。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "在“API Key”菜单创建的密钥",
      },
      {
        name: "Idempotency-Key",
        required: false,
        description: "可替代 JSON 中的 requestId；业务请求应使用唯一值",
      },
    ],
    fields: [
      {
        name: "requestId",
        type: "string",
        required: false,
        description: "幂等请求 ID；省略时读取 Idempotency-Key 或由服务端生成",
      },
      {
        name: "platform",
        type: "string",
        required: false,
        description: "邮箱平台筛选，例如 mailcom；不填则从全部平台分配",
      },
    ],
    request: { requestId: "mail-request-001", platform: "mailcom" },
    response: {
      data: {
        requestId: "mail-request-001",
        leaseExpiresAt: "2026-07-19T12:30:00+08:00",
        account: {
          mailAccountId: 21,
          mail: "user@mail.com",
          password: "password",
          platform: "mailcom",
        },
      },
    },
  },
  {
    key: "mailReport",
    title: "上报邮箱账号已使用",
    method: "POST",
    path: "/api/mail_account/report",
    summary:
      "将本次下发的邮箱账号永久标记为已使用，并关联已存在的 Claude 账号。重复上报相同关联为幂等成功。",
    sideEffect: true,
    headers: [
      {
        name: "X-API-Key",
        required: true,
        description: "必须与下发邮箱账号时使用同一个 API Key",
      },
    ],
    fields: [
      {
        name: "requestId",
        type: "string",
        required: true,
        description: "邮箱账号下发响应中的 requestId",
      },
      {
        name: "mailAccountId",
        type: "integer",
        required: true,
        description: "下发响应中的 mailAccountId",
      },
      {
        name: "claudeAccountMail",
        type: "string",
        required: true,
        description: "需要关联的已入库 Claude 账号邮箱",
      },
    ],
    request: {
      requestId: "mail-request-001",
      mailAccountId: 21,
      claudeAccountMail: "claude@example.com",
    },
    response: {
      data: {
        mailAccountId: 21,
        status: "used",
        claudeAccountId: 10,
        claudeAccountMail: "claude@example.com",
        usedAt: "2026-07-19T12:05:00+08:00",
      },
    },
  },
  {
    key: "cdkCheck",
    title: "查询 ChatGPT CDK 是否可用",
    method: "POST",
    path: "/api/chatgpt/cdk/check",
    summary: "按 CDK 查询套餐及可用状态。接口只返回兑换状态，不会暴露关联订单或任务错误详情。",
    sideEffect: false,
    headers: [{ name: "X-API-Key", required: true, description: "在“API Key”菜单创建的密钥" }],
    fields: [{ name: "code", type: "string", required: true, description: "后台生成的 UUID CDK" }],
    request: { code: "550e8400-e29b-41d4-a716-446655440000" },
    response: { data: { code: "550e8400-e29b-41d4-a716-446655440000", sku: "pro", available: true, used: false, status: "available" } },
  },
  {
    key: "cdkRedeem",
    title: "提交 ChatGPT CDK 兑换任务",
    method: "POST",
    path: "/api/chatgpt/cdk/redeem",
    summary: "校验并原子占用 CDK，由服务端调用三方升级 API。创建成功后返回本地随机 Hash taskId，不暴露自增 ID 或三方任务 ID；使用 GET /api/chatgpt/cdk/tasks/{taskId} 查询最终状态。",
    sideEffect: true,
    headers: [{ name: "X-API-Key", required: true, description: "在“API Key”菜单创建的密钥" }],
    fields: [
      { name: "code", type: "string", required: true, description: "可用 CDK" },
      { name: "channel", type: "string", required: true, description: "非空升级渠道，如 official" },
      { name: "session", type: "string", required: true, description: "JSON 序列化后的 Session 字符串，不能直接传对象" },
    ],
    request: { code: "550e8400-e29b-41d4-a716-446655440000", channel: "official", session: "{\"accessToken\":\"example_token\",\"userId\":\"123456\"}" },
    response: { data: { taskId: "ctk_2gR8vL7kYpQ4mN6xT1cD9aB3sE0", status: "pending", createdAt: "2026-07-21T11:30:00.000Z" } },
  },
];

const apiKey = ref("");
const idempotencyKey = ref(`docs-${Date.now()}`);
const fingerprint = ref("");
const bodies = reactive<Record<EndpointKey, string>>({
  add: JSON.stringify(endpoints[0].request, null, 2),
  dispatch: JSON.stringify(endpoints[1].request, null, 2),
  release: JSON.stringify(endpoints[2].request, null, 2),
  upgrade: JSON.stringify(endpoints[3].request, null, 2),
  cardDispatch: JSON.stringify(endpoints[4].request, null, 2),
  cardReport: JSON.stringify(endpoints[5].request, null, 2),
  credential: JSON.stringify(endpoints[6].request, null, 2),
  verify: JSON.stringify(endpoints[7].request, null, 2),
  googleDispatch: JSON.stringify(endpoints[8].request, null, 2),
  googleReport: JSON.stringify(endpoints[9].request, null, 2),
  mailDispatch: JSON.stringify(endpoints[10].request, null, 2),
  mailReport: JSON.stringify(endpoints[11].request, null, 2),
  cdkCheck: JSON.stringify(endpoints[12].request, null, 2),
  cdkRedeem: JSON.stringify(endpoints[13].request, null, 2),
});
const results = reactive<
  Record<
    EndpointKey,
    { status?: number; elapsed?: number; body?: string; error?: string }
  >
>({
  add: {},
  dispatch: {},
  release: {},
  upgrade: {},
  cardDispatch: {},
  cardReport: {},
  credential: {},
  verify: {},
  googleDispatch: {},
  googleReport: {},
  mailDispatch: {},
  mailReport: {},
  cdkCheck: {},
  cdkRedeem: {},
});
const loading = reactive<Record<EndpointKey, boolean>>({
  add: false,
  dispatch: false,
  release: false,
  upgrade: false,
  cardDispatch: false,
  cardReport: false,
  credential: false,
  verify: false,
  googleDispatch: false,
  googleReport: false,
  mailDispatch: false,
  mailReport: false,
  cdkCheck: false,
  cdkRedeem: false,
});
const origin = computed(() =>
  typeof location === "undefined" ? "http://127.0.0.1:4001" : location.origin,
);

function curl(endpoint: Endpoint): string {
  const headers = [
    `-H 'X-API-Key: YOUR_API_KEY'`,
    `-H 'Content-Type: application/json'`,
  ];
  if (
    endpoint.key === "dispatch" ||
    endpoint.key === "cardDispatch" ||
    endpoint.key === "googleDispatch" ||
    endpoint.key === "mailDispatch"
  )
    headers.push(`-H 'Idempotency-Key: request-001'`);
  if (endpoint.key === "verify")
    headers.push(`-H 'Fingerprint: optional-fingerprint'`);
  return [
    `curl -X POST '${origin.value}${endpoint.path}' \\`,
    ...headers.map((header) => `  ${header} \\`),
    `  -d '${JSON.stringify(endpoint.request)}'`,
  ].join("\n");
}

async function copy(value: string) {
  await navigator.clipboard.writeText(value);
  ElMessage.success("已复制");
}

async function tryEndpoint(endpoint: Endpoint) {
  if (!apiKey.value.trim()) {
    ElMessage.warning("请先填写 API Key");
    return;
  }
  let payload: unknown;
  try {
    payload = JSON.parse(bodies[endpoint.key]);
  } catch {
    ElMessage.error("请求 JSON 格式不正确");
    return;
  }
  if (endpoint.sideEffect) {
    try {
      await ElMessageBox.confirm(
        `该操作会真实执行“${endpoint.title}”，并修改库存或账号状态。确定继续？`,
        "确认真实请求",
        {
          type: "warning",
          confirmButtonText: "确认执行",
          cancelButtonText: "取消",
        },
      );
    } catch {
      return;
    }
  }
  loading[endpoint.key] = true;
  results[endpoint.key] = {};
  const started = performance.now();
  try {
    const headers: Record<string, string> = {
      "Content-Type": "application/json",
      "X-API-Key": apiKey.value.trim(),
    };
    if (
      endpoint.key === "dispatch" ||
      endpoint.key === "cardDispatch" ||
      endpoint.key === "googleDispatch" ||
      endpoint.key === "mailDispatch"
    )
      headers["Idempotency-Key"] =
        idempotencyKey.value.trim() || `docs-${Date.now()}`;
    if (endpoint.key === "verify" && fingerprint.value.trim())
      headers.Fingerprint = fingerprint.value.trim();
    const response = await fetch(endpoint.path, {
      method: "POST",
      headers,
      body: JSON.stringify(payload),
    });
    const text = await response.text();
    let formatted = text;
    try {
      formatted = JSON.stringify(JSON.parse(text), null, 2);
    } catch {
      /* retain raw response */
    }
    results[endpoint.key] = {
      status: response.status,
      elapsed: Math.round(performance.now() - started),
      body: formatted,
    };
  } catch (error: any) {
    results[endpoint.key] = {
      elapsed: Math.round(performance.now() - started),
      error: error?.message || "请求失败",
    };
  } finally {
    loading[endpoint.key] = false;
  }
}
</script>

<template>
  <div class="docs-layout">
    <el-card class="page-card docs-intro">
      <div class="docs-heading">
        <div>
          <h2>对外 API</h2>
          <p>
            所有接口以 JSON 通信，并统一使用 API Key
            鉴权。在线调试会直接请求当前环境。
          </p>
        </div>
        <el-tag type="success" effect="plain">Base URL: {{ origin }}</el-tag>
      </div>
      <el-alert
        title="安全提示：API Key 仅保存在当前页面内，刷新后会清空。在线调试会真实修改当前环境数据。"
        type="warning"
        :closable="false"
        show-icon
      />
      <section class="lease-guide">
        <div class="lease-title">
          <strong>Claude Free 账号下发状态说明</strong
          ><el-tag type="warning" effect="plain">默认租约 30 分钟</el-tag>
        </div>
        <div class="lease-flow">
          <div>
            <el-tag effect="dark">1. 下发</el-tag>
            <p>
              账号立即进入 <code>locked</code>，响应返回
              <code>requestId</code> 与 <code>leaseExpiresAt</code>。
            </p>
          </div>
          <div>
            <el-tag type="success" effect="dark">2A. 升级成功</el-tag>
            <p>
              调用升级同步接口并传入本次使用的 <code>cardPoolId</code>，账号变为
              <code>upgraded</code> 并结束租约；对应卡冷却 5
              小时，期间不再下发。
            </p>
          </div>
          <div>
            <el-tag type="danger" effect="dark">2B. 处理失败</el-tag>
            <p>
              调用失败释放接口后立即变回
              <code>available</code>，无需等待租约到期。
            </p>
          </div>
          <div>
            <el-tag type="info" effect="dark">2C. 未上报</el-tag>
            <p>
              超过 <code>leaseExpiresAt</code> 后，在下一次库存操作时自动释放回
              <code>available</code>。
            </p>
          </div>
        </div>
        <el-alert
          title="幂等规则"
          description="租约有效期间，相同 Idempotency-Key 会返回原账号；租约已经释放、过期或账号被重新分配后，再使用旧 Idempotency-Key 会返回 409 LEASE_CONFLICT，不会泄露新租约中的账号。"
          type="info"
          :closable="false"
          show-icon
        />
      </section>
      <el-form label-position="top" style="margin-top: 18px"
        ><el-form-item label="调试 API Key"
          ><el-input
            v-model="apiKey"
            type="password"
            show-password
            autocomplete="off"
            placeholder="ccm_xxx" /></el-form-item
      ></el-form>
      <div class="error-grid">
        <div>
          <strong>通用响应</strong>
          <p><code>200</code> 请求成功；业务数据位于 <code>data</code>。</p>
        </div>
        <div>
          <strong>鉴权错误</strong>
          <p><code>401 INVALID_API_KEY</code> 密钥缺失、禁用或无效。</p>
        </div>
        <div>
          <strong>业务错误</strong>
          <p>
            <code>409</code> 库存不足、租约冲突或数据重复；<code>400</code>
            参数错误。
          </p>
        </div>
      </div>
    </el-card>

    <el-card
      v-for="endpoint in endpoints"
      :key="endpoint.key"
      class="page-card endpoint-card"
    >
      <template #header
        ><div class="endpoint-title">
          <div>
            <el-tag type="success" effect="dark">{{ endpoint.method }}</el-tag
            ><code>{{ endpoint.path }}</code
            ><strong>{{ endpoint.title }}</strong>
          </div>
          <el-tag v-if="endpoint.sideEffect" type="warning" effect="plain"
            >写操作</el-tag
          ><el-tag v-else type="info" effect="plain">查询</el-tag>
        </div></template
      >
      <p class="endpoint-summary">{{ endpoint.summary }}</p>
      <el-tabs>
        <el-tab-pane label="接口说明">
          <el-alert
            v-if="endpoint.key === 'dispatch'"
            title="下发成功仅表示临时锁定"
            description="请保存响应中的 requestId。升级成功时调用升级同步接口；处理失败可调用释放接口；未处理会在 leaseExpiresAt 后自动释放。"
            type="warning"
            :closable="false"
            show-icon
          />
          <el-alert
            v-if="endpoint.key === 'release'"
            title="失败释放是可选的"
            description="主动释放能更快归还库存；也可以不调用，系统会在租约到期后的下一次库存操作中自动回收。"
            type="info"
            :closable="false"
            show-icon
          />
          <el-alert
            v-if="endpoint.key === 'cardDispatch'"
            title="请保存 cardPoolId"
            description="验证码接口只接收 cardPoolId，不再传输完整卡号；查询验证码时必须继续使用下发这张卡的同一个 API Key。"
            type="info"
            :closable="false"
            show-icon
          />
          <el-alert
            v-if="endpoint.key === 'cardReport'"
            title="上报后立即停止下发"
            description="Card 状态会变为不可用（status = -1）。重复提交相同上报是安全的，管理员仍可在 Card Pool 中重新启用。"
            type="warning"
            :closable="false"
            show-icon
          />
          <el-alert
            v-if="endpoint.key === 'credential'"
            title="该接口会覆盖渠道凭证"
            description="接口使用统一的 X-API-Key，支持 Qbit 和 Slash。Token 不会在响应、后台列表或审计日志中回显。"
            type="warning"
            :closable="false"
            show-icon
          />
          <h4>请求头</h4>
          <el-table :data="endpoint.headers" size="small" border
            ><el-table-column prop="name" label="名称" width="180"
              ><template #default="{ row }"
                ><code>{{ row.name }}</code></template
              ></el-table-column
            ><el-table-column label="必填" width="80"
              ><template #default="{ row }"
                ><el-tag
                  :type="row.required ? 'danger' : 'info'"
                  size="small"
                  >{{ row.required ? "是" : "否" }}</el-tag
                ></template
              ></el-table-column
            ><el-table-column prop="description" label="说明"
          /></el-table>
          <h4>JSON 参数</h4>
          <el-table :data="endpoint.fields" size="small" border
            ><el-table-column prop="name" label="字段" width="160"
              ><template #default="{ row }"
                ><code>{{ row.name }}</code></template
              ></el-table-column
            ><el-table-column
              prop="type"
              label="类型"
              width="110" /><el-table-column label="必填" width="80"
              ><template #default="{ row }"
                ><el-tag
                  :type="row.required ? 'danger' : 'info'"
                  size="small"
                  >{{ row.required ? "是" : "否" }}</el-tag
                ></template
              ></el-table-column
            ><el-table-column prop="description" label="说明"
          /></el-table>
          <div class="example-grid">
            <div>
              <div class="code-label">请求示例</div>
              <pre>{{ JSON.stringify(endpoint.request, null, 2) }}</pre>
            </div>
            <div>
              <div class="code-label">成功响应</div>
              <pre>{{ JSON.stringify(endpoint.response, null, 2) }}</pre>
            </div>
          </div>
          <div class="curl-block">
            <div class="code-label">
              cURL
              <el-button link type="primary" @click="copy(curl(endpoint))"
                >复制</el-button
              >
            </div>
            <pre>{{ curl(endpoint) }}</pre>
          </div>
        </el-tab-pane>
        <el-tab-pane label="在线调试">
          <el-alert
            v-if="endpoint.sideEffect"
            title="这是写操作，点击发送后会要求再次确认。"
            type="warning"
            :closable="false"
          />
          <el-form label-position="top" class="try-form">
            <el-form-item
              v-if="
                endpoint.key === 'dispatch' ||
                endpoint.key === 'cardDispatch' ||
                endpoint.key === 'googleDispatch' ||
                endpoint.key === 'mailDispatch'
              "
              label="Idempotency-Key"
              ><el-input v-model="idempotencyKey"
            /></el-form-item>
            <el-form-item
              v-if="endpoint.key === 'verify'"
              label="Fingerprint（可选）"
              ><el-input v-model="fingerprint"
            /></el-form-item>
            <el-form-item label="请求 JSON"
              ><el-input
                v-model="bodies[endpoint.key]"
                type="textarea"
                :rows="8"
                class="mono"
            /></el-form-item>
            <el-button
              type="primary"
              :loading="loading[endpoint.key]"
              @click="tryEndpoint(endpoint)"
              >发送请求</el-button
            >
          </el-form>
          <div
            v-if="results[endpoint.key].status || results[endpoint.key].error"
            class="try-result"
          >
            <div class="result-meta">
              <strong>响应结果</strong
              ><span v-if="results[endpoint.key].status"
                >HTTP {{ results[endpoint.key].status }}</span
              ><span>{{ results[endpoint.key].elapsed }} ms</span>
            </div>
            <pre :class="{ 'result-error': results[endpoint.key].error }">{{
              results[endpoint.key].error || results[endpoint.key].body
            }}</pre>
          </div>
        </el-tab-pane>
      </el-tabs>
    </el-card>
  </div>
</template>

<style scoped>
.docs-layout {
  display: grid;
  gap: 18px;
}
.docs-heading,
.endpoint-title,
.endpoint-title > div,
.result-meta,
.lease-title {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
}
.docs-heading h2 {
  margin: 0;
  font-size: 20px;
}
.docs-heading p,
.endpoint-summary {
  color: #737f90;
}
.lease-guide {
  margin-top: 18px;
  padding: 16px;
  border: 1px solid #ebeef5;
  border-radius: 10px;
  background: #fafbfc;
}
.lease-title {
  margin-bottom: 14px;
}
.lease-flow {
  display: grid;
  grid-template-columns: repeat(4, 1fr);
  gap: 10px;
  margin-bottom: 14px;
}
.lease-flow > div {
  padding: 12px;
  border-radius: 8px;
  background: #fff;
}
.lease-flow p {
  margin: 9px 0 0;
  color: #667085;
  font-size: 12px;
  line-height: 1.65;
}
.error-grid {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
}
.error-grid > div {
  padding: 14px;
  background: #f7f8fb;
  border-radius: 8px;
}
.error-grid p {
  margin: 7px 0 0;
  color: #778295;
  font-size: 12px;
}
.endpoint-title > div {
  justify-content: flex-start;
}
.endpoint-title code {
  font-size: 14px;
  color: #354052;
}
.endpoint-title strong {
  margin-left: 8px;
}
.endpoint-summary {
  margin-top: 0;
  line-height: 1.7;
}
.endpoint-card h4 {
  margin: 20px 0 10px;
}
.example-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 14px;
  margin-top: 20px;
}
.code-label {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 7px;
  color: #596579;
  font-size: 12px;
  font-weight: 600;
}
.curl-block {
  margin-top: 14px;
}
pre {
  margin: 0;
  padding: 15px;
  overflow: auto;
  border-radius: 8px;
  background: #111827;
  color: #dbe5f4;
  font:
    12px/1.65 "SFMono-Regular",
    Consolas,
    monospace;
  white-space: pre-wrap;
  word-break: break-word;
}
.try-form {
  margin-top: 16px;
}
.try-result {
  margin-top: 20px;
}
.result-meta {
  justify-content: flex-start;
  margin-bottom: 8px;
  color: #7d8796;
  font-size: 12px;
}
.result-meta strong {
  margin-right: auto;
  color: #303947;
  font-size: 14px;
}
.result-error {
  color: #fca5a5;
}
@media (max-width: 1000px) {
  .lease-flow {
    grid-template-columns: 1fr 1fr;
  }
}
@media (max-width: 700px) {
  .lease-flow,
  .error-grid,
  .example-grid {
    grid-template-columns: 1fr;
  }
  .docs-heading {
    align-items: flex-start;
    flex-direction: column;
  }
}
</style>
