package com.automation.app

import android.content.Context
import android.util.Log
import android.view.accessibility.AccessibilityNodeInfo

/**
 * UI 交互层
 * 通过 AccessibilityService 直接操作 UI 树
 * shell 命令仅用于 launch/forceStop 等系统操作
 */
class UIInteractor(private val context: Context) {

    companion object {
        private const val TAG = "UIInteractor"
    }

    private val a11y: AutoAccessibilityService?
        get() = AutoAccessibilityService.instance

    // ==================== 元素查找 ====================

    fun findElement(find: FindParams): UiElement? {
        val svc = a11y ?: run {
            Log.e(TAG, "无障碍服务未连接")
            return null
        }
        val node = when {
            find.text != null -> svc.findByText(find.text)
            find.resourceId != null -> svc.findByResourceId(find.resourceId)
            find.className != null -> svc.findByClassName(find.className)
            find.coords != null -> return UiElement(find.coords[0], find.coords[1])
            else -> null
        } ?: return null

        val (cx, cy) = svc.getNodeCenter(node)
        return UiElement(cx, cy, node)
    }

    /**
     * 带等待的元素查找：轮询直到找到或超时
     * 这是核心方法，所有需要找元素的 action 都应该用这个
     */
    fun waitAndFind(find: FindParams, timeout: Int = 10): UiElement? {
        // 坐标类型不需要等待
        if (find.coords != null) return UiElement(find.coords[0], find.coords[1])

        val endTime = System.currentTimeMillis() + timeout * 1000L
        while (System.currentTimeMillis() < endTime) {
            val el = findElement(find)
            if (el != null) return el
            Thread.sleep(100)
        }
        Log.w(TAG, "waitAndFind 超时: $find")
        return null
    }

    /**
     * 对 node 输入文本
     * method: "set_text"(默认) / "shell" / "clipboard"
     */
    fun inputToNode(node: AccessibilityNodeInfo, text: String, method: String = "set_text"): Boolean {
        val svc = a11y ?: return false
        val rect = android.graphics.Rect()
        node.getBoundsInScreen(rect)

        if (method == "shell") {
            svc.gestureClick(rect.centerX(), rect.centerY())
            Thread.sleep(200)
            shellInput(text)
            return true
        }

        if (method == "clipboard") {
            svc.gestureClick(rect.centerX(), rect.centerY())
            Thread.sleep(200)
            clipboardInput(text)
            return true
        }

        // 默认 set_text
        val result = svc.setNodeText(node, text)
        Log.d(TAG, "inputToNode via setNodeText: $text → $result")
        return result
    }

    /**
     * 直接用 shell 输入文本到当前焦点，不需要找元素
     */
    fun shellInput(text: String) {
        rootExec("input text '${text.replace("'", "\\'")}'")
        Log.d(TAG, "shellInput: $text")
    }

    /**
     * 通过剪贴板粘贴文本到当前焦点
     */
    fun clipboardInput(text: String) {
        val clipboard = context.getSystemService(Context.CLIPBOARD_SERVICE) as android.content.ClipboardManager
        clipboard.setPrimaryClip(android.content.ClipData.newPlainText("input", text))
        Thread.sleep(300)
        rootExec("input keyevent 279") // KEYCODE_PASTE
        Log.d(TAG, "clipboardInput: $text")
    }

    // ==================== UI 操作 ====================

    fun tap(x: Int, y: Int) {
        Log.d(TAG, "tap($x, $y)")
        a11y?.gestureClick(x, y)
        Thread.sleep(100)
    }

    fun clickElement(element: UiElement) {
        val svc = a11y ?: return
        if (element.node != null) {
            svc.clickNode(element.node)
        } else {
            svc.gestureClick(element.centerX, element.centerY)
        }
        Thread.sleep(100)
    }

    fun swipe(x1: Int, y1: Int, x2: Int, y2: Int, duration: Int = 300) {
        a11y?.gestureSwipe(x1, y1, x2, y2, duration.toLong())
        Thread.sleep(100)
    }

    fun isScrollable(): Boolean {
        return a11y?.findScrollable() != null
    }

    fun scrollDown(): Boolean {
        val scrollable = a11y?.findScrollable() ?: return false
        return a11y?.scrollDown(scrollable) ?: false
    }

    fun scrollUp(): Boolean {
        val scrollable = a11y?.findScrollable() ?: return false
        return a11y?.scrollUp(scrollable) ?: false
    }

    /** 循环滚到顶部（最多 max 次或连续 2 次失败即停） */
    fun scrollToTop(max: Int = 20): Int {
        var count = 0
        var consecutiveFail = 0
        for (i in 0 until max) {
            if (scrollUp()) {
                count++
                consecutiveFail = 0
                Thread.sleep(300)
            } else {
                consecutiveFail++
                if (consecutiveFail >= 2) break
                Thread.sleep(200)
            }
        }
        return count
    }

    /** 循环滚到底部 */
    fun scrollToBottom(max: Int = 20): Int {
        var count = 0
        var consecutiveFail = 0
        for (i in 0 until max) {
            if (scrollDown()) {
                count++
                consecutiveFail = 0
                Thread.sleep(300)
            } else {
                consecutiveFail++
                if (consecutiveFail >= 2) break
                Thread.sleep(200)
            }
        }
        return count
    }

    // ==================== 系统操作（仍需 shell） ====================

    fun keyEvent(code: Int) {
        rootExec("input keyevent $code")
    }

    fun keyEvent(name: String) {
        rootExec("input keyevent $name")
    }

    fun launchApp(packageName: String) {
        rootExec("monkey -p $packageName -c android.intent.category.LAUNCHER 1")
    }

    fun launchIntent(intent: String) {
        rootExec("am start $intent")
    }

    fun forceStop(packageName: String) {
        rootExec("am force-stop $packageName")
    }

    fun getCurrentPackage(): String {
        return a11y?.getCurrentPackageName() ?: ""
    }

    private fun rootExec(cmd: String): String {
        return try {
            val process = Runtime.getRuntime().exec(arrayOf("su", "-c", cmd))
            val output = process.inputStream.bufferedReader().readText().trim()
            process.waitFor()
            output
        } catch (e: Exception) {
            Log.e(TAG, "shell: $cmd - ${e.message}")
            ""
        }
    }
}

data class UiElement(
    val centerX: Int,
    val centerY: Int,
    val node: AccessibilityNodeInfo? = null
)
