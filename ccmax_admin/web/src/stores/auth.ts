import { defineStore } from 'pinia'
import { ref } from 'vue'
import { http } from '@/api'

export interface Admin { id: number; username: string; displayName: string; role: 'admin' | 'super_admin'; status: number }
export const useAuthStore = defineStore('auth', () => {
  const user = ref<Admin | null>(null)
  async function load() { user.value = (await http.get('/admin/auth/me')).data.data; return user.value }
  async function login(username: string, password: string) { user.value = (await http.post('/admin/auth/login', { username, password })).data.data }
  async function logout() { await http.post('/admin/auth/logout'); user.value = null }
  return { user, load, login, logout }
})
