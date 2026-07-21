package com.automation.app

import android.content.Context
import android.content.Intent

object AppControl {
    private val packagePattern = Regex("^[a-zA-Z][a-zA-Z0-9_]*(\\.[a-zA-Z0-9_]+)+$")

    fun validatePackageName(packageName: String): String {
        val trimmed = packageName.trim()
        require(trimmed.isNotEmpty()) { "缺少 package/package_name" }
        require(packagePattern.matches(trimmed)) { "无效包名: $trimmed" }
        return trimmed
    }

    fun openApp(context: Context, packageName: String): Pair<Boolean, String> {
        val pkg = validatePackageName(packageName)
        val launchIntent = context.packageManager.getLaunchIntentForPackage(pkg)
        if (launchIntent != null) {
            launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(launchIntent)
            return true to "已打开 App: $pkg"
        }

        val result = runDeviceCommand("monkey -p $pkg -c android.intent.category.LAUNCHER 1")
        return if (result.first) {
            true to "已打开 App: $pkg"
        } else {
            false to "打开 App 失败: ${result.second}"
        }
    }

    fun forceStop(packageName: String): Pair<Boolean, String> {
        val pkg = validatePackageName(packageName)
        val result = runDeviceCommand("am force-stop $pkg")
        return if (result.first) {
            true to "已停止 App: $pkg"
        } else {
            false to "停止 App 失败: ${result.second}"
        }
    }

    fun clearApp(context: Context, packageName: String): Pair<Boolean, String> {
        val pkg = validatePackageName(packageName)
        if (pkg == context.packageName) {
            return false to "拒绝清理当前自动化 App"
        }

        runDeviceCommand("am force-stop $pkg")
        val result = runDeviceCommand("pm clear $pkg")
        return if (result.first) {
            true to "已清理 App 数据: $pkg"
        } else {
            false to "清理 App 数据失败: ${result.second}"
        }
    }

    fun runDeviceCommand(command: String): Pair<Boolean, String> {
        val candidates = listOf(
            arrayOf("su", "-c", command),
            arrayOf("sh", "-c", command)
        )
        var lastOutput = ""
        for (candidate in candidates) {
            try {
                val process = Runtime.getRuntime().exec(candidate)
                val stdout = process.inputStream.bufferedReader().readText()
                val stderr = process.errorStream.bufferedReader().readText()
                val exit = process.waitFor()
                lastOutput = listOf(stdout, stderr).filter { it.isNotBlank() }.joinToString("\n")
                if (exit == 0) return true to lastOutput
            } catch (e: Exception) {
                lastOutput = e.message ?: "执行失败"
            }
        }
        return false to lastOutput
    }
}
