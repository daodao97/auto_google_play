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
const isLogin = computed(() => route.path === "/login");
const titles: Record<string, string> = {
  "/dashboard": "仪表盘",
  "/accounts": "Claude 账号池",
  "/google-accounts": "Google 账号池",
  "/mail-accounts": "邮箱账号池",
  "/registration": "注册机",
  "/cards": "Card 池",
  "/orders": "订单管理",
  "/api-keys": "API Key",
  "/api-docs": "API 文档",
  "/admins": "管理员",
};
async function logout() {
  await auth.logout();
  await router.push("/login");
}
</script>

<template>
  <RouterView v-if="isLogin" />
  <el-container v-else class="shell">
    <el-aside width="232px" class="sidebar">
      <div class="brand">
        <span class="brand-mark">C</span>
        <div><strong>CCMax</strong><small>运营管理后台</small></div>
      </div>
      <el-menu router :default-active="route.path" class="nav">
        <el-menu-item index="/dashboard"
          ><el-icon><DataAnalysis /></el-icon><span>仪表盘</span></el-menu-item
        >
        <el-menu-item index="/mail-accounts"
          ><el-icon><User /></el-icon><span>邮箱账号池</span></el-menu-item
        >
        <el-menu-item index="/registration"
          ><el-icon><Setting /></el-icon><span>注册机</span></el-menu-item
        >
        <el-menu-item index="/accounts"
          ><el-icon><User /></el-icon><span>Claude 账号池</span></el-menu-item
        >
        <el-menu-item index="/google-accounts"
          ><el-icon><User /></el-icon><span>Google 账号池</span></el-menu-item
        >
        <el-menu-item index="/cards"
          ><el-icon><CreditCard /></el-icon><span>Card 池</span></el-menu-item
        >
        <el-menu-item index="/orders"
          ><el-icon><Tickets /></el-icon><span>订单管理</span></el-menu-item
        >
        <el-menu-item index="/api-keys"
          ><el-icon><Key /></el-icon><span>API Key</span></el-menu-item
        >
        <el-menu-item index="/api-docs"
          ><el-icon><Document /></el-icon><span>API 文档</span></el-menu-item
        >
        <el-menu-item v-if="auth.user?.role === 'super_admin'" index="/admins"
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
