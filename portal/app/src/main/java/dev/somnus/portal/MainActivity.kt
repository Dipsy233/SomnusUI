package dev.somnus.portal

import android.app.Activity
import android.content.Intent
import android.os.Bundle
import android.provider.Settings
import android.view.View
import android.widget.Button
import android.widget.LinearLayout
import android.widget.TextView
import dev.somnus.portal.service.SomnusAccessibilityService

/**
 * Status page (doc §4.1: the guided first-run flow).
 *
 * Shows:
 * - whether the accessibility service is bound (and a jump to its settings);
 * - whether the RPC server is listening, and the exact `adb forward` line;
 * - the Android 13+ "restricted settings" hint when applicable.
 */
class MainActivity : Activity() {

    private var R_id_status: Int = 0

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        val pad = (16 * resources.displayMetrics.density).toInt()
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }

        val status = TextView(this).apply { textSize = 16f; id = View.generateViewId() }
        R_id_status = status.id
        layout.addView(status)

        val openA11y = Button(this).apply {
            text = "开启无障碍服务"
            setOnClickListener {
                startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS))
            }
        }
        layout.addView(openA11y)

        val restrictedHint = TextView(this).apply {
            text = "Android 13+ 侧载后若开关为灰色:在应用信息 → 右上角菜单 → 允许受限设置"
            textSize = 13f
        }
        layout.addView(restrictedHint)

        setContentView(layout)

        // The server lives in the foreground RpcService, which owns the
        // always-on notification; it survives the activity being swept away
        // by the ROM (observed on MIUI). The accessibility service outlives
        // both, and the executor looks the service up lazily.
        RpcService.start(this)
    }

    override fun onResume() {
        super.onResume()
        refreshStatus()
    }

    private fun refreshStatus() {
        val bound = SomnusAccessibilityService.instance != null
        val status = findViewById<TextView>(R_id_status) ?: return
        status.text = buildString {
            appendLine("无障碍服务: ${if (bound) "已连接" else "未开启"}")
            appendLine("RPC 服务: ${if (RpcService.running) "前台运行中,已监听 127.0.0.1:8677" else "未启动"}")
            appendLine()
            appendLine("主机端连接:")
            appendLine("  adb forward tcp:18677 tcp:8677")
            appendLine("  somnus --provider android observe")
        }
    }
}
