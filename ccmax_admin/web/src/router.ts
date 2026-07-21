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

const router = createRouter({ history: createWebHistory(), routes: [
  { path: '/login', component: LoginView, meta: { public: true } },
  { path: '/', redirect: '/dashboard' },
  { path: '/dashboard', component: DashboardView },
  { path: '/accounts', component: AccountsView },
  { path: '/google-accounts', component: GoogleAccountsView },
  { path: '/mail-accounts', component: MailAccountsView },
  { path: '/registration', component: RegistrationView },
  { path: '/cards', component: CardsView },
  { path: '/orders', component: OrdersView },
  { path: '/api-keys', component: ApiKeysView },
  { path: '/api-docs', component: ApiDocsView },
  { path: '/admins', component: AdminsView, meta: { superAdmin: true } },
  { path: '/:pathMatch(.*)*', redirect: '/dashboard' },
]})
router.beforeEach(async to => {
  const auth = useAuthStore()
  if (to.meta.public) return auth.user ? '/dashboard' : true
  if (!auth.user) { try { await auth.load() } catch { return '/login' } }
  if (to.meta.superAdmin && auth.user?.role !== 'super_admin') return '/dashboard'
  return true
})
export default router
