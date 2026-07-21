package com.automation.app

import com.google.gson.Gson
import com.google.gson.reflect.TypeToken
import fi.iki.elonen.NanoHTTPD

/**
 * HTTP Server: 接收远程任务指令
 * 端口: 8080
 */
class AutomationHttpServer(
    private val taskManager: TaskManager,
    port: Int = 8080
) : NanoHTTPD(port) {

    private val gson = Gson()

    override fun serve(session: IHTTPSession): Response {
        val uri = session.uri
        val method = session.method

        return try {
            when {
                uri == "/api/task/start" && method == Method.POST -> handleStart(session)
                uri == "/api/task/pause" && method == Method.POST -> handlePause()
                uri == "/api/task/resume" && method == Method.POST -> handleResume()
                uri == "/api/task/stop" && method == Method.POST -> handleStop()
                uri == "/api/task/status" && method == Method.GET -> handleStatus()
                uri == "/api/ping" && method == Method.GET -> json(mapOf("status" to "ok"))
                uri == "/api/device/reboot" && method == Method.POST -> handleReboot()
                uri == "/api/device/screen-wake" && method == Method.POST -> handleScreenWake()
                uri == "/api/device/screen-lock" && method == Method.POST -> handleScreenLock()
                uri == "/api/device/brightness" && method == Method.POST -> handleBrightness(session)
                else -> json(mapOf("error" to "Not Found"), Response.Status.NOT_FOUND)
            }
        } catch (e: Exception) {
            json(mapOf("error" to e.message), Response.Status.INTERNAL_ERROR)
        }
    }

    private fun handleStart(session: IHTTPSession): Response {
        val body = readBody(session)
        val type = object : TypeToken<Map<String, Any>>() {}.type
        val req: Map<String, Any> = gson.fromJson(body, type)

        val taskId = req["task_id"] as? String ?: return json(mapOf("error" to "缺少 task_id"), Response.Status.BAD_REQUEST)
        val config = req["config"] as? String ?: return json(mapOf("error" to "缺少 config"), Response.Status.BAD_REQUEST)
        val callbackUrl = req["callback_url"] as? String ?: ""

        @Suppress("UNCHECKED_CAST")
        val params = (req["params"] as? Map<String, String>) ?: emptyMap()

        val task = Task(id = taskId, configName = config, params = params, callbackUrl = callbackUrl)
        val result = taskManager.startTask(task)

        return if (result.isSuccess) {
            json(mapOf("status" to "success", "task_id" to taskId))
        } else {
            json(mapOf("status" to "error", "message" to result.exceptionOrNull()?.message), Response.Status.BAD_REQUEST)
        }
    }

    private fun handlePause(): Response {
        val result = taskManager.pause()
        return json(mapOf(
            "status" to if (result.isSuccess) "success" else "error",
            "message" to (result.getOrNull() ?: result.exceptionOrNull()?.message)
        ))
    }

    private fun handleResume(): Response {
        val result = taskManager.resume()
        return json(mapOf(
            "status" to if (result.isSuccess) "success" else "error",
            "message" to (result.getOrNull() ?: result.exceptionOrNull()?.message)
        ))
    }

    private fun handleStop(): Response {
        taskManager.stop()
        return json(mapOf("status" to "success", "message" to "已停止"))
    }

    private fun handleStatus(): Response {
        return json(taskManager.getStatus())
    }

    private fun handleReboot(): Response {
        taskManager.rebootDevice()
        return json(mapOf("status" to "rebooting"))
    }

    private fun handleScreenWake(): Response {
        return try {
            Runtime.getRuntime().exec(arrayOf("su", "-c",
                "input keyevent 224 && sleep 0.5 && input swipe 540 1800 540 800"))
            json(mapOf("status" to "success", "message" to "screen wakeup + unlock sent"))
        } catch (e: Exception) {
            json(mapOf("status" to "error", "message" to e.message), Response.Status.INTERNAL_ERROR)
        }
    }

    private fun handleScreenLock(): Response {
        return try {
            Runtime.getRuntime().exec(arrayOf("su", "-c", "input keyevent 223"))
            json(mapOf("status" to "success", "message" to "screen sleep sent"))
        } catch (e: Exception) {
            json(mapOf("status" to "error", "message" to e.message), Response.Status.INTERNAL_ERROR)
        }
    }

    private fun handleBrightness(session: IHTTPSession): Response {
        val body = readBody(session)
        val type = object : TypeToken<Map<String, Any>>() {}.type
        val req: Map<String, Any> = gson.fromJson(body, type)

        val percent = (req["brightness"] as? Number)?.toInt()
            ?: return json(mapOf("error" to "缺少 brightness (0-100)"), Response.Status.BAD_REQUEST)

        if (percent < 0 || percent > 100) {
            return json(mapOf("error" to "brightness 须在 0-100 之间"), Response.Status.BAD_REQUEST)
        }

        return try {
            // 读取设备硬件最大亮度（不同设备不同，如 255、2047、4095 等）
            val maxBrightness = Runtime.getRuntime()
                .exec(arrayOf("su", "-c", "cat /sys/devices/platform/disp_leds/leds/lcd-backlight/max_brightness"))
                .inputStream.bufferedReader().readText().trim().toIntOrNull() ?: 255
            val value = (percent.toLong() * maxBrightness / 100).toInt()

            Runtime.getRuntime().exec(arrayOf("su", "-c",
                "settings put system screen_brightness_mode 0 && settings put system screen_brightness $value"))
            json(mapOf("status" to "success", "brightness_percent" to percent,
                "brightness_raw" to value, "max_brightness" to maxBrightness))
        } catch (e: Exception) {
            json(mapOf("status" to "error", "message" to e.message), Response.Status.INTERNAL_ERROR)
        }
    }

    private fun readBody(session: IHTTPSession): String {
        val map = mutableMapOf<String, String>()
        session.parseBody(map)
        return map["postData"] ?: ""
    }

    private fun json(data: Any, status: Response.Status = Response.Status.OK): Response {
        return newFixedLengthResponse(status, "application/json", gson.toJson(data))
    }
}
