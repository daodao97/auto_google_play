# Interlace Money 自动化 Chrome 扩展

功能：

- 在 `interlace.money` 登录页自动填写用户名、密码并点击登录。
- 兼容用户名和密码同时出现、以及先用户名后密码的两步登录页；登录表单动态加载后会立即重试。
- 在页面主世界拦截 `fetch` / `XMLHttpRequest` 请求头中的 `Authorization`。
- 去掉 `Bearer ` 前缀后，上报到：
  `http://38.97.63.31:7788/api/card/verify-code/token`
- 按配置的时间间隔重复上报最新 token。
- 定时请求 Interlace 登录态探活接口；返回 `401` 时刷新页面并重新登录。
- 通过点击扩展图标打开 popup，配置登录、上报和探活参数并查看运行日志。
- popup 提供「开始 / 停止」总开关和标签页监测状态。
- 自动化运行期间若没有 Interlace 标签页，会自动打开 `https://www.interlace.money/app/#/app/dashboard`；关闭标签页或将其导航到其他站点后也会自动补开。
- 多个 Interlace 标签页中只选一个主标签页执行自动登录、token 上报和探活；主标签页关闭后由其他标签页自动接管。

## 安装

1. 打开 Chrome：`chrome://extensions`
2. 开启右上角「开发者模式」
3. 点击「加载已解压的扩展程序」
4. 选择本目录：`interlace-money-extension`
5. 点击扩展图标，打开 popup，填写用户名和密码并保存
6. 点击「打开 Interlace」

## 默认配置

- `source`: `interlace`
- `submitUrl`: `http://38.97.63.31:7788/api/card/verify-code/token`
- `X-API-Key`: 已按需求内置默认值，可在 popup 覆盖
- `reportIntervalSeconds`: `60`
- `probeUrl`: `https://assets-prod.interlace.money/api/task-progress/page`
- `probeIntervalSeconds`: `30`

## 权限范围

扩展只对以下地址生效：

- `https://interlace.money/*`
- `https://www.interlace.money/*`
- `https://assets-prod.interlace.money/*`

上报只允许访问：

- `http://38.97.63.31:7788/*`

## 调试

- 登录页自动填充失败：打开 DevTools Console，查看 `[Interlace Automation]` 日志。
- token 未上报：登录成功后触发一次需要鉴权的页面操作，再查看 Console 中的 `token submit result`。
- 探活状态可直接在 popup 的「运行日志」中查看；正常时显示 HTTP 状态码，`401` 时会记录重新登录动作。
- 如果 Interlace 登录页 input/button 文案改动较大，需要调整 `content.js` 中的选择器匹配逻辑。
