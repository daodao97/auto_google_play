package com.automation.app

import android.util.Log
import org.yaml.snakeyaml.Yaml
import java.net.HttpURLConnection
import java.net.URL

/**
 * YAML 配置解析器。
 * 当前业务使用管理端下发的远程 YAML URL；imports 支持相对远程路径。
 */
class ConfigParser {

    companion object {
        private const val TAG = "ConfigParser"
    }

    private data class LoadedYaml(
        val content: String,
        val baseUrl: String
    )

    private val yaml = Yaml()

    fun parse(configName: String, params: Map<String, String>): TaskConfig {
        val loaded = loadYaml(configName)
        val resolvedRaw = resolveTemplate(loaded.content, params)
        val data = yaml.load<Map<String, Any>>(resolvedRaw) ?: emptyMap()
        val modules = loadModules(data, params, loaded.baseUrl)
        val stepsList = data["steps"] as? List<Map<String, Any>> ?: emptyList()
        val steps = expandSteps(stepsList, modules, params)

        return TaskConfig(
            name = data["name"] as? String ?: configName,
            steps = steps
        )
    }

    @Suppress("UNCHECKED_CAST")
    private fun loadModules(data: Map<String, Any>, params: Map<String, String>, baseUrl: String): Map<String, ModuleConfig> {
        val imports = data["imports"] as? List<Any> ?: return emptyMap()
        val modules = mutableMapOf<String, ModuleConfig>()
        for (item in imports) {
            val path = item.toString()
            try {
                val loaded = loadYaml(path, baseUrl)
                val raw = resolveTemplate(loaded.content, params)
                val moduleData = yaml.load<Map<String, Any>>(raw) ?: emptyMap()
                val id = moduleData["id"] as? String ?: path.removeSuffix(".yaml").replace("/", ".")
                val steps = moduleData["steps"] as? List<Map<String, Any>> ?: emptyList()
                modules[id] = ModuleConfig(id, steps)
                Log.i(TAG, "加载 YAML 模块: $id <- $path")
            } catch (e: Exception) {
                Log.w(TAG, "加载 YAML 模块失败: $path, ${e.message}")
            }
        }
        return modules
    }

    @Suppress("UNCHECKED_CAST")
    private fun expandSteps(
        stepsList: List<Map<String, Any>>,
        modules: Map<String, ModuleConfig>,
        params: Map<String, String>
    ): List<Step> {
        val result = mutableListOf<Step>()
        for (stepMap in stepsList) {
            val useKey = stepMap["use"] as? String
            if (useKey.isNullOrEmpty()) {
                result.add(parseStep(stepMap))
                continue
            }

            val module = modules[useKey]
            if (module == null) {
                result.add(Step(
                    name = stepMap["id"] as? String ?: useKey,
                    action = "",
                    onError = stepMap["on_error"] as? String ?: "stop"
                ))
                continue
            }

            val withParams = mapToStringMap(stepMap["with"] as? Map<String, Any>, params)
            val localParams = params + withParams
            val wrapperId = stepMap["id"] as? String ?: useKey
            for (moduleStep in module.steps) {
                val resolved = resolveStepMap(moduleStep, localParams)
                val parsed = parseStep(resolved)
                result.add(parsed.copy(name = if (parsed.name.isEmpty()) wrapperId else "$wrapperId/${parsed.name}"))
            }
        }
        return result
    }

    private fun mapToStringMap(map: Map<String, Any>?, params: Map<String, String>): Map<String, String> {
        if (map == null) return emptyMap()
        return map.mapValues { (_, value) -> resolveTemplate(value.toString(), params) }
    }

    @Suppress("UNCHECKED_CAST")
    private fun resolveStepMap(map: Map<String, Any>, params: Map<String, String>): Map<String, Any> {
        return map.mapValues { (_, value) -> resolveStepValue(value, params) }
    }

    @Suppress("UNCHECKED_CAST")
    private fun resolveStepValue(value: Any, params: Map<String, String>): Any {
        return when (value) {
            is String -> resolveTemplate(value, params)
            is Map<*, *> -> resolveStepMap(value as Map<String, Any>, params)
            is List<*> -> value.map { item ->
                if (item == null) null else resolveStepValue(item, params)
            }
            else -> value
        }
    }

    private fun resolveTemplate(raw: String, params: Map<String, String>): String {
        var resolved = raw
        for ((key, value) in params) {
            resolved = resolved.replace("{$key}", value)
            resolved = resolved.replace("{{ $key }}", value)
            resolved = resolved.replace("{{$key}}", value)
        }
        return resolved
    }

    @Suppress("UNCHECKED_CAST")
    private fun parseStep(map: Map<String, Any>): Step {
        val find = (map["find"] as? Map<String, Any>)?.let { parseFindParams(it) }
        val subSteps = (map["steps"] as? List<Map<String, Any>>)?.map { parseStep(it) }
        val rules = (map["rules"] as? List<Map<String, Any>>)?.map { parseRule(it) }

        return Step(
            name = map["name"] as? String ?: map["id"] as? String ?: "",
            action = map["action"] as? String ?: "",
            find = find,
            value = map["value"] as? String,
            method = map["method"] as? String,
            waitFor = (map["wait_for"] as? List<String>),
            timeout = (map["timeout"] as? Number)?.toInt(),
            max = (map["max"] as? Number)?.toInt(),
            steps = subSteps,
            rules = rules,
            params = map["params"] as? Map<String, String>,
            optional = map["optional"] as? Boolean ?: false,
            onError = parseOnErrorDefault(map["on_error"]),
            onErrorRules = parseOnErrorRules(map["on_error"]),
            onSuccess = map["on_success"] as? String,
            manualHint = map["manual_hint"] as? String,
            from = (map["from"] as? List<Number>)?.map { it.toInt() },
            to = (map["to"] as? List<Number>)?.map { it.toInt() },
            duration = (map["duration"] as? Number)?.toInt(),
            repeat = (map["repeat"] as? Number)?.toInt() ?: 1,
            delay = (map["delay"] as? Number)?.toLong() ?: 0L,
            condition = map["condition"] as? String
        )
    }

