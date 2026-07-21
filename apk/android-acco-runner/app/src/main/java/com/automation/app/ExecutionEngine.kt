package com.automation.app

import android.content.Context
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo
import java.net.HttpURLConnection
import java.net.URL
import org.json.JSONObject

/**
 * 执行引擎：解析 Step 并调用 UIInteractor 执行
 * 核心原则：元素驱动，不靠固定延时
 * - tap/input 等操作内置等待，元素出现即执行
 * - 只有 sleep action 才做硬等待
 */
class ExecutionEngine(private val context: Context) {

    companion object {
        private const val TAG = "Engine"
        private const val DEFAULT_TIMEOUT = 10 // 默认等待超时（秒）
    }

    private val ui = UIInteractor(context)
    private val accountManager = PluginAccountManager(context)

    // 停止检查，由 TaskManager 设置
    var isStopRequested: () -> Boolean = { false }

    // 任务变量：extract_text 提取的值存这里，可在后续步骤中通过 {var_name} 引用
    val variables = mutableMapOf<String, String>()

    fun executeStep(step: Step): StepResult {
        if (isStopRequested()) return StepResult(false, "任务已停止")
        Log.i(TAG, "[${step.name}] action=${step.action}")
        val result = try {
            when (step.action) {
                "tap" -> executeTap(step)
                "input" -> executeInput(step)
                "swipe" -> executeSwipe(step)
                "wait" -> executeWait(step)
                "loop" -> executeLoop(step)
                "launch_app", "open_app" -> executeOpenApp(step)
                "launch_intent" -> executeLaunchIntent(step)
                "force_stop" -> executeForceStop(step)
                "clear_app" -> executeClearApp(step)
                "key" -> executeKey(step)
                "shell" -> executeShell(step)
                "wait_app" -> executeWaitApp(step)
                "check_text" -> executeCheckText(step)
                "sleep" -> executeSleep(step)
                "scroll_find_tap" -> executeScrollFindTap(step)
                "remove_all_accounts" -> executeRemoveAllAccounts(step)
                "list_accounts" -> executeListAccounts(step)
                "check_account" -> executeCheckAccount(step)
                "decision" -> executeDecision(step)
                "extract_text" -> executeExtractText(step)
                "http_request" -> executeHttpRequest(step)
                "wait_variable" -> executeWaitVariable(step)
                "scroll_top" -> executeScrollTop(step)
                "scroll_bottom" -> executeScrollBottom(step)
                "find_text" -> executeFindText(step)
                "assert_text" -> executeAssertText(step)
                "back" -> executeBack(step)
                else -> StepResult(false, "未知 action: ${step.action}")
            }
        } catch (e: Exception) {
            StepResult(false, "${step.name}: ${e.message}")
        }
        // 处理 on_success
        if (result.success && step.onSuccess == "complete") {
            return result.copy(completeTask = true)
        }
        if (result.success && step.onSuccess == "fail") {
            return StepResult(false, "触发失败条件: ${result.message}", failTask = true)
        }
        if (result.success && step.onSuccess == "break") {
            return result.copy(breakLoop = true)
        }
        return result
    }

    /**
     * tap: 等待元素出现 → 点击
     */
    private fun executeTap(step: Step): StepResult {
        val find = step.find ?: return StepResult(false, "缺少 find")
        val timeout = step.timeout ?: DEFAULT_TIMEOUT

        for (i in 0 until step.repeat) {
            val el = ui.waitAndFind(find, timeout) ?: return if (step.optional) {
                StepResult(true, "可选，未找到")
            } else {
                StepResult(false, "等待元素超时")
            }
            Log.i(TAG, "tap 找到: text=${el.node?.text}, class=${el.node?.className}, pos=(${el.centerX},${el.centerY})")
            ui.clickElement(el)
            if (step.repeat > 1) Thread.sleep(300)
        }
        return StepResult(true, "点击成功")
    }

