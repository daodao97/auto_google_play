<script setup lang="ts">
import { reactive, ref } from "vue";
import { useRouter } from "vue-router";
import { ElMessage } from "element-plus";
import { useAuthStore } from "@/stores/auth";
import { messageOf } from "@/api";
const router = useRouter(),
  auth = useAuthStore(),
  loading = ref(false),
  form = reactive({ username: "admin", password: "" });
async function submit() {
  loading.value = true;
  try {
    await auth.login(form.username, form.password);
    await router.push("/dashboard");
  } catch (e) {
    ElMessage.error(messageOf(e));
  } finally {
    loading.value = false;
  }
}
</script>
<template>
  <div class="login-page">
    <section class="login-art">
      <span class="brand-mark">C</span>
      <h1>账号交付，清晰而可靠。</h1>
      <p>
        集中管理 Claude 账号、Card Pool、订单交付和外部
        API，所有数据安全保存在本地 SQLite。
      </p>
    </section>
    <section class="login-panel">
      <div class="login-box">
        <h2>欢迎回来</h2>
        <p>登录 CCMax 运营管理后台</p>
        <el-form label-position="top" @keyup.enter="submit"
          ><el-form-item label="用户名"
            ><el-input
              v-model="form.username"
              size="large"
              autocomplete="username" /></el-form-item
          ><el-form-item label="密码"
            ><el-input
              v-model="form.password"
              size="large"
              type="password"
              show-password
              autocomplete="current-password" /></el-form-item
          ><el-button type="primary" :loading="loading" @click="submit"
            >登录</el-button
          ></el-form
        >
      </div>
    </section>
  </div>
</template>
