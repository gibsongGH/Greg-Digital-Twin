import os
import re
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor

STT_MODEL = os.getenv("DEEPGRAM_STT_MODEL", "nova-3")
TTS_MODEL = os.getenv("DEEPGRAM_TTS_MODEL", "aura-2-orpheus-en")
TTS_MAX_CHARS = 2000
TTS_SAMPLE_RATE = 24000
TTS_CHUNK_TARGET = 80  # ~a sentence per Deepgram request; smaller = lower latency
TTS_TAIL_TARGET = 60  # smaller chunks for the remainder, so the final wait is short
TTS_WORKERS = 4

_client = None


def _deepgram():
    """Lazy Deepgram client so text-only chat works without the key."""
    global _client
    if _client is None:
        key = os.getenv("DEEPGRAM_API_KEY")
        if not key:
            raise RuntimeError(
                "DEEPGRAM_API_KEY is missing — add it to .env to enable voice."
            )
        from deepgram import DeepgramClient

        _client = DeepgramClient(api_key=key)
    return _client


def transcribe_audio(audio_path: str) -> str:
    """Speech-to-text with Deepgram Nova."""
    with open(audio_path, "rb") as f:
        audio_bytes = f.read()
    response = _deepgram().listen.v1.media.transcribe_file(
        request=audio_bytes,
        model=STT_MODEL,
        smart_format=True,
        punctuate=True,
    )
    return response.results.channels[0].alternatives[0].transcript.strip()


# Emoji and related invisibles (ZWJ, variation selectors, keycaps) — Deepgram
# reads them aloud by name ("waving hand", "rocket"), so drop them from speech.
_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"  # emoticons, symbols, transport, supplemental, extended-A
    "☀-➿"          # misc symbols + dingbats (sun, scissors, check, heart)
    "⬀-⯿"          # symbols and arrows (star, up arrow)
    "⌀-⏿"          # misc technical (watch, alarm clock, hourglass)
    "←-⇿"          # arrows
    "‼⁉"           # double bang, interrobang
    "〰〽"           # wavy dash, part-alternation mark
    "︀-️"          # variation selectors
    "‍"                 # zero-width joiner
    "⃣"                 # combining keycap
    "]+"
)

# Markdown syntax the TTS would otherwise read aloud ("star", "backtick", ...).
_MD_PATTERNS = [
    (re.compile(r"```.*?```", re.DOTALL), " "),               # fenced code blocks
    (re.compile(r"`([^`]*)`"), r"\1"),                        # inline code
    (re.compile(r"!\[([^\]]*)\]\([^)]*\)"), r"\1"),           # images -> alt text
    (re.compile(r"\[([^\]]+)\]\([^)]*\)"), r"\1"),            # links -> link text
    (re.compile(r"\*{1,3}(\S(?:[^*]*?\S)?)\*{1,3}"), r"\1"),  # bold / italics
    (re.compile(r"(?<!\w)_{1,2}(\S(?:[^_]*?\S)?)_{1,2}(?!\w)"), r"\1"),  # _emphasis_
    (re.compile(r"^#{1,6}\s+", re.MULTILINE), ""),            # headers
    (re.compile(r"^\s*[-*+]\s+", re.MULTILINE), ""),          # list bullets
    (re.compile(r"^\s*>\s?", re.MULTILINE), ""),              # blockquotes
    (_EMOJI, " "),                                            # emoji -> silence
]

# A pending markdown span (e.g. an unclosed "**") — don't cut a chunk through it.
_MD_CHARS = re.compile(r"[*_`#\[\]>]")

# End of a sentence followed by whitespace.
_SENTENCE_END = re.compile(r"[.!?][\"')\]]*\s")


def strip_markdown(text: str) -> str:
    """Plain-text version of a markdown reply for speech synthesis."""
    for pattern, repl in _MD_PATTERNS:
        text = pattern.sub(repl, text)
    return text


def _synth_pcm(text: str) -> bytes:
    """One Deepgram TTS request returning raw 16-bit PCM."""
    stream = _deepgram().speak.v1.audio.generate(
        text=text,
        model=TTS_MODEL,
        encoding="linear16",
        sample_rate=TTS_SAMPLE_RATE,
        container="none",
    )
    return b"".join(stream)


def _split_chunks(text: str, target: int = TTS_CHUNK_TARGET) -> list[str]:
    """Split plain text at sentence boundaries into ~target-char pieces."""
    chunks: list[str] = []
    buf = ""
    for sentence in re.split(r"(?<=[.!?])\s+", text):
        if buf and len(buf) + len(sentence) > target:
            chunks.append(buf)
            buf = sentence
        else:
            buf = f"{buf} {sentence}".strip()
        # A single run-on sentence still has to fit in one request
        while len(buf) > TTS_MAX_CHARS:
            chunks.append(buf[:TTS_MAX_CHARS])
            buf = buf[TTS_MAX_CHARS:]
    if buf.strip():
        chunks.append(buf)
    return chunks


class SpeechStream:
    """Synthesizes speech concurrently while the reply text is still streaming.

    Call feed() with the accumulated reply as it streams: completed sentences are
    sent to Deepgram in parallel background threads. Call finish() with the final
    text: it synthesizes the remainder, stitches the PCM into one WAV, and returns
    its path — so the wait after the text completes is roughly one small chunk."""

    def __init__(self):
        self._executor = ThreadPoolExecutor(max_workers=TTS_WORKERS)
        self._futures = []
        self._consumed = 0  # chars of stripped text already submitted

    def _submit(self, chunk: str) -> None:
        if chunk.strip():
            self._futures.append(self._executor.submit(_synth_pcm, chunk))

    def feed(self, text_so_far: str) -> None:
        pending = strip_markdown(text_so_far)[:TTS_MAX_CHARS][self._consumed:]
        # An unclosed markdown span could straddle the cut and leak "*" into
        # speech — defer until it resolves (finish() handles any leftovers).
        if _MD_CHARS.search(pending):
            return
        while True:
            cut = None
            for m in _SENTENCE_END.finditer(pending):
                if m.end() >= TTS_CHUNK_TARGET:
                    cut = m.end()
                    break
            if cut is None:
                return
            self._submit(pending[:cut])
            self._consumed += cut
            pending = pending[cut:]

    def finish(self, final_text: str) -> str | None:
        """Synthesize any remaining text and return the stitched WAV path."""
        pending = strip_markdown(final_text)[:TTS_MAX_CHARS][self._consumed:]
        for chunk in _split_chunks(pending, target=TTS_TAIL_TARGET):
            self._submit(chunk)
        try:
            parts = [f.result() for f in self._futures]
        finally:
            self._executor.shutdown(wait=False)
        if not parts:
            return None
        out = tempfile.NamedTemporaryFile(delete=False, suffix=f"_reply_{time.time_ns()}.wav")
        out.close()
        with wave.open(out.name, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(TTS_SAMPLE_RATE)
            for pcm in parts:
                w.writeframes(pcm)
        return out.name

    def cancel(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)


def speak_text(text: str) -> str | None:
    """Text-to-speech (parallel chunks); returns a unique WAV path (helps autoplay)."""
    return SpeechStream().finish(text)
