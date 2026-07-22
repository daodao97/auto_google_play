<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { http, messageOf } from "@/api";

interface CDK {
  id: number;
  code: string;
  sku: "plus" | "pro" | "prolite";
  status: "available" | "redeeming" | "used";
  used: boolean;
  orderNo: string;
  remark: string;
  usedAt?: string;
  taskId?: number;
  createdAt: string;
}

const items = ref<CDK[]>([]), total = ref(0), loading = ref(false), dialog = ref(false), resultDialog = ref(false);
const filters = reactive({ q: "", sku: "", status: "" });
const pager = reactive({ page: 1, size: 20 });
const form = reactive({ sku: "plus", quantity: 10, orderNo: "", remark: "", format: "uuid" });
const generated = ref<CDK[]>([]);
const generatedText = computed(() => generated.value.map(item => item.code).join("\n") + (generated.value.length ? "\n" : ""));

async function load() {
  loading.value = true;
  try {
    const data = (await http.get("/admin/chatgpt-cdks", { params: { ...filters, ...pager } })).data.data;
    items.value = data.items; total.value = data.total;
  } catch (error) { ElMessage.error(messageOf(error)); }
  finally { loading.value = false; }
}
function generateOrderNo() {
  const now = new Date(), pad = (value: number, size = 2) => String(value).padStart(size, "0");
  return `CDK-${now.getFullYear()}${pad(now.getMonth() + 1)}${pad(now.getDate())}-${pad(now.getHours())}${pad(now.getMinutes())}${pad(now.getSeconds())}-${pad(now.getMilliseconds(), 3)}`;
}
function openGenerate() { Object.assign(form, { sku: "plus", quantity: 10, orderNo: generateOrderNo(), remark: "", format: "uuid" }); dialog.value = true; }
async function generate() {
  try {
    const data = (await http.post("/admin/chatgpt-cdks/generate", form)).data.data;
    generated.value = data.items; dialog.value = false; resultDialog.value = true; load();
    ElMessage.success(`已生成 ${data.count} 个 CDK`);
  } catch (error) { ElMessage.error(messageOf(error)); }
}
async function copyGenerated() {
  try { await navigator.clipboard.writeText(generatedText.value); ElMessage.success("已复制全部 CDK"); }
  catch { ElMessage.error("复制失败，请手动选择复制"); }
}
function exportCSV() {
  const params = new URLSearchParams();
  Object.entries(filters).forEach(([key, value]) => { if (value) params.set(key, value); });
  window.open(`/api/admin/chatgpt-cdks/export?${params.toString()}`, "_blank");
}
function statusLabel(status: string) { return ({ available: "可用", redeeming: "兑换中", used: "已使用" } as Record<string,string>)[status] || status; }
onMounted(load);
</script>

<template>
  <el-card class="page-card">
    <div class="toolbar">
      <div class="filters">
        <el-input v-model="filters.q" placeholder="CDK / 订单 / 任务 ID" clearable @keyup.enter="load" />
        <el-select v-model="filters.sku" placeholder="全部套餐" clearable>
          <el-option label="Plus" value="plus" /><el-option label="Pro" value="pro" /><el-option label="Pro Lite" value="prolite" />
        </el-select>
        <el-select v-model="filters.status" placeholder="全部状态" clearable>
          <el-option label="可用" value="available" /><el-option label="兑换中" value="redeeming" /><el-option label="已使用" value="used" />
        </el-select>
        <el-button @click="load">查询</el-button>
      </div>
      <div class="actions"><el-button @click="exportCSV">导出 CSV</el-button><el-button type="primary" @click="openGenerate">生成 CDK</el-button></div>
    </div>
    <el-table v-loading="loading" :data="items">
      <el-table-column prop="id" label="ID" width="72" />
      <el-table-column prop="code" label="CDK" min-width="300"><template #default="{ row }"><span class="mono">{{ row.code }}</span></template></el-table-column>
      <el-table-column prop="sku" label="SKU" width="95"><template #default="{ row }"><el-tag>{{ row.sku }}</el-tag></template></el-table-column>
      <el-table-column label="是否使用" width="100"><template #default="{ row }"><el-tag :type="row.status === 'available' ? 'success' : row.status === 'used' ? 'info' : 'warning'">{{ statusLabel(row.status) }}</el-tag></template></el-table-column>
      <el-table-column prop="orderNo" label="订单号" min-width="200" show-overflow-tooltip />
      <el-table-column prop="remark" label="备注" min-width="150" show-overflow-tooltip />
      <el-table-column prop="taskId" label="本地任务 ID" width="120"><template #default="{ row }"><span class="mono">{{ row.taskId || '-' }}</span></template></el-table-column>
      <el-table-column prop="usedAt" label="使用时间" min-width="165"><template #default="{ row }">{{ row.usedAt || '-' }}</template></el-table-column>
      <el-table-column prop="createdAt" label="创建时间" min-width="165" />
    </el-table>
    <div class="pagination"><el-pagination v-model:current-page="pager.page" v-model:page-size="pager.size" :total="total" layout="total, sizes, prev, pager, next" @change="load" /></div>
  </el-card>

  <el-dialog v-model="dialog" title="批量生成 ChatGPT CDK" width="520">
    <el-form label-position="top">
      <el-form-item label="升级套餐"><el-select v-model="form.sku" style="width:100%"><el-option label="Plus" value="plus" /><el-option label="Pro" value="pro" /><el-option label="Pro Lite" value="prolite" /></el-select></el-form-item>
      <el-form-item label="数量（1～1000）"><el-input-number v-model="form.quantity" :min="1" :max="1000" style="width:100%" /></el-form-item>
      <el-form-item label="订单号（按当前时间生成）"><el-input v-model="form.orderNo" /></el-form-item>
      <el-form-item label="备注"><el-input v-model="form.remark" type="textarea" :rows="3" placeholder="可选备注" /></el-form-item>
      <el-form-item label="CDK 格式"><el-input value="UUID" disabled /></el-form-item>
    </el-form>
    <template #footer><el-button @click="dialog=false">取消</el-button><el-button type="primary" @click="generate">生成</el-button></template>
  </el-dialog>
  <el-dialog v-model="resultDialog" title="CDK 生成结果" width="660">
    <div class="order-account-output-head"><span class="muted">共 {{ generated.length }} 个，请及时复制或导出</span><el-button type="primary" link @click="copyGenerated">复制全部</el-button></div>
    <pre class="order-account-output">{{ generatedText }}</pre>
    <template #footer><el-button type="primary" @click="resultDialog=false">完成</el-button></template>
  </el-dialog>
</template>
