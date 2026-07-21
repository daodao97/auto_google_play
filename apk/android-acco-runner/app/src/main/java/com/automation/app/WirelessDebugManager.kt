package com.automation.app

import android.content.Context

class WirelessDebugManager(private val context: Context) {

    companion object {
        const val KEY_WIRELESS_DEBUG_ENABLED = "wireless_debug_enabled"
        const val KEY_ADB_TCP_PORT = "adb_tcp_port"
        const val DEFAULT_ADB_TCP_PORT = 5555
    }

    data class Result(
        val success: Boolean,
        val message: String,
        val detail: String = ""
    )

    private val prefs = context.getSharedPreferences(TaskManager.PREF_NAME, Context.MODE_PRIVATE)

    fun isEnabled(): Boolean = prefs.getBoolean(KEY_WIRELESS_DEBUG_ENABLED, false)

    fun setEnabled(enabled: Boolean) {
        prefs.edit().putBoolean(KEY_WIRELESS_DEBUG_ENABLED, enabled).apply()
    }

    fun getPort(): Int = normalizePort(prefs.getInt(KEY_ADB_TCP_PORT, DEFAULT_ADB_TCP_PORT))

    fun setPort(port: Int) {
        prefs.edit().putInt(KEY_ADB_TCP_PORT, normalizePort(port)).apply()
    }

    fun enforceIfEnabled(): Result {
        if (!isEnabled()) return Result(true, "无线调试固定端口未启用")
        return applyPort(getPort())
    }

    fun applyPort(portInput: Int): Result {
        val port = normalizePort(portInput)
        val command = listOf(
            "settings put global adb_enabled 1",
            "setprop persist.adb.tcp.port $port",
            "setprop persist.service.adb.tcp.port $port",
            "setprop service.adb.tcp.port $port",
            "stop adbd",
            "start adbd"
        ).joinToString("; ")

        val result = runRoot(command)
        if (!result.success) {
            return Result(false, "无线调试设置失败: ${result.summary()}", result.detail())
        }

        Thread.sleep(500)
        val status = readStatus()
        val expected = port.toString()
        val confirmed = status.servicePort == expected || status.persistPort == expected || status.legacyPersistPort == expected
        if (!confirmed) {
            return Result(false, "无线调试命令已执行，但端口属性未确认为 $port", status.toDisplayText())
        }
        return Result(true, "无线调试端口已固定为 $port", status.toDisplayText())
    }

    fun readStatusText(): String = readStatus().toDisplayText()

    fun readServiceStatusText(): String {
        val status = readStatus()
        val currentPort = status.activePort()
        val fixedPort = if (isEnabled()) getPort().toString() else "未启用"
        return if (currentPort.isNotEmpty()) {
            "无线 ADB: 已开启 | 当前端口: $currentPort | 固定端口: $fixedPort"
        } else {
            "无线 ADB: 未开启 | 固定端口: $fixedPort"
        }
    }

    private fun readStatus(): AdbTcpStatus {
        return AdbTcpStatus(
            servicePort = getProp("service.adb.tcp.port"),
            persistPort = getProp("persist.adb.tcp.port"),
            legacyPersistPort = getProp("persist.service.adb.tcp.port"),
            adbEnabled = readSetting("global", "adb_enabled")
        )
    }

    private fun getProp(name: String): String {
        val result = runRoot("getprop $name")
        return if (result.success) result.stdout.trim() else ""
    }

    private fun readSetting(namespace: String, name: String): String {
        val result = runRoot("settings get $namespace $name")
        return if (result.success) result.stdout.trim() else ""
    }

    private fun normalizePort(port: Int): Int {
        return if (port in 1024..65535) port else DEFAULT_ADB_TCP_PORT
    }

    private fun runRoot(command: String): ShellResult {
        return try {
            val process = Runtime.getRuntime().exec(arrayOf("su", "-c", command))
            val stdout = process.inputStream.bufferedReader().readText().trim()
            val stderr = process.errorStream.bufferedReader().readText().trim()
            val exitCode = process.waitFor()
            ShellResult(exitCode == 0, exitCode, stdout, stderr)
        } catch (e: Exception) {
            ShellResult(false, -1, "", e.message.orEmpty())
        }
    }

    private data class ShellResult(
        val success: Boolean,
        val exitCode: Int,
        val stdout: String,
        val stderr: String
    ) {
        fun summary(): String = when {
            stderr.isNotEmpty() -> stderr
            stdout.isNotEmpty() -> stdout
            else -> "exit=$exitCode"
        }

        fun detail(): String = "exit=$exitCode stdout=$stdout stderr=$stderr"
    }

    private data class AdbTcpStatus(
        val servicePort: String,
        val persistPort: String,
        val legacyPersistPort: String,
        val adbEnabled: String
    ) {
        fun activePort(): String {
            return listOf(servicePort, persistPort, legacyPersistPort)
                .firstOrNull { it.isNotBlank() && it != "0" && it != "-1" } ?: ""
        }

        fun toDisplayText(): String {
            return listOf(
                "adb_enabled=$adbEnabled",
                "service.adb.tcp.port=$servicePort",
                "persist.adb.tcp.port=$persistPort",
                "persist.service.adb.tcp.port=$legacyPersistPort"
            ).joinToString("\n")
        }
    }
}
