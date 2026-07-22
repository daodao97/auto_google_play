<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";
import { CircleCheck, Loading, WarningFilled } from "@element-plus/icons-vue";
import { http, messageOf } from "@/api";

type CheckState = "idle" | "checking" | "valid" | "invalid";
type TaskState = "idle" | "submitting" | "pending" | "processing" | "success" | "failed";

const code = ref("");
const session = ref("");
const checkState = ref<CheckState>("idle");
const checkMessage = ref("");
const sku = ref("");
const taskState = ref<TaskState>("idle");
const taskId = ref("");
const taskMessage = ref("");
let checkTimer: number | undefined;
let pollTimer: number | undefined;
let checkVersion = 0;

const canSubmit = computed(() => checkState.value === "valid" && session.value.trim() !== "" && taskState.value === "idle");
const taskRunning = computed(() => ["submitting", "pending", "processing"].includes(taskState.value));

watch(code, value => {
  checkVersion++;
  window.clearTimeout(checkTimer);
  stopPolling();
  sku.value = "";
  checkMessage.value = "";
  taskState.value = "idle";
  taskId.value = "";
  taskMessage.value = "";
  const normalized = value.trim();
  if (!normalized) { checkState.value = "idle"; return; }
  checkState.value = "checking";
  const version = checkVersion;
  checkTimer = window.setTimeout(() => validateCode(normalized, version), 550);
});

async function validateCode(value: string, version: number) {
  try {
    const data = (await http.post("/chatgpt/redeem/check", { code: value })).data.data;
    if (version !== checkVersion) return;
    if (data.available) {
      checkState.value = "valid";
      sku.value = data.sku;
      checkMessage.value = `CDK 有效，可兑换 ChatGPT ${String(data.sku).toUpperCase()}`;
    } else {
      checkState.value = "invalid";
      checkMessage.value = data.used ? "该 CDK 已使用" : "该 CDK 当前不可用";
    }
  } catch (error) {
    if (version !== checkVersion) return;
    checkState.value = "invalid";
    checkMessage.value = messageOf(error);
  }
}

async function submit() {
  if (!canSubmit.value) return;
  try { JSON.parse(session.value); }
  catch { taskMessage.value = "Session 必须是有效的 JSON 内容"; return; }
  taskState.value = "submitting";
  taskMessage.value = "正在提交升级任务…";
  try {
    const data = (await http.post("/chatgpt/redeem/submit", { code: code.value.trim(), channel: "official", session: session.value.trim() })).data.data;
    taskId.value = data.taskId;
    taskState.value = data.status === "processing" ? "processing" : "pending";
    taskMessage.value = "任务已进入升级队列，请保持页面打开";
    schedulePoll(1200);
  } catch (error) {
    taskState.value = "idle";
    taskMessage.value = messageOf(error);
    if ((error as any)?.response?.status === 409) {
      checkState.value = "invalid";
      checkMessage.value = "该 CDK 已被使用或正在兑换";
    }
  }
}

function schedulePoll(delay = 3000) {
  window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(pollTask, delay);
}
async function pollTask() {
  if (!taskId.value) return;
  try {
    const data = (await http.post("/chatgpt/redeem/task", { code: code.value.trim(), taskId: taskId.value })).data.data;
    taskState.value = data.status;
    if (data.status === "success") {
      taskMessage.value = data.result?.message || "ChatGPT 账号升级成功";
      return;
    }
    if (data.status === "failed") {
      taskMessage.value = data.error?.message || "账号升级失败";
      return;
    }
    taskMessage.value = data.status === "processing" ? "正在升级账号，请稍候…" : "任务排队中，请稍候…";
    schedulePoll();
  } catch (error) {
    taskMessage.value = `查询任务状态失败，将自动重试：${messageOf(error)}`;
    schedulePoll(5000);
  }
}
function stopPolling() { window.clearTimeout(pollTimer); pollTimer = undefined; }
onBeforeUnmount(() => { window.clearTimeout(checkTimer); stopPolling(); });
</script>

