package dev.somnus.portal.service

import android.graphics.Rect
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject

/**
 * AccessibilityNodeInfo tree -> RawTree JSON (SomnusDroid doc §4.3).
 *
 * Rules that keep the serializer honest and fast:
 *
 * - **One traversal, one JSON tree.** The host gets the whole screen in a
 *   single reply; it never asks for individual nodes (each `getChild` is an
 *   IPC into the app process, so laziness would be N round trips, not one).
 * - **Identity stays in the key.** The node key (`"<windowId>/<indexPath>"`)
 *   is the stable address the host uses for `perform`; resource-ids are
 *   carried for *fingerprints only* - the host's compiler guarantees they
 *   never reach the model, and this serializer emits nothing model-facing.
 * - **Password text never leaves the phone.** `isPassword` is reported, the
 *   text is replaced with "•••" (doc §9).
 * - **Recycle what you touch** on API < 33 (recycle is deprecated but a
 *   harmless no-op above; guarding keeps one code path for all versions).
 * - **Guards, not guesses.** A runaway hierarchy or a pathological node count
 *   throws [Truncated]; the snapshot reports itself as a lower bound instead
 *   of hanging the service.
 */
object TreeSerializer {

    private const val PASSWORD_MASK = "•••"
    private const val MAX_DEPTH = 40
    private const val MAX_NODES = 8000

    class Truncated : Exception()

    fun serializeWindow(
        root: android.view.accessibility.AccessibilityNodeInfo,
        windowId: Int,
        pkg: String?,
        title: String?,
        active: Boolean,
        revision: Int,
        displayId: Int = 0,
    ): JSONObject {
        val counter = intArrayOf(0)
        val rootJson = nodeToJson(root, windowId, emptyList(), 0, counter)
        val window = JSONObject()
            .put("id", windowId)
            .put("type", "application")
            .put("pkg", pkg ?: "")
            .put("title", title ?: pkg ?: "")
            .put("active", active)
            .put("root", rootJson)
        return JSONObject()
            .put("display", displayId)
            .put("rev", revision)
            .put("windows", JSONArray().put(window))
    }

    /**
     * Depth-first serialization with an explicit index path. The path *is*
     * the key: stable for as long as the window's structure holds, which is
     * exactly the lifetime a ref needs between snapshot and perform.
     */
    private fun nodeToJson(
        node: android.view.accessibility.AccessibilityNodeInfo,
        windowId: Int,
        path: List<Int>,
        depth: Int,
        counter: IntArray,
    ): JSONObject {
        if (++counter[0] > MAX_NODES) throw Truncated()
        if (depth > MAX_DEPTH) throw Truncated()

        val json = JSONObject()
        json.put("key", buildKey(windowId, path))
        json.put("cls", node.className?.toString() ?: "")

        val isPassword = node.isPassword
        val text = node.text?.toString()
        json.put("text", if (isPassword && !text.isNullOrEmpty()) PASSWORD_MASK else text)
        json.put("desc", node.contentDescription?.toString() ?: JSONObject.NULL)
        node.hintText?.toString()?.let { json.put("hint", it) }
        node.stateDescription?.toString()?.let { json.put("state_desc", it) }
        node.paneTitle?.toString()?.let { json.put("pane_title", it) }
        // resource-id: fingerprint evidence only (host-side rule, doc §4.1).
        json.put("rid", node.viewIdResourceName?.takeIf { it.isNotEmpty() } ?: JSONObject.NULL)

        val actions = JSONArray()
        for (action in node.actionList) actions.put(actionName(action.id))
        json.put("actions", actions)

        json.put("enabled", node.isEnabled)
        json.put("editable", node.isEditable)
        json.put("password", isPassword)
        json.put("checkable", node.isCheckable)
        json.put("checked", if (node.isCheckable) node.isChecked else JSONObject.NULL)
        json.put("selected", node.isSelected)
        json.put("focused", node.isFocused)
        json.put("visible", node.isVisibleToUser)
        if (!node.isVisibleToUser) json.put("offscreen", true)

        node.collectionInfo?.let { collection ->
            val collJson = JSONObject()
            collJson.put("count", if (collection.rowCount >= 0) collection.rowCount else -1)
            collJson.put("columnCount", collection.columnCount)
            collJson.put("virtualized", collection.rowCount < 0)
            json.put("collection", collJson)
        }
        node.collectionItemInfo?.let { itemInfo ->
            val itemJson = JSONObject()
            // API 35 renamed the Kotlin properties to rowIndex/columnIndex;
            // the getters are the stable surface across all levels.
            itemJson.put("row", itemInfo.rowIndex)
            itemJson.put("column", itemInfo.columnIndex)
            json.put("item", itemJson)
        }
        node.rangeInfo?.let { range ->
            val rangeJson = JSONObject()
            rangeJson.put("min", range.min.toDouble())
            rangeJson.put("max", range.max.toDouble())
            rangeJson.put("value", range.current.toDouble())
            json.put("range", rangeJson)
        }

        val bounds = Rect()
        node.getBoundsInScreen(bounds)
        if (bounds.isEmpty) {
            json.put("bounds", JSONObject.NULL)
        } else {
            json.put(
                "bounds",
                JSONArray()
                    .put(bounds.left).put(bounds.top)
                    .put(bounds.right).put(bounds.bottom)
            )
        }

        val children = JSONArray()
        for (i in 0 until node.childCount) {
            val child = node.getChild(i) ?: continue
            try {
                // Every real child must be represented even when filtered:
                // the key path uses the LIVE child indices, so dropping a
                // slot would shift every later sibling's key and make
                // `perform` miss (found on a real device: the walk landed on
                // the wrong node). Filtered children become null placeholders.
                //
                // Invisible subtrees are NOT recursed into (Maestro's rule):
                // a virtualised list's off-screen rows are invisible but
                // clickable, and recursing them is hundreds of extra binder
                // round trips per snapshot - measured live: QQ's chat screen
                // froze the serializer while uiautomator (which skips them)
                // dumped the same screen in under a second. The rows come
                // back with real bounds when scrolled into view, at which
                // point they are visible and get serialized.
                val meaningful = child.isVisibleToUser || child.isScrollable
                if (!meaningful) {
                    children.put(JSONObject.NULL)
                } else {
                    children.put(nodeToJson(child, windowId, path + i, depth + 1, counter))
                }
            } finally {
                if (android.os.Build.VERSION.SDK_INT < 33) runCatching { child.recycle() }
            }
        }
        json.put("children", children)
        return json
    }

