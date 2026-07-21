<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { http, messageOf } from "@/api";

interface RegistrationRun {
  runId: string;
  status: "running" | "completed" | "failed";
  platform: string;
  requestedCount: number;
  importedCount: number;
  consumedCount: number;
  startedAt: string;
  finishedAt?: string;
  error?: string;
  summary?: Record<string, number>;
  tasks?: Array<Record<string, any>>;
}

interface RegistrationSchedule {
  enabled: boolean;
  platform: string;
  count: number;
  concurrency: number;
  retryMax: number;
  proxyMode: string;
  proxyTemplate: string;
  mailFastPath: boolean;
  updatedAt?: string;
}

const loading = ref(false);
const starting = ref(false);
const stopping = ref(false);
const savingToken = ref(false);
const savingSchedule = ref(false);
const token = ref("");
const tokenConfigured = ref(false);
const baseUrl = ref("");
const run = ref<RegistrationRun>();
const available = ref(0);
const scheduleEnabled = ref(false);
const scheduleUpdatedAt = ref("");
const form = reactive({
  platform: "mailcom",
  count: 1,
  concurrency: 2,
  retryMax: 2,
  proxyMode: "configured",
  proxyTemplate: "",
  mailFastPath: false,
});
let timer: number | undefined;
let scheduleLoaded = false;

const statusText = computed(() => {
  if (!run.value) return "暂无任务";
  if (run.value.status === "running") return "运行中";
  if (run.value.status === "completed") return "已完成";
  return "失败";
});

function dateTime(value?: string) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "—";
}

function claudeStatus(value?: string) {
  return (
    {
      added: "已添加",
      linked: "已关联现有账号",
      failed: "入库失败",
      not_eligible: "未满足免 KYC",
      not_added: "未添加",
    }[value || ""] || "待处理"
  );
}

function setMaximum() {
  form.count = Math.max(1, Math.min(200, available.value));
}

async function load(showError = true) {
  if (showError) loading.value = true;
  try {
    const requestedPlatform = form.platform;
    const overview = await http.get("/admin/registration", {
      params: { platform: requestedPlatform },
    });
    const data = overview.data.data;
    tokenConfigured.value = data.tokenConfigured;
    baseUrl.value = data.baseUrl;
    run.value = data.run || undefined;
    available.value = data.available;
    if (!scheduleLoaded && data.schedule) {
      const schedule = data.schedule as RegistrationSchedule;
      scheduleEnabled.value = schedule.enabled;
      scheduleUpdatedAt.value = schedule.updatedAt || "";
      form.platform = schedule.platform;
      form.count = schedule.count;
      form.concurrency = schedule.concurrency;
      form.retryMax = schedule.retryMax;
      form.proxyMode = schedule.proxyMode;
      form.proxyTemplate = schedule.proxyTemplate || "";
      form.mailFastPath = schedule.mailFastPath;
      scheduleLoaded = true;
      if (schedule.platform !== requestedPlatform) {
        available.value = (
          await http.get("/admin/registration", {
            params: { platform: schedule.platform },
          })
        ).data.data.available;
      }
    } else if (data.schedule) {
      scheduleEnabled.value = data.schedule.enabled;
      scheduleUpdatedAt.value = data.schedule.updatedAt || "";
    }
  } catch (error) {
    if (showError) ElMessage.error(messageOf(error));
  } finally {
    if (showError) loading.value = false;
    schedulePoll();
  }
}

function schedulePoll() {
  if (timer) window.clearTimeout(timer);
  if (run.value?.status === "running" || scheduleEnabled.value) {
    timer = window.setTimeout(() => load(false), 5000);
  }
}

async function saveSchedule() {
  if (!tokenConfigured.value && scheduleEnabled.value) {
    ElMessage.warning("请先保存注册机 Token");
    return;
  }
  savingSchedule.value = true;
  try {
    const result = await http.put("/admin/registration/schedule", {
      enabled: scheduleEnabled.value,
      ...form,
      proxyTemplate:
        form.proxyMode === "override" ? form.proxyTemplate : "",
    });
    const schedule = result.data.data as RegistrationSchedule;
    scheduleEnabled.value = schedule.enabled;
    scheduleUpdatedAt.value = schedule.updatedAt || "";
    ElMessage.success(schedule.enabled ? "定时任务已开启" : "定时任务已关闭");
    schedulePoll();
  } catch (error) {
    ElMessage.error(messageOf(error));
  } finally {
    savingSchedule.value = false;
  }
}

