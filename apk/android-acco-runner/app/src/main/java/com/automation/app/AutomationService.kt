package com.automation.app

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.util.Log

class AutomationService : Service() {

    companion object {
        private const val TAG = "AutoService"
        private const val CHANNEL_ID = "automation_service"
        private const val NOTIFICATION_ID = 1
    }

    private var httpServer: AutomationHttpServer? = null
    @Volatile private var wirelessDebugEnforceRunning = false

    override fun onCreate() {
        super.onCreate()
        Log.i(TAG, "服务启动")
        createNotificationChannel()
        startForeground(NOTIFICATION_ID, buildNotification())

        val app = application as AutomationApp
        httpServer = AutomationHttpServer(app.taskManager, 8080)
        httpServer?.start()
        app.httpServer = httpServer!!
        Log.i(TAG, "HTTP Server 已启动: 8080")
        enforceWirelessDebugIfNeeded()
    }

    override fun onDestroy() {
        httpServer?.stop()
        Log.i(TAG, "服务停止")
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        enforceWirelessDebugIfNeeded()
        return START_STICKY
    }

    private fun enforceWirelessDebugIfNeeded() {
        if (wirelessDebugEnforceRunning) return
        wirelessDebugEnforceRunning = true
        Thread {
            try {
                val manager = WirelessDebugManager(this)
                if (!manager.isEnabled()) return@Thread
                val result = manager.enforceIfEnabled()
                if (result.success) {
                    Log.i(TAG, result.message)
                    TaskLogger.log(result.message)
                } else {
                    Log.w(TAG, "${result.message}\n${result.detail}")
                    TaskLogger.log(result.message)
                }
            } finally {
                wirelessDebugEnforceRunning = false
            }
        }.start()
    }

    private fun createNotificationChannel() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            val channel = NotificationChannel(
                CHANNEL_ID,
                "自动化服务",
                NotificationManager.IMPORTANCE_LOW
            ).apply {
                description = "保持自动化服务后台运行"
            }
            val manager = getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(channel)
        }
    }

    private fun buildNotification(): Notification {
        val builder = if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
            Notification.Builder(this, CHANNEL_ID)
        } else {
            @Suppress("DEPRECATION")
            Notification.Builder(this)
        }
        return builder
            .setContentTitle("AutoAcco 运行中")
            .setContentText("HTTP Server: 8080")
            .setSmallIcon(android.R.drawable.ic_menu_manage)
            .setOngoing(true)
            .build()
    }
}
