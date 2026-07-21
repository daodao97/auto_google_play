<script setup lang="ts">
import { onMounted, reactive, ref } from "vue";
import { ElMessage } from "element-plus";
import { http, messageOf } from "@/api";
interface Admin {
  id?: number;
  username: string;
  displayName: string;
  role: string;
  status: number;
  password?: string;
  lastLoginAt?: string;
}
const items = ref<Admin[]>([]),
  dialog = ref(false),
  editing = ref<number>(),
  form = reactive<Admin>({
    username: "",
    displayName: "",
    role: "admin",
    status: 1,
    password: "",
  });
async function load() {
  try {
    items.value = (await http.get("/admin/admin-users")).data.data;
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
}
function open(row?: Admin) {
  editing.value = row?.id;
  Object.assign(
    form,
    row || {
      username: "",
      displayName: "",
      role: "admin",
      status: 1,
      password: "",
    },
  );
  form.password = "";
  dialog.value = true;
}
async function save() {
  try {
    editing.value
      ? await http.put(`/admin/admin-users/${editing.value}`, form)
      : await http.post("/admin/admin-users", form);
    ElMessage.success("保存成功");
    dialog.value = false;
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
      <div>
        <strong>管理员账号</strong>
        <div class="muted" style="font-size: 12px; margin-top: 5px">
          支持多管理员与超级管理员权限隔离
        </div>
      </div>
      <el-button type="primary" @click="open()">新增管理员</el-button>
    </div>
    <el-table :data="items"
      ><el-table-column prop="id" label="ID" width="70" /><el-table-column
        prop="username"
        label="用户名"
      /><el-table-column prop="displayName" label="显示名称" /><el-table-column
        label="角色"
        ><template #default="{ row }"
          ><el-tag :type="row.role === 'super_admin' ? 'warning' : 'info'">{{
            row.role
          }}</el-tag></template
        ></el-table-column
      ><el-table-column label="状态"
        ><template #default="{ row }"
          ><el-tag :type="row.status === 1 ? 'success' : 'danger'">{{
            row.status === 1 ? "启用" : "禁用"
          }}</el-tag></template
        ></el-table-column
      ><el-table-column prop="lastLoginAt" label="最后登录" /><el-table-column
        label="操作"
        ><template #default="{ row }"
          ><el-button link type="primary" @click="open(row)"
            >编辑</el-button
          ></template
        ></el-table-column
      ></el-table
    ></el-card
  ><el-dialog
    v-model="dialog"
    :title="editing ? '编辑管理员' : '新增管理员'"
    width="520"
    ><el-form label-position="top"
      ><el-form-item label="用户名"
        ><el-input
          v-model="form.username"
          :disabled="!!editing" /></el-form-item
      ><el-form-item label="显示名称"
        ><el-input v-model="form.displayName" /></el-form-item
      ><el-form-item :label="editing ? '重置密码（留空不修改）' : '密码'"
        ><el-input
          v-model="form.password"
          type="password"
          show-password /></el-form-item
      ><el-row :gutter="16"
        ><el-col :span="12"
          ><el-form-item label="角色"
            ><el-select v-model="form.role" style="width: 100%"
              ><el-option label="管理员" value="admin" /><el-option
                label="超级管理员"
                value="super_admin" /></el-select></el-form-item></el-col
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
</template>
