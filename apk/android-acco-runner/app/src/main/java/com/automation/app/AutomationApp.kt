package com.automation.app

import android.app.Application

class AutomationApp : Application() {

    lateinit var taskManager: TaskManager
    lateinit var httpServer: AutomationHttpServer

    override fun onCreate() {
        super.onCreate()
        instance = this
        taskManager = TaskManager(this)
        taskManager.configureRuntime()
    }

    companion object {
        lateinit var instance: AutomationApp
            private set
    }
}
