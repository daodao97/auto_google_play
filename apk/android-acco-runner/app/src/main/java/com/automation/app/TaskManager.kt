package com.automation.app

import android.content.Context
import android.content.SharedPreferences
import android.util.Log
import kotlinx.coroutines.*
import java.io.File
import java.net.HttpURLConnection
import java.net.URL

enum class TaskState { IDLE, RUNNING, PAUSED, COMPLETED, FAILED, STOPPED }

data class Task(
    val id: String,
    val configName: String,
    val params: Map<String, String>,
    val callbackUrl: String
)

class TaskManager(private val context: Context) {

    companion object {
        private const val TAG = "TaskManager"
        const val PREF_NAME = "automation_config"
        const val KEY_VERIFY_TIMEOUT = "verify_timeout"
        const val KEY_TASK_TIMEOUT = "task_timeout"
        const val KEY_PHONE_ID = "phone_id"
        const val KEY_API_BASE_URL = "api_base_url"
        const val KEY_WS_BASE_URL = "ws_base_url"
        const val KEY_API_TOKEN = "api_token"
        const val DEFAULT_VERIFY_TIMEOUT = 30
        const val DEFAULT_TASK_TIMEOUT = 120
    }

    var state = TaskState.IDLE
        private set
    private var currentTask: Task? = null
    private var currentStepIndex = 0
    private var totalSteps = 0
    private var startTime = 0L
    private var currentStepName = ""
    private var failMessage: String? = null

    // 停止标志，所有 action 执行前检查
    @Volatile
    var stopRequested = false
        private set

