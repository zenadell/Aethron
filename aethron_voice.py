#!/usr/bin/env python3
"""Ember listens: speech to text on the Mac, on the device.

Double-tap Ember and it listens; it stops by itself once you have been
quiet for SILENCE seconds after speaking (or NOTHING seconds if you never
say a word). The words go back to the page, which turns them into a clear
request for Aethron.

Uses Apple's Speech framework (SFSpeechRecognizer) fed by AVAudioEngine,
and asks for on-device recognition when the Mac supports it, so what you
say does not leave the machine. macOS asks once for the microphone and
for speech recognition; the app's Info.plist carries both reasons.

Only `available()`, `Listener.start/poll/stop` are used by the studio
(/api/voice/start, /api/voice/poll, /api/voice/stop). Off a Mac, or when
the frameworks are missing, `available()` is False and the page falls back
to the browser's own recognizer, or to typing.

    python3 aethron_voice.py --selftest
"""
import sys
import threading
import time

SILENCE = 1.8        # quiet for this long after speaking = finished
NOTHING = 10.0       # no words at all for this long = nothing to hear
LONGEST = 90.0       # one turn of speech is never longer than this


def available() -> bool:
    if sys.platform != "darwin":
        return False
    try:
        import AVFoundation  # noqa: F401
        import Speech  # noqa: F401
        return True
    except Exception:
        return False


def decide(state, text, started, last_change, now, silence=SILENCE, nothing=NOTHING, longest=LONGEST):
    """Pure rule for when listening is over. Returns (state, why)."""
    if state != "listening":
        return state, ""
    if now - started > longest:
        return "done", "that was the longest I listen at once"
    if text.strip():
        if now - last_change >= silence:
            return "done", ""
        return "listening", ""
    if now - started > nothing:
        return "done", "I didn't hear anything"
    return "listening", ""