    /**
     * input: 设置文本
     * find 为空时直接用 shell input text 输入到当前焦点
     * find 不为空时等待元素出现再输入
     * method: "set_text"(默认) / "shell" 由 YAML 配置
     */
    private fun executeInput(step: Step): StepResult {
        val rawValue = step.value ?: return StepResult(false, "缺少 value")
        val value = resolveVariables(rawValue)
        val method = step.method ?: "set_text"

        // 无 find：直接输入到当前焦点
        val find = step.find
        if (find == null) {
            when (method) {
                "clipboard" -> ui.clipboardInput(value)
                else -> ui.shellInput(value)
            }
            return StepResult(true, "输入成功: $value")
        }

        val timeout = step.timeout ?: DEFAULT_TIMEOUT

        val el = ui.waitAndFind(find, timeout)
            ?: return StepResult(false, "等待输入框超时")

        val node = el.node
        if (node != null && isEditText(node)) {
            // 找到的就是 EditText，直接输入
            ui.inputToNode(node, value, method)
        } else if (node != null) {
            // 找到的是标题/label，在页面上找输入框
            Log.i(TAG, "找到的不是输入框，查找页面输入框")
            val editEl = ui.waitAndFind(FindParams(className = "android.widget.EditText"), 2)
                ?: ui.waitAndFind(FindParams(className = "AutoCompleteTextView"), 2)
            if (editEl?.node != null) {
                ui.inputToNode(editEl.node, value, method)
            } else {
                return StepResult(false, "页面上未找到输入框")
            }
        } else {
            // coords 模式
            ui.tap(el.centerX, el.centerY)
            Thread.sleep(200)
            val editFind = FindParams(className = "android.widget.EditText")
            val editEl = ui.waitAndFind(editFind, 3)
            if (editEl?.node != null) {
                ui.inputToNode(editEl.node, value, method)
            } else {
                return StepResult(false, "无法输入文本")
            }
        }
        return StepResult(true, "输入成功: $value")
    }

    private fun isEditText(node: AccessibilityNodeInfo): Boolean {
        val cls = node.className?.toString() ?: ""
        return cls.contains("EditText") || cls.contains("AutoCompleteTextView")
    }

    private fun executeSwipe(step: Step): StepResult {
        val from = step.from ?: return StepResult(false, "缺少 from")
        val to = step.to ?: return StepResult(false, "缺少 to")
        ui.swipe(from[0], from[1], to[0], to[1], step.duration ?: 300)
        return StepResult(true, "滑动成功")
    }

    /**
     * wait: 纯等待某个元素出现（不做任何操作）
     */
    private fun executeWait(step: Step): StepResult {
        val texts = step.waitFor ?: return StepResult(false, "缺少 wait_for")
        val timeout = step.timeout ?: DEFAULT_TIMEOUT
        val find = FindParams(text = texts)
        val el = ui.waitAndFind(find, timeout)
        return if (el != null) StepResult(true, "元素已出现")
        else StepResult(false, "等待超时")
    }

    private fun executeLoop(step: Step): StepResult {
        val max = step.max ?: 5
        val subSteps = step.steps ?: return StepResult(false, "缺少 steps")
        for (i in 0 until max) {
            var conditionMet = false
            for (sub in subSteps) {
                val result = executeStep(sub)
                // sub-step 要求提前完成、失败或退出循环，直接向上传递
                if (result.completeTask || result.failTask) {
                    return result
                }
                if (result.breakLoop) {
                    return StepResult(true, "循环条件满足: ${result.message}")
                }
                if (sub.action == "check_text" || sub.action == "wait") {
                    if (result.success) {
                        conditionMet = true
                        continue
                    } else {
                        // check_text 失败，跳过本轮剩余 sub-steps
                        break
                    }
                }
            }
            if (conditionMet) {
                return StepResult(true, "循环条件满足，已执行后续步骤")
            }
            Thread.sleep(500)
        }
        return StepResult(true, "循环完成")
    }

    private fun executeOpenApp(step: Step): StepResult {
        val pkg = resolvePackageParam(step) ?: return StepResult(false, "缺少 package/package_name/value")
        val result = AppControl.openApp(context, pkg)
        return StepResult(result.first, result.second)
    }

    private fun executeLaunchIntent(step: Step): StepResult {
        val intent = step.value ?: return StepResult(false, "缺少 value")
        ui.launchIntent(intent)
        return StepResult(true, "已启动 intent")
    }

