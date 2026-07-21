package com.automation.app

import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Button
import android.widget.EditText
import android.widget.ScrollView
import android.widget.Switch
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicReference

class MainActivity : AppCompatActivity() {

    private lateinit var tvServerStatus: TextView
    private lateinit var tvA11yStatus: TextView
    private lateinit var tvAccounts: TextView
    private lateinit var tvTaskStatus: TextView
    private lateinit var tvRuntimeTestResult: TextView
    private lateinit var tvLog: TextView
    private lateinit var svLog: ScrollView
    private lateinit var btnRefresh: Button
    private lateinit var btnRemoveAll: Button
    private lateinit var tvRootStatus: TextView
    private lateinit var tvWirelessAdbStatus: TextView
    private lateinit var wirelessDebugManager: WirelessDebugManager
    private lateinit var accountManager: PluginAccountManager
    @Volatile private var wirelessAdbStatusRefreshing = false

    private val handler = Handler(Looper.getMainLooper())
    private val statusUpdater = object : Runnable {
        override fun run() {
            updateStatus()
            handler.postDelayed(this, 2000)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        tvServerStatus = findViewById(R.id.tv_server_status)
        tvA11yStatus = findViewById(R.id.tv_a11y_status)
        tvAccounts = findViewById(R.id.tv_accounts)
        tvTaskStatus = findViewById(R.id.tv_task_status)
        tvRuntimeTestResult = findViewById(R.id.tv_runtime_test_result)
        tvLog = findViewById(R.id.tv_log)
        svLog = findViewById(R.id.sv_log)
        tvRootStatus = findViewById(R.id.tv_root_status)
        tvWirelessAdbStatus = findViewById(R.id.tv_wireless_adb_status)
        btnRefresh = findViewById(R.id.btn_refresh_accounts)
        btnRemoveAll = findViewById(R.id.btn_remove_all)

        val app = application as AutomationApp

        val serviceIntent = Intent(this, AutomationService::class.java)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            startForegroundService(serviceIntent)
        } else {
            startService(serviceIntent)
        }

        accountManager = PluginAccountManager(this)
        wirelessDebugManager = WirelessDebugManager(this)
        app.taskManager.configureRuntime()

        val ip = getLocalIp()
        tvServerStatus.text = "HTTP Server: $ip:8080"

        btnRefresh.setOnClickListener { refreshAccounts() }
        btnRemoveAll.setOnClickListener { removeAllAccounts() }

        val btnStopTask = findViewById<Button>(R.id.btn_stop_task)
        btnStopTask.setOnClickListener {
            app.taskManager.stop()
            android.widget.Toast.makeText(this, "任务已停止", android.widget.Toast.LENGTH_SHORT).show()
        }

        val etPhoneId = findViewById<EditText>(R.id.et_phone_id)
        val etTaskTimeout = findViewById<EditText>(R.id.et_task_timeout)
        val etApiUrl = findViewById<EditText>(R.id.et_api_url)
        val etWsUrl = findViewById<EditText>(R.id.et_ws_url)
        val etApiToken = findViewById<EditText>(R.id.et_api_token)
        val swWirelessDebug = findViewById<Switch>(R.id.sw_wireless_debug)
        val etAdbTcpPort = findViewById<EditText>(R.id.et_adb_tcp_port)
        val tvAdbTcpStatus = findViewById<TextView>(R.id.tv_adb_tcp_status)
        val btnSaveConfig = findViewById<Button>(R.id.btn_save_config)
        val btnTestApi = findViewById<Button>(R.id.btn_test_api)
        val btnTestWs = findViewById<Button>(R.id.btn_test_ws)

        etPhoneId.setText(app.taskManager.getPhoneId())
        etTaskTimeout.setText(app.taskManager.getTaskTimeout().toString())
        app.taskManager.getApiBaseUrl().takeIf { it.isNotEmpty() }?.let { etApiUrl.setText(it) }
        app.taskManager.getWsBaseUrl().takeIf { it.isNotEmpty() }?.let { etWsUrl.setText(it) }
        app.taskManager.getApiToken().takeIf { it.isNotEmpty() }?.let { etApiToken.setText(it) }
        swWirelessDebug.isChecked = wirelessDebugManager.isEnabled()
        etAdbTcpPort.setText(wirelessDebugManager.getPort().toString())
        etAdbTcpPort.isEnabled = swWirelessDebug.isChecked
        swWirelessDebug.setOnCheckedChangeListener { _, enabled ->
            etAdbTcpPort.isEnabled = enabled
            tvAdbTcpStatus.text = if (enabled) "保存配置后会固定 ADB TCP 端口" else "无线调试固定端口未启用"
        }
        refreshWirelessDebugStatus(wirelessDebugManager, tvAdbTcpStatus)
        refreshWirelessAdbServiceStatus(force = true)

        btnSaveConfig.setOnClickListener {
            saveRuntimeConfig(app, etPhoneId, etTaskTimeout, etApiUrl, etWsUrl, etApiToken, swWirelessDebug, etAdbTcpPort, wirelessDebugManager)
            android.widget.Toast.makeText(this, "运行配置已保存", android.widget.Toast.LENGTH_SHORT).show()
            applyWirelessDebugConfig(wirelessDebugManager, tvAdbTcpStatus)
        }

        btnTestApi.setOnClickListener {
            saveRuntimeConfig(app, etPhoneId, etTaskTimeout, etApiUrl, etWsUrl, etApiToken, swWirelessDebug, etAdbTcpPort, wirelessDebugManager)
            tvRuntimeTestResult.text = "API 测试中..."
            Thread {
                val result = testRemoteApi(
                    apiBaseUrl = etApiUrl.text.toString().trim(),
                    apiToken = etApiToken.text.toString().trim(),
                    phoneId = etPhoneId.text.toString().trim()
                )
                handler.post { tvRuntimeTestResult.text = result }
            }.start()
        }

        btnTestWs.setOnClickListener {
            saveRuntimeConfig(app, etPhoneId, etTaskTimeout, etApiUrl, etWsUrl, etApiToken, swWirelessDebug, etAdbTcpPort, wirelessDebugManager)
            tvRuntimeTestResult.text = "WS 测试中..."
            Thread {
                val result = testRemoteWs(
                    apiBaseUrl = etApiUrl.text.toString().trim(),
                    wsBaseUrl = etWsUrl.text.toString().trim(),
                    apiToken = etApiToken.text.toString().trim(),
                    phoneId = etPhoneId.text.toString().trim()
                )
                handler.post { tvRuntimeTestResult.text = result }
            }.start()
        }

        checkRootPermission()
        handler.post(statusUpdater)

        svLog.setOnTouchListener { v, _ ->
            v.parent.requestDisallowInterceptTouchEvent(true)
            false
        }

        TaskLogger.onLogUpdated = { lines ->
            tvLog.text = lines.joinToString("\n")
            svLog.post { svLog.fullScroll(android.view.View.FOCUS_DOWN) }
        }
        val existingLogs = TaskLogger.getAll()
        if (existingLogs.isNotEmpty()) tvLog.text = existingLogs.joinToString("\n")
    }

