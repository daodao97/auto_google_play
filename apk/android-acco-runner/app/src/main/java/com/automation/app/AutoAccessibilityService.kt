package com.automation.app

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.util.Log
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject

/**
 * 无障碍服务：直接访问 UI 树，无需 dump XML
 * 所有 UI 操作通过此服务完成
 */
class AutoAccessibilityService : AccessibilityService() {

    companion object {
        private const val TAG = "A11yService"
        var instance: AutoAccessibilityService? = null
            private set
    }

    // 当前前台包名缓存，通过事件实时更新
    var currentPackage: String = ""
        private set

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        Log.i(TAG, "无障碍服务已连接")
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event?.eventType == AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED) {
            val pkg = event.packageName?.toString() ?: return
            if (pkg != currentPackage) {
                currentPackage = pkg
            }
        }
    }
    override fun onInterrupt() {}

    override fun onDestroy() {
        instance = null
        super.onDestroy()
    }

    // ==================== 查找元素 ====================

    fun findByText(texts: List<String>): AccessibilityNodeInfo? {
        val root = rootInActiveWindow ?: return null

        // 收集所有候选文字的匹配结果，带优先级评分
        data class Match(val node: AccessibilityNodeInfo, val score: Int, val keyword: String)

        val allMatches = mutableListOf<Match>()

        for (text in texts) {
            val candidates = mutableListOf<AccessibilityNodeInfo>()

            // 系统 API 搜索
            val nodes = root.findAccessibilityNodeInfosByText(text)
            if (!nodes.isNullOrEmpty()) {
                candidates.addAll(nodes.filter {
                    val t = it.text?.toString() ?: ""
                    val d = it.contentDescription?.toString() ?: ""
                    val h = it.hintText?.toString() ?: ""
                    t.contains(text, ignoreCase = true)
                        || d.contains(text, ignoreCase = true)
                        || h.contains(text, ignoreCase = true)
                })
            }

            // 树遍历补充
            if (candidates.isEmpty()) {
                collectMatchingNodes(root, text, candidates)
            }

            // 对每个候选打分
            for (node in candidates) {
                val nodeText = node.text?.toString() ?: ""
                val isExact = nodeText.equals(text, ignoreCase = true)
                val isClickable = node.isClickable
                val score = when {
                    isExact && isClickable -> 4  // 最高：精确+可点击
                    isExact -> 3                 // 精确但不可点击
                    isClickable -> 2             // 模糊但可点击
                    else -> 1                    // 模糊且不可点击
                }
                allMatches.add(Match(node, score, text))
            }
        }

        if (allMatches.isEmpty()) return null

        // 按 score 降序，相同 score 保持文字列表中的顺序（靠前优先）
        val maxScore = allMatches.maxOf { it.score }
        val best = allMatches.first { it.score == maxScore }
        Log.d(TAG, "findByText 最佳匹配: keyword='${best.keyword}', score=${best.score}, text='${best.node.text}', clickable=${best.node.isClickable}")
        return best.node
    }

    private fun collectMatchingNodes(node: AccessibilityNodeInfo, text: String, results: MutableList<AccessibilityNodeInfo>) {
        val nodeText = node.text?.toString() ?: ""
        val nodeDesc = node.contentDescription?.toString() ?: ""
        val nodeHint = node.hintText?.toString() ?: ""
        if (nodeText.contains(text, ignoreCase = true)
            || nodeDesc.contains(text, ignoreCase = true)
            || nodeHint.contains(text, ignoreCase = true)) {
            results.add(node)
        }
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            collectMatchingNodes(child, text, results)
        }
    }

    fun findByResourceId(id: String): AccessibilityNodeInfo? {
        val root = rootInActiveWindow ?: return null
        val nodes = root.findAccessibilityNodeInfosByViewId(id)
        return nodes?.firstOrNull()
    }

    fun findByClassName(className: String): AccessibilityNodeInfo? {
        val root = rootInActiveWindow ?: return null
        return findNodeByClass(root, className)
    }

    private fun findNodeByClass(node: AccessibilityNodeInfo, className: String): AccessibilityNodeInfo? {
        if (node.className?.toString()?.contains(className) == true) return node
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val found = findNodeByClass(child, className)
            if (found != null) return found
        }
        return null
    }

    fun findScrollable(): AccessibilityNodeInfo? {
        val root = rootInActiveWindow ?: return null
        return findScrollableNode(root)
    }

    private fun findScrollableNode(node: AccessibilityNodeInfo): AccessibilityNodeInfo? {
        if (node.isScrollable) return node
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            val found = findScrollableNode(child)
            if (found != null) return found
        }
        return null
    }

    // ==================== 操作 ====================

    fun clickNode(node: AccessibilityNodeInfo): Boolean {
        // 先尝试直接点击
        if (node.isClickable) {
            return node.performAction(AccessibilityNodeInfo.ACTION_CLICK)
        }
        // 向上找可点击的父节点
        var parent = node.parent
        while (parent != null) {
            if (parent.isClickable) {
                return parent.performAction(AccessibilityNodeInfo.ACTION_CLICK)
            }
            parent = parent.parent
        }
        // 都不行，用手势点击坐标
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return gestureClick(rect.centerX(), rect.centerY())
    }

    fun setNodeText(node: AccessibilityNodeInfo, text: String): Boolean {
        // 先聚焦
        node.performAction(AccessibilityNodeInfo.ACTION_FOCUS)
        // 清空
        node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, "")
        })
        Thread.sleep(100)
        // 设置文本
        return node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply {
            putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text)
        })
    }

    fun gestureClick(x: Int, y: Int): Boolean {
        val path = Path()
        path.moveTo(x.toFloat(), y.toFloat())
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, 50))
            .build()
        return dispatchGesture(gesture, null, null)
    }

    fun gestureSwipe(x1: Int, y1: Int, x2: Int, y2: Int, duration: Long = 300): Boolean {
        val path = Path()
        path.moveTo(x1.toFloat(), y1.toFloat())
        path.lineTo(x2.toFloat(), y2.toFloat())
        val gesture = GestureDescription.Builder()
            .addStroke(GestureDescription.StrokeDescription(path, 0, duration))
            .build()
        return dispatchGesture(gesture, null, null)
    }

    fun scrollDown(node: AccessibilityNodeInfo): Boolean {
        return node.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
    }

    fun scrollUp(node: AccessibilityNodeInfo): Boolean {
        return node.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD)
    }

    fun getCurrentPackageName(): String {
        // 优先使用事件缓存，fallback 到 rootInActiveWindow
        return currentPackage.ifEmpty {
            rootInActiveWindow?.packageName?.toString() ?: ""
        }
    }

    fun getNodeCenter(node: AccessibilityNodeInfo): Pair<Int, Int> {
        val rect = Rect()
        node.getBoundsInScreen(rect)
        return Pair(rect.centerX(), rect.centerY())
    }

    fun dumpTreeJson(): JSONObject {
        val root = rootInActiveWindow
        val json = JSONObject()
        json.put("package", getCurrentPackageName())
        json.put("timestamp", System.currentTimeMillis())
        json.put("root", if (root != null) nodeToJson(root, 0) else JSONObject.NULL)
        return json
    }

    private fun nodeToJson(node: AccessibilityNodeInfo, depth: Int): JSONObject {
        val rect = Rect()
        node.getBoundsInScreen(rect)
        val obj = JSONObject()
        obj.put("depth", depth)
        obj.put("text", node.text?.toString() ?: "")
        obj.put("content_description", node.contentDescription?.toString() ?: "")
        obj.put("hint", node.hintText?.toString() ?: "")
        obj.put("class", node.className?.toString() ?: "")
        obj.put("resource_id", node.viewIdResourceName ?: "")
        obj.put("package", node.packageName?.toString() ?: "")
        obj.put("clickable", node.isClickable)
        obj.put("enabled", node.isEnabled)
        obj.put("focusable", node.isFocusable)
        obj.put("focused", node.isFocused)
        obj.put("scrollable", node.isScrollable)
        obj.put("selected", node.isSelected)
        obj.put("bounds", JSONObject().apply {
            put("left", rect.left)
            put("top", rect.top)
            put("right", rect.right)
            put("bottom", rect.bottom)
            put("center_x", rect.centerX())
            put("center_y", rect.centerY())
        })
        val children = JSONArray()
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            children.put(nodeToJson(child, depth + 1))
        }
        obj.put("children", children)
        return obj
    }
}
