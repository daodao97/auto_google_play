<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { http, messageOf } from "@/api";
interface Order {
  id: number;
  batchNo: string;
  buyer: string;
  salePriceCents: number;
  quantity: number;
  plan: string;
  status: string;
  createdAt: string;
  downloadCount: number;
  remark: string;
}
const items = ref<Order[]>([]),
  total = ref(0),
  loading = ref(false),
  dialog = ref(false),
  detailDialog = ref(false),
  detail = ref<any>(),
  filters = reactive({ q: "", plan: "", status: "" }),
  pager = reactive({ page: 1, size: 20 }),
  form = reactive({
    batchNo: "",
    buyer: "",
    salePrice: "",
    quantity: 1,
    plan: "max_20x",
    remark: "",
  });
const inventory = reactive({ freeAccounts: 0, maxAccounts: 0 });
const detailText = computed(() => {
  const accounts = detail.value?.accounts;
  if (!Array.isArray(accounts) || accounts.length === 0) return "";
  return `${accounts
    .map(
      (account: any) =>
        `${account.mail}----${account.password}----${account.sessionKey}`,
    )
    .join("\n")}\n`;
});
const availableCount = computed(() =>
  form.plan === "max_20x" ? inventory.maxAccounts : inventory.freeAccounts,
);
const cannotCreate = computed(
  () =>
    !form.batchNo ||
    !form.buyer.trim() ||
    availableCount.value < 1 ||
    form.quantity > availableCount.value,
);
function generateBatchNo() {
  const now = new Date(),
    pad = (value: number) => String(value).padStart(2, "0");
  return `ORD-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}-${Math.random().toString(36).slice(2, 6).toUpperCase()}`;
}
async function loadInventory() {
  try {
    const data = (await http.get("/admin/dashboard")).data.data;
    inventory.freeAccounts = Number(data.freeAccounts || 0);
    inventory.maxAccounts = Number(data.maxAccounts || 0);
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
function openCreate() {
  Object.assign(form, {
    batchNo: generateBatchNo(),
    buyer: "",
    salePrice: "",
    quantity: 1,
    plan: "max_20x",
    remark: "",
  });
  dialog.value = true;
  loadInventory();
}
async function load() {
  loading.value = true;
  try {
    const r = (
      await http.get("/admin/orders", { params: { ...filters, ...pager } })
    ).data.data;
    items.value = r.items;
    total.value = r.total;
  } catch (e) {
    ElMessage.error(messageOf(e));
  } finally {
    loading.value = false;
  }
}
async function create() {
  if (form.quantity > availableCount.value) {
    ElMessage.warning(
      `库存不足，${form.plan} 当前仅可售 ${availableCount.value} 个`,
    );
    return;
  }
  const cents = Math.round(Number(form.salePrice) * 100);
  try {
    await http.post("/admin/orders", {
      batchNo: form.batchNo,
      buyer: form.buyer,
      salePriceCents: cents,
      quantity: form.quantity,
      plan: form.plan,
      remark: form.remark,
    });
    ElMessage.success("订单已创建并完成账号分配");
    dialog.value = false;
    load();
    loadInventory();
  } catch (e) {
    ElMessage.error(messageOf(e));
    loadInventory();
  }
}
async function show(row: Order) {
  try {
    detail.value = (await http.get(`/admin/orders/${row.id}`)).data.data;
    detailDialog.value = true;
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
function download(row: Order) {
  window.open(`/api/admin/orders/${row.id}/download`, "_blank");
}
async function copyDetailAccounts() {
  if (!detailText.value) {
    ElMessage.warning("暂无账号内容可复制");
    return;
  }
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(detailText.value);
    } else {
      const textarea = document.createElement("textarea");
      textarea.value = detailText.value;
      textarea.style.position = "fixed";
      textarea.style.opacity = "0";
      document.body.appendChild(textarea);
      textarea.select();
      const copied = document.execCommand("copy");
      textarea.remove();
      if (!copied) throw new Error("copy failed");
    }
    ElMessage.success("账号内容已复制");
  } catch {
    ElMessage.error("复制失败，请手动选择复制");
  }
}
async function cancel(row: Order) {
  await ElMessageBox.confirm("取消后账号会释放回库存，确定继续？");
  try {
    await http.post(`/admin/orders/${row.id}/cancel`);
    ElMessage.success("订单已取消");
    load();
    loadInventory();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
onMounted(() => {
  load();
  loadInventory();
});
</script>
<template>
  <el-card class="page-card"
    ><div class="toolbar">
      <div class="filters">
        <el-input
          v-model="filters.q"
          placeholder="批次号 / 购买人"
          clearable
        /><el-select v-model="filters.plan" placeholder="全部计划" clearable
          ><el-option label="Free" value="free" /><el-option
            label="Max 20x"
            value="max_20x" /></el-select
        ><el-select v-model="filters.status" placeholder="全部状态" clearable
          ><el-option label="已分配" value="allocated" /><el-option
            label="已取消"
            value="cancelled" /></el-select
        ><el-button @click="load">查询</el-button>
      </div>
      <el-button type="primary" @click="openCreate">新建订单</el-button>
    </div>
    <el-table v-loading="loading" :data="items"
      ><el-table-column
        prop="batchNo"
        label="批次号"
        min-width="160"
      /><el-table-column
        prop="buyer"
        label="购买人"
        min-width="130"
      /><el-table-column label="计划" width="105"
        ><template #default="{ row }"
          ><el-tag :type="row.plan === 'max_20x' ? 'success' : 'info'">{{
            row.plan
          }}</el-tag></template
        ></el-table-column
      ><el-table-column
        prop="quantity"
        label="数量"
        width="75"
      /><el-table-column label="售价" width="110"
        ><template #default="{ row }"
          >¥{{ (row.salePriceCents / 100).toFixed(2) }}</template
        ></el-table-column
      ><el-table-column label="状态" width="95"
        ><template #default="{ row }"
          ><el-tag :type="row.status === 'allocated' ? 'success' : 'info'">{{
            row.status === "allocated" ? "已分配" : "已取消"
          }}</el-tag></template
        ></el-table-column
      ><el-table-column
        prop="downloadCount"
        label="下载次数"
        width="90"
      /><el-table-column
        prop="createdAt"
        label="创建时间"
        min-width="165"
      /><el-table-column label="操作" width="205" fixed="right"
        ><template #default="{ row }"
          ><el-button link @click="show(row)">详情</el-button
          ><el-button
            v-if="row.status === 'allocated'"
            link
            type="primary"
            @click="download(row)"
            >下载 TXT</el-button
          ><el-button
            v-if="row.status === 'allocated' && row.downloadCount === 0"
            link
            type="danger"
            @click="cancel(row)"
            >取消</el-button
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
  <el-dialog v-model="dialog" title="新建订单并分配账号" width="620"
    ><el-form label-position="top"
      ><el-row :gutter="16"
        ><el-col :span="12"
          ><el-form-item label="批次号（自动生成）"
            ><el-input v-model="form.batchNo" readonly /></el-form-item></el-col
        ><el-col :span="12"
          ><el-form-item label="购买人"
            ><el-input v-model="form.buyer" /></el-form-item></el-col></el-row
      ><el-row :gutter="16"
        ><el-col :span="8"
          ><el-form-item label="计划"
            ><el-select v-model="form.plan" style="width: 100%"
              ><el-option
                :label="`Free（可售 ${inventory.freeAccounts}）`"
                value="free" /><el-option
                :label="`Max 20x（可售 ${inventory.maxAccounts}）`"
                value="max_20x" /></el-select></el-form-item></el-col
        ><el-col :span="8"
          ><el-form-item label="数量"
            ><el-input-number
              v-model="form.quantity"
              :min="1"
              :max="Math.max(1, availableCount)"
              :disabled="availableCount === 0"
              style="width: 100%" /></el-form-item></el-col
        ><el-col :span="8"
          ><el-form-item label="售卖总价"
            ><el-input
              v-model="form.salePrice"
              prefix-icon="Money" /></el-form-item></el-col></el-row
      ><el-alert
        v-if="availableCount === 0"
        :title="`${form.plan} 当前没有可售账号，无法创建订单`"
        type="error"
        :closable="false"
        show-icon /><el-alert
        v-else
        :title="`${form.plan} 当前可售 ${availableCount} 个账号`"
        type="success"
        :closable="false"
        show-icon /><el-form-item label="备注" style="margin-top: 16px"
        ><el-input
          v-model="form.remark"
          type="textarea" /></el-form-item></el-form
    ><template #footer
      ><el-button @click="dialog = false">取消</el-button
      ><el-button type="primary" :disabled="cannotCreate" @click="create"
        >创建并分配</el-button
      ></template
    ></el-dialog
  >
  <el-dialog v-model="detailDialog" title="订单账号明细" width="850"
    ><el-descriptions v-if="detail" :column="4" border
      ><el-descriptions-item label="批次号">{{
        detail.order.batchNo
      }}</el-descriptions-item
      ><el-descriptions-item label="购买人">{{
        detail.order.buyer
      }}</el-descriptions-item
      ><el-descriptions-item label="计划">{{
        detail.order.plan
      }}</el-descriptions-item
      ><el-descriptions-item label="数量">{{
        detail.order.quantity
      }}</el-descriptions-item></el-descriptions
    >
    <div v-if="detail" class="order-account-output-head">
      <strong>账号内容（与下载 TXT 格式一致）</strong>
      <el-button
        type="primary"
        plain
        :disabled="!detailText"
        @click="copyDetailAccounts"
        >复制全部</el-button
      >
    </div>
    <pre v-if="detail" class="order-account-output">{{ detailText }}</pre>
    ></el-dialog
  >
</template>