    override fun onDestroy() {
        handler.removeCallbacks(statusUpdater)
        TaskLogger.onLogUpdated = null
        super.onDestroy()
    }

    private fun saveRuntimeConfig(
        app: AutomationApp,
        etPhoneId: EditText,
        etTaskTimeout: EditText,
        etApiUrl: EditText,
        etWsUrl: EditText,
        etApiToken: EditText,
        swWirelessDebug: Switch,
        etAdbTcpPort: EditText,
        wirelessDebugManager: WirelessDebugManager
    ) {
        val taskTimeout = etTaskTimeout.text.toString().toIntOrNull() ?: TaskManager.DEFAULT_TASK_TIMEOUT
        val adbPort = etAdbTcpPort.text.toString().toIntOrNull() ?: WirelessDebugManager.DEFAULT_ADB_TCP_PORT
        app.taskManager.setPhoneId(etPhoneId.text.toString().trim())
        app.taskManager.setTaskTimeout(taskTimeout)
        app.taskManager.setApiBaseUrl(etApiUrl.text.toString().trim())
        app.taskManager.setWsBaseUrl(etWsUrl.text.toString().trim())
        app.taskManager.setApiToken(etApiToken.text.toString().trim())
        wirelessDebugManager.setEnabled(swWirelessDebug.isChecked)
        wirelessDebugManager.setPort(adbPort)
        etAdbTcpPort.setText(wirelessDebugManager.getPort().toString())
        app.taskManager.configureRuntime()
    }

