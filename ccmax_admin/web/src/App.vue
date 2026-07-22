<script setup lang="ts">
import { computed } from "vue";
import { useRoute, useRouter } from "vue-router";
import { useAuthStore } from "@/stores/auth";
import {
  DataAnalysis,
  User,
  CreditCard,
  Tickets,
  Key,
  Document,
  Setting,
  SwitchButton,
} from "@element-plus/icons-vue";

const route = useRoute(),
  router = useRouter(),
  auth = useAuthStore();
const isStandalone = computed(() => route.path === "/admin/login" || route.path === "/redeem");
const titles: Record<string, string> = {
  "/admin/dashboard": "仪表盘",
  "/admin/accounts": "Claude 账号池",
  "/admin/google-accounts": "Google 账号池",
  "/admin/mail-accounts": "邮箱账号池",
  "/admin/registration": "注册机",
  "/admin/cards": "Card 池",
  "/admin/orders": "订单管理",
  "/admin/chatgpt-cdks": "ChatGPT CDK",
  "/admin/chatgpt-tasks": "ChatGPT 任务",
  "/admin/api-keys": "API Key",
  "/admin/api-docs": "API 文档",
  "/admin/admins": "管理员",
};
async function logout() {
  await auth.logout();
  await router.push("/admin/login");
}
</script>

<template>
  <RouterView v-if="isStandalone" />
  <el-container v-else class="shell">
    <el-aside width="232px" class="sidebar">
      <div class="brand">
        <span class="brand-mark">C</span>
        <div><strong>CCMax</strong><small>运营管理后台</small></div>
      </div>
      <el-menu router :default-active="route.path" class="nav">
        <el-menu-item index="/admin/dashboard"
          ><el-icon><DataAnalysis /></el-icon><span>仪表盘</span></el-menu-item
        >
        <el-menu-item index="/admin/mail-accounts"
          ><el-icon><User /></el-icon><span>邮箱账号池</span></el-menu-item
        >
        <el-menu-item index="/admin/registration"
          ><el-icon><Setting /></el-icon><span>注册机</span></el-menu-item
        >
        <el-menu-item index="/admin/accounts"
          ><el-icon><User /></el-icon><span>Claude 账号池</span></el-menu-item
        >
        <el-menu-item index="/admin/google-accounts"
          ><el-icon><User /></el-icon><span>Google 账号池</span></el-menu-item
        >
        <el-menu-item index="/admin/cards"
          ><el-icon><CreditCard /></el-icon><span>Card 池</span></el-menu-item
        >
        <el-menu-item index="/admin/orders"
          ><el-icon><Tickets /></el-icon><span>订单管理</span></el-menu-item
        >
        <el-menu-item index="/admin/chatgpt-cdks"
          ><el-icon><Tickets /></el-icon><span>ChatGPT CDK</span></el-menu-item
        >
        <el-menu-item index="/admin/chatgpt-tasks"
          ><el-icon><Document /></el-icon><span>ChatGPT 任务</span></el-menu-item
        >
        <el-menu-item index="/admin/api-keys"
          ><el-icon><Key /></el-icon><span>API Key</span></el-menu-item
        >
        <el-menu-item index="/admin/api-docs"
          ><el-icon><Document /></el-icon><span>API 文档</span></el-menu-item
        >
        <el-menu-item v-if="auth.user?.role === 'super_admin'" index="/admin/admins"
          ><el-icon><Setting /></el-icon><span>管理员</span></el-menu-item
        >
      </el-menu>
      <div class="sidebar-foot">SQLite · Local first</div>
    </el-aside>
    <el-container>
      <el-header class="topbar">
        <div>
          <h1>{{ titles[route.path] || "CCMax" }}</h1>
          <p>库存、订单与交付状态集中管理</p>
        </div>
        <el-dropdown>
          <div class="profile">
            <el-avatar :size="36">{{
              auth.user?.username?.slice(0, 1).toUpperCase()
            }}</el-avatar
            ><span>{{ auth.user?.displayName || auth.user?.username }}</span>
          </div>
          <template #dropdown
            ><el-dropdown-menu
              ><el-dropdown-item :icon="SwitchButton" @click="logout"
                >退出登录</el-dropdown-item
              ></el-dropdown-menu
            ></template
          >
        </el-dropdown>
      </el-header>
      <el-main class="content"><RouterView /></el-main>
    </el-container>
  </el-container>
</template>