    private fun executeForceStop(step: Step): StepResult {
        val pkg = resolvePackageParam(step) ?: return StepResult(false, "缺少 package/package_name/value")
        val result = AppControl.forceStop(pkg)
        return StepResult(result.first, result.second)
    }

    private fun executeClearApp(step: Step): StepResult {
        val pkg = resolvePackageParam(step) ?: return StepResult(false, "缺少 package/package_name/value")
        val result = AppControl.clearApp(context, pkg)
        return StepResult(result.first, result.second)
    }

    private fun executeKey(step: Step): StepResult {
        val key = step.value ?: return StepResult(false, "缺少 value")
        ui.keyEvent(key)
        return StepResult(true, "按键 $key")
    }

    private fun executeShell(step: Step): StepResult {
        val cmd = step.value ?: return StepResult(false, "缺少 value")
        val wrappedCmd = "nsenter -t 1 -m -- sh -c '${cmd.replace("'", "'\\''")}'"
        val process = ProcessBuilder("su", "-c", wrappedCmd)
            .redirectErrorStream(true)
            .start()
        val output = process.inputStream.bufferedReader().readText().trim()
        val exitCode = process.waitFor()
        val message = output.ifEmpty { "shell exit=$exitCode" }
        return StepResult(exitCode == 0, message)
    }

    private fun executeWaitApp(step: Step): StepResult {
        val pkg = resolvePackageParam(step) ?: return StepResult(false, "缺少 package/package_name/value")
        val timeout = step.timeout ?: DEFAULT_TIMEOUT
        val endTime = System.currentTimeMillis() + timeout * 1000L
        while (System.currentTimeMillis() < endTime) {
            if (ui.getCurrentPackage() == pkg) {
                return StepResult(true, "已进入 $pkg")
            }
            Thread.sleep(100)
        }
        return StepResult(false, "等待 $pkg 超时")
    }

    /**
     * check_text: 立即检查文本是否存在（不等待）
     */
    private fun executeCheckText(step: Step): StepResult {
        val texts = step.waitFor ?: return StepResult(false, "缺少 wait_for")
        val el = ui.findElement(FindParams(text = texts))
        return if (el != null) StepResult(true, "找到文本")
        else StepResult(false, "未找到文本")
    }

    private fun executeSleep(step: Step): StepResult {
        val ms = step.delay
        Thread.sleep(ms)
        return StepResult(true, "等待 ${ms}ms")
    }

    private fun executeScrollFindTap(step: Step): StepResult {
        val find = step.find ?: return StepResult(false, "缺少 find")
        val max = step.max ?: 5
        val breakTexts = step.waitFor

        for (i in 0 until max) {
            val el = ui.findElement(find)
            if (el != null) {
                ui.clickElement(el)
                return StepResult(true, "找到并点击")
            }
            if (breakTexts != null) {
                val breakEl = ui.findElement(FindParams(text = breakTexts))
                if (breakEl != null) {
                    return StepResult(true, "检测到退出条件")
                }
            }
            if (ui.isScrollable()) {
                ui.scrollDown()
            } else {
                break
            }
            Thread.sleep(500)
        }
        return if (step.optional) StepResult(true, "可选，未找到")
        else StepResult(false, "滚动查找失败")
    }

    private fun executeRemoveAllAccounts(step: Step): StepResult {
        val (success, message) = accountManager.removeAllAccountsSync()
        return StepResult(success, message)
    }

    private fun executeListAccounts(step: Step): StepResult {
        val (success, accounts) = accountManager.listAccountsSync()
        return if (success) StepResult(true, "账号: ${accounts.joinToString(", ")}")
        else StepResult(false, "获取账号列表失败")
    }

    private fun executeCheckAccount(step: Step): StepResult {
        val email = step.value ?: return StepResult(false, "缺少 value (email)")
        val (success, accounts) = accountManager.listAccountsSync()
        if (!success) return StepResult(false, "获取账号列表失败")
        return if (accounts.any { it.equals(email, ignoreCase = true) }) {
            StepResult(true, "账号已存在: $email")
        } else {
            StepResult(false, "账号未找到: $email")
        }
    }