    private fun applyWirelessDebugConfig(manager: WirelessDebugManager, statusView: TextView) {
        if (!manager.isEnabled()) {
            statusView.text = "无线调试固定端口未启用"
            return
        }
        statusView.text = "无线调试设置中..."
        Thread {
            val result = manager.enforceIfEnabled()
            handler.post {
                statusView.text = if (result.detail.isNotEmpty()) {
                    "${result.message}\n${result.detail}"
                } else {
                    result.message
                }
                refreshWirelessAdbServiceStatus(force = true)
            }
        }.start()
    }

    private fun refreshWirelessAdbServiceStatus(force: Boolean = false) {
        if (!force && wirelessAdbStatusRefreshing) return
        if (wirelessAdbStatusRefreshing) return
        wirelessAdbStatusRefreshing = true
        Thread {
            val status = try {
                wirelessDebugManager.readServiceStatusText()
            } catch (e: Exception) {
                "无线 ADB: 状态读取失败: ${e.message}"
            }
            handler.post {
                tvWirelessAdbStatus.text = status
                wirelessAdbStatusRefreshing = false
            }
        }.start()
    }

    private fun refreshWirelessDebugStatus(manager: WirelessDebugManager, statusView: TextView) {
        if (!manager.isEnabled()) {
            statusView.text = "无线调试固定端口未启用"
            return
        }
        statusView.text = "无线调试状态检查中..."
        Thread {
            val text = manager.readStatusText()
            handler.post { statusView.text = text }
        }.start()
    }

    private fun updateStatus() {
        val a11y = AutoAccessibilityService.instance != null
        tvA11yStatus.text = if (a11y) "无障碍服务: 已连接 ✓" else "无障碍服务: 未连接 ✗"
        val app = application as AutomationApp
        val status = app.taskManager.getStatus()
        val state = status["state"] as? String ?: "idle"
        val stepName = status["step_name"] as? String ?: ""
        val stepIdx = status["current_step"] ?: 0
        val totalSteps = status["total_steps"] ?: 0
        tvTaskStatus.text = when (state) {
            "idle" -> "空闲"
            "running" -> "运行中: [$stepIdx/$totalSteps] $stepName"
            "paused" -> "已暂停: [$stepIdx/$totalSteps] $stepName"
            "completed" -> "已完成"
            "failed" -> "失败: $stepName"
            "stopped" -> "已停止"
            else -> state
        }
    }

    private fun refreshAccounts() {
        tvAccounts.text = "查询中..."
        btnRefresh.isEnabled = false
        accountManager.listAccounts { success, accounts, message ->
            handler.post {
                btnRefresh.isEnabled = true
                tvAccounts.text = if (success) {
                    if (accounts.isEmpty()) "无已登录账号" else accounts.mapIndexed { i, email -> "${i + 1}. $email" }.joinToString("\n")
                } else {
                    "查询失败: $message\n(确认 GMS Hook 已安装并激活)"
                }
            }
        }
    }

    private fun removeAllAccounts() {
        tvAccounts.text = "移除中..."
        btnRemoveAll.isEnabled = false
        accountManager.removeAllAccounts { success, message ->
            handler.post {
                btnRemoveAll.isEnabled = true
                tvAccounts.text = if (success) "$message\n\n点击刷新查看" else "失败: $message"
            }
        }
    }

    private fun testRemoteApi(apiBaseUrl: String, apiToken: String, phoneId: String): String {
        if (apiBaseUrl.isEmpty()) return "API 测试失败: 缺少 API 地址"
        if (apiToken.isEmpty()) return "API 测试失败: 缺少密码"
        val deviceId = phoneId.ifEmpty { "apk_api_test" }
        return try {
            val encoded = URLEncoder.encode(deviceId, "UTF-8")
            val url = URL("${apiBaseUrl.trimEnd('/')}/api/devices/commands?device_id=$encoded")
            val conn = url.openConnection() as HttpURLConnection
            conn.requestMethod = "GET"
            conn.setRequestProperty("Authorization", "Bearer $apiToken")
            conn.connectTimeout = 5000
            conn.readTimeout = 5000
            val code = conn.responseCode
            val body = try {
                conn.inputStream.bufferedReader().readText()
            } catch (e: Exception) {
                conn.errorStream?.bufferedReader()?.readText().orEmpty()
            }
            conn.disconnect()
            if (code in 200..299) "API 正常: HTTP $code" else "API 异常: HTTP $code\n$body"
        } catch (e: Exception) {
            "API 测试失败: ${e.message}"
        }
    }