<template>
  <main class="redeem-page">
    <section class="redeem-card">
      <div class="redeem-brand"><span>C</span><div><strong>ChatGPT 升级兑换</strong><small>安全提交 · 实时查询升级结果</small></div></div>
      <div class="steps"><div class="active"><b>1</b><span>验证 CDK</span></div><i></i><div :class="{ active: checkState === 'valid' }"><b>2</b><span>提交 Session</span></div><i></i><div :class="{ active: taskState !== 'idle' }"><b>3</b><span>完成升级</span></div></div>

      <el-form label-position="top" size="large">
        <el-form-item label="CDK 兑换码">
          <el-input v-model="code" placeholder="请输入 UUID 格式的 CDK" :disabled="taskRunning || taskState === 'success'" clearable>
            <template #suffix><el-icon v-if="checkState === 'checking'" class="is-loading"><Loading /></el-icon><el-icon v-else-if="checkState === 'valid'" color="#16a36a"><CircleCheck /></el-icon><el-icon v-else-if="checkState === 'invalid'" color="#e05252"><WarningFilled /></el-icon></template>
          </el-input>
        </el-form-item>
        <div v-if="checkMessage" class="check-message" :class="checkState"><el-icon><CircleCheck v-if="checkState === 'valid'" /><WarningFilled v-else /></el-icon>{{ checkMessage }}</div>

        <transition name="slide">
          <div v-if="checkState === 'valid'" class="session-block">
            <el-form-item label="ChatGPT Session">
              <el-input v-model="session" type="textarea" :rows="7" resize="vertical" placeholder='粘贴 JSON，例如：{"accessToken":"...","userId":"..."}' :disabled="taskState !== 'idle'" />
            </el-form-item>
            <p class="privacy">Session 仅用于本次升级请求，不会显示在后台列表或审计日志中。</p>
            <el-button type="primary" class="submit" :loading="taskState === 'submitting'" :disabled="!canSubmit" @click="submit">提交升级任务</el-button>
          </div>
        </transition>
      </el-form>

      <div v-if="taskState !== 'idle' || taskMessage" class="task-panel" :class="taskState">
        <div class="task-icon"><el-icon v-if="taskRunning" class="is-loading"><Loading /></el-icon><el-icon v-else-if="taskState === 'success'"><CircleCheck /></el-icon><el-icon v-else><WarningFilled /></el-icon></div>
        <div><strong>{{ taskState === 'success' ? '升级成功' : taskState === 'failed' ? '升级失败' : taskRunning ? '升级处理中' : '提交提示' }}</strong><p>{{ taskMessage }}</p><small v-if="taskId">任务 ID：{{ taskId }}</small></div>
      </div>
    </section>
  </main>
</template>

<style scoped>
.redeem-page{min-height:100vh;display:grid;place-items:center;padding:40px 20px;background:radial-gradient(circle at 18% 15%,rgba(104,91,255,.18),transparent 30%),radial-gradient(circle at 85% 85%,rgba(20,184,166,.13),transparent 28%),#f4f6fa}.redeem-card{width:min(680px,100%);padding:38px 42px;border:1px solid #e4e8ef;border-radius:20px;background:rgba(255,255,255,.96);box-shadow:0 24px 70px rgba(24,35,55,.11)}.redeem-brand{display:flex;align-items:center;gap:14px}.redeem-brand>span{display:grid;place-items:center;width:46px;height:46px;border-radius:13px;color:#fff;background:linear-gradient(135deg,#6557ef,#8e6bff);font-size:23px;font-weight:800}.redeem-brand strong,.redeem-brand small{display:block}.redeem-brand strong{font-size:21px}.redeem-brand small{margin-top:4px;color:#8993a3;font-size:12px}.steps{display:flex;align-items:center;margin:34px 0}.steps div{display:flex;align-items:center;gap:7px;color:#a1a9b5;font-size:12px;white-space:nowrap}.steps b{display:grid;place-items:center;width:24px;height:24px;border-radius:50%;background:#e9ecf1}.steps .active{color:#5d50de}.steps .active b{color:#fff;background:#695cec}.steps i{flex:1;height:1px;margin:0 9px;background:#e1e5eb}.check-message{display:flex;align-items:center;gap:7px;margin:-6px 0 20px;padding:10px 12px;border-radius:8px;font-size:13px}.check-message.valid{color:#117a50;background:#ecf9f3}.check-message.invalid{color:#bb3e3e;background:#fdf0f0}.session-block{padding-top:4px}.privacy{margin:-8px 0 18px;color:#8a94a4;font-size:12px;line-height:1.6}.submit{width:100%;height:46px;font-weight:600}.task-panel{display:flex;gap:14px;margin-top:24px;padding:18px;border-radius:12px;color:#536070;background:#f2f4f8}.task-panel.success{color:#116b49;background:#eaf8f2}.task-panel.failed{color:#ae3636;background:#fceeee}.task-icon{display:grid;place-items:center;flex:none;width:38px;height:38px;border-radius:50%;background:rgba(255,255,255,.72);font-size:22px}.task-panel strong{font-size:15px}.task-panel p{margin:6px 0;color:inherit;font-size:13px;line-height:1.55}.task-panel small{opacity:.7;font-family:monospace}.slide-enter-active,.slide-leave-active{transition:.2s ease}.slide-enter-from,.slide-leave-to{opacity:0;transform:translateY(-8px)}@media(max-width:600px){.redeem-card{padding:28px 22px}.steps span{display:none}}
</style>
