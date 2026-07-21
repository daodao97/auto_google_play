<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { http, messageOf } from "@/api";

interface GoogleAccount {
  id: number;
  mail: string;
  password: string;
  status: "unused" | "used";
  enabled: 1 | -1;
  dispatchCount: number;
  lastDispatchedAt?: string;
  lockedUntil?: string;
  claudeAccountId?: number;
  claudeAccountMail?: string;
  usedAt?: string;
  createdAt: string;
}

const loading = ref(false);
const items = ref<GoogleAccount[]>([]);
const total = ref(0);
const importDialog = ref(false);
const importing = ref(false);
const showSecrets = ref(false);
const importText = ref("");
const filters = reactive({ q: "", status: "", enabled: "" });
const pager = reactive({ page: 1, size: 20 });
const stats = reactive({ unused: 0, locked: 0, used: 0, total: 0 });

function dateTime(value?: string) {
  return value ? new Date(value).toLocaleString("zh-CN", { hour12: false }) : "—";
}

async function load() {
  loading.value = true;
  try {
    const data = (
      await http.get("/admin/google-accounts", {
        params: { ...filters, ...pager },
      })
    ).data.data;
    items.value = data.items;
    total.value = data.total;
    Object.assign(stats, data.stats);
  } catch (error) {
    ElMessage.error(messageOf(error));
  } finally {
    loading.value = false;
  }
}

async function importAccounts() {
  if (!importText.value.trim()) {
    ElMessage.warning("请输入 Google 账号");
    return;
  }
  importing.value = true;
  try {
    const result = (
      await http.post("/admin/google-accounts/import", {
        lines: importText.value,
      })
    ).data.data;
    ElMessage.success(`新增 ${result.created}，重复 ${result.duplicates}`);
    if (result.errors.length) {
      ElMessage.warning(result.errors.slice(0, 3).join("；"));
    }
    importText.value = "";
    importDialog.value = false;
    pager.page = 1;
    await load();
  } catch (error) {
    ElMessage.error(messageOf(error));
  } finally {
    importing.value = false;
  }
}

async function toggle(item: GoogleAccount) {
  const next = item.enabled === 1 ? -1 : 1;
  await ElMessageBox.confirm(
    `确定${next === 1 ? "启用" : "禁用"} ${item.mail}？`,
    next === 1 ? "启用账号" : "禁用账号",
    { type: next === 1 ? "info" : "warning" },
  );
  try {
    await http.patch(`/admin/google-accounts/${item.id}/status`, {
      enabled: next,
    });
    ElMessage.success(next === 1 ? "已启用" : "已禁用");
    await load();
  } catch (error) {
    ElMessage.error(messageOf(error));
  }
}

async function remove(item: GoogleAccount) {
  await ElMessageBox.confirm(
    `删除后无法恢复，确定删除 ${item.mail}？`,
    "删除 Google 账号",
    {
      type: "warning",
      confirmButtonText: "删除",
      cancelButtonText: "取消",
    },
  );
  try {
    await http.delete(`/admin/google-accounts/${item.id}`);
    ElMessage.success("已删除");
    await load();
  } catch (error) {
    ElMessage.error(messageOf(error));
  }
}

onMounted(load);
</script>

