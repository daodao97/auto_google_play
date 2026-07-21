<script setup lang="ts">
import { onMounted, ref } from "vue";
import { http, messageOf } from "@/api";
import { ElMessage } from "element-plus";
const data = ref<Record<string, number>>({});
onMounted(async () => {
  try {
    data.value = (await http.get("/admin/dashboard")).data.data;
  } catch (e) {
    ElMessage.error(messageOf(e));
  }
});
const metrics = [
  ["freeAccounts", "Free 可售账号", "个"],
  ["maxAccounts", "Max 20x 可售账号", "个"],
  ["availableCards", "可用卡片", "张"],
  ["orders", "累计订单", "单"],
  ["todayDispatches", "今日 API 下发", "次"],
  ["todaySalesCents", "今日销售额", "分"],
];
</script>
<template>
  <div class="metric-grid">
    <article v-for="item in metrics" :key="item[0]" class="metric">
      <div class="metric-label">{{ item[1] }}</div>
      <div class="metric-value">
        {{
          item[0] === "todaySalesCents"
            ? `¥${((data[item[0]] || 0) / 100).toFixed(2)}`
            : data[item[0]] || 0
        }}
      </div>
      <div class="metric-note">当前实时数据 · {{ item[2] }}</div>
    </article>
  </div>
  <el-card class="page-card" style="margin-top: 18px"
    ><template #header><strong>运营提示</strong></template
    ><el-alert
      title="API 下发后账号进入租约锁定；升级成功同步会结束租约并更新计划，处理失败可主动释放，未处理则超时回到库存。"
      type="info"
      :closable="false"
      show-icon
  /></el-card>
</template>
