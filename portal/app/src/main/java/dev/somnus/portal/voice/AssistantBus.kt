package dev.somnus.portal.voice

import org.json.JSONObject
import java.util.concurrent.atomic.AtomicLong

/**
 * The bridge between the system voice-assistant surface and the brain.
 *
 * The brain (host-side Somnus agent loop) owns planning: it polls for a
 * spoken request, drives the phone through the regular perform/snapshot
 * RPC, and posts a final answer back. The Portal only owns the *system*
 * parts it is uniquely positioned for:
 *
 * - receiving the system assistant invocation (VoiceInteractionSession:
 *   long-press home / power / corner swipe, whatever the ROM maps);
 * - capturing the utterance (SpeechRecognizer) and queueing it;
 * - speaking the reply (TTS) and showing it on the session surface.
 *
 * Everything here is in-memory by design: a request nobody polled is a
 * wake that never reached a running brain - holding it forever would make
 * a stale assistant fire minutes later, mid-something-else.
 */
object AssistantBus {

    private val ids = AtomicLong(0)

    /** The request waiting for the brain, or null. One at a time: a new
     *  wake supersedes whatever the brain has not picked up yet. */
    @Volatile
    private var pending: Request? = null

    /** Replies the session surface has not consumed yet, oldest first. */
    private val replies = ArrayDeque<Reply>()

    data class Request(val id: String, val text: String)

    data class Reply(val id: String, val text: String)

    /** A wake produced an utterance. Replaces any un-taken request. */
    @Synchronized
    fun submit(text: String): Request {
        val request = Request(id = ids.incrementAndGet().toString(), text = text)
        pending = request
        return request
    }

    /** The brain takes the request (null when nothing was spoken). */
    @Synchronized
    fun poll(): Request? {
        val taken = pending
        pending = null
        return taken
    }

    fun pollJson(): JSONObject {
        val request = poll()
            ?: return JSONObject().put("request", JSONObject.NULL)
        return JSONObject()
            .put("request", JSONObject().put("id", request.id).put("text", request.text))
    }

    /** The brain posts the final answer for the request it took. */
    @Synchronized
    fun reply(id: String, text: String) {
        replies.addLast(Reply(id, text))
        // Bound the backlog: the session surface consumes replies promptly;
        // anything older than the last few is a superseded conversation.
        while (replies.size > 3) replies.removeFirst()
    }

    /** The session surface takes the reply to speak/show (null when none). */
    @Synchronized
    fun takeReply(afterId: String?): Reply? {
        while (replies.isNotEmpty()) {
            val candidate = replies.first()
            if (afterId != null && candidate.id <= afterId) {
                replies.removeFirst()
                continue
            }
            return candidate
        }
        return null
    }

    /** A fresh assistant session replaces the conversation: drop stale state. */
    @Synchronized
    fun reset() {
        pending = null
        replies.clear()
    }
}
