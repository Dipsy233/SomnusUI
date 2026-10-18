package dev.somnus.portal.service

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.AccessibilityServiceInfo
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject

/**
 * SomnusDroid's perception + actuation service (doc §4.1).
 *
 * Responsibilities:
 * - hold the single service instance for the RPC layer;
 * - enumerate interactive windows and snapshot them in one traversal;
 * - maintain a revision counter + last-change time for the host's settler:
 *   content events are folded into a counter, never forwarded one by one
 *   (doc §4.4 - TYPE_WINDOW_CONTENT_CHANGED is far too frequent to stream);
 * - resolve node keys from the last snapshot and perform actions.
 *
 * The service deliberately holds NO policy: what may be touched is decided
 * by the host's compiler + policy. The Portal is a window, not a brain.
 */
class SomnusAccessibilityService : AccessibilityService() {

    companion object {
        @Volatile
        var instance: SomnusAccessibilityService? = null
            private set
    }

    /** Bumped on the service's own view of "content changed". */
    @Volatile
    var revision: Int = 0
        private set

    /** Monotonic-ish event counter; the host's settler polls this. */
    @Volatile
    var eventCount: Long = 0
        private set

    /** Wall clock of the last UI event (settle's quiet window measures it). */
    @Volatile
    var lastChangeMs: Long = 0
        private set

    /** Gesture channel toggle (doc §7.1): off until explicitly enabled. */
    @Volatile
    var gesturesEnabled: Boolean = false

    override fun onServiceConnected() {
        super.onServiceConnected()
        instance = this
        serviceInfo = serviceInfo.apply {
            flags = flags or
                    AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS or
                    AccessibilityServiceInfo.FLAG_INCLUDE_NOT_IMPORTANT_VIEWS or
                    AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS
        }
        revision = 0
        eventCount = 0
    }

    override fun onUnbind(intent: android.content.Intent?): Boolean {
        if (instance === this) instance = null
        return super.onUnbind(intent)
    }

    override fun onAccessibilityEvent(event: AccessibilityEvent?) {
        if (event == null) return
        when (event.eventType) {
            AccessibilityEvent.TYPE_WINDOW_STATE_CHANGED,
            AccessibilityEvent.TYPE_WINDOWS_CHANGED,
            -> {
                revision++
                eventCount++
                lastChangeMs = System.currentTimeMillis()
            }
            AccessibilityEvent.TYPE_WINDOW_CONTENT_CHANGED,
            AccessibilityEvent.TYPE_VIEW_TEXT_CHANGED,
            AccessibilityEvent.TYPE_VIEW_SCROLLED,
            AccessibilityEvent.TYPE_VIEW_FOCUSED,
            -> {
                eventCount++
                lastChangeMs = System.currentTimeMillis()
            }
        }
    }

    override fun onInterrupt() {}

    // ------------------------------------------------------------------ trees

    /**
     * All interactive windows as one snapshot JSON (doc §4.3).
     *
     * `metadataOnly = true` returns window entries without the node tree -
     * the host uses that for `list_windows`.
     */
    fun snapshot(metadataOnly: Boolean = false): JSONObject {
        val windowsArray = JSONArray()
        data class WindowEntry(val window: AccessibilityWindowInfo, val root: AccessibilityNodeInfo)
        val windowList = ArrayList<WindowEntry>()
        try {
            for (window in windows) {
                val root = window.root
                if (root == null) {
                    window.recycle()
                    continue
                }
                windowList.add(WindowEntry(window, root))
            }

            // Active (focused) window first so the host's "pick the active
            // window" default sees it first, and application windows before
            // input-method / system overlays.
            windowList.sortWith(
                compareByDescending<WindowEntry> { it.window.isActive }
                    .thenBy { it.window.type != AccessibilityWindowInfo.TYPE_APPLICATION }
            )

            for (item in windowList) {
                val entry = JSONObject()
                entry.put("id", item.window.id)
                entry.put("type", windowType(item.window.type))
                entry.put("pkg", item.root.packageName?.toString() ?: "")
                entry.put("title", item.root.packageName?.toString() ?: "")
                entry.put("active", item.window.isActive)
                entry.put("visible", item.window.isActive || item.window.isFocused)
                entry.put("layer", item.window.layer)
                if (!metadataOnly) {
                    entry.put("root", serializeRoot(item.root, item.window.id))
                } else {
                    item.root.recycle()
                }
                windowsArray.put(entry)
                item.window.recycle()
            }
        } catch (t: TreeSerializer.Truncated) {
            // A guard fired: return what was serialized plus a marker, so the
            // host knows the tree is a lower bound, not a lie.
            windowsArray.put(JSONObject().put("truncated", true))
        } finally {
            for (item in windowList) {
                runCatching { item.window.recycle() }
                runCatching { item.root.recycle() }
            }
        }
        return JSONObject()
            .put("display", 0)
            .put("rev", revision)
            .put("windows", windowsArray)
    }

