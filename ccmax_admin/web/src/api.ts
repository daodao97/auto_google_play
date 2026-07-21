import axios from 'axios'

export const http = axios.create({ baseURL: '/api', withCredentials: true, timeout: 15000 })
http.interceptors.response.use(
  response => response,
  error => {
    if (error.response?.status === 401 && !location.pathname.startsWith('/login')) location.href = '/login'
    return Promise.reject(error)
  },
)
export function messageOf(error: any): string {
  return error?.response?.data?.message || error?.message || '请求失败'
}
