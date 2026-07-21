package com.automation.app

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.os.Build
import android.util.Log
import org.json.JSONObject
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

/**
 * 与 GmsAccountHook 插件通信，管理 Google 账号
 *
 * APP → Broadcast(ACTION_ACCOUNT_CMD) → GMS 中的插件
 * GMS 中的插件 → Broadcast(ACTION_ACCOUNT_RESULT) → APP
 */
class PluginAccountManager(private val context: Context) {

    companion object {
        private const val TAG = "PluginAccountMgr"
        private const val ACTION_CMD = "com.automation.app.ACTION_ACCOUNT_CMD"
        private const val ACTION_RESULT = "com.automation.app.ACTION_ACCOUNT_RESULT"
        private const val TIMEOUT_SEC = 3L
    }

    /**
     * 异步回调方式：列出账号
     */
    fun listAccounts(callback: (Boolean, List<String>, String) -> Unit) {
        sendCommand("list_accounts", null) { result ->
            val success = result.optBoolean("success", false)
            val message = result.optString("message", "")
            val accounts = mutableListOf<String>()
            val arr = result.optJSONArray("accounts")
            if (arr != null) {
                for (i in 0 until arr.length()) {
                    accounts.add(arr.getString(i))
                }
            }
            callback(success, accounts, message)
        }
    }

    /**
     * 移除指定账号
     */
    fun removeAccount(email: String, callback: (Boolean, String) -> Unit) {
        sendCommand("remove_account", mapOf("email" to email)) { result ->
            callback(result.optBoolean("success", false), result.optString("message", ""))
        }
    }

    /**
     * 移除所有账号
     */
    fun removeAllAccounts(callback: (Boolean, String) -> Unit) {
        sendCommand("remove_all", null) { result ->
            callback(result.optBoolean("success", false), result.optString("message", ""))
        }
    }

    /**
     * 同步版本：列出账号（用于 ExecutionEngine）
     */
    fun listAccountsSync(): Pair<Boolean, List<String>> {
        val latch = CountDownLatch(1)
        var success = false
        val accounts = mutableListOf<String>()

        listAccounts { s, list, _ ->
            success = s
            accounts.addAll(list)
            latch.countDown()
        }

        latch.await(TIMEOUT_SEC, TimeUnit.SECONDS)
        return Pair(success, accounts)
    }

    /**
     * 同步版本：移除所有账号
     */
    fun removeAllAccountsSync(): Pair<Boolean, String> {
        val latch = CountDownLatch(1)
        var success = false
        var message = "超时"

        removeAllAccounts { s, m ->
            success = s
            message = m
            latch.countDown()
        }

        latch.await(TIMEOUT_SEC, TimeUnit.SECONDS)
        return Pair(success, message)
    }

    private fun sendCommand(
        action: String,
        extras: Map<String, String>?,
        callback: (JSONObject) -> Unit
    ) {
        val requestId = System.currentTimeMillis().toString()

        // 注册结果接收器
        val receiver = object : BroadcastReceiver() {
            override fun onReceive(ctx: Context, intent: Intent) {
                val resultStr = intent.getStringExtra("result") ?: return
                try {
                    val result = JSONObject(resultStr)
                    if (result.optString("request_id") == requestId) {
                        context.unregisterReceiver(this)
                        callback(result)
                    }
                } catch (e: Exception) {
                    Log.e(TAG, "解析结果失败: ${e.message}")
                }
            }
        }

        val filter = IntentFilter(ACTION_RESULT)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            context.registerReceiver(receiver, filter, Context.RECEIVER_EXPORTED)
        } else {
            context.registerReceiver(receiver, filter)
        }

        // 发送指令
        val intent = Intent(ACTION_CMD)
        intent.putExtra("action", action)
        intent.putExtra("request_id", requestId)
        extras?.forEach { (k, v) -> intent.putExtra(k, v) }
        context.sendBroadcast(intent)
        Log.i(TAG, "发送指令: action=$action, requestId=$requestId")

        // 超时自动注销
        android.os.Handler(android.os.Looper.getMainLooper()).postDelayed({
            try {
                context.unregisterReceiver(receiver)
                Log.w(TAG, "指令超时: $action")
            } catch (_: Exception) {}
        }, TIMEOUT_SEC * 1000)
    }
}