    private val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())
    private var currentJob: Job? = null
    private var timeoutJob: Job? = null
    private val configParser = ConfigParser()
    val engine = ExecutionEngine(context).also {
        it.isStopRequested = { stopRequested }
    }
    private val deviceRuntime = DeviceRuntimeClient(context, this)

    private val prefs: SharedPreferences
        get() = context.getSharedPreferences(PREF_NAME, Context.MODE_PRIVATE)

    fun getVerifyTimeout(): Int = prefs.getInt(KEY_VERIFY_TIMEOUT, DEFAULT_VERIFY_TIMEOUT)
    fun setVerifyTimeout(seconds: Int) = prefs.edit().putInt(KEY_VERIFY_TIMEOUT, seconds).apply()

    fun getTaskTimeout(): Int = prefs.getInt(KEY_TASK_TIMEOUT, DEFAULT_TASK_TIMEOUT)
    fun setTaskTimeout(seconds: Int) = prefs.edit().putInt(KEY_TASK_TIMEOUT, seconds).apply()

    fun getPhoneId(): String = prefs.getString(KEY_PHONE_ID, "") ?: ""
    fun setPhoneId(id: String) = prefs.edit().putString(KEY_PHONE_ID, id).apply()

    fun getApiBaseUrl(): String = prefs.getString(KEY_API_BASE_URL, "") ?: ""
    fun setApiBaseUrl(url: String) = prefs.edit().putString(KEY_API_BASE_URL, url).apply()

    fun getWsBaseUrl(): String = prefs.getString(KEY_WS_BASE_URL, "") ?: ""
    fun setWsBaseUrl(url: String) = prefs.edit().putString(KEY_WS_BASE_URL, url).apply()

    fun getApiToken(): String = prefs.getString(KEY_API_TOKEN, "") ?: ""
    fun setApiToken(token: String) = prefs.edit().putString(KEY_API_TOKEN, token).apply()

    fun configureRuntime() {
        deviceRuntime.configure(getApiBaseUrl(), getApiToken(), getPhoneId(), getWsBaseUrl())
    }

    fun startTask(task: Task): Result<String> {
        if (state == TaskState.RUNNING) return Result.failure(Exception("任务运行中"))

        currentTask = task
        state = TaskState.RUNNING
        startTime = System.currentTimeMillis()
        currentStepIndex = 0
        failMessage = null
        stopRequested = false

        // 将任务参数注入引擎变量
        engine.variables.clear()
        engine.variables.putAll(task.params)
        // 注入验证码超时配置，任务参数优先
        engine.variables["verify_timeout"] = task.params["verify_timeout"] ?: getVerifyTimeout().toString()

        val apiBaseUrl = task.params["api_base_url"] ?: getApiBaseUrl()
        val apiToken = task.params["api_token"] ?: getApiToken()
        val phoneId = task.params["phone_id"] ?: getPhoneId()
        val wsBaseUrl = task.params["ws_base_url"] ?: getWsBaseUrl()
        val taskTimeout = task.params["task_timeout"] ?: getTaskTimeout().toString()
        deviceRuntime.configure(apiBaseUrl, apiToken, phoneId, wsBaseUrl)

        mapOf(
            "api_base_url" to apiBaseUrl,
            "api_token" to apiToken,
            "phone_id" to phoneId,
            "ws_base_url" to wsBaseUrl,
            "task_timeout" to taskTimeout
        ).forEach { (key, value) ->
            if (value.isNotEmpty()) engine.variables[key] = value
        }

        TaskLogger.clear()
        TaskLogger.log("任务启动: ${task.configName}")
        deviceRuntime.reportEvent("task_started", mapOf(
            "job_id" to getCurrentJobId(),
            "run_id" to getCurrentRunId(),
            "yaml_id" to (task.params["yaml_id"] ?: task.configName),
            "yaml_version" to (task.params["yaml_version"] ?: "1"),
            "context" to getRuntimeContext()
        ))

        // 启动任务（配置解析在协程内部执行，支持网络下载）
        currentJob = scope.launch { executeTask(task) }

        // 启动总超时定时器
        val taskTimeoutSeconds = taskTimeout.toIntOrNull() ?: getTaskTimeout()
        timeoutJob = scope.launch {
            delay(taskTimeoutSeconds * 1000L)
            if (state == TaskState.RUNNING) {
                Log.w(TAG, "任务总超时: ${taskTimeoutSeconds}s")
                TaskLogger.log("任务总超时: ${taskTimeoutSeconds}s")
                stop()
            }
        }

        return Result.success("任务已启动")
    }

    fun pause(): Result<String> {
        if (state != TaskState.RUNNING) return Result.failure(Exception("任务未运行"))
        state = TaskState.PAUSED
        return Result.success("已暂停")
    }

    fun resume(): Result<String> {
        if (state != TaskState.PAUSED) return Result.failure(Exception("任务未暂停"))
        state = TaskState.RUNNING
        return Result.success("已恢复")
    }

    fun stop(): Result<String> {
        stopRequested = true
        currentJob?.cancel()
        timeoutJob?.cancel()
        val wasRunning = state == TaskState.RUNNING || state == TaskState.PAUSED
        state = TaskState.STOPPED
        TaskLogger.log("任务已停止")

        if (wasRunning) {
            postCallback("stopped", mapOf(
                "step" to currentStepName,
                "step_index" to currentStepIndex,
                "elapsed_seconds" to ((System.currentTimeMillis() - startTime) / 1000)
            ))
        }
        cleanup()
        return Result.success("已停止")
    }

    fun getStatus(): Map<String, Any?> {
        val elapsed = if (startTime > 0) (System.currentTimeMillis() - startTime) / 1000 else 0
        return mapOf(
            "state" to state.name.lowercase(),
            "task_id" to currentTask?.id,
            "current_step" to currentStepIndex,
            "total_steps" to totalSteps,
            "step_name" to currentStepName,
            "fail_message" to failMessage,
            "elapsed_seconds" to elapsed,
            "can_start" to (state != TaskState.RUNNING && state != TaskState.PAUSED)
        )
    }

    fun getCurrentJobId(): Long {
        return currentTask?.params?.get("job_id")?.toLongOrNull() ?: 0L
    }

    fun getCurrentRunId(): String {
        return currentTask?.params?.get("run_id") ?: currentTask?.id ?: ""
    }

    fun getRuntimeContext(): Map<String, Any?> {
        val task = currentTask
        val params = if (engine.variables.isNotEmpty()) engine.variables else (task?.params ?: emptyMap())
        return mapOf(
            "task_id" to task?.id,
            "job_id" to getCurrentJobId(),
            "run_id" to getCurrentRunId(),
            "yaml_id" to (params["yaml_id"] ?: task?.configName),
            "yaml_version" to (params["yaml_version"] ?: "1"),
            "current_step" to currentStepName,
            "step_index" to currentStepIndex,
            "google_email_masked" to (params["google_email_masked"] ?: maskEmail(params["email"] ?: "")),
            "claude_email_masked" to maskEmail(params["claude_email"] ?: ""),
            "card_id" to (params["card_id"] ?: ""),
            "card_last4" to (params["card_last4"] ?: params["card_number"]?.takeLast(4).orEmpty())
        )
    }

    fun getResumeState(): Map<String, Any?> {
        return mapOf(
            "job_id" to getCurrentJobId(),
            "run_id" to getCurrentRunId(),
            "current_step" to currentStepName,
            "step_index" to currentStepIndex,
            "state" to state.name.lowercase(),
            "context" to getRuntimeContext()
        )
    }

    fun handleRuntimeCommand(command: String, payload: org.json.JSONObject): Pair<Boolean, String> {
        return try {
            when (command) {
                "ping" -> true to "pong"
                "cancel_job", "stop_task" -> {
                    stop()
                    true to "任务已停止"
                }
                "pause_job" -> {
                    val result = pause()
                    result.isSuccess to (result.getOrNull() ?: result.exceptionOrNull()?.message ?: "")
                }
                "resume_job" -> {
                    val result = resume()
                    result.isSuccess to (result.getOrNull() ?: result.exceptionOrNull()?.message ?: "")
                }
                "replace_resource" -> replaceRuntimeResource(payload)
                "run_step" -> runSingleStep(payload)
                "start_job" -> {
                    val task = taskFromCommandPayload(payload)
                    val result = startTask(task)
                    result.isSuccess to (result.getOrNull() ?: result.exceptionOrNull()?.message ?: "")
                }
                "dump_ui" -> dumpUi()
                "upload_screenshot" -> uploadScreenshot()
                "open_app" -> openApp(payload)
                "clear_app" -> clearApp(payload)
                else -> false to "未知命令: $command"
            }
        } catch (e: Exception) {
            false to (e.message ?: "命令执行失败")
        }
    }

    private suspend fun executeTask(task: Task) {
        try {
            val config = withContext(Dispatchers.IO) {
                configParser.parse(task.configName, task.params)
            }
            totalSteps = config.steps.size
            TaskLogger.log("配置解析完成: ${config.name} (${totalSteps} 步)")
            val resumeFrom = task.params["resume_from"] ?: ""
            val startIndex = if (resumeFrom.isNotEmpty()) {
                config.steps.indexOfFirst { stepMatches(it, resumeFrom) }.also {
                    if (it < 0) throw Exception("恢复步骤不存在: $resumeFrom")
                }
            } else {
                0
            }

            for (index in startIndex until config.steps.size) {
                val step = config.steps[index]
                // 停止检查
                if (stopRequested) break

                // 暂停检查
                while (state == TaskState.PAUSED) { delay(500) }
                if (state == TaskState.IDLE || state == TaskState.STOPPED) break

                currentStepIndex = index
                currentStepName = step.name
                Log.i(TAG, "[$index/${totalSteps}] ${step.name}")
                TaskLogger.log("[$index/${totalSteps}] ${step.name}")
                deviceRuntime.reportEvent("step_started", mapOf(
                    "job_id" to getCurrentJobId(),
                    "run_id" to getCurrentRunId(),
                    "step_id" to step.name,
                    "step_index" to index,
                    "attempt" to 1,
                    "context" to getRuntimeContext()
                ))

                val result = engine.executeStep(step)

                // 停止检查
                if (stopRequested) break

                // failTask 标志：直接终止任务
                if (result.failTask) {
                    TaskLogger.log("  ✗ ${result.message}")
                    reportStepFailed(step, index, result.message)
                    onTaskFailed(result.message)
                    return
                }

                if (!result.success) {
                    reportStepFailed(step, index, result.message)
                    val effectiveOnError = if (step.optional) "continue" else resolveOnError(step, result.message)
                    when (effectiveOnError) {
                        "continue" -> {
                            Log.w(TAG, "步骤失败但继续: ${result.message}")
                            TaskLogger.log("  ⚠ ${result.message} (继续)")
                        }
                        "complete" -> {
                            Log.i(TAG, "步骤失败，提前完成: ${result.message}")
                            TaskLogger.log("  ✓ ${result.message} (提前完成)")
                            onTaskCompleted()
                            return
                        }
                        "manual" -> {
                            state = TaskState.PAUSED
                            postCallback("manual_intervention", mapOf(
                                "step" to step.name,
                                "reason" to result.message,
                                "hint" to (step.manualHint ?: "需要手动操作")
                            ))
                            while (state == TaskState.PAUSED) { delay(500) }
                        }
                        else -> {
                            onTaskFailed(result.message)
                            return
                        }
                    }
                }

                Log.i(TAG, "[$index] ${step.name} → ${result.message}")
                if (result.success) {
                    TaskLogger.log("  ✓ ${result.message}")
                    deviceRuntime.reportEvent("step_completed", mapOf(
                        "job_id" to getCurrentJobId(),
                        "run_id" to getCurrentRunId(),
                        "step_id" to step.name,
                        "step_index" to index,
                        "result" to mapOf("message" to result.message),
                        "context" to getRuntimeContext()
                    ))
                    if (step.onSuccess == "complete" || result.completeTask) {
                        Log.i(TAG, "步骤成功，提前完成任务")
                        TaskLogger.log("  ✓ 提前完成任务")
                        onTaskCompleted()
                        return
                    }
                }
            }
            if (!stopRequested) onTaskCompleted()
        } catch (e: CancellationException) {
            Log.i(TAG, "任务被取消")
        } catch (e: Exception) {
            onTaskFailed(e.message ?: "未知错误")
        }
    }

    private fun onTaskCompleted() {
        state = TaskState.COMPLETED
        timeoutJob?.cancel()
        val task = currentTask ?: return
        val duration = (System.currentTimeMillis() - startTime) / 1000
        TaskLogger.log("任务完成，耗时 ${duration}s")

        postCallback("completed", mapOf(
            "duration_seconds" to duration,
            "account" to mapOf(
                "email" to task.params["email"],
                "password" to task.params["password"]
            ),
            "card" to mapOf(
                "card_number" to task.params["card_number"],
                "expiry" to task.params["expiry"],
                "cvv" to task.params["cvv"]
            ),
        ))
        deviceRuntime.reportEvent("task_completed", mapOf(
            "job_id" to getCurrentJobId(),
            "run_id" to getCurrentRunId(),
            "duration_seconds" to duration,
            "context" to getRuntimeContext()
        ))
        deviceRuntime.reportEvent("artifact_created", mapOf(
            "job_id" to getCurrentJobId(),
            "run_id" to getCurrentRunId(),
            "artifact" to mapOf(
                "type" to "task_result",
                "value_text" to "completed",
                "value_json" to mapOf(
                    "duration_seconds" to duration,
                    "context" to getRuntimeContext()
                )
            )
        ))
        cleanup()
    }

    private fun onTaskFailed(error: String) {
        state = TaskState.FAILED
        failMessage = error
        timeoutJob?.cancel()
        TaskLogger.log("任务失败: $error")
        postCallback("failed", mapOf(
            "step" to currentStepName,
            "step_index" to currentStepIndex,
            "message" to error
        ))
        deviceRuntime.reportEvent("task_failed", mapOf(
            "job_id" to getCurrentJobId(),
            "run_id" to getCurrentRunId(),
            "step_id" to currentStepName,
            "step_index" to currentStepIndex,
            "error_code" to normalizeErrorCode(error),
            "error_message" to error,
            "recoverable" to true,
            "context" to getRuntimeContext()
        ))
        cleanup()
    }

    private fun reportStepFailed(step: Step, index: Int, message: String) {
        deviceRuntime.reportEvent("step_failed", mapOf(
            "job_id" to getCurrentJobId(),
            "run_id" to getCurrentRunId(),
            "step_id" to step.name,
            "step_index" to index,
            "error_code" to normalizeErrorCode(message),
            "error_message" to message,
            "recoverable" to !message.contains("未知 action"),
            "context" to getRuntimeContext()
        ))
    }

    private fun postCallback(status: String, extra: Map<String, Any?>) {
        val task = currentTask ?: return
        if (task.callbackUrl.isNullOrEmpty()) return
        scope.launch(Dispatchers.IO) {
            try {
                val data = mutableMapOf<String, Any?>(
                    "task_id" to task.id,
                    "status" to status
                )
                data.putAll(extra)
                val json = com.google.gson.Gson().toJson(data)

                val url = URL(task.callbackUrl)
                val conn = url.openConnection() as HttpURLConnection
                conn.requestMethod = "POST"
                conn.setRequestProperty("Content-Type", "application/json")
                conn.doOutput = true
                conn.outputStream.write(json.toByteArray())
                conn.responseCode
                conn.disconnect()
                Log.i(TAG, "回调成功: $status")
            } catch (e: Exception) {
                Log.e(TAG, "回调失败: ${e.message}")
            }
        }
    }

    private fun cleanup() {
        currentTask = null
        currentStepIndex = 0
        totalSteps = 0
        startTime = 0
    }

    private fun taskFromCommandPayload(payload: org.json.JSONObject): Task {
        val taskId = payload.optString("task_id", "cmd_task_${System.currentTimeMillis()}")
        val config = payload.optString("yaml_url", payload.optString("config", ""))
        val callbackUrl = payload.optString("callback_url", "")
        val paramsObj = payload.optJSONObject("params") ?: org.json.JSONObject()
        val params = mutableMapOf<String, String>()
        flattenJson(paramsObj, "", params)
        val resourcesObj = payload.optJSONObject("resources")
        if (resourcesObj != null) flattenJson(resourcesObj, "resources", params)
        payload.optString("resume_from", "").takeIf { it.isNotEmpty() }?.let { params["resume_from"] = it }
        payload.optString("job_id", "").takeIf { it.isNotEmpty() }?.let { params["job_id"] = it }
        payload.optString("run_id", "").takeIf { it.isNotEmpty() }?.let { params["run_id"] = it }
        payload.optString("yaml_id", "").takeIf { it.isNotEmpty() }?.let { params["yaml_id"] = it }
        payload.optString("yaml_version", "").takeIf { it.isNotEmpty() }?.let { params["yaml_version"] = it }
        return Task(taskId, config, params, callbackUrl)
    }

    private fun flattenJson(obj: org.json.JSONObject, prefix: String, out: MutableMap<String, String>) {
        val keys = obj.keys()
        while (keys.hasNext()) {
            val key = keys.next()
            val fullKey = if (prefix.isEmpty()) key else "$prefix.$key"
            when (val value = obj.opt(key)) {
                is org.json.JSONObject -> flattenJson(value, fullKey, out)
                else -> out[fullKey] = value?.toString() ?: ""
            }
        }
    }

    private fun dumpUi(): Pair<Boolean, String> {
        val dump = AutoAccessibilityService.instance?.dumpTreeJson()
            ?: return false to "无障碍服务未连接"
        val bytes = dump.toString(2).toByteArray()
        val filename = "ui_dump_${System.currentTimeMillis()}.json"
        val result = deviceRuntime.uploadArtifactFile(
            type = "ui_dump",
            filename = filename,
            mimeType = "application/json",
            bytes = bytes,
            metadata = getRuntimeContext(),
            sensitive = true
        )
        return true to "UI dump 已上传: artifact_id=${result.optLong("artifact_id")}"
    }

    private fun uploadScreenshot(): Pair<Boolean, String> {
        val file = File(context.cacheDir, "screenshot_${System.currentTimeMillis()}.png")
        val commands = listOf(
            arrayOf("su", "-c", "screencap -p '${file.absolutePath}'"),
            arrayOf("sh", "-c", "screencap -p '${file.absolutePath}'")
        )
        var ok = false
        for (cmd in commands) {
            try {
                val process = Runtime.getRuntime().exec(cmd)
                if (process.waitFor() == 0 && file.exists() && file.length() > 0) {
                    ok = true
                    break
                }
            } catch (_: Exception) {}
        }
        if (!ok) return false to "截图失败"
        val result = deviceRuntime.uploadArtifactFile(
            type = "screenshot",
            filename = file.name,
            mimeType = "image/png",
            bytes = file.readBytes(),
            metadata = getRuntimeContext(),
            sensitive = true
        )
        file.delete()
        return true to "截图已上传: artifact_id=${result.optLong("artifact_id")}"
    }

    private fun runSingleStep(payload: org.json.JSONObject): Pair<Boolean, String> {
        if (state == TaskState.RUNNING || state == TaskState.PAUSED) return false to "任务运行中，不能单步执行"
        val configName = payload.optString("yaml_url", payload.optString("config", currentTask?.configName ?: ""))
        val stepId = payload.optString("step_id", "")
        if (configName.isEmpty()) return false to "缺少 yaml_url/config"
        if (stepId.isEmpty()) return false to "缺少 step_id"
        val params = mutableMapOf<String, String>()
        flattenJson(payload.optJSONObject("params") ?: org.json.JSONObject(), "", params)
        val config = configParser.parse(configName, params)
        val step = config.steps.find { stepMatches(it, stepId) } ?: return false to "步骤不存在: $stepId"
        state = TaskState.RUNNING
        currentStepName = step.name
        currentStepIndex = config.steps.indexOf(step)
        engine.variables.clear()
        engine.variables.putAll(params)
        deviceRuntime.reportEvent("step_started", mapOf(
            "step_id" to step.name,
            "step_index" to currentStepIndex,
            "context" to getRuntimeContext()
        ))
        val result = engine.executeStep(step)
        state = if (result.success) TaskState.COMPLETED else TaskState.FAILED
        if (result.success) {
            deviceRuntime.reportEvent("step_completed", mapOf(
                "step_id" to step.name,
                "step_index" to currentStepIndex,
                "result" to mapOf("message" to result.message),
                "context" to getRuntimeContext()
            ))
            cleanup()
            return true to result.message
        }
        reportStepFailed(step, currentStepIndex, result.message)
        cleanup()
        return false to result.message
    }

    private fun replaceRuntimeResource(payload: org.json.JSONObject): Pair<Boolean, String> {
        val paramsObj = payload.optJSONObject("params") ?: org.json.JSONObject()
        val resourcesObj = payload.optJSONObject("resources") ?: org.json.JSONObject()
        val updates = mutableMapOf<String, String>()
        flattenJson(paramsObj, "", updates)
        flattenJson(resourcesObj, "resources", updates)
        if (updates.isEmpty()) return false to "没有可替换资源"
        engine.variables.putAll(updates)
        deviceRuntime.reportEvent("context_updated", mapOf(
            "job_id" to getCurrentJobId(),
            "run_id" to getCurrentRunId(),
            "context" to getRuntimeContext(),
            "updated_keys" to updates.keys.toList()
        ))
        return true to "资源已替换: ${updates.keys.joinToString(",")}"
    }

    private fun openApp(payload: org.json.JSONObject): Pair<Boolean, String> {
        val packageName = resolvePackageName(payload)
            ?: return false to "缺少 package/package_name"
        return AppControl.openApp(context, packageName)
    }

    private fun clearApp(payload: org.json.JSONObject): Pair<Boolean, String> {
        val packageName = resolvePackageName(payload)
            ?: return false to "缺少 package/package_name"
        return AppControl.clearApp(context, packageName)
    }

    private fun resolvePackageName(payload: org.json.JSONObject): String? {
        val packageName = payload.optString("package", payload.optString("package_name", "")).trim()
        if (packageName.isEmpty()) return null
        return AppControl.validatePackageName(packageName)
    }

    private fun stepMatches(step: Step, key: String): Boolean {
        return step.name == key || step.name.endsWith("/$key")
    }

    private fun resolveOnError(step: Step, message: String): String {
        val code = normalizeErrorCode(message)
        return step.onErrorRules[code]
            ?: step.onErrorRules["default"]
            ?: step.onError
    }

    private fun maskEmail(email: String): String {
        if (email.isEmpty() || !email.contains("@")) return if (email.isEmpty()) "" else "***"
        val parts = email.split("@", limit = 2)
        return "${parts[0].take(1)}***@${parts[1]}"
    }

    private fun normalizeErrorCode(message: String): String {
        val lower = message.lowercase()
        return when {
            lower.contains("declined") || message.contains("卡") -> "card_error"
            message.contains("登录") || lower.contains("login") -> "login_error"
            message.contains("超时") || lower.contains("timeout") -> "timeout"
            else -> "step_failed"
        }
    }

    fun rebootDevice() {
        try {
            Runtime.getRuntime().exec(arrayOf("su", "-c", "reboot"))
        } catch (e: Exception) {
            Log.e(TAG, "重启失败: ${e.message}")
        }
    }
}