    private fun testRemoteWs(apiBaseUrl: String, wsBaseUrl: String, apiToken: String, phoneId: String): String {
        if (apiToken.isEmpty()) return "WS 测试失败: 缺少密码"
        val resolvedWsBaseUrl = wsBaseUrl.trimEnd('/').ifEmpty { deriveWsBaseUrl(apiBaseUrl) }
        if (resolvedWsBaseUrl.isEmpty()) return "WS 测试失败: 缺少 WS 地址，且无法从 API 地址推导"
        val deviceId = phoneId.ifEmpty { "apk_ws_test" }
        val encodedDeviceId = URLEncoder.encode(deviceId, "UTF-8")
        val encodedToken = URLEncoder.encode(apiToken, "UTF-8")
        val target = "$resolvedWsBaseUrl/ws/devices/$encodedDeviceId?token=$encodedToken"
        val latch = CountDownLatch(1)
        val result = AtomicReference("WS 测试超时: $target")
        val client = OkHttpClient.Builder()
            .connectTimeout(5, TimeUnit.SECONDS)
            .readTimeout(5, TimeUnit.SECONDS)
            .build()
        val request = Request.Builder()
            .url(target)
            .addHeader("Authorization", "Bearer $apiToken")
            .build()
        val socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                result.set("WS 正常: ${response.code} $resolvedWsBaseUrl")
                webSocket.close(1000, "test_complete")
                latch.countDown()
            }

            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                val code = response?.code?.let { "HTTP $it " } ?: ""
                result.set("WS 异常: $code${t.message ?: "连接失败"}")
                latch.countDown()
            }
        })
        return try {
            latch.await(8, TimeUnit.SECONDS)
            socket.cancel()
            result.get()
        } finally {
            client.dispatcher.executorService.shutdown()
        }
    }

    private fun deriveWsBaseUrl(apiBaseUrl: String): String {
        if (apiBaseUrl.isEmpty()) return ""
        return try {
            val url = URL(apiBaseUrl)
            if (url.protocol == "https") {
                val port = if (url.port > 0) ":${url.port}" else ""
                "wss://${url.host}$port"
            } else {
                val port = if (url.port > 0 && url.port != 4399) ":${url.port}" else ":4400"
                "ws://${url.host}$port"
            }
        } catch (_: Exception) {
            ""
        }
    }

    private fun checkRootPermission() {
        tvRootStatus.text = "Root 权限: 检测中..."
        Thread {
            val hasRoot = try {
                val process = Runtime.getRuntime().exec(arrayOf("su", "-c", "id"))
                val output = process.inputStream.bufferedReader().readText().trim()
                val exitCode = process.waitFor()
                exitCode == 0 && output.contains("uid=0")
            } catch (e: Exception) {
                false
            }
            handler.post {
                if (hasRoot) {
                    tvRootStatus.text = "Root 权限: 已授权"
                    tvRootStatus.setTextColor(0xFF4CAF50.toInt())
                } else {
                    tvRootStatus.text = "Root 权限: 未授权 (请在弹窗中授权)"
                    tvRootStatus.setTextColor(0xFFF44336.toInt())
                }
            }
        }.start()
    }

    private fun getLocalIp(): String {
        return try {
            val interfaces = java.net.NetworkInterface.getNetworkInterfaces()
            while (interfaces.hasMoreElements()) {
                val iface = interfaces.nextElement()
                val addrs = iface.inetAddresses
                while (addrs.hasMoreElements()) {
                    val addr = addrs.nextElement()
                    if (!addr.isLoopbackAddress && addr is java.net.Inet4Address) return addr.hostAddress ?: "unknown"
                }
            }
            "unknown"
        } catch (e: Exception) {
            "unknown"
        }
    }
}