<template>
  <div class="metric-grid" style="margin-bottom: 18px">
    <div class="metric">
      <div class="metric-label">当前可下发</div>
      <div class="metric-value">{{ stats.unused }}</div>
    </div>
    <div class="metric">
      <div class="metric-label">下发锁定中</div>
      <div class="metric-value">{{ stats.locked }}</div>
    </div>
    <div class="metric">
      <div class="metric-label">已使用</div>
      <div class="metric-value">{{ stats.used }}</div>
    </div>
    <div class="metric">
      <div class="metric-label">总账号</div>
      <div class="metric-value">{{ stats.total }}</div>
    </div>
  </div>

  <el-card class="page-card" shadow="never">
    <div class="toolbar">
      <div class="filters">
        <el-input
          v-model="filters.q"
          clearable
          placeholder="搜索邮箱"
          style="width: 240px"
          @keyup.enter="load"
        />
        <el-select
          v-model="filters.status"
          clearable
          placeholder="全部状态"
          style="width: 140px"
        >
          <el-option label="未使用" value="unused" />
          <el-option label="已使用" value="used" />
        </el-select>
        <el-select
          v-model="filters.enabled"
          clearable
          placeholder="启用状态"
          style="width: 130px"
        >
          <el-option label="启用" value="1" />
          <el-option label="禁用" value="-1" />
        </el-select>
        <el-button @click="load">查询</el-button>
      </div>
      <div class="actions">
        <el-switch v-model="showSecrets" active-text="显示密码" />
        <el-button type="primary" @click="importDialog = true">
          批量导入
        </el-button>
      </div>
    </div>

    <el-table v-loading="loading" :data="items" row-key="id">
      <el-table-column prop="id" label="ID" width="70" />
      <el-table-column prop="mail" label="Google 邮箱" min-width="250" />
      <el-table-column label="密码" min-width="150">
        <template #default="{ row }">
          <span class="mono secret">{{
            showSecrets ? row.password : "••••••••••"
          }}</span>
        </template>
      </el-table-column>
      <el-table-column label="状态" width="120">
        <template #default="{ row }">
          <el-tag
            :type="
              row.status === 'used'
                ? 'info'
                : row.lockedUntil
                  ? 'warning'
                  : 'success'
            "
          >
            {{
              row.status === "used"
                ? "已使用"
                : row.lockedUntil
                  ? "下发锁定中"
                  : "未使用"
            }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column label="启用状态" width="100">
        <template #default="{ row }">
          <el-tag :type="row.enabled === 1 ? 'success' : 'danger'">
            {{ row.enabled === 1 ? "启用" : "禁用" }}
          </el-tag>
        </template>
      </el-table-column>
      <el-table-column prop="dispatchCount" label="下发次数" width="95" />
      <el-table-column label="租约到期" width="175">
        <template #default="{ row }">{{ dateTime(row.lockedUntil) }}</template>
      </el-table-column>
      <el-table-column label="关联 Claude 账号" min-width="230">
        <template #default="{ row }">
          {{ row.claudeAccountMail || "—" }}
        </template>
      </el-table-column>
      <el-table-column label="使用时间" width="175">
        <template #default="{ row }">{{ dateTime(row.usedAt) }}</template>
      </el-table-column>
      <el-table-column label="创建时间" width="175">
        <template #default="{ row }">{{ dateTime(row.createdAt) }}</template>
      </el-table-column>
      <el-table-column label="操作" width="150" fixed="right">
        <template #default="{ row }">
          <el-button link type="primary" @click="toggle(row)">
            {{ row.enabled === 1 ? "禁用" : "启用" }}
          </el-button>
          <el-button link type="danger" @click="remove(row)">删除</el-button>
        </template>
      </el-table-column>
    </el-table>

    <div class="pagination">
      <el-pagination
        v-model:current-page="pager.page"
        v-model:page-size="pager.size"
        :total="total"
        :page-sizes="[20, 50, 100, 200]"
        layout="total, sizes, prev, pager, next"
        @change="load"
      />
    </div>
  </el-card>

  <el-dialog v-model="importDialog" title="批量导入 Google 账号" width="680">
    <el-alert
      title="每行一个账号，格式：邮箱|密码；重复邮箱会自动跳过。"
      type="info"
      :closable="false"
      style="margin-bottom: 16px"
    />
    <el-input
      v-model="importText"
      type="textarea"
      :rows="12"
      placeholder="kin8y5dlhrwfcrw@example.com|Soller123@"
    />
    <template #footer>
      <el-button @click="importDialog = false">取消</el-button>
      <el-button type="primary" :loading="importing" @click="importAccounts">
        开始导入
      </el-button>
    </template>
  </el-dialog>
</template>
