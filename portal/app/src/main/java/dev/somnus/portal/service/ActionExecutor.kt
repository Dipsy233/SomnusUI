package dev.somnus.portal.service

import org.json.JSONArray
import org.json.JSONObject

/**
 * Action execution facade: the RPC layer's single entry point for anything
 * that touches the screen (doc §4.2, §7).
 *
 * Kept separate from the service so the routing table stays readable and the
 * service stays a thin AccessibilityService. Every route returns
 * {ok, error?, message?}; error spellings match the host's
 * PORTAL_ERROR_CODES so the session can render them as SUI-Text ERR lines
 * without a second mapping.
 */
class ActionExecutor(private val service: () -> SomnusAccessibilityService?) {

    /** set_text channel 2 (doc §7.2): clipboard + ACTION_PASTE. */
    var clipboardAllowed: Boolean = true

    /** Launch foreground-verification result (see [launch]). */
    private val verified = java.util.concurrent.atomic.AtomicBoolean(false)

    fun snapshot(metadataOnly: Boolean = false): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return try {
            svc.snapshot(metadataOnly)
        } catch (t: Throwable) {
            err("SERVICE_DISCONNECTED", t.message ?: "snapshot failed")
        }
    }

    fun perform(
        windowId: Int,
        key: String,
        action: String,
        value: String?,
        expect: org.json.JSONObject?,
    ): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return svc.perform(windowId, key, action, value, expect)
    }

    fun global(action: String): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return svc.global(action)
    }

    /** Launcher page turn (see [SomnusAccessibilityService.page]). */
    fun page(direction: String): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return svc.page(direction)
    }

    fun gesture(windowId: Int, key: String, kind: String, expect: org.json.JSONObject?): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return svc.gesture(windowId, key, kind, expect)
    }

    /**
     * Channel toggles (doc §7.1: both sides must opt in). Currently only
     * `gestures` is settable; unknown keys are ignored so future channels
     * can be added without a protocol bump.
     */
    fun config(params: org.json.JSONObject): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        if (params.has("gestures")) svc.gesturesEnabled = params.optBoolean("gestures", false)
        if (params.has("clipboard")) clipboardAllowed = params.optBoolean("clipboard", true)
        return JSONObject()
            .put("ok", true)
            .put("gestures", svc.gesturesEnabled)
            .put("clipboard", clipboardAllowed)
    }

    fun launch(packageName: String): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return try {
            val context = svc.applicationContext
            val intent = context.packageManager.getLaunchIntentForPackage(packageName)
                ?: return err("APP_NOT_FOUND", "no launchable activity for $packageName")
            intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(intent)
            // MIUI's "background activity start" permission silently drops the
            // startActivity when the Portal is in the background and the
            // permission is off - the call returns ok but nothing happens
            // (found live: APP open returned ok while the launcher stayed
            // active). Verify the foreground actually moved; report loudly
            // when it did not (doc: fail loud).
            //
            // The check runs on its own thread with a hard join timeout:
            // snapshot() walks the launched app's window tree, and a huge or
            // hostile tree (QQ's SplashActivity measured live) can block the
            // walk far past any RPC timeout - without the join bound the whole
            // server wedges behind one launch call (found live: every later
            // connection timed out while the verifier thread sat in a native
            // getChild call).
            val verifier = Thread {
                try {
                    Thread.sleep(1200)
                    val windows = svc.snapshot(true).optJSONArray("windows") ?: JSONArray()
                    for (i in 0 until windows.length()) {
                        val w = windows.optJSONObject(i) ?: continue
                        if (w.optBoolean("active") && w.optString("pkg") == packageName) {
                            verified.set(true)
                            break
                        }
                    }
                } catch (_: Throwable) {
                    // Verification is best-effort; an unreadable tree must not
                    // fail a launch that visibly worked.
                }
            }
            verifier.isDaemon = true
            verifier.start()
            verifier.join(5000)
            return if (verified.get()) ok()
            else JSONObject()
                .put("ok", false)
                .put("error", "APP_NOT_IN_FOREGROUND")
                .put("message",
                    "launched $packageName but could not confirm it reached the " +
                    "foreground; on MIUI enable 后台弹出界面 for the Portal")
        } catch (t: Throwable) {
            err("ACTION_FAILED", t.message ?: "launch failed")
        }
    }

    fun apps(): JSONArray {
        val svc = service() ?: return JSONArray()
        val context = svc.applicationContext
        val pm = context.packageManager
        val intent = android.content.Intent(android.content.Intent.ACTION_MAIN)
            .addCategory(android.content.Intent.CATEGORY_LAUNCHER)
        val resolve = pm.queryIntentActivities(intent, 0)
        val out = JSONArray()
        for (info in resolve) {
            out.put(
                JSONObject()
                    .put("package", info.activityInfo.packageName)
                    .put("label", info.loadLabel(pm).toString())
            )
        }
        return out
    }

    /**
     * Deep link: jump straight to a URI the app registers for
     * (ACTION_VIEW). This is the fast lane the vendor assistants ride: an
     * in-app page reached by intent skips every navigation step (and every
     * LLM round-trip between them). Same foreground verification as
     * [launch] - a dropped start must not read as success.
     */
    fun openUri(uri: String): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return try {
            val context = svc.applicationContext
            val intent = android.content.Intent(
                android.content.Intent.ACTION_VIEW,
                android.net.Uri.parse(uri)
            ).addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            if (intent.resolveActivity(context.packageManager) == null) {
                return err("APP_NOT_FOUND", "no app handles $uri")
            }
            context.startActivity(intent)
            val targetPkg = intent.resolveActivity(context.packageManager)?.packageName ?: ""
            val verified = java.util.concurrent.atomic.AtomicBoolean(false)
            val verifier = Thread {
                try {
                    Thread.sleep(1200)
                    val windows = svc.snapshot(true).optJSONArray("windows") ?: JSONArray()
                    for (i in 0 until windows.length()) {
                        val w = windows.optJSONObject(i) ?: continue
                        if (w.optBoolean("active") && w.optString("pkg") == targetPkg) {
                            verified.set(true)
                            break
                        }
                    }
                } catch (_: Throwable) {
                }
            }
            verifier.isDaemon = true
            verifier.start()
            verifier.join(5000)
            if (verified.get()) ok()
            else JSONObject()
                .put("ok", false)
                .put("error", "APP_NOT_IN_FOREGROUND")
                .put("message", "opened $uri but could not confirm it reached the foreground")
        } catch (t: Throwable) {
            err("ACTION_FAILED", t.message ?: "openUri failed")
        }
    }

    /** set_text channel 2's clipboard write half (doc §4.2 clipboard.set). */
    fun setClipboard(text: String): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        if (!clipboardAllowed) return err("UNSUPPORTED", "clipboard channel disabled")
        val cm = svc.applicationContext.getSystemService(android.content.Context.CLIPBOARD_SERVICE)
            as android.content.ClipboardManager
        cm.setPrimaryClip(android.content.ClipData.newPlainText("somnus", text))
        return ok()
    }

    /** Host-settle support: the revision + event counters (doc §4.4). */
    fun events(): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return JSONObject()
            .put("rev", svc.revision)
            .put("events", svc.eventCount)
            .put("lastChangeMs", svc.lastChangeMs)
    }
}

private fun ok(): JSONObject = JSONObject().put("ok", true)

private fun err(code: String, message: String): JSONObject =
    JSONObject().put("ok", false).put("error", code).put("message", message)
