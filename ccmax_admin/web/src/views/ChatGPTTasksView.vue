<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { http, messageOf } from "@/api";

const items = ref<any[]>([]), total = ref(0), loading = ref(false), detailDialog = ref(false), detail = ref<any>();
const filters = reactive({ q: "", status: "" });
const pager = reactive({ page: 1, size: 20 });
async function load() {
  loading.value = true;
  try { const data = (await http.get("/admin/chatgpt-tasks", { params: { ...filters, ...pager } })).data.data; items.value = data.items; total.value = data.total; }
  catch (error) { ElMessage.error(messageOf(error)); }
  finally { loading.value = false; }
}
async function show(row: any) {
  try { detail.value = (await http.get(`/admin/chatgpt-tasks/${row.id}`)).data.data; detailDialog.value = true; }
  catch (error) { ElMessage.error(messageOf(error)); }
}
async function copySession() {
  try { await navigator.clipboard.writeText(detail.value?.session || ""); ElMessage.success("Session 已复制"); }
  catch { ElMessage.error("复制失败"); }
}
function statusType(status: string) { return status === "success" ? "success" : ["failed", "create_failed"].includes(status) ? "danger" : status === "processing" ? "warning" : "info"; }
onMounted(load);
</script>

<template>
  <el-card class="page-card">
    <div class="toolbar"><div class="filters"><el-input v-model="filters.q" placeholder="任务 Hash / CDK / 邮箱 / 远程任务 ID" clearable @keyup.enter="load" /><el-select v-model="filters.status" placeholder="全部状态" clearable><el-option label="创建中" value="creating" /><el-option label="排队中" value="pending" /><el-option label="处理中" value="processing" /><el-option label="成功" value="success" /><el-option label="失败" value="failed" /><el-option label="创建失败" value="create_failed" /></el-select><el-button @click="load">查询</el-button></div><el-button @click="load">刷新</el-button></div>
    <el-table v-loading="loading" :data="items">
      <el-table-column prop="id" label="本地 ID" width="85" />
      <el-table-column prop="hashId" label="任务 Hash ID" min-width="245"><template #default="{ row }"><span class="mono">{{ row.hashId }}</span></template></el-table-column>
      <el-table-column prop="cdk" label="CDK" min-width="290"><template #default="{ row }"><span class="mono">{{ row.cdk }}</span></template></el-table-column>
      <el-table-column prop="userEmail" label="用户邮箱" min-width="190" />
      <el-table-column prop="sku" label="SKU" width="90" />
      <el-table-column prop="remoteTaskId" label="远程任务 ID" min-width="230" show-overflow-tooltip><template #default="{ row }"><span class="mono">{{ row.remoteTaskId || '-' }}</span></template></el-table-column>
      <el-table-column label="状态" width="110"><template #default="{ row }"><el-tag :type="statusType(row.status)">{{ row.status }}</el-tag></template></el-table-column>
      <el-table-column prop="errorMessage" label="错误" min-width="180" show-overflow-tooltip />
      <el-table-column prop="createdAt" label="创建时间" min-width="165" />
      <el-table-column label="操作" width="80" fixed="right"><template #default="{ row }"><el-button link type="primary" @click="show(row)">详情</el-button></template></el-table-column>
    </el-table>
    <div class="pagination"><el-pagination v-model:current-page="pager.page" v-model:page-size="pager.size" :total="total" layout="total, sizes, prev, pager, next" @change="load" /></div>
  </el-card>
  <el-dialog v-model="detailDialog" title="ChatGPT 任务详情" width="860">
    <el-descriptions v-if="detail" :column="2" border><el-descriptions-item label="本地任务 ID">{{ detail.task.id }}</el-descriptions-item><el-descriptions-item label="任务 Hash ID"><span class="mono">{{ detail.task.hashId }}</span></el-descriptions-item><el-descriptions-item label="状态"><el-tag :type="statusType(detail.task.status)">{{ detail.task.status }}</el-tag></el-descriptions-item><el-descriptions-item label="用户邮箱">{{ detail.task.userEmail }}</el-descriptions-item><el-descriptions-item label="远程任务 ID"><span class="mono">{{ detail.task.remoteTaskId || '-' }}</span></el-descriptions-item><el-descriptions-item label="CDK"><span class="mono">{{ detail.task.cdk }}</span></el-descriptions-item><el-descriptions-item label="SKU / 渠道">{{ detail.task.sku }} / {{ detail.task.channel }}</el-descriptions-item><el-descriptions-item label="错误码">{{ detail.task.errorCode || '-' }}</el-descriptions-item><el-descriptions-item label="错误信息">{{ detail.task.errorMessage || '-' }}</el-descriptions-item></el-descriptions>
    <div class="order-account-output-head"><strong>Session（敏感信息）</strong><el-button type="primary" link @click="copySession">复制</el-button></div><pre class="history-entry-raw">{{ detail?.session }}</pre>
    <div class="order-account-output-head"><strong>三方响应</strong></div><pre class="history-entry-raw">{{ detail?.task?.resultJson || '-' }}</pre>
  </el-dialog>
</template>
