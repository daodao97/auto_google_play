<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { http, messageOf } from "@/api";

function randomSlashCardName() {
  const randomPart = Math.random().toString(36).slice(2, 8);
  return `ccmax-${Date.now().toString(36)}-${randomPart}`;
}

interface Card {
  id?: number;
  source: string;
  cardId: string;
  cardNo: string;
  expireMmyy: string;
  ccv: string;
  usageCount: number;
  status: number;
}
interface ChannelCredential {
  source: string;
  status: number;
}
const items = ref<Card[]>([]),
  total = ref(0),
  loading = ref(false),
  dialog = ref(false),
  importDialog = ref(false),
  slashDialog = ref(false),
  slashCreating = ref(false),
  slashImportDialog = ref(false),
  slashImporting = ref(false),
  slashSourcesLoading = ref(false),
  slashSources = ref<string[]>([]),
  credentialDialog = ref(false),
  historyDialog = ref(false),
  historyLoading = ref(false),
  historyRaw = ref(""),
  historyCard = ref<Card>(),
  editing = ref<number>(),
  showSecrets = ref(false);
const stats = reactive({ available: 0, cooling: 0, total: 0 });
const filters = reactive({ q: "", source: "", status: "" }),
  pager = reactive({ page: 1, size: 20 }),
  form = reactive<Card>({
    source: "qbit",
    cardId: "",
    cardNo: "",
    expireMmyy: "",
    ccv: "",
    usageCount: 0,
    status: 1,
  }),
  batch = reactive({ source: "qbit", lines: "" }),
  credential = reactive({ source: "qbit", token: "" }),
  slashForm = reactive({
    source: "slash",
    name: randomSlashCardName(),
    accountId: "",
    virtualAccountId: "",
    cardGroupId: "card_group_3febhaydgdiq9",
    cardProductId: "",
    legalEntity: "",
    isSingleUse: false,
  }),
  slashImportForm = reactive({
    source: "slash",
    cardId: "",
    legalEntity: "",
  });
