<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { http, messageOf } from "@/api";

interface Account {
  id?: number;
  mail: string;
  password: string;
  sessionKey: string;
  plan: string;
  status: number;
  dispatchCount?: number;
  deliveryStatus?: "available" | "locked" | "upgraded" | "sold" | "delivered";
  lockedUntil?: string;
  deliveredAt?: string;
  orderBatchNo?: string;
  createdAt?: string;
  upgradedAt?: string;
  aliveStatus?: "unchecked" | "alive" | "dead";
  aliveCheckedAt?: string;
}
const loading = ref(false),
  items = ref<Account[]>([]),
  total = ref(0),
  dialog = ref(false),
  importDialog = ref(false),
  editing = ref<number>(),
  showSecrets = ref(false),
  selectedIds = ref<number[]>([]),
  checkingIds = ref<Set<number>>(new Set());
const filters = reactive({ q: "", plan: "", status: "" }),
  pager = reactive({ page: 1, size: 20 }),
  form = reactive<Account>({
    mail: "",
    password: "",
    sessionKey: "",
    plan: "free",
    status: 1,
  }),
  importText = ref("");
async function load() {
  loading.value = true;
  try {
    const r = (
      await http.get("/admin/claude-accounts", {
        params: { ...filters, ...pager },
      })
    ).data.data;
    items.value = r.items;
    total.value = r.total;
  } catch (e) {
    ElMessage.error(messageOf(e));
  } finally {
    loading.value = false;
  }
}
function open(item?: Account) {
  editing.value = item?.id;
  Object.assign(
    form,
    item || { mail: "", password: "", sessionKey: "", plan: "free", status: 1 },
  );
  dialog.value = true;
}
async function save() {
  try {
    editing.value
      ? await http.put(`/admin/claude-accounts/${editing.value}`, form)
      : await http.post("/admin/claude-accounts", form);
    ElMessage.success("保存成功");
    dialog.value = false;
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function toggle(item: Account) {
  await ElMessageBox.confirm(
    `确定${item.status === 1 ? "禁用" : "启用"}该账号？`,
  );
  try {
    await http.patch(`/admin/claude-accounts/${item.id}/status`, {
      status: item.status === 1 ? -1 : 1,
    });
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function release(item: Account) {
  await ElMessageBox.confirm(
    `确定释放 ${item.mail} 的下发锁定？释放后可能再次被下发。`,
    "释放租约",
    { type: "warning" },
  );
  try {
    await http.post(`/admin/claude-accounts/${item.id}/release`);
    ElMessage.success("已释放");
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function remove(item: Account) {
  await ElMessageBox.confirm(
    `删除后无法恢复，确定删除 ${item.mail}？`,
    "删除账号",
    { type: "warning", confirmButtonText: "删除", cancelButtonText: "取消" },
  );
  try {
    await http.delete(`/admin/claude-accounts/${item.id}`);
    ElMessage.success("已删除");
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function reset(item: Account) {
  await ElMessageBox.confirm(
    `确定重置 ${item.mail}？计划将恢复为 free，并清除升级、锁定和交付标记。`,
    "重置账号",
    { type: "warning", confirmButtonText: "重置", cancelButtonText: "取消" },
  );
  try {
    await http.post(`/admin/claude-accounts/${item.id}/reset`);
    ElMessage.success("已重置");
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
function deliveryLabel(value?: string) {
  return value === "locked"
    ? "锁定中"
    : value === "upgraded"
      ? "已升级"
      : value === "sold" || value === "delivered"
        ? "已售出"
        : "可下发";
}
function deliveryType(value?: string) {
  return value === "locked"
    ? "warning"
    : value === "upgraded"
      ? "success"
      : value === "sold" || value === "delivered"
        ? "info"
        : "success";
}
function dateTime(value?: string) {
  return value
    ? new Date(value).toLocaleString("zh-CN", { hour12: false })
    : "";
}
function aliveLabel(value?: string) {
  return value === "alive" ? "存活" : value === "dead" ? "失效" : "未检测";
}
function aliveType(value?: string) {
  return value === "alive" ? "success" : value === "dead" ? "danger" : "info";
}
function checking(id?: number) {
  return id !== undefined && checkingIds.value.has(id);
}
function selectionChanged(rows: Account[]) {
  selectedIds.value = rows.flatMap((row) => (row.id ? [row.id] : []));
}
async function checkAlive(ids: number[]) {
  const targets = [...new Set(ids.filter((id) => id > 0))];
  if (!targets.length) {
    ElMessage.warning("请先选择需要探活的账号");
    return;
  }
  checkingIds.value = new Set([...checkingIds.value, ...targets]);
  try {
    const result = (
      await http.post(
        "/admin/claude-accounts/check-alive",
        { ids: targets },
        { timeout: 600000 },
      )
    ).data.data;
    ElMessage.success(
      `检测完成：存活 ${result.alive}，失效 ${result.dead}，共 ${result.total}`,
    );
    await load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  } finally {
    const next = new Set(checkingIds.value);
    targets.forEach((id) => next.delete(id));
    checkingIds.value = next;
  }
}
async function submitImport() {
  const accounts = importText.value
    .split(/\r?\n/)
    .filter(Boolean)
    .map((line) => {
      const [mail, password, sessionKey, plan = "free"] = line.split("----");
      return { mail, password, sessionKey, plan, status: 1 };
    });
  try {
    const r = (await http.post("/admin/claude-accounts/import", { accounts }))
      .data.data;
    ElMessage.success(`新增 ${r.created}，重复 ${r.duplicates}`);
    if (r.errors.length) ElMessage.warning(r.errors.slice(0, 3).join("；"));
    importDialog.value = false;
    importText.value = "";
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
onMounted(load);
</script>
<template>
  <el-card class="page-card"
    ><div class="toolbar">
      <div class="filters">
        <el-input
          v-model="filters.q"
          placeholder="搜索邮箱"
          clearable
          @keyup.enter="load"
        /><el-select
          v-model="filters.plan"
          placeholder="全部计划"
          clearable
          style="width: 140px"
          ><el-option label="Free" value="free" /><el-option
            label="Max 20x"
            value="max_20x" /></el-select
        ><el-select
          v-model="filters.status"
          placeholder="全部状态"
          clearable
          style="width: 130px"
          ><el-option label="启用" value="1" /><el-option
            label="禁用"
            value="-1" /></el-select
        ><el-button @click="load">查询</el-button>
      </div>
      <div class="actions">
        <el-button
          :disabled="selectedIds.length === 0"
          :loading="selectedIds.some((id) => checking(id))"
          @click="checkAlive(selectedIds)"
          >批量探活{{
            selectedIds.length ? ` (${selectedIds.length})` : ""
          }}</el-button
        ><el-switch v-model="showSecrets" active-text="显示密钥" /><el-button
          @click="importDialog = true"
          >批量导入</el-button
        ><el-button type="primary" @click="open()">新增账号</el-button>
      </div>
    </div>
    <el-table
      v-loading="loading"
      :data="items"
      row-key="id"
      @selection-change="selectionChanged"
      ><el-table-column type="selection" width="48" /><el-table-column
        prop="id"
        label="ID"
        width="70"
      /><el-table-column
        prop="mail"
        label="邮箱"
        min-width="190"
      /><el-table-column label="密码" min-width="150"
        ><template #default="{ row }"
          ><span class="mono secret">{{
            showSecrets ? row.password : "••••••••••"
          }}</span></template
        ></el-table-column
      ><el-table-column label="Session Key" min-width="180"
        ><template #default="{ row }"
          ><span class="mono secret">{{
            showSecrets ? row.sessionKey : "••••••••••••••••"
          }}</span></template
        ></el-table-column
      ><el-table-column label="计划" width="110"
        ><template #default="{ row }"
          ><el-tag :type="row.plan === 'max_20x' ? 'success' : 'info'">{{
            row.plan
          }}</el-tag></template
        ></el-table-column
      ><el-table-column
        prop="dispatchCount"
        label="下发次数"
        width="95"
      /><el-table-column label="升级时间" width="175"
        ><template #default="{ row }">{{
          dateTime(row.upgradedAt) || "—"
        }}</template></el-table-column
      ><el-table-column label="流转状态" width="170"
        ><template #default="{ row }"
          ><el-tag :type="deliveryType(row.deliveryStatus)">{{
            deliveryLabel(row.deliveryStatus)
          }}</el-tag>
          <div
            v-if="row.deliveryStatus === 'locked'"
            style="margin-top: 4px; font-size: 11px; color: #909399"
          >
            至 {{ dateTime(row.lockedUntil) }}
          </div></template
        ></el-table-column
      ><el-table-column prop="orderBatchNo" label="关联订单号" min-width="180"
        ><template #default="{ row }"
          ><span class="mono">{{ row.orderBatchNo || "—" }}</span></template
        ></el-table-column
      ><el-table-column label="探活状态" width="175"
        ><template #default="{ row }"
          ><el-tag :type="aliveType(row.aliveStatus)">{{
            aliveLabel(row.aliveStatus)
          }}</el-tag>
          <div
            v-if="row.aliveCheckedAt"
            style="margin-top: 4px; font-size: 11px; color: #909399"
          >
            {{ dateTime(row.aliveCheckedAt) }}
          </div></template
        ></el-table-column
      ><el-table-column label="账号状态" width="90"
        ><template #default="{ row }"
          ><el-tag :type="row.status === 1 ? 'success' : 'danger'">{{
            row.status === 1 ? "启用" : "禁用"
          }}</el-tag></template
        ></el-table-column
      ><el-table-column label="操作" width="350" fixed="right"
        ><template #default="{ row }"
          ><el-button
            link
            type="success"
            :loading="checking(row.id)"
            @click="checkAlive(row.id ? [row.id] : [])"
            >探活</el-button
          ><el-button link type="primary" @click="open(row)">编辑</el-button
          ><el-button
            v-if="row.deliveryStatus === 'locked'"
            link
            type="warning"
            @click="release(row)"
            >释放</el-button
          ><el-button
            link
            :type="row.status === 1 ? 'danger' : 'success'"
            @click="toggle(row)"
            >{{ row.status === 1 ? "禁用" : "启用" }}</el-button
          ><el-button
            link
            type="warning"
            :disabled="
              row.deliveryStatus === 'sold' ||
              row.deliveryStatus === 'delivered'
            "
            :title="
              row.deliveryStatus === 'sold' ||
              row.deliveryStatus === 'delivered'
                ? '已售出账号关联订单，不能重置'
                : ''
            "
            @click="reset(row)"
            >重置</el-button
          ><el-button
            link
            type="danger"
            :disabled="
              row.deliveryStatus === 'sold' ||
              row.deliveryStatus === 'delivered'
            "
            :title="
              row.deliveryStatus === 'sold' ||
              row.deliveryStatus === 'delivered'
                ? '已售出账号关联订单，不能删除'
                : ''
            "
            @click="remove(row)"
            >删除</el-button
          ></template
        ></el-table-column
      ></el-table
    >
    <div class="pagination">
      <el-pagination
        v-model:current-page="pager.page"
        v-model:page-size="pager.size"
        :total="total"
        layout="total, sizes, prev, pager, next"
        @change="load"
      /></div
  ></el-card>
  <el-dialog
    v-model="dialog"
    :title="editing ? '编辑账号' : '新增账号'"
    width="560"
    ><el-form class="dialog-form" label-position="top"
      ><el-form-item label="邮箱"><el-input v-model="form.mail" /></el-form-item
      ><el-form-item label="密码"
        ><el-input v-model="form.password" show-password /></el-form-item
      ><el-form-item label="Session Key"
        ><el-input
          v-model="form.sessionKey"
          type="textarea"
          :rows="3" /></el-form-item
      ><el-row :gutter="16"
        ><el-col :span="12"
          ><el-form-item label="计划"
            ><el-select v-model="form.plan" style="width: 100%"
              ><el-option label="Free" value="free" /><el-option
                label="Max 20x"
                value="max_20x" /></el-select></el-form-item></el-col
        ><el-col :span="12"
          ><el-form-item label="状态"
            ><el-select v-model="form.status" style="width: 100%"
              ><el-option label="启用" :value="1" /><el-option
                label="禁用"
                :value="
                  -1
                " /></el-select></el-form-item></el-col></el-row></el-form
    ><template #footer
      ><el-button @click="dialog = false">取消</el-button
      ><el-button type="primary" @click="save">保存</el-button></template
    ></el-dialog
  >
  <el-dialog v-model="importDialog" title="批量导入 Claude 账号" width="680"
    ><el-alert
      title="每行格式：mail----password----sessionKey----plan；plan 可省略，默认 free。"
      type="info"
      :closable="false"
    /><el-input
      v-model="importText"
      type="textarea"
      :rows="12"
      style="margin-top: 16px"
      placeholder="user@example.com----password----sessionKey----free"
    /><template #footer
      ><el-button @click="importDialog = false">取消</el-button
      ><el-button type="primary" @click="submitImport"
        >开始导入</el-button
      ></template
    ></el-dialog
  >
</template>