    @Suppress("UNCHECKED_CAST")
    private fun parseOnErrorDefault(value: Any?): String {
        if (value is String) return value
        if (value is Map<*, *>) {
            val default = value["default"]
            if (default is String) return default
            if (default is Map<*, *>) {
                return (default["result"] ?: default["action"] ?: "stop").toString()
            }
        }
        return "stop"
    }

    private fun parseOnErrorRules(value: Any?): Map<String, String> {
        if (value !is Map<*, *>) return emptyMap()
        val result = mutableMapOf<String, String>()
        for ((key, raw) in value) {
            if (key == null) continue
            result[key.toString()] = when (raw) {
                is String -> raw
                is Map<*, *> -> (raw["result"] ?: raw["action"] ?: "stop").toString()
                else -> "stop"
            }
        }
        return result
    }

    @Suppress("UNCHECKED_CAST")
    private fun parseRule(map: Map<String, Any>): DecisionRule {
        return DecisionRule(
            when_ = map["when"] as? List<String> ?: emptyList(),
            whenAccount = map["when_account"] as? String,
            whenApp = map["when_app"] as? String,
            result = map["result"] as? String ?: "continue",
            message = map["message"] as? String
        )
    }

    private fun parseFindParams(map: Map<String, Any>): FindParams {
        return FindParams(
            text = (map["text"] as? List<String>)
                ?: (map["text"] as? String)?.split("|"),
            resourceId = map["resource_id"] as? String,
            className = map["class"] as? String,
            coords = (map["coords"] as? List<Number>)?.map { it.toInt() }
        )
    }

    private fun loadYaml(configName: String, baseUrl: String? = null): LoadedYaml {
        val source = resolveYamlUrl(configName, baseUrl)
        Log.i(TAG, "从 URL 下载配置: $source")
        return LoadedYaml(
            content = downloadYaml(source),
            baseUrl = source.substringBeforeLast("/") + "/"
        )
    }

    private fun resolveYamlUrl(configName: String, baseUrl: String?): String {
        val source = configName.trim()
        if (source.startsWith("http://") || source.startsWith("https://")) return source
        if (!baseUrl.isNullOrEmpty()) return URL(URL(baseUrl), source).toString()
        throw IllegalArgumentException("只支持远程 YAML URL: $configName")
    }

    private fun downloadYaml(urlStr: String, maxRetries: Int = 3, retryDelay: Long = 2000): String {
        var lastError: Exception? = null

        repeat(maxRetries) { attempt ->
            try {
                val url = URL(urlStr)
                val conn = url.openConnection() as HttpURLConnection
                conn.connectTimeout = 10_000
                conn.readTimeout = 30_000
                conn.requestMethod = "GET"

                val code = conn.responseCode
                if (code == HttpURLConnection.HTTP_OK) {
                    val content = conn.inputStream.bufferedReader().readText()
                    conn.disconnect()
                    Log.i(TAG, "配置下载成功 (第 ${attempt + 1} 次)")
                    return content
                } else {
                    conn.disconnect()
                    throw Exception("HTTP $code")
                }
            } catch (e: Exception) {
                lastError = e
                Log.w(TAG, "配置下载失败 (第 ${attempt + 1}/$maxRetries 次): ${e.message}")
                if (attempt < maxRetries - 1) {
                    Thread.sleep(retryDelay)
                }
            }
        }

        throw Exception("配置下载失败 ($urlStr): ${lastError?.message}")
    }
}

data class TaskConfig(
    val name: String,
    val steps: List<Step>
)

data class ModuleConfig(
    val id: String,
    val steps: List<Map<String, Any>>
)

data class Step(
    val name: String,
    val action: String,
    val find: FindParams? = null,
    val value: String? = null,
    val method: String? = null,
    val waitFor: List<String>? = null,
    val timeout: Int? = null,
    val max: Int? = null,
    val steps: List<Step>? = null,
    val rules: List<DecisionRule>? = null,
    val params: Map<String, String>? = null,
    val optional: Boolean = false,
    val onError: String = "stop",
    val onErrorRules: Map<String, String> = emptyMap(),
    val onSuccess: String? = null,
    val manualHint: String? = null,
    val from: List<Int>? = null,
    val to: List<Int>? = null,
    val duration: Int? = null,
    val repeat: Int = 1,
    val delay: Long = 0L,
    val condition: String? = null
)

data class DecisionRule(
    val when_: List<String>,
    val whenAccount: String? = null,
    val whenApp: String? = null,
    val result: String = "continue",
    val message: String? = null
)

data class FindParams(
    val text: List<String>? = null,
    val resourceId: String? = null,
    val className: String? = null,
    val coords: List<Int>? = null
)
