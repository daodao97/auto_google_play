package com.automation.app

import android.content.Context
import android.util.Base64
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import java.net.URLEncoder
import java.util.concurrent.TimeUnit

/**
 * 设备运行时客户端。
 *
 * 当前优先使用 WebSocket 双工通信：
 * - 手机通过 WS 上报注册、心跳、步骤事件和命令结果。
 * - 服务端通过 WS 直接下发命令。
 *
 * HTTP 注册、心跳、事件和命令轮询保留为回退通道。
 */
class DeviceRuntimeClient(
    private val context: Context,
    private val taskManager: TaskManager
) {
    companion object {
        private const val TAG = "DeviceRuntime"
        private const val RUNNER_VERSION = "2.2-ws-duplex"
        private const val HEARTBEAT_INTERVAL_MS = 5_000L
        private const val COMMAND_POLL_INTERVAL_MS = 3_000L
        private const val WS_RECONNECT_INTERVAL_MS = 5_000L
        private const val DEFAULT_WS_PORT = 4400
    }

    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private val httpClient = OkHttpClient.Builder()
        .pingInterval(15, TimeUnit.SECONDS)
        .retryOnConnectionFailure(true)
        .build()

    private var heartbeatJob: Job? = null
    private var commandJob: Job? = null
    private var webSocket: WebSocket? = null

    @Volatile private var apiBaseUrl: String = ""
    @Volatile private var wsBaseUrl: String = ""
    @Volatile private var apiToken: String = ""
    @Volatile private var deviceId: String = ""
    @Volatile private var registered = false
    @Volatile private var wsConnected = false
    @Volatile private var wsConnecting = false
    @Volatile private var lastWsAttemptAt = 0L

    fun configure(baseUrl: String, token: String, phoneId: String, configuredWsBaseUrl: String = "") {
        val nextBaseUrl = baseUrl.trimEnd('/')
        val nextWsBaseUrl = configuredWsBaseUrl.trimEnd('/').ifEmpty { deriveWsBaseUrl(nextBaseUrl) }
        val changed = nextBaseUrl != apiBaseUrl || nextWsBaseUrl != wsBaseUrl || token != apiToken || phoneId != deviceId

        apiBaseUrl = nextBaseUrl
        wsBaseUrl = nextWsBaseUrl
        apiToken = token
        deviceId = phoneId

        if (changed) {
            registered = false
            closeWebSocket()
        }
        if (apiBaseUrl.isNotEmpty() && apiToken.isNotEmpty() && deviceId.isNotEmpty()) {
            start()
        }
    }

    fun start() {
        if (apiBaseUrl.isEmpty() || apiToken.isEmpty() || deviceId.isEmpty()) return
        if (heartbeatJob?.isActive != true) {
            heartbeatJob = scope.launch {
                registerDeviceByHttp()
                connectWebSocket()
                while (isActive) {
                    if (!wsConnected) connectWebSocket()
                    sendHeartbeat()
                    delay(HEARTBEAT_INTERVAL_MS)
                }
            }
        }
        if (commandJob?.isActive != true) {
            commandJob = scope.launch {
                while (isActive) {
                    if (!wsConnected) pollCommands()
                    delay(COMMAND_POLL_INTERVAL_MS)
                }
            }
        }
    }

    fun reportEvent(type: String, data: Map<String, Any?> = emptyMap()) {
        if (apiBaseUrl.isEmpty() || apiToken.isEmpty() || deviceId.isEmpty()) return
        scope.launch {
            val payload = JSONObject()
            payload.put("device_id", deviceId)
            payload.put("type", type)
            payload.put("timestamp", System.currentTimeMillis())
            for ((key, value) in data) payload.put(key, toJsonValue(value))
            if (!sendWsPayload(payload)) {
                postJson("/api/devices/events", payload)
            }
        }
    }

    fun uploadArtifactFile(
        type: String,
        filename: String,
        mimeType: String,
        bytes: ByteArray,
        metadata: Map<String, Any?> = emptyMap(),
        sensitive: Boolean = false
    ): JSONObject {
        val payload = JSONObject()
        payload.put("device_id", deviceId)
        payload.put("job_id", taskManager.getCurrentJobId())
        payload.put("run_id", taskManager.getCurrentRunId())
        payload.put("step_id", taskManager.getStatus()["step_name"] ?: "")
        payload.put("type", type)
        payload.put("filename", filename)
        payload.put("mime_type", mimeType)
        payload.put("content_base64", Base64.encodeToString(bytes, Base64.NO_WRAP))
        payload.put("metadata", toJsonValue(metadata))
        payload.put("sensitive", sensitive)
        val text = postJson("/api/artifacts/files", payload)
        val result = JSONObject(text)
        reportEvent("artifact_created", mapOf(
            "job_id" to taskManager.getCurrentJobId(),
            "run_id" to taskManager.getCurrentRunId(),
            "step_id" to (taskManager.getStatus()["step_name"] ?: ""),
            "artifact" to mapOf(
                "type" to type,
                "value_text" to result.optJSONObject("file")?.optString("sha256").orEmpty(),
                "value_json" to mapOf(
                    "artifact_id" to result.optLong("artifact_id"),
                    "filename" to filename,
                    "mime_type" to mimeType,
                    "size_bytes" to bytes.size
                )
            )
        ))
        return result
    }

    private fun registerDeviceByHttp() {
        if (registered) return
        try {
            postJson("/api/devices/register", buildDeviceRegisteredPayload())
            reportEvent("resume_state", mapOf("state" to taskManager.getResumeState()))
            registered = true
        } catch (e: Exception) {
            Log.w(TAG, "设备注册失败: ${e.message}")
        }
    }

    private fun sendDeviceRegisteredByWs() {
        sendWsPayload(buildDeviceRegisteredPayload())
        sendWsPayload(JSONObject().apply {
            put("device_id", deviceId)
            put("type", "resume_state")
            put("state", toJsonValue(taskManager.getResumeState()))
            put("timestamp", System.currentTimeMillis())
        })
    }

    private fun buildDeviceRegisteredPayload(): JSONObject {
        val payload = JSONObject()
        payload.put("device_id", deviceId)
        payload.put("type", "device_registered")
        payload.put("display_name", deviceId)
        payload.put("app_version", appVersion())
        payload.put("runner_version", RUNNER_VERSION)
        payload.put("capabilities", JSONArray(listOf(
            "websocket",
            "http_fallback",
            "yaml_v2",
            "yaml_imports",
            "step_events",
            "context_report",
            "command_control",
            "resume_from",
            "run_step",
            "replace_resource",
            "screenshot_upload",
            "ui_dump_upload",
            "open_app",
            "clear_app"
        )))
        payload.put("context", toJsonValue(taskManager.getRuntimeContext()))
        return payload
    }

    private fun sendHeartbeat() {
        try {
            val status = taskManager.getStatus()
            val payload = JSONObject()
            payload.put("device_id", deviceId)
            payload.put("type", "heartbeat")
            payload.put("status", if (status["state"] == "running" || status["state"] == "paused") "busy" else "online")
            payload.put("job_id", taskManager.getCurrentJobId())
            payload.put("run_id", taskManager.getCurrentRunId())
            payload.put("current_step", status["step_name"] ?: "")
            payload.put("context", toJsonValue(taskManager.getRuntimeContext()))
            if (!sendWsPayload(payload)) {
                postJson("/api/devices/heartbeat", payload)
            }
        } catch (e: Exception) {
            Log.w(TAG, "心跳失败: ${e.message}")
        }
    }

    private fun connectWebSocket() {
        if (wsBaseUrl.isEmpty() || apiToken.isEmpty() || deviceId.isEmpty()) return
        if (wsConnected || wsConnecting) return
        val now = System.currentTimeMillis()
        if (now - lastWsAttemptAt < WS_RECONNECT_INTERVAL_MS) return
        lastWsAttemptAt = now
        wsConnecting = true

        try {
            val encodedDeviceId = URLEncoder.encode(deviceId, "UTF-8")
            val encodedToken = URLEncoder.encode(apiToken, "UTF-8")
            val request = Request.Builder()
                .url("$wsBaseUrl/ws/devices/$encodedDeviceId?token=$encodedToken")
                .addHeader("Authorization", "Bearer $apiToken")
                .build()
            webSocket = httpClient.newWebSocket(request, object : WebSocketListener() {
                override fun onOpen(webSocket: WebSocket, response: Response) {
                    Log.i(TAG, "WebSocket 已连接")
                    wsConnecting = false
                    wsConnected = true
                    sendDeviceRegisteredByWs()
                }

                override fun onMessage(webSocket: WebSocket, text: String) {
                    handleWsMessage(text)
                }

                override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                    webSocket.close(code, reason)
                }

                override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                    Log.w(TAG, "WebSocket 已关闭: $code $reason")
                    wsConnecting = false
                    wsConnected = false
                }

                override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                    Log.w(TAG, "WebSocket 连接失败: ${t.message}")
                    wsConnecting = false
                    wsConnected = false
                }
            })
        } catch (e: Exception) {
            wsConnecting = false
            wsConnected = false
            Log.w(TAG, "WebSocket 初始化失败: ${e.message}")
        }
    }

    private fun closeWebSocket() {
        try {
            webSocket?.close(1000, "reconfigure")
        } catch (_: Exception) {}
        webSocket = null
        wsConnected = false
        wsConnecting = false
    }

    private fun handleWsMessage(text: String) {
        try {
            val item = JSONObject(text)
            when (item.optString("type")) {
                "command" -> handleCommand(item)
                "server_ping" -> sendHeartbeat()
                "server_hello", "device_registered_ack" -> {
                    Log.i(TAG, "WS 服务端消息: ${item.optString("type")}")
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "WebSocket 消息处理失败: ${e.message}")
        }
    }

    private fun handleCommand(item: JSONObject) {
        val commandId = item.optString("command_id")
        val command = item.optString("command")
        val payload = item.optJSONObject("payload") ?: JSONObject()
        if (commandId.isEmpty() || command.isEmpty()) return

        scope.launch {
            sendCommandResult(commandId, "accepted", mapOf("command" to command))
            val result = taskManager.handleRuntimeCommand(command, payload)
            if (result.first) {
                sendCommandResult(commandId, "succeeded", mapOf("message" to result.second))
            } else {
                sendCommandResult(commandId, "failed", mapOf("message" to result.second), result.second)
            }
        }
    }

    private fun pollCommands() {
        try {
            val encoded = URLEncoder.encode(deviceId, "UTF-8")
            val text = getText("/api/devices/commands?device_id=$encoded&claim=1")
            val commands = JSONObject(text).optJSONArray("commands") ?: JSONArray()
            for (i in 0 until commands.length()) {
                val item = commands.optJSONObject(i) ?: continue
                val commandId = item.optString("command_id")
                val command = item.optString("command")
                val payload = item.optJSONObject("payload") ?: JSONObject()
                if (commandId.isEmpty() || command.isEmpty()) continue

                sendCommandResult(commandId, "accepted", mapOf("command" to command))
                val result = taskManager.handleRuntimeCommand(command, payload)
                if (result.first) {
                    sendCommandResult(commandId, "succeeded", mapOf("message" to result.second))
                } else {
                    sendCommandResult(commandId, "failed", mapOf("message" to result.second), result.second)
                }
            }
        } catch (e: Exception) {
            Log.w(TAG, "命令轮询失败: ${e.message}")
        }
    }

    private fun sendCommandResult(commandId: String, status: String, result: Map<String, Any?>, error: String = "") {
        try {
            val payload = JSONObject()
            payload.put("type", if (status == "accepted") "command_ack" else "command_result")
            payload.put("device_id", deviceId)
            payload.put("command_id", commandId)
            payload.put("status", status)
            payload.put("result", JSONObject(result))
            payload.put("error", error)
            if (!sendWsPayload(payload)) {
                postJson("/api/devices/commands", payload)
            }
        } catch (e: Exception) {
            Log.w(TAG, "命令结果上报失败: ${e.message}")
        }
    }

    private fun sendWsPayload(payload: JSONObject): Boolean {
        val socket = webSocket ?: return false
        if (!wsConnected) return false
        return try {
            socket.send(payload.toString())
        } catch (e: Exception) {
            wsConnected = false
            Log.w(TAG, "WebSocket 发送失败: ${e.message}")
            false
        }
    }

    private fun postJson(path: String, body: JSONObject): String {
        val conn = open(path)
        conn.requestMethod = "POST"
        conn.setRequestProperty("Content-Type", "application/json")
        conn.doOutput = true
        conn.outputStream.use { it.write(body.toString().toByteArray()) }
        return readResponse(conn)
    }

    private fun getText(path: String): String {
        val conn = open(path)
        conn.requestMethod = "GET"
        return readResponse(conn)
    }

    private fun open(path: String): HttpURLConnection {
        val conn = URL("$apiBaseUrl$path").openConnection() as HttpURLConnection
        conn.connectTimeout = 5_000
        conn.readTimeout = 10_000
        conn.setRequestProperty("Authorization", "Bearer $apiToken")
        return conn
    }

    private fun readResponse(conn: HttpURLConnection): String {
        val code = conn.responseCode
        val stream = if (code in 200..299) conn.inputStream else conn.errorStream
        val text = stream?.bufferedReader()?.readText() ?: ""
        conn.disconnect()
        if (code !in 200..299) throw Exception("HTTP $code: $text")
        return text
    }

    private fun deriveWsBaseUrl(baseUrl: String): String {
        if (baseUrl.isEmpty()) return ""
        return try {
            val url = URL(baseUrl)
            if (url.protocol == "https") {
                val port = if (url.port > 0) ":${url.port}" else ""
                "wss://${url.host}$port"
            } else {
                val port = if (url.port > 0 && url.port != 4399) ":${url.port}" else ":$DEFAULT_WS_PORT"
                "ws://${url.host}$port"
            }
        } catch (_: Exception) {
            ""
        }
    }

    private fun appVersion(): String {
        return try {
            val info = context.packageManager.getPackageInfo(context.packageName, 0)
            info.versionName ?: ""
        } catch (_: Exception) {
            ""
        }
    }

    private fun toJsonValue(value: Any?): Any {
        return when (value) {
            null -> JSONObject.NULL
            is JSONObject -> value
            is JSONArray -> value
            is Map<*, *> -> {
                val obj = JSONObject()
                for ((k, v) in value) obj.put(k.toString(), toJsonValue(v))
                obj
            }
            is Iterable<*> -> {
                val arr = JSONArray()
                for (item in value) arr.put(toJsonValue(item))
                arr
            }
            else -> value
        }
    }
}
