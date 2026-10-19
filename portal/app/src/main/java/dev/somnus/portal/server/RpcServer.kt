package dev.somnus.portal.server

import dev.somnus.portal.service.ActionExecutor
import java.io.BufferedReader
import java.io.BufferedWriter
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.InetAddress
import java.net.ServerSocket
import java.net.Socket
import java.net.SocketTimeoutException
import java.security.MessageDigest
import java.util.Base64
import java.util.concurrent.Executors
import org.json.JSONObject

/**
 * Embedded JSON-RPC server over WebSocket (SomnusDroid doc §4.2).
 *
 * Binds the loopback only: the phone-side attack surface is "whatever adb
 * forward exposes", which is exactly the trust boundary the host already
 * owns. A device that is not attached to adb cannot reach this port.
 *
 * Hand-rolled framing (one text frame per RPC, server replies unmasked) so
 * the Portal carries no network library; the host's transport.py mirrors the
 * same minimal protocol.
 *
 * Methods:
 *   snapshot {metadataOnly?}          -> RawTree JSON
 *   perform {window, node, action, value?} -> {ok, error?}
 *   global {action}                   -> {ok, error?}
 *   gesture {window, node, kind}      -> {ok, error?}
 *   launch {package}                  -> {ok, error?}
 *   apps                              -> [{package, label}]
 *   clipboard.set {text}              -> {ok, error?}
 *   config {gestures?, clipboard?}    -> {ok, gestures, clipboard}
 *   events                            -> {rev, events, lastChangeMs}
 *   ping                              -> {ok, pong}
 */