async function saveToken() {
  if (!token.value.trim()) {
    ElMessage.warning("请输入注册机 WEBUI_TOKEN");
    return;
  }
  savingToken.value = true;
  try {
    await http.put("/admin/channel-credentials/claude_register", {
      token: token.value,
    });
    token.value = "";
    tokenConfigured.value = true;
    ElMessage.success("注册机 Token 已保存");
  } catch (error) {
    ElMessage.error(messageOf(error));
  } finally {
    savingToken.value = false;
  }
}

async function start() {
  if (!tokenConfigured.value) {
    ElMessage.warning("请先保存注册机 Token");
    return;
  }
  if (form.count > available.value) {
    ElMessage.warning(`当前平台只有 ${available.value} 个可用邮箱`);
    return;
  }
  await ElMessageBox.confirm(
    `将从 ${form.platform} 邮箱池锁定 ${form.count} 个账号并启动注册任务，确定继续？`,
    "启动注册机",
    { type: "warning" },
  );
  starting.value = true;
  try {
    run.value = (
      await http.post(
        "/admin/registration/start",
        {
          ...form,
          proxyTemplate:
            form.proxyMode === "override" ? form.proxyTemplate : null,
        },
        { timeout: 60000 },
      )
    ).data.data;
    ElMessage.success("注册任务已启动");
    schedulePoll();
  } catch (error) {
    ElMessage.error(messageOf(error));
  } finally {
    starting.value = false;
  }
}

async function stop() {
  await ElMessageBox.confirm("确定停止当前注册任务？", "停止注册机", {
    type: "warning",
  });
  stopping.value = true;
  try {
    await http.post("/admin/registration/stop", {});
    ElMessage.success("已发送停止请求");
    schedulePoll();
  } catch (error) {
    ElMessage.error(messageOf(error));
  } finally {
    stopping.value = false;
  }
}

onMounted(load);
onBeforeUnmount(() => {
  if (timer) window.clearTimeout(timer);
});
</script>