    private fun serializeRoot(root: AccessibilityNodeInfo, windowId: Int): JSONObject {
        return try {
            TreeSerializer.serializeWindow(
                root = root,
                windowId = windowId,
                pkg = root.packageName?.toString(),
                title = null,
                active = true,
                revision = revision,
            ).getJSONArray("windows").getJSONObject(0).getJSONObject("root")
        } finally {
            runCatching { root.recycle() }
        }
    }

    private fun windowType(type: Int): String = when (type) {
        AccessibilityWindowInfo.TYPE_APPLICATION -> "application"
        AccessibilityWindowInfo.TYPE_INPUT_METHOD -> "ime"
        AccessibilityWindowInfo.TYPE_SYSTEM -> "system"
        AccessibilityWindowInfo.TYPE_ACCESSIBILITY_OVERLAY -> "overlay"
        AccessibilityWindowInfo.TYPE_SPLIT_SCREEN_DIVIDER -> "divider"
        else -> "unknown"
    }

    // ------------------------------------------------------------------ perform

    /**
     * Execute one node action by key. Returns {ok:true} or
     * {ok:false, error, message}; error codes match the host's
     * PORTAL_ERROR_CODES table.
     */
    fun perform(windowId: Int, key: String, actionName: String, value: String?): JSONObject {
        val actionId = TreeSerializer.actionId(actionName)
            ?: return err("UNSUPPORTED", "unknown action $actionName")

        val node = findByKey(windowId, key)
            ?: return err("NODE_GONE", "no node for key $key; re-observe")

        return node.use { target ->
            if (!target.isEnabled) return@use err("TARGET_DISABLED", "node is disabled")
            when (actionName.uppercase()) {
                "SET_TEXT" -> {
                    val args = Bundle().apply {
                        putCharSequence(
                            AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE,
                            value ?: ""
                        )
                    }
                    if (target.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, args))
                        okResult() else err("ACTION_FAILED", "setText refused")
                }
                "SET_PROGRESS" -> {
                    val args = Bundle().apply {
                        putFloat(
                            AccessibilityNodeInfo.ACTION_ARGUMENT_PROGRESS_VALUE,
                            value?.toFloatOrNull() ?: 0f
                        )
                    }
                    if (target.performAction(
                            AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_PROGRESS.id,
                            args
                        )
                    ) okResult() else err("ACTION_FAILED", "setProgress refused")
                }
                else -> {
                    if (target.performAction(actionId)) okResult()
                    else err("ACTION_FAILED", "perform refused")
                }
            }
        }
    }

    /** BACK / HOME / RECENTS / NOTIFICATIONS / QUICK_SETTINGS. */
    fun global(action: String): JSONObject {
        val id = when (action.uppercase()) {
            "GLOBAL_ACTION_BACK" -> GLOBAL_ACTION_BACK
            "GLOBAL_ACTION_HOME" -> GLOBAL_ACTION_HOME
            "GLOBAL_ACTION_RECENTS" -> GLOBAL_ACTION_RECENTS
            "GLOBAL_ACTION_NOTIFICATIONS" -> GLOBAL_ACTION_NOTIFICATIONS
            "GLOBAL_ACTION_QUICK_SETTINGS" -> GLOBAL_ACTION_QUICK_SETTINGS
            else -> return err("UNSUPPORTED", "unknown global action $action")
        }
        return if (performGlobalAction(id)) okResult()
        else err("ACTION_FAILED", "global action refused")
    }

    /**
     * Gesture channel (doc §7.1): synthesize tap / long_press at the node's
     * bounds centre. Coordinates exist only inside the Portal; the host marks
     * the result OK_UNVERIFIED unless its delta proves the effect.
     */
    fun gesture(windowId: Int, key: String, kind: String): JSONObject {
        if (!gesturesEnabled) return err("UNSUPPORTED", "gesture channel disabled")
        val node = findByKey(windowId, key)
            ?: return err("NODE_GONE", "no node for key $key")
        return node.use { target ->
            val r = Rect()
            target.getBoundsInScreen(r)
            if (r.isEmpty) return@use err("ACTION_FAILED", "empty bounds")
            val path = Path().apply { moveTo(r.exactCenterX(), r.exactCenterY()) }
            val stroke = android.accessibilityservice.GestureDescription.StrokeDescription(
                path, 0, if (kind == "long_press") 800L else 50L
            )
            val ok = dispatchGesture(
                android.accessibilityservice.GestureDescription.Builder()
                    .addStroke(stroke).build(), null, null
            )
            if (ok) okResult() else err("ACTION_FAILED", "gesture refused")
        }
    }

    // -------------------------------------------------------------- node lookup

    /**
     * Resolve a snapshot key ("12/0.3.1") against the live window.
     *
     * The key's index path is the walk order of the last snapshot; the live
     * tree may have moved since, so the path is a *hint*: we walk it and on
     * mismatch report no match (the host re-observes). This is the deliberate
     * fail-loud trade: guessing a nearest node would act on the wrong element.
     */
    fun findByKey(windowId: Int, key: String): AccessibilityNodeInfo? {
        val parts = key.split('/')
        val windowPart = parts.getOrNull(0)?.toIntOrNull() ?: return null
        if (windowPart != windowId) return null
        // The path after the window id; any non-numeric segment fails loud.
        val path = parts.drop(1).map { it.toIntOrNull() ?: return null }

        val root = rootForWindow(windowId) ?: return null
        try {
            var current: AccessibilityNodeInfo = root
            // The segments after the window id are the FULL path from the
            // root: the root's own key is "<windowId>" with no path, and each
            // segment is one getChild index. Dropping any segment lands on
            // the wrong node or falls off the tree (found on device: 14-level
            // key resolved to 11 levels -> NODE_GONE).
            for (index in path) {
                val next = current.getChild(index) ?: return null
                runCatching { current.recycle() }
                current = next
            }
            return current
        } catch (t: Throwable) {
            runCatching { root.recycle() }
            return null
        }
    }

    private fun rootForWindow(windowId: Int): AccessibilityNodeInfo? {
        var found: AccessibilityNodeInfo? = null
        for (window in windows) {
            val root = window.root
            if (found == null && window.id == windowId && root != null) {
                found = root
                window.recycle()
                continue
            }
            root?.recycle()
            window.recycle()
        }
        return found
    }
}

// ---------------------------------------------------------------------------
// helpers
// ---------------------------------------------------------------------------

/** Use-and-recycle scope for an AccessibilityNodeInfo. */
private inline fun <R> AccessibilityNodeInfo.use(block: (AccessibilityNodeInfo) -> R): R {
    try {
        return block(this)
    } finally {
        if (Build.VERSION.SDK_INT < 33) {
            runCatching { recycle() }
        }
    }
}

private fun okResult(): JSONObject = JSONObject().put("ok", true)

private fun err(code: String, message: String): JSONObject =
    JSONObject().put("ok", false).put("error", code).put("message", message)
