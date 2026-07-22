import { createRouter, createWebHistory } from 'vue-router'
import { useAuthStore } from '@/stores/auth'
import LoginView from '@/views/LoginView.vue'
import DashboardView from '@/views/DashboardView.vue'
import AccountsView from '@/views/AccountsView.vue'
import GoogleAccountsView from '@/views/GoogleAccountsView.vue'
import MailAccountsView from '@/views/MailAccountsView.vue'
import RegistrationView from '@/views/RegistrationView.vue'
import CardsView from '@/views/CardsView.vue'
import OrdersView from '@/views/OrdersView.vue'
import ApiKeysView from '@/views/ApiKeysView.vue'
import AdminsView from '@/views/AdminsView.vue'
import ApiDocsView from '@/views/ApiDocsView.vue'
import ChatGPTCDKsView from '@/views/ChatGPTCDKsView.vue'
import RedeemView from '@/views/RedeemView.vue'
import ChatGPTTasksView from '@/views/ChatGPTTasksView.vue'

const router = createRouter({ history: createWebHistory(), routes: [
  { path: '/admin/login', component: LoginView, meta: { public: true } },
  { path: '/', component: RedeemView, meta: { guestAllowed: true, title: 'ChatGPT 升级兑换' } },
  { path: '/redeem', component: RedeemView, meta: { guestAllowed: true, title: 'ChatGPT 升级兑换' } },
  { path: '/admin', redirect: '/admin/dashboard' },
  { path: '/admin/dashboard', component: DashboardView },
  { path: '/admin/accounts', component: AccountsView },
  { path: '/admin/google-accounts', component: GoogleAccountsView },
  { path: '/admin/mail-accounts', component: MailAccountsView },
  { path: '/admin/registration', component: RegistrationView },
  { path: '/admin/cards', component: CardsView },
  { path: '/admin/orders', component: OrdersView },
  { path: '/admin/chatgpt-cdks', component: ChatGPTCDKsView },
  { path: '/admin/chatgpt-tasks', component: ChatGPTTasksView },
  { path: '/admin/api-keys', component: ApiKeysView },
  { path: '/admin/api-docs', component: ApiDocsView },
  { path: '/admin/admins', component: AdminsView, meta: { superAdmin: true } },
  { path: '/:pathMatch(.*)*', redirect: '/admin' },
]})
router.beforeEach(async to => {
  const auth = useAuthStore()
  if (to.meta.guestAllowed) return true
  if (to.meta.public) return auth.user ? '/admin/dashboard' : true
  if (!auth.user) { try { await auth.load() } catch { return '/admin/login' } }
  if (to.meta.superAdmin && auth.user?.role !== 'super_admin') return '/admin/dashboard'
  return true
})
router.afterEach(to => {
  document.title = typeof to.meta.title === 'string' ? to.meta.title : 'CCMax 管理后台'
})
export default router