class Listener:
    """One listening turn at a time."""

    def __init__(self):
        self.lock = threading.Lock()
        self._reset()

    def _reset(self):
        self.state, self.text, self.why = "idle", "", ""
        self.started = self.changed = 0.0
        self.engine = self.request = self.task = None

    # -------------------------------------------------- public
    def start(self):
        with self.lock:
            if self.state in ("asking", "listening"):
                return self.poll_locked()
            self._reset()
            self.state = "asking"
        threading.Thread(target=self._begin, daemon=True).start()
        return self.poll()

    def poll(self):
        with self.lock:
            return self.poll_locked()

    def poll_locked(self):
        if self.state == "listening":
            st, why = decide(self.state, self.text, self.started, self.changed, time.time())
            if st != "listening":
                self._finish(st, why)
        return {"state": self.state, "text": self.text, "why": self.why}

    def stop(self):
        with self.lock:
            if self.state in ("asking", "listening"):
                self._finish("done", "")
            return {"state": self.state, "text": self.text, "why": self.why}

    # -------------------------------------------------- Apple
    def _permissions(self):
        """Ask for speech recognition and the microphone, waiting for the
        person to answer macOS's own dialogs. Returns a refusal or ''."""
        import AVFoundation as AV
        import Speech as SP
        ev = threading.Event()
        got = {}

        st = SP.SFSpeechRecognizer.authorizationStatus()
        if st == 0:                                  # not determined
            def done(s):
                got["speech"] = s
                ev.set()
            SP.SFSpeechRecognizer.requestAuthorization_(done)
            ev.wait(60)
            st = got.get("speech", SP.SFSpeechRecognizer.authorizationStatus())
        if st != 3:                                  # 3 = authorized
            return ("speech recognition is switched off for Aethron: System Settings, "
                    "Privacy & Security, Speech Recognition")
        mic = AV.AVCaptureDevice.authorizationStatusForMediaType_(AV.AVMediaTypeAudio)
        if mic == 0:
            ev.clear()

            def ok(granted):
                got["mic"] = bool(granted)
                ev.set()
            AV.AVCaptureDevice.requestAccessForMediaType_completionHandler_(AV.AVMediaTypeAudio, ok)
            ev.wait(60)
            mic = 3 if got.get("mic") else 2
        if mic != 3:
            return ("the microphone is switched off for Aethron: System Settings, "
                    "Privacy & Security, Microphone")
        return ""

    def _begin(self):
        try:
            import AVFoundation as AV
            import Speech as SP
            refused = self._permissions()
            if refused:
                with self.lock:
                    self.state, self.why = "error", refused
                return
            rec = SP.SFSpeechRecognizer.alloc().init()
            if rec is None or not rec.isAvailable():
                with self.lock:
                    self.state, self.why = "error", "speech recognition is not available right now"
                return
            try:   # results on a queue of our own: the studio thread has no run loop
                from Foundation import NSOperationQueue
                rec.setQueue_(NSOperationQueue.alloc().init())
            except Exception:
                pass
            req = SP.SFSpeechAudioBufferRecognitionRequest.alloc().init()
            req.setShouldReportPartialResults_(True)
            try:
                if rec.supportsOnDeviceRecognition():
                    req.setRequiresOnDeviceRecognition_(True)
            except Exception:
                pass
            eng = AV.AVAudioEngine.alloc().init()
            node = eng.inputNode()
            fmt = node.outputFormatForBus_(0)

            def tap(buf, when):
                try:
                    req.appendAudioPCMBuffer_(buf)
                except Exception:
                    pass
            node.installTapOnBus_bufferSize_format_block_(0, 1024, fmt, tap)
            eng.prepare()
            ok, err = eng.startAndReturnError_(None)
            if not ok:
                with self.lock:
                    self.state, self.why = "error", f"the microphone did not start ({err})"
                return

            def heard(result, error):
                with self.lock:
                    if self.state != "listening":
                        return
                    if result is not None:
                        t = str(result.bestTranscription().formattedString())
                        if t != self.text:
                            self.text, self.changed = t, time.time()
                        if result.isFinal():
                            self._finish("done", "")
                    elif error is not None and not self.text:
                        self._finish("error", "I couldn't make that out")

            with self.lock:
                self.engine, self.request = eng, req
                self.state, self.started, self.changed = "listening", time.time(), time.time()
            self.task = rec.recognitionTaskWithRequest_resultHandler_(req, heard)
        except Exception as e:
            with self.lock:
                self.state, self.why = "error", f"listening failed ({type(e).__name__})"

    def _finish(self, state, why):
        """Called with the lock held."""
        self.state, self.why = state, why
        eng, req, task = self.engine, self.request, self.task
        self.engine = self.request = self.task = None
        try:
            if eng is not None:
                eng.stop()
                eng.inputNode().removeTapOnBus_(0)
            if req is not None:
                req.endAudio()
            if task is not None:
                task.cancel() if state != "done" else task.finish()
        except Exception:
            pass


def _selftest() -> int:
    ok = fails = 0

    def check(name, cond):
        nonlocal ok, fails
        if cond:
            ok += 1; print(f"  ok   {name}")
        else:
            fails += 1; print(f"  FAIL {name}")

    t0 = 100.0
    check("keeps listening while words are still coming", decide("listening", "make the", t0, t0 + 3, t0 + 4)[0] == "listening")
    check("finishes after the silence once something was said", decide("listening", "make the hero warmer", t0, t0 + 3, t0 + 3 + SILENCE + .01)[0] == "done")
    check("a short pause is not the end", decide("listening", "make", t0, t0 + 2, t0 + 2 + SILENCE * .6)[0] == "listening")
    st, why = decide("listening", "", t0, t0, t0 + NOTHING + .1)
    check("nothing said at all ends with a reason", st == "done" and "didn't hear" in why)
    check("waits a while before deciding nothing was said", decide("listening", "", t0, t0, t0 + 4)[0] == "listening")
    check("a turn never runs forever", decide("listening", "and then", t0, t0 + LONGEST, t0 + LONGEST + 1)[0] == "done")
    check("a finished turn stays finished", decide("done", "x", t0, t0, t0)[0] == "done")
    L = Listener()
    check("a fresh listener is idle", L.poll() == {"state": "idle", "text": "", "why": ""})
    check("off a Mac it is simply unavailable", sys.platform == "darwin" or not available())
    print(f"\n{ok} passed, {fails} failed")
    return 1 if fails else 0


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        sys.exit(_selftest())
    print(__doc__)