    fun buildKey(windowId: Int, path: List<Int>): String =
        StringBuilder().apply {
            append(windowId)
            for (p in path) append('/').append(p)
        }.toString()

    /** Wire name -> the wire name the host's `ACTION_TO_PATTERN` table keys on
     * (doc §4.3: "CLICK", "LONG_CLICK", ...). Typed AccessibilityActions map
     * through their `id` so the serializer and the executor agree on names.
     */
    fun actionName(id: Int): String = when (id) {
        AccessibilityNodeInfo.ACTION_CLICK -> "CLICK"
        AccessibilityNodeInfo.ACTION_LONG_CLICK -> "LONG_CLICK"
        AccessibilityNodeInfo.ACTION_SET_TEXT -> "SET_TEXT"
        AccessibilityNodeInfo.ACTION_PASTE -> "PASTE"
        AccessibilityNodeInfo.ACTION_SCROLL_FORWARD -> "SCROLL_FORWARD"
        AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD -> "SCROLL_BACKWARD"
        AccessibilityNodeInfo.ACTION_SELECT -> "SELECT"
        AccessibilityNodeInfo.ACTION_CLEAR_SELECTION -> "CLEAR_SELECTION"
        AccessibilityNodeInfo.ACTION_FOCUS -> "FOCUS"
        AccessibilityNodeInfo.ACTION_EXPAND -> "EXPAND"
        AccessibilityNodeInfo.ACTION_COLLAPSE -> "COLLAPSE"
        AccessibilityNodeInfo.ACTION_DISMISS -> "DISMISS"
        // Typed-action ids (values overlap with nothing above).
        AccessibilityNodeInfo.AccessibilityAction.ACTION_SHOW_ON_SCREEN.id -> "SHOW_ON_SCREEN"
        AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_TO_POSITION.id -> "SCROLL_TO_POSITION"
        AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_UP.id -> "SCROLL_TO_TOP"
        AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_DOWN.id -> "SCROLL_TO_BOTTOM"
        AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_LEFT.id -> "SCROLL_LEFT"
        AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_RIGHT.id -> "SCROLL_RIGHT"
        AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_PROGRESS.id -> "SET_PROGRESS"
        AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER.id -> "IME_ENTER"
        else -> "ACTION_$id"
    }

    /** Wire name -> action, for `perform` (inverse of [actionName]).
     *
     * Two action families exist in the platform API:
     * - legacy int constants on AccessibilityNodeInfo itself;
     * - typed [AccessibilityNodeInfo.AccessibilityAction] objects (API 21+).
     * `performAction(int)` takes the int ids, which for the typed actions are
     * the object's `id` field - so the mapping returns the object's id.
     */
    fun actionId(name: String): Int? {
        val n = name.uppercase()
        // Typed actions first: several (SET_PROGRESS, SHOW_ON_SCREEN, the
        // directional scrolls) exist only here, not as int constants.
        typedByName(n)?.let { return it.id }
        return when (n) {
            "CLICK" -> AccessibilityNodeInfo.ACTION_CLICK
            "LONG_CLICK" -> AccessibilityNodeInfo.ACTION_LONG_CLICK
            "SET_TEXT" -> AccessibilityNodeInfo.ACTION_SET_TEXT
            "PASTE" -> AccessibilityNodeInfo.ACTION_PASTE
            "SCROLL_FORWARD" -> AccessibilityNodeInfo.ACTION_SCROLL_FORWARD
            "SCROLL_BACKWARD" -> AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD
            "SCROLL_TO_POSITION" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_TO_POSITION.id
            "FOCUS" -> AccessibilityNodeInfo.ACTION_FOCUS
            "SELECT" -> AccessibilityNodeInfo.ACTION_SELECT
            "CLEAR_SELECTION" -> AccessibilityNodeInfo.ACTION_CLEAR_SELECTION
            "EXPAND" -> AccessibilityNodeInfo.ACTION_EXPAND
            "COLLAPSE" -> AccessibilityNodeInfo.ACTION_COLLAPSE
            "DISMISS" -> AccessibilityNodeInfo.ACTION_DISMISS
            else -> null
        }
    }

    private fun typedByName(name: String): AccessibilityNodeInfo.AccessibilityAction? =
        when (name) {
            "SET_PROGRESS" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SET_PROGRESS
            "SHOW_ON_SCREEN" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SHOW_ON_SCREEN
            "SCROLL_TO_POSITION" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_TO_POSITION
            "SCROLL_TO_TOP" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_UP
            "SCROLL_TO_BOTTOM" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_DOWN
            "SCROLL_LEFT" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_LEFT
            "SCROLL_RIGHT" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_SCROLL_RIGHT
            "EXPAND" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_EXPAND
            "COLLAPSE" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_COLLAPSE
            "DISMISS" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_DISMISS
            "IME_ENTER" -> AccessibilityNodeInfo.AccessibilityAction.ACTION_IME_ENTER
            "SHOW_ON_SCREEN_" -> null
            else -> null
        }
}