const historyRecords = computed<Record<string, any>[]>(() => {
  if (!historyRaw.value) return [];
  try {
    const payload = JSON.parse(historyRaw.value);
    const candidates = [
      payload?.data?.records,
      payload?.data?.items,
      payload?.data?.events,
      payload?.data?.authorizationEvents,
      payload?.data,
      payload?.records,
      payload?.items,
      payload?.events,
      payload?.authorizationEvents,
      payload,
    ];
    const records = candidates.find(Array.isArray);
    return Array.isArray(records)
      ? records.filter((item) => item && typeof item === "object")
      : [];
  } catch {
    return [];
  }
});
function historyField(row: Record<string, any>, paths: string[]) {
  for (const path of paths) {
    let value: any = row;
    for (const key of path.split(".")) value = value?.[key];
    if (value !== undefined && value !== null && value !== "")
      return String(value);
  }
  return "—";
}
function historyTime(row: Record<string, any>) {
  const raw = historyField(row, [
    "transactionTime",
    "authorizedAt",
    "createdAt",
    "occurredAt",
    "timestamp",
    "date",
    "completeTime",
  ]);
  if (
    raw === "—" ||
    !(/T.*(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) || /^\d{13}$/.test(raw))
  )
    return raw;
  const date = new Date(/^\d{13}$/.test(raw) ? Number(raw) : raw);
  if (Number.isNaN(date.getTime())) return raw;
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    hourCycle: "h23",
  }).format(date);
}
function historyType(row: Record<string, any>) {
  return historyField(row, [
    "messageType",
    "transactionType",
    "eventType",
    "type",
    "category",
  ]);
}
function historyDetail(row: Record<string, any>) {
  return historyField(row, [
    "detail",
    "merchantData.description",
    "merchant.description",
    "merchant",
    "merchantDescription",
    "description",
    "memo",
  ]);
}
function historyAmount(row: Record<string, any>) {
  const cents = historyField(row, ["amountCents"]);
  if (cents !== "—") {
    const value = Number(cents);
    if (Number.isFinite(value))
      return `${value < 0 ? "-" : ""}$${(Math.abs(value) / 100).toFixed(2)}`;
  }
  const amount = historyField(row, ["originalAmount", "settleAmount"]);
  if (amount === "—") return amount;
  const currency = historyField(row, [
    "originalCurrency.code",
    "originalCurrency",
  ]);
  return currency === "—" ? amount : `${amount} ${currency}`;
}
function historyStatus(row: Record<string, any>) {
  const reason = historyField(row, ["providerDeclinedReason"]);
  if (reason !== "—") return reason;
  if (typeof row.approvedBySlash === "boolean")
    return row.approvedBySlash ? "Slash 已批准" : "Slash 已拒绝";
  return historyField(row, ["detailedStatus", "status", "state"]);
}
function prettyHistory(row: Record<string, any>) {
  return JSON.stringify(row, null, 2);
}
async function load() {
  loading.value = true;
  try {
    const r = (
      await http.get("/admin/cards", { params: { ...filters, ...pager } })
    ).data.data;
    items.value = r.items;
    total.value = r.total;
    Object.assign(stats, r.stats);
  } catch (e) {
    ElMessage.error(messageOf(e));
  } finally {
    loading.value = false;
  }
}
function open(item?: Card) {
  editing.value = item?.id;
  Object.assign(
    form,
    item || {
      source: "qbit",
      cardId: "",
      cardNo: "",
      expireMmyy: "",
      ccv: "",
      usageCount: 0,
      status: 1,
    },
  );
  dialog.value = true;
}
async function save() {
  try {
    editing.value
      ? await http.put(`/admin/cards/${editing.value}`, form)
      : await http.post("/admin/cards", form);
    dialog.value = false;
    ElMessage.success("保存成功");
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function loadSlashSources() {
  slashSourcesLoading.value = true;
  try {
    const credentials = (
      await http.get("/admin/channel-credentials")
    ).data.data as ChannelCredential[];
    slashSources.value = credentials
      .filter(
        (item) =>
          item.status === 1 && item.source.toLowerCase().startsWith("slash"),
      )
      .map((item) => item.source);
    if (!slashSources.value.length) {
      ElMessage.warning("请先配置至少一个 slash 渠道 Token");
    }
  } catch (e) {
    ElMessage.error(messageOf(e));
  } finally {
    slashSourcesLoading.value = false;
  }
}
function defaultSlashSource() {
  return slashSources.value.includes("slash")
    ? "slash"
    : slashSources.value[0] || "slash";
}
async function openSlashCreate() {
  Object.assign(slashForm, {
    source: "slash",
    name: randomSlashCardName(),
    accountId: "",
    virtualAccountId: "",
    cardGroupId: "card_group_3febhaydgdiq9",
    cardProductId: "",
    legalEntity: "",
    isSingleUse: false,
  });
  slashDialog.value = true;
  await loadSlashSources();
  slashForm.source = defaultSlashSource();
}
async function openSlashImport() {
  Object.assign(slashImportForm, {
    source: "slash",
    cardId: "",
    legalEntity: "",
  });
  slashImportDialog.value = true;
  await loadSlashSources();
  slashImportForm.source = defaultSlashSource();
}
async function createSlashCard() {
  if (!slashSources.value.includes(slashForm.source)) {
    ElMessage.warning("请选择已配置的 Slash 渠道");
    return;
  }
  if (!slashForm.name.trim()) {
    ElMessage.warning("请输入 Slash 卡片名称");
    return;
  }
  slashCreating.value = true;
  try {
    const result = (
      await http.post("/admin/cards/slash-create", slashForm, {
        timeout: 60000,
      })
    ).data.data;
    ElMessage.success(`Slash 卡创建并入库成功：${result.cardId}`);
    slashDialog.value = false;
    await load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  } finally {
    slashCreating.value = false;
  }
}
async function importSlashCard() {
  if (!slashSources.value.includes(slashImportForm.source)) {
    ElMessage.warning("请选择已配置的 Slash 渠道");
    return;
  }
  if (!slashImportForm.cardId.trim()) {
    ElMessage.warning("请输入 Slash Card ID");
    return;
  }
  slashImporting.value = true;
  try {
    const result = (
      await http.post("/admin/cards/slash-import", slashImportForm, {
        timeout: 60000,
      })
    ).data.data;
    ElMessage.success(`Slash 卡快速入库成功：${result.cardId}`);
    slashImportDialog.value = false;
    await load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  } finally {
    slashImporting.value = false;
  }
}
async function toggle(item: Card) {
  await ElMessageBox.confirm(
    `确定${item.status === 1 ? "禁用" : "启用"}这张卡？`,
  );
  try {
    await http.patch(`/admin/cards/${item.id}/status`, {
      status: item.status === 1 ? -1 : 1,
    });
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function importCards() {
  try {
    const r = (await http.post("/admin/cards/import", batch)).data.data;
    ElMessage.success(`新增 ${r.created}，重复 ${r.duplicates}`);
    if (r.errors.length) ElMessage.warning(r.errors.slice(0, 3).join("；"));
    importDialog.value = false;
    batch.lines = "";
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function saveCredential() {
  try {
    await http.put(`/admin/channel-credentials/${credential.source}`, {
      token: credential.token,
    });
    ElMessage.success("渠道 Token 已保存");
    credentialDialog.value = false;
    credential.token = "";
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function viewHistory(item: Card) {
  historyCard.value = item;
  historyRaw.value = "";
  historyDialog.value = true;
  historyLoading.value = true;
  try {
    const r = (await http.get(`/admin/cards/${item.id}/history`)).data.data;
    historyRaw.value = r.raw;
  } catch (e) {
    ElMessage.error(messageOf(e));
  } finally {
    historyLoading.value = false;
  }
}
onMounted(load);
</script>
<template>
  <div
    class="metric-grid"
    style="grid-template-columns: repeat(3, 1fr); margin-bottom: 18px"
  >
    <article class="metric">
      <div class="metric-label">当前可用卡数</div>
      <div class="metric-value">{{ stats.available }}</div>
      <div class="metric-note">启用且不在冷却期</div>
    </article>
    <article class="metric">
      <div class="metric-label">冷却卡数</div>
      <div class="metric-value">{{ stats.cooling }}</div>
      <div class="metric-note">升级成功后冷却 5 小时</div>
    </article>
    <article class="metric">
      <div class="metric-label">总卡数</div>
      <div class="metric-value">{{ stats.total }}</div>
      <div class="metric-note">包含不可用卡片</div>
    </article>
  </div>
  <el-card class="page-card"
    ><div class="toolbar">
      <div class="filters">
        <el-input
          v-model="filters.q"
          placeholder="搜索卡号"
          clearable
        /><el-input
          v-model="filters.source"
          placeholder="来源"
          clearable
        /><el-select
          v-model="filters.status"
          placeholder="全部状态"
          clearable
          style="width: 130px"
          ><el-option label="可用" value="1" /><el-option
            label="不可用"
            value="-1" /></el-select
        ><el-button @click="load">查询</el-button>
      </div>
      <div class="actions">
        <el-switch v-model="showSecrets" active-text="显示卡信息" /><el-button
          @click="credentialDialog = true"
          >渠道 Token</el-button
        ><el-button type="success" plain @click="openSlashCreate"
          >Slash 创建卡</el-button
        ><el-button type="success" plain @click="openSlashImport"
          >Card ID 入库</el-button
        ><el-button @click="importDialog = true">批量导入</el-button
        ><el-button type="primary" @click="open()">新增卡片</el-button>
      </div>
    </div>
    <el-table v-loading="loading" :data="items"
      ><el-table-column prop="id" label="ID" width="65" /><el-table-column
        prop="source"
        label="来源"
        width="110"
      /><el-table-column
        prop="cardId"
        label="Card ID"
        min-width="160"
        show-overflow-tooltip
      /><el-table-column label="卡号" min-width="180"
        ><template #default="{ row }"
          ><span class="mono">{{
            showSecrets ? row.cardNo : `•••• •••• •••• ${row.cardNo.slice(-4)}`
          }}</span></template
        ></el-table-column
      ><el-table-column
        prop="expireMmyy"
        label="MMYY"
        width="85"
      /><el-table-column label="CCV" width="80"
        ><template #default="{ row }"
          ><span class="mono">{{
            showSecrets ? row.ccv : "•••"
          }}</span></template
        ></el-table-column
      ><el-table-column
        prop="usageCount"
        label="使用次数"
        width="90"
      /><el-table-column label="状态" width="90"
        ><template #default="{ row }"
          ><el-tag :type="row.status === 1 ? 'success' : 'danger'">{{
            row.status === 1 ? "可用" : "不可用"
          }}</el-tag></template
        ></el-table-column
      ><el-table-column label="操作" width="205" fixed="right"
        ><template #default="{ row }"
          ><el-button link type="primary" @click="viewHistory(row)"
            >查看</el-button
          ><el-button link type="primary" @click="open(row)">编辑</el-button
          ><el-button
            link
            :type="row.status === 1 ? 'danger' : 'success'"
            @click="toggle(row)"
            >{{ row.status === 1 ? "禁用" : "启用" }}</el-button
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
    :title="editing ? '编辑卡片' : '新增卡片'"
    width="600"
    ><el-form label-position="top"
      ><el-row :gutter="16"
        ><el-col :span="12"
          ><el-form-item label="来源"
            ><el-input v-model="form.source" /></el-form-item></el-col
        ><el-col :span="12"
          ><el-form-item label="Card ID"
            ><el-input v-model="form.cardId" /></el-form-item></el-col></el-row
      ><el-form-item label="卡号"
        ><el-input v-model="form.cardNo" /></el-form-item
      ><el-row :gutter="16"
        ><el-col :span="8"
          ><el-form-item label="MMYY"
            ><el-input
              v-model="form.expireMmyy"
              maxlength="4" /></el-form-item></el-col
        ><el-col :span="8"
          ><el-form-item label="CCV"
            ><el-input
              v-model="form.ccv"
              maxlength="4" /></el-form-item></el-col
        ><el-col :span="8"
          ><el-form-item label="状态"
            ><el-select v-model="form.status"
              ><el-option label="可用" :value="1" /><el-option
                label="不可用"
                :value="
                  -1
                " /></el-select></el-form-item></el-col></el-row></el-form
    ><template #footer
      ><el-button @click="dialog = false">取消</el-button
      ><el-button type="primary" @click="save">保存</el-button></template
    ></el-dialog
  >
  <el-dialog
    v-model="slashDialog"
    title="通过 Slash API 创建虚拟卡"
    width="680"
  >
    <el-alert
      title="创建成功后会从 Slash Vault 读取完整卡号和 CVV，并自动写入 Card Pool。请先在“渠道 Token”中配置 slash API Key。"
      type="info"
      :closable="false"
      show-icon
    />
    <el-form label-position="top" style="margin-top: 16px">
      <el-form-item label="Slash 渠道（必选）">
        <el-select
          v-model="slashForm.source"
          :loading="slashSourcesLoading"
          placeholder="请选择已配置的 Slash 渠道"
          style="width: 100%"
        >
          <el-option
            v-for="source in slashSources"
            :key="source"
            :label="source"
            :value="source"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="卡片名称（必填）">
        <el-input v-model="slashForm.name" />
      </el-form-item>
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="Account ID（推荐）">
            <el-input
              v-model="slashForm.accountId"
              placeholder="留空使用 API Key 的默认商业账户"
            />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="Virtual Account ID（可选）">
            <el-input
              v-model="slashForm.virtualAccountId"
              placeholder="virtual account id"
            />
          </el-form-item>
        </el-col>
      </el-row>
      <el-row :gutter="16">
        <el-col :span="12">
          <el-form-item label="Card Group ID">
            <el-input v-model="slashForm.cardGroupId" />
          </el-form-item>
        </el-col>
        <el-col :span="12">
          <el-form-item label="Card Product ID（可选）">
            <el-input
              v-model="slashForm.cardProductId"
              placeholder="留空由 Slash 自动选择"
            />
          </el-form-item>
        </el-col>
      </el-row>
      <el-form-item label="Legal Entity（User-scoped Key 时填写）">
        <el-input v-model="slashForm.legalEntity" />
      </el-form-item>
      <el-form-item label="单次使用卡">
        <el-switch v-model="slashForm.isSingleUse" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="slashDialog = false">取消</el-button>
      <el-button
        type="primary"
        :loading="slashCreating"
        @click="createSlashCard"
        >创建并入库</el-button
      >
    </template>
  </el-dialog>
  <el-dialog
    v-model="slashImportDialog"
    title="根据 Slash Card ID 快速入库"
    width="620"
  >
    <el-alert
      title="不会创建新卡；系统会根据 card_id 读取卡片详情与 Vault PAN/CVV，然后写入 Card Pool。"
      type="info"
      :closable="false"
      show-icon
    />
    <el-form label-position="top" style="margin-top: 16px">
      <el-form-item label="Slash 渠道（必选）">
        <el-select
          v-model="slashImportForm.source"
          :loading="slashSourcesLoading"
          style="width: 100%"
        >
          <el-option
            v-for="source in slashSources"
            :key="source"
            :label="source"
            :value="source"
          />
        </el-select>
      </el-form-item>
      <el-form-item label="Card ID（必填）">
        <el-input
          v-model="slashImportForm.cardId"
          placeholder="c_1txgprmcrslzw"
        />
      </el-form-item>
      <el-form-item label="Legal Entity（User-scoped Key 时填写）">
        <el-input v-model="slashImportForm.legalEntity" />
      </el-form-item>
    </el-form>
    <template #footer>
      <el-button @click="slashImportDialog = false">取消</el-button>
      <el-button
        type="primary"
        :loading="slashImporting"
        @click="importSlashCard"
      >
        读取并入库
      </el-button>
    </template>
  </el-dialog>
  <el-dialog v-model="importDialog" title="批量导入卡片" width="680"
    ><el-form label-position="top"
      ><el-form-item label="来源"
        ><el-input v-model="batch.source" /></el-form-item
      ><el-alert
        title="支持 MMYY 或 MM/YY；有效期前可包含持卡人姓名"
        type="info"
        :closable="false"
        style="margin-bottom: 16px" /><el-form-item label="卡片数据"
        ><el-input
          v-model="batch.lines"
          type="textarea"
          :rows="12"
          placeholder="6264259812798577  04/29  414  23234234&#10;5177467478887927  Ku Kan  07/29  217  asdfasdf" /></el-form-item></el-form
    ><template #footer
      ><el-button @click="importDialog = false">取消</el-button
      ><el-button type="primary" @click="importCards"
        >开始导入</el-button
      ></template
    ></el-dialog
  >
  <el-dialog v-model="credentialDialog" title="验证码渠道 Token" width="540"
    ><el-alert
      title="Token 只写不读，保存后页面不会回显明文。"
      type="warning"
      :closable="false"
    /><el-form label-position="top" style="margin-top: 16px"
      ><el-form-item label="来源"
        ><el-input v-model="credential.source" /></el-form-item
      ><el-form-item label="Token"
        ><el-input
          v-model="credential.token"
          type="textarea"
          :rows="4" /></el-form-item></el-form
    ><template #footer
      ><el-button @click="credentialDialog = false">取消</el-button
      ><el-button type="primary" @click="saveCredential"
        >保存</el-button
      ></template
    ></el-dialog
  >
  <el-dialog
    v-model="historyDialog"
    :title="`渠道历史 · ${historyCard?.source || ''} · ${historyCard?.cardId || ''}`"
    width="1100"
    ><el-alert
      title="渠道历史记录"
      description="按条展示渠道响应；展开行可查看该条完整 JSON，底部保留未经处理的完整原始响应。"
      type="info"
      :closable="false"
      show-icon
    />
    <div class="history-summary">共 {{ historyRecords.length }} 条记录</div>
    <el-table
      v-loading="historyLoading"
      :data="historyRecords"
      border
      stripe
      max-height="500"
      empty-text="暂无历史记录"
      ><el-table-column type="expand" width="44"
        ><template #default="{ row }">
          <pre class="history-entry-raw">{{ prettyHistory(row) }}</pre>
        </template></el-table-column
      ><el-table-column label="时间" width="175"
        ><template #default="{ row }">{{
          historyTime(row)
        }}</template></el-table-column
      ><el-table-column label="类型" width="145"
        ><template #default="{ row }"
          ><span class="mono">{{ historyType(row) }}</span></template
        ></el-table-column
      ><el-table-column label="描述" min-width="280" show-overflow-tooltip
        ><template #default="{ row }">{{
          historyDetail(row)
        }}</template></el-table-column
      ><el-table-column label="金额（元）" width="140"
        ><template #default="{ row }">{{
          historyAmount(row)
        }}</template></el-table-column
      ><el-table-column label="状态" width="115"
        ><template #default="{ row }"
          ><el-tag size="small" type="info">{{
            historyStatus(row)
          }}</el-tag></template
        ></el-table-column
      ></el-table
    ><el-collapse class="history-source"
      ><el-collapse-item title="查看完整原始响应" name="raw"
        ><div class="history-raw">
          <pre>{{
            historyRaw || (historyLoading ? "加载中…" : "暂无数据")
          }}</pre>
        </div></el-collapse-item
      ></el-collapse
    ><template #footer
      ><el-button @click="historyDialog = false">关闭</el-button></template
    ></el-dialog
  >
</template>
