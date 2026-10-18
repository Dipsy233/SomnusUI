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

    fun snapshot(metadataOnly: Boolean = false): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return try {
            svc.snapshot(metadataOnly)
        } catch (t: Throwable) {
            err("SERVICE_DISCONNECTED", t.message ?: "snapshot failed")
        }
    }

    fun perform(windowId: Int, key: String, action: String, value: String?): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return svc.perform(windowId, key, action, value)
    }

    fun global(action: String): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return svc.global(action)
    }

    fun gesture(windowId: Int, key: String, kind: String): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return svc.gesture(windowId, key, kind)
    }

    fun launch(packageName: String): JSONObject {
        val svc = service() ?: return err("SERVICE_DISCONNECTED", "accessibility service not bound")
        return try {
            val context = svc.applicationContext
            val intent = context.packageManager.getLaunchIntentForPackage(packageName)
                ?: return err("APP_NOT_FOUND", "no launchable activity for $packageName")
            intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)
            context.startActivity(intent)
            ok()
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
