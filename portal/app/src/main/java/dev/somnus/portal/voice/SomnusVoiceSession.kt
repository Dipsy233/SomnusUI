package dev.somnus.portal.voice

import android.app.Activity
import android.content.Context
import android.content.Intent
import android.os.Bundle
import android.speech.RecognitionListener
import android.speech.RecognizerIntent
import android.speech.SpeechRecognizer
import android.speech.tts.TextToSpeech
import android.service.voice.VoiceInteractionSession
import android.view.ViewGroup
import android.widget.LinearLayout
import android.widget.TextView

/**
 * One assistant appearance: the system shows this surface on wake, the mic
 * listens, the utterance goes to the brain (AssistantBus), and the brain's
 * final answer is spoken and shown.
 *
 * The surface deliberately stays tiny - a status line and the transcript -
 * because the real work happens off-screen: the brain drives the phone
 * through the accessibility layer while this card is up. `showThinking`
 * keeps the card alive for the whole run; `finish()` folds it when the
 * reply arrives (or the user taps away).
 */
class SomnusVoiceSession(context: Context) : VoiceInteractionSession(context) {

    private var status: TextView? = null
    private var transcript: TextView? = null
    private var recognizer: SpeechRecognizer? = null
    private var tts: TextToSpeech? = null
    private var ttsReady = false
    private var answered = false

    override fun onShow(args: Bundle?, showCommand: Int) {
        answered = false
        AssistantBus.reset()
        tts = TextToSpeech(context) { state -> ttsReady = state == TextToSpeech.SUCCESS }
        showSurface()
        listen()
    }

    override fun onHide() {
        stopListening()
    }

    override fun onDestroy() {
        stopListening()
        tts?.shutdown()
        tts = null
        super.onDestroy()
    }

    // ------------------------------------------------------------------ UI

    private fun showSurface() {
        val pad = (24 * context.resources.displayMetrics.density).toInt()
        val layout = LinearLayout(context).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
            setBackgroundColor(0xF0141414.toInt())
            setOnClickListener { hide() }
        }
        status = TextView(context).apply {
            text = "Somnus 助手 · 正在聆听…"
            setTextColor(0xFFFFFFFF.toInt())
            textSize = 16f
        }
        layout.addView(status)
        transcript = TextView(context).apply {
            setTextColor(0xFFB0B0B0.toInt())
            textSize = 14f
            setPadding(0, pad / 2, 0, 0)
        }
        layout.addView(transcript)
        setContentView(layout)
    }

    private fun setStatus(text: String) {
        status?.text = text
    }

    // ------------------------------------------------------------- listening

    private fun listen() {
        if (!SpeechRecognizer.isRecognitionAvailable(context)) {
            // No recognizer (rare ROMs): fall back to text entry so the
            // assistant still works - the brain does not care how the
            // utterance arrived.
            typeFallback()
            return
        }
        recognizer = SpeechRecognizer.createSpeechRecognizer(context).apply {
            setRecognitionListener(Listener())
            startListening(
                Intent(RecognizerIntent.ACTION_RECOGNIZE_SPEECH).apply {
                    putExtra(
                        RecognizerIntent.EXTRA_LANGUAGE_MODEL,
                        RecognizerIntent.LANGUAGE_MODEL_FREE_FORM
                    )
                    putExtra(RecognizerIntent.EXTRA_PARTIAL_RESULTS, true)
                }
            )
        }
    }

    private fun stopListening() {
        recognizer?.destroy()
        recognizer = null
    }

    private fun typeFallback() {
        // The voice-interaction surface cannot host an EditText; jump to a
        // plain input activity wired to the same bus (textAssistant not
        // implemented in v1 - show why we are closing instead of hanging).
        setStatus("此设备无语音识别; 请直接运行 brain 脚本输入")
        scheduleFinish()
    }

    // ------------------------------------------------------------- lifecycle

    private fun onUtterance(text: String) {
        transcript?.text = text
        stopListening()
        val request = AssistantBus.submit(text)
        setStatus("已收到 · 大脑处理中 (${request.id})")
        // The brain polls; when its reply lands, the poller of the bus is
        // also the one who speaks it - see maybeSpeak, called from a light
        // wait loop here so the surface stays in charge of TTS.
        waitForReply(request.id)
    }

    private fun waitForReply(id: String) {
        val view = status ?: return
        view.postDelayed({
            if (answered) return@postDelayed
            val reply = AssistantBus.takeReply(null)
            if (reply != null) {
                answered = true
                transcript?.text = reply.text
                setStatus("Somnus 助手 · 完成")
                speak(reply.text)
                scheduleFinish()
            } else {
                setStatus("已收到 · 大脑处理中…")
                waitForReply(id)
            }
        }, 500L)
    }

    private fun speak(text: String) {
        if (!ttsReady) return
        tts?.speak(text, TextToSpeech.QUEUE_FLUSH, null, "somnus-reply")
    }

    private fun scheduleFinish() {
        // Long enough to read a short answer; the card also closes on tap.
        (status ?: return).postDelayed({ hide() }, 6000L)
    }

    // ---------------------------------------------------------------- listener

    private inner class Listener : RecognitionListener {
        override fun onPartialResults(partialResults: Bundle?) {
            val text = partialResults
                ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull() ?: return
            transcript?.text = text
        }

        override fun onResults(results: Bundle?) {
            val text = results
                ?.getStringArrayList(SpeechRecognizer.RESULTS_RECOGNITION)
                ?.firstOrNull()
            if (text.isNullOrBlank()) {
                setStatus("没有听清 · 再唤醒一次")
                scheduleFinish()
                return
            }
            onUtterance(text)
        }

        override fun onError(error: Int) {
            val what = when (error) {
                SpeechRecognizer.ERROR_NO_MATCH, SpeechRecognizer.ERROR_SPEECH_TIMEOUT ->
                    "没有听到说话"
                SpeechRecognizer.ERROR_INSUFFICIENT_PERMISSIONS ->
                    "缺少麦克风权限"
                else -> "识别失败 (code=$error)"
            }
            setStatus("$what · 关闭")
            scheduleFinish()
        }

        override fun onReadyForSpeech(params: Bundle?) {}
        override fun onBeginningOfSpeech() {}
        override fun onRmsChanged(rmsdB: Float) {}
        override fun onBufferReceived(buffer: ByteArray?) {}
        override fun onEndOfSpeech() {
            setStatus("识别中…")
        }
        override fun onEvent(eventType: Int, params: Bundle?) {}
    }
}