    /**
     * decision: 轮询页面内容，根据规则决定结果
     * rules 按优先级排列，第一个匹配的生效
     * result: continue(继续) / fail(任务失败) / complete(任务完成)
     */
    private fun executeDecision(step: Step): StepResult {
        val rules = step.rules ?: return StepResult(false, "缺少 rules")
        val timeout = step.timeout ?: DEFAULT_TIMEOUT
        val endTime = System.currentTimeMillis() + timeout * 1000L

        while (System.currentTimeMillis() < endTime) {
            // 按优先级逐条检查规则
            for (rule in rules) {
                // when_app: 检查前台应用
                if (rule.whenApp != null) {
                    if (ui.getCurrentPackage() == rule.whenApp) {
                        val msg = rule.message ?: "已进入: ${rule.whenApp}"
                        Log.i(TAG, "decision 命中: $msg → ${rule.result}")
                        TaskLogger.log("  决策: $msg")
                        return decisionResult(rule)
                    }
                    continue
                }

                // when_account: 检查账号是否存在
                if (rule.whenAccount != null) {
                    val (success, accounts) = accountManager.listAccountsSync()
                    if (success && accounts.any { it.equals(rule.whenAccount, ignoreCase = true) }) {
                        val msg = rule.message ?: "账号已存在: ${rule.whenAccount}"
                        Log.i(TAG, "decision 命中: $msg → ${rule.result}")
                        TaskLogger.log("  决策: $msg")
                        return decisionResult(rule)
                    }
                    continue
                }

                // when: 检查页面文字
                if (rule.when_.isNotEmpty()) {
                    val el = ui.findElement(FindParams(text = rule.when_))
                    if (el != null) {
                        val matchedText = el.node?.text?.toString() ?: "匹配"
                        val msg = rule.message ?: "检测到: $matchedText"
                        Log.i(TAG, "decision 命中: $msg → ${rule.result}")
                        TaskLogger.log("  决策: $msg")
                        return decisionResult(rule)
                    }
                }
            }
            Thread.sleep(100)
        }
        return StepResult(false, "决策超时，未匹配到任何规则")
    }

    private fun decisionResult(rule: DecisionRule): StepResult {
        val msg = rule.message ?: "决策: ${rule.result}"
        return when (rule.result) {
            "continue" -> StepResult(true, msg)
            "fail" -> StepResult(false, msg, failTask = true)
            "complete" -> StepResult(true, msg, completeTask = true)
            "break" -> StepResult(true, msg, breakLoop = true)
            else -> StepResult(true, msg)
        }
    }

    /**
     * extract_text: 从当前页面提取匹配正则的文字，存入变量
     * params.pattern: 正则表达式
     * params.variable: 存入的变量名
     */
    private fun executeExtractText(step: Step): StepResult {
        val pattern = step.params?.get("pattern") ?: return StepResult(false, "缺少 pattern")
        val variable = step.params["variable"] ?: return StepResult(false, "缺少 variable")
        val timeout = step.timeout ?: DEFAULT_TIMEOUT
        val regex = Regex(pattern)

        val endTime = System.currentTimeMillis() + timeout * 1000L
        while (System.currentTimeMillis() < endTime) {
            val svc = AutoAccessibilityService.instance
            val root = svc?.rootInActiveWindow
            if (root != null) {
                val allText = collectAllText(root)
                for (text in allText) {
                    val match = regex.find(text)
                    if (match != null) {
                        val extracted = match.groupValues.getOrElse(1) { match.value }
                        variables[variable] = extracted
                        Log.i(TAG, "extract_text: $variable = $extracted")
                        TaskLogger.log("  提取: $variable = $extracted")
                        return StepResult(true, "提取成功: $extracted")
                    }
                }
            }
            Thread.sleep(100)
        }
        return StepResult(false, "未匹配到文本: $pattern")
    }

