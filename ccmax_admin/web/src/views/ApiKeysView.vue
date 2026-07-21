<script setup lang="ts">
import { onMounted, ref } from "vue";
import { ElMessage, ElMessageBox } from "element-plus";
import { http, messageOf } from "@/api";
const items = ref<any[]>([]),
  name = ref(""),
  dialog = ref(false),
  keyDialog = ref(false),
  createdKey = ref("");
async function load() {
  try {
    items.value = (await http.get("/admin/api-keys")).data.data;
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function create() {
  try {
    createdKey.value = (
      await http.post("/admin/api-keys", { name: name.value })
    ).data.data.key;
    keyDialog.value = true;
    dialog.value = false;
    name.value = "";
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function toggle(row: any) {
  await ElMessageBox.confirm(
    `确定${row.status === 1 ? "禁用" : "启用"}该 API Key？`,
  );
  try {
    await http.patch(`/admin/api-keys/${row.id}/status`, {
      status: row.status === 1 ? -1 : 1,
    });
    load();
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
async function copy() {
  await navigator.clipboard.writeText(createdKey.value);
  ElMessage.success("已复制");
}
onMounted(load);
</script>
<template>
  <el-card class="page-card"
    ><div class="toolbar">
      <div>
        <strong>外部接口密钥</strong>
        <div class="muted" style="font-size: 12px; margin-top: 5px">
          完整密钥只在创建时展示一次
        </div>
      </div>
      <el-button type="primary" @click="dialog = true">创建 API Key</el-button>
    </div>
    <el-table :data="items"
      ><el-table-column prop="id" label="ID" width="70" /><el-table-column
        prop="name"
        label="名称"
      /><el-table-column prop="prefix" label="Key 前缀"
        ><template #default="{ row }"
          ><span class="mono">{{ row.prefix }}••••••••</span></template
        ></el-table-column
      ><el-table-column prop="lastUsedAt" label="最后使用" /><el-table-column
        label="状态"
        ><template #default="{ row }"
          ><el-tag :type="row.status === 1 ? 'success' : 'danger'">{{
            row.status === 1 ? "启用" : "禁用"
          }}</el-tag></template
        ></el-table-column
      ><el-table-column label="操作"
        ><template #default="{ row }"
          ><el-button
            link
            :type="row.status === 1 ? 'danger' : 'success'"
            @click="toggle(row)"
            >{{ row.status === 1 ? "禁用" : "启用" }}</el-button
          ></template
        ></el-table-column
      ></el-table
    ></el-card
  ><el-dialog v-model="dialog" title="创建 API Key" width="480"
    ><el-form label-position="top"
      ><el-form-item label="用途名称"
        ><el-input
          v-model="name"
          placeholder="例如：自动升级服务" /></el-form-item></el-form
    ><template #footer
      ><el-button @click="dialog = false">取消</el-button
      ><el-button type="primary" @click="create">创建</el-button></template
    ></el-dialog
  ><el-dialog
    v-model="keyDialog"
    title="请立即保存 API Key"
    width="620"
    :close-on-click-modal="false"
    ><el-alert
      title="关闭后无法再次查看完整密钥。"
      type="warning"
      :closable="false"
    /><el-input
      :model-value="createdKey"
      readonly
      class="mono"
      style="margin-top: 16px"
      ><template #append
        ><el-button @click="copy">复制</el-button></template
      ></el-input
    ></el-dialog
  >
</template>
