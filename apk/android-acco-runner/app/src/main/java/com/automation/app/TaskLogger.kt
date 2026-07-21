package com.automation.app

import android.os.Handler
import android.os.Looper
import java.text.SimpleDateFormat
import java.util.Locale

/**
 * 任务执行日志管理器（单例）
 * UI 通过 listener 实时接收日志更新
 */
object TaskLogger {

    private const val MAX_LINES = 200
    private val lines = mutableListOf<String>()
    private val mainHandler = Handler(Looper.getMainLooper())
    private val timeFormat = SimpleDateFormat("HH:mm:ss", Locale.getDefault())

    var onLogUpdated: ((List<String>) -> Unit)? = null

    fun log(message: String) {
        val time = timeFormat.format(System.currentTimeMillis())
        val line = "[$time] $message"
        synchronized(lines) {
            lines.add(line)
            if (lines.size > MAX_LINES) {
                lines.removeAt(0)
            }
        }
        notifyUI()
    }

    fun clear() {
        synchronized(lines) { lines.clear() }
        notifyUI()
    }

    fun getAll(): List<String> {
        synchronized(lines) { return lines.toList() }
    }

    private fun notifyUI() {
        val snapshot = getAll()
        mainHandler.post { onLogUpdated?.invoke(snapshot) }
    }
}