<template>
  <div class="metric-grid" style="margin-bottom: 18px">
    <div class="metric">
      <div class="metric-label">当前平台可用邮箱</div>
      <div class="metric-value">{{ available }}</div>
    </div>
    <div class="metric">
      <div class="metric-label">任务状态</div>
      <div class="metric-value" style="font-size: 22px">{{ statusText }}</div>
    </div>
    <div class="metric">
      <div class="metric-label">免 KYC 已入库</div>
      <div class="metric-value">{{ run?.importedCount || 0 }}</div>
    </div>
    <div class="metric">
      <div class="metric-label">其他已消费邮箱</div>
      <div class="metric-value">{{ run?.consumedCount || 0 }}</div>
    </div>
  </div>

  <el-row :gutter="18">
    <el-col :xs="24" :lg="9">
      <el-card class="page-card" shadow="never">
        <template #header><strong>注册任务配置</strong></template>
        <el-alert
          :title="
            tokenConfigured
              ? `注册机已配置：${baseUrl}`
              : '请先配置注册机 WEBUI_TOKEN'
          "
          :type="tokenConfigured ? 'success' : 'warning'"
          :closable="false"
          style="margin-bottom: 16px"
        />
        <el-form label-position="top">
          <el-form-item label="注册机 Token">
            <el-input
              v-model="token"
              type="password"
              show-password
              placeholder="WEBUI_TOKEN（保存后不回显）"
            >
              <template #append>
                <el-button :loading="savingToken" @click="saveToken">
                  保存
                </el-button>
              </template>
            </el-input>
          </el-form-item>
          <el-row :gutter="12">
            <el-col :span="12">
              <el-form-item label="邮箱平台">
                <el-select
                  v-model="form.platform"
                  style="width: 100%"
                  @change="load"
                >
                  <el-option label="mailcom" value="mailcom" />
                  <el-option label="imap" value="imap" />
                </el-select>
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="账号数量">
                <div
                  style="display: flex; align-items: center; gap: 8px; width: 100%"
                >
                  <el-input-number
                    v-model="form.count"
                    :min="1"
                    :max="200"
                    style="flex: 1"
                  />
                  <span
                    style="
                      white-space: nowrap;
                      color: var(--el-text-color-secondary);
                    "
                  >
                    可用 {{ available }}
                  </span>
                  <el-button
                    link
                    type="primary"
                    :disabled="available < 1"
                    @click="setMaximum"
                  >
                    最大
                  </el-button>
                </div>
              </el-form-item>
            </el-col>
          </el-row>
          <el-row :gutter="12">
            <el-col :span="12">
              <el-form-item label="并发数">
                <el-input-number
                  v-model="form.concurrency"
                  :min="1"
                  :max="10"
                  style="width: 100%"
                />
              </el-form-item>
            </el-col>
            <el-col :span="12">
              <el-form-item label="重试次数">
                <el-input-number
                  v-model="form.retryMax"
                  :min="0"
                  :max="5"
                  style="width: 100%"
                />
              </el-form-item>
            </el-col>
          </el-row>
          <el-form-item label="代理模式">
            <el-select v-model="form.proxyMode" style="width: 100%">
              <el-option label="沿用注册机配置" value="configured" />
              <el-option label="本次指定代理" value="override" />
              <el-option label="本次直连" value="direct" />
            </el-select>
          </el-form-item>
          <el-form-item
            v-if="form.proxyMode === 'override'"
            label="代理模板"
          >
            <el-input
              v-model="form.proxyTemplate"
              placeholder="支持 {session} 占位符"
            />
          </el-form-item>
          <el-form-item>
            <el-switch
              v-model="form.mailFastPath"
              active-text="启用邮箱快速路径"
            />
          </el-form-item>
          <el-card shadow="never" style="margin-bottom: 16px">
            <div
              style="display: flex; align-items: center; justify-content: space-between"
            >
              <div>
                <div style="font-weight: 600">每分钟自动执行</div>
                <div
                  style="margin-top: 4px; color: var(--el-text-color-secondary)"
                >
                  有可用邮箱时自动启动；已有任务未完成则跳过
                </div>
              </div>
              <el-switch v-model="scheduleEnabled" />
            </div>
            <el-button
              :loading="savingSchedule"
              style="width: 100%; margin-top: 12px"
              @click="saveSchedule"
            >
              保存定时配置
            </el-button>
            <div
              v-if="scheduleUpdatedAt"
              style="margin-top: 8px; color: var(--el-text-color-secondary)"
            >
              上次保存：{{ dateTime(scheduleUpdatedAt) }}
            </div>
          </el-card>
          <el-button
            type="primary"
            :loading="starting"
            :disabled="run?.status === 'running'"
            style="width: 100%"
            @click="start"
          >
            启动注册任务
          </el-button>
        </el-form>
      </el-card>
    </el-col>

    <el-col :xs="24" :lg="15">
      <el-card v-loading="loading" class="page-card" shadow="never">
        <template #header>
          <div style="display: flex; justify-content: space-between">
            <strong>当前任务</strong>
            <el-button
              v-if="run?.status === 'running'"
              type="danger"
              plain
              :loading="stopping"
              @click="stop"
            >
              停止
            </el-button>
          </div>
        </template>
        <el-empty v-if="!run" description="尚未启动注册任务" />
        <template v-else>
          <el-descriptions :column="2" border>
            <el-descriptions-item label="Run ID">
              <span class="mono">{{ run.runId }}</span>
            </el-descriptions-item>
            <el-descriptions-item label="状态">
              {{ statusText }}
            </el-descriptions-item>
            <el-descriptions-item label="邮箱平台">
              {{ run.platform }}
            </el-descriptions-item>
            <el-descriptions-item label="账号数">
              {{ run.requestedCount }}
            </el-descriptions-item>
            <el-descriptions-item label="开始时间">
              {{ dateTime(run.startedAt) }}
            </el-descriptions-item>
            <el-descriptions-item label="完成时间">
              {{ dateTime(run.finishedAt) }}
            </el-descriptions-item>
          </el-descriptions>
          <el-alert
            v-if="run.error"
            :title="run.error"
            type="error"
            :closable="false"
            style="margin-top: 16px"
          />
          <el-alert
            v-if="run.status === 'completed'"
            :title="
              run.importedCount > 0
                ? `已添加或关联 ${run.importedCount} 个 Claude 账号`
                : '任务已完成，没有可添加的免 KYC Claude 账号'
            "
            :type="run.importedCount > 0 ? 'success' : 'warning'"
            :closable="false"
            show-icon
            style="margin-top: 16px"
          />
          <el-table
            v-if="run.tasks?.length"
            :data="run.tasks"
            style="margin-top: 16px"
          >
            <el-table-column prop="email" label="邮箱" min-width="230" />
            <el-table-column prop="status" label="状态" width="100" />
            <el-table-column prop="stage" label="阶段" width="130" />
            <el-table-column prop="kyc_status" label="KYC" width="130" />
            <el-table-column label="Claude 账号" min-width="145">
              <template #default="{ row }">
                <el-tag
                  :type="
                    ['added', 'linked'].includes(row.claude_account_status)
                      ? 'success'
                      : row.claude_account_status === 'failed'
                        ? 'danger'
                        : 'info'
                  "
                >
                  {{ claudeStatus(row.claude_account_status) }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="attempts" label="尝试" width="75" />
            <el-table-column prop="elapsed" label="耗时(s)" width="90" />
          </el-table>
        </template>
      </el-card>
    </el-col>
  </el-row>
</template>
