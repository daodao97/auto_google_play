package com.automation.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.os.Build
import android.util.Log

class BootReceiver : BroadcastReceiver() {

    companion object {
        private const val TAG = "BootReceiver"
    }

    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action == Intent.ACTION_BOOT_COMPLETED) {
            Log.i(TAG, "开机自启动")

            unlockScreenIfNeeded()

            // 启动前台服务
            val serviceIntent = Intent(context, AutomationService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(serviceIntent)
            } else {
                context.startService(serviceIntent)
            }
            // 启动 Activity
            val launchIntent = Intent(context, MainActivity::class.java)
            launchIntent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(launchIntent)
        }
    }

    private fun unlockScreenIfNeeded() {
        try {
            if (isUnlockedHomeScreen()) {
                Log.i(TAG, "当前已在解锁后的桌面，跳过上滑")
                return
            }
            Runtime.getRuntime().exec(arrayOf("su", "-c", "input keyevent 82"))
            Thread.sleep(500)
            if (isUnlockedHomeScreen()) {
                Log.i(TAG, "按键后已进入桌面，跳过上滑")
                return
            }
            Runtime.getRuntime().exec(arrayOf("su", "-c", "input swipe 540 1800 540 800"))
        } catch (e: Exception) {
            Log.w(TAG, "开机解锁动作失败: ${e.message}")
        }
    }

    private fun isUnlockedHomeScreen(): Boolean {
        val windowState = runRoot("dumpsys window")
        if (windowState.isBlank()) return false
        val lower = windowState.lowercase()
        val locked = listOf(
            "keyguard",
            "lockscreen",
            "mshowinglockscreen=true",
            "mdreaminglockscreen=true",
            "isstatusbarkeyguard=true"
        ).any { lower.contains(it) }
        if (locked) return false

        val focusLines = windowState.lineSequence()
            .filter { it.contains("mCurrentFocus") || it.contains("mFocusedApp") || it.contains("mTopFullscreenOpaqueWindowState") }
            .joinToString("\n")
            .lowercase()

        return listOf(
            "launcher",
            "trebuchet",
            "home",
            "com.miui.home",
            "com.android.launcher",
            "com.google.android.apps.nexuslauncher",
            "com.sec.android.app.launcher",
            "com.oppo.launcher",
            "com.vivo.launcher",
            "com.huawei.android.launcher"
        ).any { focusLines.contains(it) }
    }

    private fun runRoot(command: String): String {
        return try {
            val process = Runtime.getRuntime().exec(arrayOf("su", "-c", command))
            val output = process.inputStream.bufferedReader().readText()
            process.waitFor()
            output
        } catch (_: Exception) {
            ""
        }
    }
}