    private fun collectAllText(node: AccessibilityNodeInfo): List<String> {
        val result = mutableListOf<String>()
        val text = node.text?.toString()
        if (!text.isNullOrEmpty()) result.add(text)
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            result.addAll(collectAllText(child))
        }
        return result
    }

    /**
     * http_request: 发送 HTTP 请求
     * params.url: 请求地址（支持变量替换）
     * params.method: GET/POST（默认 POST）
     * params.body: JSON body（支持变量替换）
     * params.header_auth: Authorization header 值
     * params.poll: "true" 时轮询直到响应中 status != "pending"
     * params.poll_interval_ms: 轮询间隔，默认 1000ms
     * params.save: 将响应字段存入变量，格式 "field:variable"，支持逗号分隔和 data.code 这类路径
     */
    private fun executeHttpRequest(step: Step): StepResult {
        val rawUrl = step.params?.get("url") ?: return StepResult(false, "缺少 url")
        val url = resolveVariables(rawUrl)
        val method = step.params["method"] ?: "POST"
        val rawBody = step.params["body"]
        val body = if (rawBody != null) resolveVariables(rawBody) else null
        val auth = step.params["header_auth"]?.let { resolveVariables(it) } ?: ""
        val poll = step.params["poll"] == "true"
        val save = step.params["save"]
        val pollIntervalMs = step.params["poll_interval_ms"]?.toLongOrNull()?.coerceAtLeast(200L) ?: 1000L
        val timeout = if (poll) {
            step.timeout ?: variables["runtime_request_timeout"]?.toIntOrNull() ?: variables["verify_timeout"]?.toIntOrNull() ?: DEFAULT_TIMEOUT
        } else {
            step.timeout ?: DEFAULT_TIMEOUT
        }

        val endTime = System.currentTimeMillis() + timeout * 1000L

        while (true) {
            try {
                val conn = URL(url).openConnection() as HttpURLConnection
                conn.requestMethod = method
                conn.setRequestProperty("Content-Type", "application/json")
                if (auth.isNotEmpty()) {
                    conn.setRequestProperty("Authorization", auth)
                }
                conn.connectTimeout = 5000
                conn.readTimeout = 5000

                if (body != null) {
                    conn.doOutput = true
                    conn.outputStream.write(body.toByteArray())
                }

                val responseCode = conn.responseCode
                val responseBody = try {
                    conn.inputStream.bufferedReader().readText()
                } catch (e: Exception) {
                    conn.errorStream?.bufferedReader()?.readText() ?: ""
                }
                conn.disconnect()

                Log.i(TAG, "http_request: $method $url → $responseCode")

                if (responseCode !in 200..299) {
                    if (System.currentTimeMillis() >= endTime) {
                        return StepResult(false, "HTTP $responseCode: $responseBody")
                    }
                    if (poll) { Thread.sleep(pollIntervalMs); continue }
                    return StepResult(false, "HTTP $responseCode: $responseBody")
                }

                val json = JSONObject(responseBody)

                val status = json.optString("status", "")
                if (poll && status == "pending") {
                    if (System.currentTimeMillis() >= endTime) {
                        return StepResult(false, "轮询超时: ${json.optString("message", "等待运行期请求结果")}")
                    }
                    val retryAfter = json.optLong("retry_after_ms", pollIntervalMs).coerceAtLeast(200L)
                    Thread.sleep(retryAfter)
                    continue
                }

                if (status == "failed" || status == "error") {
                    val errorCode = json.optString("error_code", status)
                    val message = json.optString("message", json.optString("error", "运行期请求失败"))
                    return StepResult(false, "$errorCode: $message")
                }

                saveResponseVariables(json, save)
                return StepResult(true, "HTTP $responseCode OK")
            } catch (e: Exception) {
                if (System.currentTimeMillis() >= endTime) {
                    return StepResult(false, "请求失败: ${e.message}")
                }
                if (poll) { Thread.sleep(pollIntervalMs); continue }
                return StepResult(false, "请求失败: ${e.message}")
            }
        }
    }

    private fun saveResponseVariables(json: JSONObject, save: String?) {
        if (save.isNullOrBlank()) return
        for (mapping in save.split(",")) {
            val parts = mapping.trim().split(":", limit = 2)
            if (parts.size != 2) continue
            val fieldPath = parts[0].trim()
            val variableName = parts[1].trim()
            if (fieldPath.isEmpty() || variableName.isEmpty()) continue
            val value = readJsonPath(json, fieldPath)
            if (value.isNotEmpty()) {
                variables[variableName] = value
                Log.i(TAG, "http_request save: $variableName = ${maskSensitive(variableName, value)}")
                TaskLogger.log("  保存: $variableName = ${maskSensitive(variableName, value)}")
            }
        }
    }

    private fun readJsonPath(json: JSONObject, path: String): String {
        var current: Any? = json
        for (part in path.split(".")) {
            current = when (current) {
                is JSONObject -> current.opt(part)
                else -> null
            }
            if (current == null) return ""
        }
        return when (current) {
            is String -> current
            JSONObject.NULL -> ""
            else -> current.toString()
        }
    }

    private fun maskSensitive(name: String, value: String): String {
        val lowered = name.lowercase()
        val sensitive = listOf("code", "token", "password", "secret", "key", "cvv").any { lowered.contains(it) }
        if (!sensitive) return value
        if (value.length <= 4) return "****"
        return "****${value.takeLast(4)}"
    }

    /**
     * wait_variable: 等待引擎变量出现
     * value: 变量名
     */
    private fun executeWaitVariable(step: Step): StepResult {
        val varName = step.value ?: return StepResult(false, "缺少 value (变量名)")
        val timeout = step.timeout ?: 60
        val endTime = System.currentTimeMillis() + timeout * 1000L
        while (System.currentTimeMillis() < endTime) {
            val v = variables[varName]
            if (!v.isNullOrEmpty()) {
                Log.i(TAG, "wait_variable: $varName = $v")
                TaskLogger.log("  变量就绪: $varName")
                return StepResult(true, "$varName 已就绪")
            }
            Thread.sleep(500)
        }
        return StepResult(false, "等待变量 $varName 超时")
    }

    /** 滚动到顶部 */
    private fun executeScrollTop(step: Step): StepResult {
        val max = step.max ?: 20
        val count = ui.scrollToTop(max)
        return StepResult(true, "滚动到顶部，执行 $count 次")
    }

    /** 滚动到底部 */
    private fun executeScrollBottom(step: Step): StepResult {
        val max = step.max ?: 20
        val count = ui.scrollToBottom(max)
        return StepResult(true, "滚动到底部，执行 $count 次")
    }

    /**
     * find_text: 等待文字出现（轮询 timeout 秒），不点击
     * 配合 on_success/on_error 走分支
     */
    private fun executeFindText(step: Step): StepResult {
        val texts = step.waitFor ?: return StepResult(false, "缺少 wait_for")
        val timeout = step.timeout ?: DEFAULT_TIMEOUT
        val el = ui.waitAndFind(FindParams(text = texts), timeout)
        return if (el != null) StepResult(true, "找到: ${texts.joinToString("/")}")
        else StepResult(false, "未找到: ${texts.joinToString("/")}")
    }

    /**
     * assert_text: 立即断言页面包含指定文字，不存在即任务失败
     */
    private fun executeAssertText(step: Step): StepResult {
        val texts = step.waitFor ?: return StepResult(false, "缺少 wait_for")
        val el = ui.findElement(FindParams(text = texts))
        return if (el != null) StepResult(true, "断言成功: ${texts.joinToString("/")}")
        else StepResult(false, "断言失败，未找到: ${texts.joinToString("/")}", failTask = true)
    }

    /** 点击返回键 */
    private fun executeBack(step: Step): StepResult {
        ui.keyEvent("KEYCODE_BACK")
        return StepResult(true, "已返回")
    }

    /** 替换字符串中的 {variable} 为变量值 */
    fun resolveVariables(text: String): String {
        var result = text
        for ((key, value) in variables) {
            result = result.replace("{$key}", value)
        }
        return result
    }

    private fun resolvePackageParam(step: Step): String? {
        val raw = step.params?.get("package")
            ?: step.params?.get("package_name")
            ?: step.value
            ?: return null
        return resolveVariables(raw)
    }
}

data class StepResult(
    val success: Boolean,
    val message: String,
    val completeTask: Boolean = false,
    val failTask: Boolean = false,
    val breakLoop: Boolean = false
)
