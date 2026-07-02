import os
from pathlib import Path
from huggingface_hub import snapshot_download

import gradio as gr
from dotenv import load_dotenv

from twin import rag
from twin.convolog import log_turn
from twin.llm import respond
from twin.voice import SpeechStream, transcribe_audio

load_dotenv(override=True)

REQUIRED_ENV = ["OPENAI_API_KEY", "CAL_API_KEY", "CAL_EVENT_TYPE_ID"]

CSS = """
#record-box { background: rgb(39, 39, 42) !important; border-radius: 8px !important; padding: 8px !important; }
#record-box #mic { width: 100% !important; background: transparent !important; }
#record-box #mic .wrap, #record-box #mic .form, #record-box #mic .empty,
#record-box #mic .audio-container { background: transparent !important; }
#record-box #mic .empty, #record-box #mic .audio-container { min-height: 0 !important; }
#reply-audio { position: absolute !important; width: 1px !important; height: 1px !important;
  overflow: hidden !important; opacity: 0 !important; pointer-events: none !important; }
"""

# Browsers block audio autoplay; this replays the hidden audio element once it loads.
RESET_AUDIO_JS = """
() => {
  const app = document.querySelector('gradio-app');
  const root = (app && app.shadowRoot) ? app.shadowRoot : document;
  const c = root.querySelector('#reply-audio');
  if (!c) return;
  const m = c.querySelector('audio, video');
  if (!m) return;
  const play = () => { try { m.currentTime = 0; } catch (e) {} const p = m.play(); if (p && p.catch) p.catch(() => {}); };
  if (m.readyState >= 2) play(); else m.addEventListener('loadeddata', play, { once: true });
}
"""


def _check_env() -> None:
    missing = [k for k in REQUIRED_ENV if not os.getenv(k)]
    if missing:
        raise RuntimeError(
            f"Missing required env vars: {', '.join(missing)}. "
            "Fill in required variable values into file .env."
        )

def get_knowledge_base_path() -> Path:
    local_path = Path("knowledge_base")

    # Use local folder during development if present
    if local_path.exists():
        return local_path

    # Otherwise pull private dataset in production
    hf_token = os.getenv("HF_TOKEN")
    if not hf_token:
        raise RuntimeError("Missing HF_TOKEN secret for private dataset access.")

    dataset_path = snapshot_download(
        repo_id="gibsongHF/digital_twin_docs",
        repo_type="dataset",
        token=hf_token,
        allow_patterns=["knowledge_base/*.md"],
    )

    return Path(dataset_path) / "knowledge_base"


def _chat_message(role: str, text: str) -> dict:
    """Gradio 6 messages format with structured content blocks."""
    return {"role": role, "content": [{"type": "text", "text": text}]}


def _message_text(message: dict) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            b.get("text") or ""
            for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _session_id(request: gr.Request) -> str:
    return getattr(request, "session_hash", None) or "unknown"


def add_user_message(message, history):
    """Step 1 (text): show the user message and clear the textbox immediately."""
    history = history or []
    if not message or not message.strip():
        return history, message
    return history + [_chat_message("user", message.strip())], ""


def voice_transcribe(audio, history):
    """Step 1 (voice): transcribe speech, show it as the user message, clear the mic."""
    history = history or []
    if audio is None:
        return history, None
    try:
        transcript = transcribe_audio(audio)
    except Exception as e:
        return history + [_chat_message("assistant", f"Voice error: {e}")], None
    if not transcript:
        return history, None
    return history + [_chat_message("user", transcript)], None


def bot_reply(history, request: gr.Request):
    """Step 2 (text): stream the assistant reply in chat only (no TTS)."""
    history = history or []
    if not history or history[-1]["role"] != "user":
        yield history
        return
    user_message = _message_text(history[-1])
    partial = _chat_message("assistant", "")
    final_reply = ""
    try:
        for chunk in respond(user_message, history[:-1]):
            final_reply = chunk
            partial["content"][0]["text"] = chunk
            yield history + [partial]
    except Exception as e:
        yield history + [_chat_message("assistant", f"Error: {e}")]
        return
    log_turn(_session_id(request), user_message, final_reply)


def voice_reply(history, request: gr.Request):
    """Step 2 (voice): stream the assistant reply, then speak it via autoplay."""
    history = history or []
    if not history or history[-1]["role"] != "user":
        yield history, None
        return
    user_message = _message_text(history[-1])
    partial = _chat_message("assistant", "")
    final_reply = ""
    speech = SpeechStream()  # synthesizes sentences in parallel while text streams
    try:
        for chunk in respond(user_message, history[:-1]):
            final_reply = chunk
            partial["content"][0]["text"] = chunk
            speech.feed(chunk)
            yield history + [partial], None
        audio_path = speech.finish(final_reply)
    except Exception as e:
        speech.cancel()
        yield history + [_chat_message("assistant", f"Error: {e}")], None
        return
    log_turn(_session_id(request), user_message, final_reply)
    yield history + [partial], audio_path


def build_ui() -> gr.Blocks:
    with gr.Blocks(title="Chat with Greg's Digital Twin") as demo:
        gr.Markdown(
            "# Chat with Greg's Digital Twin\n"
            "Hi — I'm a chatbot that represents Greg. Ask me about his work in AI "
            "engineering — type a message or record one below."
        )
        chatbot = gr.Chatbot(
            placeholder="Ready and listening — say hi 👋",
            height=480,
            show_label=False,
            autoscroll=True,
        )
        text_in = gr.Textbox(
            show_label=False,
            placeholder="Type a message, or record one below",
            container=False,
        )
        with gr.Column(elem_id="record-box"):
            mic = gr.Audio(
                sources=["microphone"],
                type="filepath",
                show_label=False,
                container=False,
                elem_id="mic",
            )

        audio_out = gr.Audio(autoplay=True, interactive=False, elem_id="reply-audio", buttons=[])

        mic.stop_recording(
            voice_transcribe, [mic, chatbot], [chatbot, mic], queue=False,
        ).then(voice_reply, chatbot, [chatbot, audio_out])

        text_in.submit(
            add_user_message, [text_in, chatbot], [chatbot, text_in], queue=False,
        ).then(bot_reply, chatbot, chatbot)

        audio_out.change(None, None, None, js=RESET_AUDIO_JS)

    demo.queue(default_concurrency_limit=1)
    demo._deprecated_css = CSS
    return demo


if __name__ == "__main__":
    _check_env()
    indexed = rag.build_index()
    print(f"[startup] Knowledge base indexed: {indexed} chunks in '{rag.COLLECTION_NAME}'")

    ui = build_ui()
    ui.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", 7860)),
    )
