package com.automation.plugin;

import android.accounts.Account;
import android.accounts.AccountManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.util.Log;

import org.json.JSONArray;
import org.json.JSONObject;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedHelpers;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

/**
 * GMS 账号管理 Hook
 *
 * 在 GMS 进程内执行账号操作（列出/移除/移除全部）
 * 通信方式：
 *   APP → Broadcast(ACTION_ACCOUNT_CMD) → 本插件
 *   本插件 → Broadcast(ACTION_ACCOUNT_RESULT) → APP
 */
public class GmsAccountHook implements IXposedHookLoadPackage {

    private static final String TAG = "GmsAccountHook";
    private static final String GMS_PACKAGE = "com.google.android.gms";

    // APP 发给插件的指令
    private static final String ACTION_ACCOUNT_CMD = "com.automation.app.ACTION_ACCOUNT_CMD";
    // 插件返回给 APP 的结果
    private static final String ACTION_ACCOUNT_RESULT = "com.automation.app.ACTION_ACCOUNT_RESULT";
    private static final String APP_PACKAGE = "com.automation.app";

    private boolean registered = false;

    @Override
    public void handleLoadPackage(XC_LoadPackage.LoadPackageParam lpparam) {
        if (!lpparam.packageName.equals(GMS_PACKAGE)) return;
        Log.i(TAG, "GmsAccountHook v1.0 已加载到 GMS 进程");
        hookApplicationOnCreate(lpparam);
    }

    /**
     * Hook Application.onCreate，在 GMS 启动后注册 BroadcastReceiver
     */
    private void hookApplicationOnCreate(XC_LoadPackage.LoadPackageParam lpparam) {
        XposedHelpers.findAndHookMethod(
                "android.app.Application", lpparam.classLoader,
                "onCreate",
                new XC_MethodHook() {
                    @Override
                    protected void afterHookedMethod(MethodHookParam param) {
                        if (registered) return;
                        registered = true;

                        Context context = (Context) param.thisObject;
                        Log.i(TAG, "注册账号管理 Receiver");

                        IntentFilter filter = new IntentFilter(ACTION_ACCOUNT_CMD);
                        context.registerReceiver(new AccountCmdReceiver(), filter,
                                Context.RECEIVER_EXPORTED);
                    }
                }
        );
    }

    /**
     * 接收 APP 的账号管理指令
     */
    private class AccountCmdReceiver extends BroadcastReceiver {
        @Override
        public void onReceive(Context context, Intent intent) {
            String action = intent.getStringExtra("action");
            String requestId = intent.getStringExtra("request_id");
            Log.i(TAG, "收到指令: action=" + action + ", requestId=" + requestId);

            try {
                JSONObject result;
                switch (action != null ? action : "") {
                    case "list_accounts":
                        result = listAccounts(context);
                        break;
                    case "remove_account":
                        String email = intent.getStringExtra("email");
                        result = removeAccount(context, email);
                        break;
                    case "remove_all":
                        result = removeAllAccounts(context);
                        break;
                    default:
                        result = new JSONObject();
                        result.put("success", false);
                        result.put("message", "未知指令: " + action);
                }
                result.put("request_id", requestId);
                sendResult(context, result);
            } catch (Exception e) {
                Log.e(TAG, "执行指令失败: " + e.getMessage());
                try {
                    JSONObject err = new JSONObject();
                    err.put("success", false);
                    err.put("message", e.getMessage());
                    err.put("request_id", requestId);
                    sendResult(context, err);
                } catch (Exception ignored) {}
            }
        }
    }

    private JSONObject listAccounts(Context context) throws Exception {
        AccountManager am = AccountManager.get(context);
        Account[] accounts = am.getAccountsByType("com.google");

        JSONArray list = new JSONArray();
        for (Account account : accounts) {
            list.put(account.name);
        }

        JSONObject result = new JSONObject();
        result.put("success", true);
        result.put("accounts", list);
        result.put("count", accounts.length);
        result.put("message", "共 " + accounts.length + " 个账号");
        Log.i(TAG, "列出账号: " + list.toString());
        return result;
    }

    private JSONObject removeAccount(Context context, String email) throws Exception {
        AccountManager am = AccountManager.get(context);
        Account account = new Account(email, "com.google");
        boolean removed = am.removeAccountExplicitly(account);

        JSONObject result = new JSONObject();
        result.put("success", removed);
        result.put("message", removed ? "已移除 " + email : "移除失败（账号可能不存在）");
        Log.i(TAG, "移除账号 " + email + ": " + removed);
        return result;
    }

    private JSONObject removeAllAccounts(Context context) throws Exception {
        AccountManager am = AccountManager.get(context);
        Account[] accounts = am.getAccountsByType("com.google");

        int count = 0;
        for (Account account : accounts) {
            if (am.removeAccountExplicitly(account)) count++;
        }

        JSONObject result = new JSONObject();
        result.put("success", true);
        result.put("removed", count);
        result.put("message", "已移除 " + count + " 个账号");
        Log.i(TAG, "移除所有账号: " + count);
        return result;
    }

    private void sendResult(Context context, JSONObject result) {
        try {
            Intent intent = new Intent(ACTION_ACCOUNT_RESULT);
            intent.setPackage(APP_PACKAGE);
            intent.putExtra("result", result.toString());
            context.sendBroadcast(intent);
            Log.i(TAG, "已发送结果: " + result.toString());
        } catch (Exception e) {
            Log.e(TAG, "发送结果失败: " + e.getMessage());
        }
    }
}