class RpcServer(
    private val executor: ActionExecutor,
    private val port: Int = 8677,
) {
    @Volatile
    private var running = false

    private var serverSocket: ServerSocket? = null
    private val pool = Executors.newCachedThreadPool()

    fun start() {
        if (running) return
        running = true
        val thread = Thread({
            try {
                // Bind IPv4 loopback explicitly: InetAddress.getLoopbackAddress()
                // may resolve to ::1, and adb forward's tcp: speaks IPv4 only -
                // a [::1]-only listener accepts nothing from adb (found live:
                // the port showed up in `ss` as [::1]:8677 while every host
                // connection timed out or was reset).
                // Bind failure retries with backoff: a too-early start (the
                // previous process's socket not yet released after a
                // force-stop) used to kill the accept thread silently and
                // leave the service "running" with no listener (found live:
                // after a restart the port answered nothing until the whole
                // app was cold-started again).
                var bound = false
                for (attempt in 0 until 10) {
                    try {
                        serverSocket = ServerSocket(
                            port, 16,
                            InetAddress.getByAddress("127.0.0.1", byteArrayOf(127, 0, 0, 1))
                        )
                        bound = true
                        break
                    } catch (e: Exception) {
                        System.err.println("[somnus-rpc] bind attempt $attempt failed: $e")
                        Thread.sleep(1000L * (attempt + 1))
                    }
                }
                if (!bound) {
                    System.err.println("[somnus-rpc] giving up after 10 bind attempts")
                    running = false
                    return@Thread
                }
                while (running) {
                    val socket = try {
                        serverSocket!!.accept()
                    } catch (e: Exception) {
                        if (running) continue else break
                    }
                    pool.execute { handleConnection(socket) }
                }
            } catch (e: Exception) {
                if (running) e.printStackTrace()
            }
        }, "somnus-rpc-accept")
        thread.isDaemon = true
        thread.start()
    }

    fun stop() {
        running = false
        runCatching { serverSocket?.close() }
        pool.shutdownNow()
    }

    // ------------------------------------------------------------------ conn

    private fun handleConnection(socket: Socket) {
        try {
            // Handshake must not wait forever: a half-open connection (host
            // died mid-handshake, adb forward flapped) would park a pool
            // thread on readLine() indefinitely. The idle read timeout is
            // re-set to 0 after the handshake - an established RPC session
            // may idle arbitrarily long between calls.
            socket.soTimeout = 10_000
            val reader = BufferedReader(InputStreamReader(socket.inputStream, Charsets.UTF_8))
            if (!handshake(socket, reader)) {
                runCatching { socket.close() }
                return
            }
            socket.soTimeout = 0
            val input = FrameReader(socket.getInputStream())
            val output = BufferedWriter(OutputStreamWriter(socket.outputStream, Charsets.UTF_8))

            while (running) {
                val payload = try {
                    input.readTextFrame() ?: break
                } catch (e: SocketTimeoutException) {
                    continue
                } ?: break
                val reply = dispatch(payload)
                writeTextFrame(socket, reply)
            }
        } catch (e: java.io.IOException) {
            // A dropped client or a timed-out handshake is normal (host
            // restart, adb reconnect, half-open forward); the socket is
            // closed in finally and the pool thread is released.
        } catch (e: Exception) {
            e.printStackTrace()
        } finally {
            runCatching { socket.close() }
        }
    }

    /** Minimal RFC6455 server handshake (loopback-only trust, no origin check). */
    private fun handshake(socket: Socket, reader: BufferedReader): Boolean {
        var key: String? = null
        while (true) {
            val line = reader.readLine() ?: return false
            if (line.isEmpty()) break
            val marker = "Sec-WebSocket-Key:"
            if (line.startsWith(marker, ignoreCase = true)) {
                key = line.substring(marker.length).trim()
            }
        }
        val wsKey = key ?: return false
        val accept = Base64.getEncoder().encodeToString(
            MessageDigest.getInstance("SHA-1")
                .digest((wsKey + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").toByteArray())
        )
        val response = "HTTP/1.1 101 Switching Protocols\r\n" +
                "Upgrade: websocket\r\n" +
                "Connection: Upgrade\r\n" +
                "Sec-WebSocket-Accept: $accept\r\n\r\n"
        socket.getOutputStream().write(response.toByteArray(Charsets.UTF_8))
        socket.getOutputStream().flush()
        return true
    }

    private fun writeTextFrame(socket: Socket, text: String) {
        val data = text.toByteArray(Charsets.UTF_8)
        val out = socket.getOutputStream()
        out.write(0x81)
        when {
            data.size < 126 -> out.write(data.size)
            data.size < 65536 -> {
                out.write(126)
                out.write((data.size shr 8) and 0xFF)
                out.write(data.size and 0xFF)
            }
            else -> {
                out.write(127)
                for (shift in 56 downTo 0 step 8) {
                    out.write((data.size.toLong() shr shift).toInt() and 0xFF)
                }
            }
        }
        out.write(data)
        out.flush()
    }

    // -------------------------------------------------------------- dispatch

    fun dispatch(payload: String): String {
        val request = try {
            JSONObject(payload)
        } catch (e: Exception) {
            return JSONObject()
                .put("jsonrpc", "2.0")
                .put("id", JSONObject.NULL)
                .put("error", JSONObject().put("code", -32700).put("message", "parse error"))
                .toString()
        }
        val id = request.opt("id")
        val method = request.optString("method")
        val params = request.optJSONObject("params") ?: JSONObject()
        try {
            val result: Any = when (method) {
                "snapshot" -> executor.snapshot(params.optBoolean("metadataOnly"))
                "perform" -> executor.perform(
                    params.getInt("window"),
                    params.getString("node"),
                    params.getString("action"),
                    params.optString("value").takeIf { params.has("value") },
                    params.optJSONObject("expect"),
                )
                "global" -> executor.global(params.getString("action"))
                "page" -> executor.page(params.optString("direction", "next"))
                "gesture" -> executor.gesture(
                    params.getInt("window"), params.getString("node"),
                    params.optString("kind", "tap"),
                    params.optJSONObject("expect"),
                )
                "config" -> executor.config(params)
                "launch" -> executor.launch(params.getString("package"))
                "openuri" -> executor.openUri(params.getString("uri"))
                "apps" -> executor.apps()
                "clipboard.set" -> executor.setClipboard(params.getString("text"))
                "events" -> executor.events()
                "ping" -> JSONObject().put("ok", true).put("pong", true)
                else -> throw UnknownMethod(method)
            }
            return JSONObject()
                .put("jsonrpc", "2.0")
                .put("id", id)
                .put("result", result)
                .toString()
        } catch (e: UnknownMethod) {
            return rpcError(id, -32601, "unknown method ${e.method}")
        } catch (e: Exception) {
            return rpcError(id, -32000, e.message ?: "internal error")
        }
    }

    private class UnknownMethod(val method: String) : Exception(method)

    private fun rpcError(id: Any?, code: Int, message: String): String =
        JSONObject()
            .put("jsonrpc", "2.0")
            .put("id", id)
            .put("error", JSONObject().put("code", code).put("message", message))
            .toString()
}

/**
 * Server-side frame reader: client frames are always masked per RFC6455.
 * Returns null on clean close, throws on protocol violation.
 */
private class FrameReader(private val input: java.io.InputStream) {

    companion object {
        /** 64 MB: a full snapshot with an 8000-node cap is far below this. */
        const val MAX_FRAME_BYTES = 64L * 1024 * 1024
    }

    /** null = clean close; a string = one text frame. */
    fun readTextFrame(): String? {
        val header = readExact(2) ?: return null
        val opcode = header[0].toInt() and 0x0F
        var length = (header[1].toInt() and 0x7F).toLong()
        val masked = (header[1].toInt() and 0x80) != 0
        if (length == 126L) length = readExact(2)?.let { b ->
            ((b[0].toLong() and 0xFF) shl 8) or (b[1].toLong() and 0xFF)
        } ?: return null
        if (length == 127L) length = readExact(8)?.let { b ->
            var v = 0L
            for (byte in b) v = (v shl 8) or (byte.toLong() and 0xFF)
            v
        } ?: return null

        val mask = if (masked) readExact(4) ?: return null else null
        // RFC 6455 has no protocol-level max, but a frame length from a
        // desynchronised stream is garbage: allocating it is an OOM bomb
        // (found live: a 1.1 GB allocation crashed the whole process after a
        // connection got out of step). Loopback RPC frames are JSON snapshots
        // - tens of MB at the very most; anything larger is corruption.
        if (length > MAX_FRAME_BYTES) {
            throw java.io.IOException("frame too large: $length bytes (max $MAX_FRAME_BYTES)")
        }
        val payload = readExact(length.toInt()) ?: return null
        if (mask != null) {
            for (i in payload.indices) {
                payload[i] = (payload[i].toInt() xor mask[i % 4].toInt()).toByte()
            }
        }
        when (opcode) {
            0x8 -> return null // close
            0x9 -> return ""    // ping: treat as no-op (pong not needed on loopback)
        }
        return String(payload, Charsets.UTF_8)
    }

    private fun readExact(n: Int): ByteArray? {
        if (n == 0) return ByteArray(0)
        val buf = ByteArray(n)
        var read = 0
        while (read < n) {
            val chunk = input.read(buf, read, n - read)
            if (chunk < 0) return null
            read += chunk
        }
        return buf
    }
}
