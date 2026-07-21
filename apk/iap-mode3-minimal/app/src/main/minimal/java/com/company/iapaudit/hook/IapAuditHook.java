package com.company.iapaudit.hook;

import android.os.Bundle;

import java.lang.reflect.Method;
import java.util.HashSet;
import java.util.Locale;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;

import de.robv.android.xposed.IXposedHookLoadPackage;
import de.robv.android.xposed.XC_MethodHook;
import de.robv.android.xposed.XposedBridge;
import de.robv.android.xposed.callbacks.XC_LoadPackage;

public final class IapAuditHook implements IXposedHookLoadPackage {

    private static final String SELF_PACKAGE = "com.company.iapaudit";
    private static final int TARGET_MODE = 3;

    private static final String[] BILLING_BUILDER_CLASSES = {
            "com.android.billingclient.api.BillingFlowParams$SubscriptionUpdateParams$Builder",
            "com.android.billingclient.api.BillingFlowParams$ProductDetailsParams$SubscriptionProductReplacementParams$Builder",
            "com.android.billingclient.api.BillingFlowParams$Builder"
    };

    private static final Set<String> HOOKED_CLASSES = ConcurrentHashMap.newKeySet();
    private static final Set<String> HOOKED_METHODS = ConcurrentHashMap.newKeySet();
    private static final Set<String> INSTALLED_PROCESSES = ConcurrentHashMap.newKeySet();

    @Override
    public void handleLoadPackage(XC_LoadPackage.LoadPackageParam lpparam) {
        if (shouldSkip(lpparam.packageName)) return;
        String processKey = safe(lpparam.packageName) + ":" + safe(lpparam.processName);
        if (!INSTALLED_PROCESSES.add(processKey)) return;

        installKnownBillingHooks(lpparam.classLoader);
        installDeferredBillingHooks();
        installBundleHooks();
    }

    private static void installKnownBillingHooks(ClassLoader classLoader) {
        for (String className : BILLING_BUILDER_CLASSES) {
            Class<?> clazz = findClass(className, classLoader);
            if (clazz != null) hookBillingBuilderClass(clazz);
        }
    }

    private static void installDeferredBillingHooks() {
        String key = "java.lang.ClassLoader#loadClass";
        if (!HOOKED_METHODS.add(key)) return;
        try {
            XposedBridge.hookAllMethods(ClassLoader.class, "loadClass", new XC_MethodHook() {
                @Override
                protected void afterHookedMethod(MethodHookParam param) {
                    Object result = param.getResult();
                    if (!(result instanceof Class<?>)) return;
                    Class<?> clazz = (Class<?>) result;
                    if (isBillingBuilderClassName(clazz.getName())) {
                        hookBillingBuilderClass(clazz);
                    }
                }
            });
        } catch (Throwable ignored) {
            HOOKED_METHODS.remove(key);
        }
    }

    private static void hookBillingBuilderClass(Class<?> clazz) {
        if (clazz == null || !HOOKED_CLASSES.add(clazz.getName())) return;
        Method[] methods;
        try {
            methods = clazz.getDeclaredMethods();
        } catch (Throwable ignored) {
            return;
        }
        Set<String> names = new HashSet<>();
        for (Method method : methods) {
            String name = method.getName();
            if (!names.add(name)) continue;
            if (!isModeSetter(name)) continue;
            hookAllModeSetterMethods(clazz, name);
        }
    }

    private static void hookAllModeSetterMethods(Class<?> clazz, String methodName) {
        String key = clazz.getName() + "#" + methodName;
        if (!HOOKED_METHODS.add(key)) return;
        try {
            XposedBridge.hookAllMethods(clazz, methodName, new XC_MethodHook() {
                @Override
                protected void beforeHookedMethod(MethodHookParam param) {
                    if (param.args == null || param.args.length != 1) return;
                    if (param.args[0] instanceof Integer) {
                        param.args[0] = TARGET_MODE;
                    }
                }
            });
        } catch (Throwable ignored) {
            HOOKED_METHODS.remove(key);
        }
    }

    private static void installBundleHooks() {
        hookBundlePutInt(Bundle.class);
        Class<?> baseBundle = findBootClass("android.os.BaseBundle");
        if (baseBundle != null) hookBundlePutInt(baseBundle);
    }

    private static void hookBundlePutInt(Class<?> clazz) {
        String key = clazz.getName() + "#putInt";
        if (!HOOKED_METHODS.add(key)) return;
        try {
            XposedBridge.hookAllMethods(clazz, "putInt", new XC_MethodHook() {
                @Override
                protected void beforeHookedMethod(MethodHookParam param) {
                    if (param.args == null || param.args.length < 2) return;
                    if (!(param.args[0] instanceof String)) return;
                    if (!(param.args[1] instanceof Integer)) return;
                    String bundleKey = (String) param.args[0];
                    if (isModeBundleKey(bundleKey)) {
                        param.args[1] = TARGET_MODE;
                    }
                }
            });
        } catch (Throwable ignored) {
            HOOKED_METHODS.remove(key);
        }
    }

    private static boolean isModeSetter(String methodName) {
        String lower = lower(methodName);
        return lower.startsWith("set")
                && (lower.contains("prorationmode")
                || lower.contains("replacementmode")
                || lower.contains("replaceprorationmode"));
    }

    private static boolean isModeBundleKey(String key) {
        String lower = lower(key);
        return lower.contains("prorationmode")
                || lower.contains("replacementmode")
                || lower.contains("replaceprorationmode");
    }

    private static boolean isBillingBuilderClassName(String className) {
        if (className == null) return false;
        if (!className.startsWith("com.android.billingclient.api.BillingFlowParams$")) {
            return false;
        }
        return className.endsWith("$Builder") || className.contains("$Builder$");
    }

    private static Class<?> findClass(String className, ClassLoader classLoader) {
        try {
            return Class.forName(className, false, classLoader);
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static Class<?> findBootClass(String className) {
        try {
            return Class.forName(className);
        } catch (Throwable ignored) {
            return null;
        }
    }

    private static boolean shouldSkip(String packageName) {
        return packageName == null
                || SELF_PACKAGE.equals(packageName)
                || "android".equals(packageName)
                || "system".equals(packageName)
                || "com.android.systemui".equals(packageName);
    }

    private static String lower(String value) {
        return value == null ? "" : value.toLowerCase(Locale.US);
    }

    private static String safe(String value) {
        return value == null ? "" : value;
    }
}
