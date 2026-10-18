package dev.somnus.portal

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import dev.somnus.portal.server.RpcServer
import dev.somnus.portal.service.ActionExecutor
import dev.somnus.portal.service.SomnusAccessibilityService

/**
 * Foreground service owning the RpcServer lifecycle (doc §13: ROM background
 * kills are the deployment risk; a plain activity-bound server dies with the
 * task, which was observed on-device when MIUI swept the activity away).
 *
 * The always-on notification is required for foreground state anyway and
 * doubles as the user-visible "Portal is running" signal (doc §4 red lines).
 *
 * The accessibility service outlives this component and the executor looks
 * it up lazily, so a temporary SERVICE_DISCONNECTED is reported loudly to
 * the host instead of crashing anything.
 */
class RpcService : Service() {

    companion object {
        const val CHANNEL_ID = "somnus"
        const val NOTIFICATION_ID = 1

        @Volatile
        var running: Boolean = false
            private set

        fun start(context: Context) {
            val intent = Intent(context, RpcService::class.java)
            if (Build.VERSION.SDK_INT >= 26) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }
    }

    private var server: RpcServer? = null

    override fun onCreate() {
        super.onCreate()
        startForeground(NOTIFICATION_ID, buildNotification())
        if (server == null) {
            server = RpcServer(ActionExecutor { SomnusAccessibilityService.instance })
            server?.start()
        }
        running = true
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        // Already foreground + listening; repeated starts (ROM restarts) are no-ops.
        return START_STICKY
    }

    override fun onDestroy() {
        running = false
        server?.stop()
        server = null
        super.onDestroy()
    }

    override fun onBind(intent: Intent?): IBinder? = null

    private fun buildNotification(): Notification {
        val manager = getSystemService(Context.NOTIFICATION_SERVICE) as NotificationManager
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL_ID, getString(R.string.notif_channel), NotificationManager.IMPORTANCE_LOW)
        )
        return Notification.Builder(this, CHANNEL_ID)
            .setSmallIcon(android.R.drawable.ic_menu_view)
            .setContentTitle(getString(R.string.notif_title))
            .setContentText(getString(R.string.notif_text))
            .setContentIntent(
                android.app.PendingIntent.getActivity(
                    this, 0,
                    Intent(this, MainActivity::class.java),
                    android.app.PendingIntent.FLAG_IMMUTABLE
                )
            )
            .setOngoing(true)
            .build()
    }
}
