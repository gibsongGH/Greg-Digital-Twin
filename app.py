import os
from pathlib import Path
from huggingface_hub import snapshot_download

import gradio as gr
from dotenv import load_dotenv

from twin import rag
from twin.convolog import log_turn
from twin.llm import respond

load_dotenv(override=True)

REQUIRED_ENV = ["OPENAI_API_KEY", "CAL_API_KEY", "CAL_EVENT_TYPE_ID"]


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

def chat_fn(message: str, history: list[dict], request: gr.Request):
    session_id = getattr(request, "session_hash", None) or "unknown"
    final_reply = ""
    for partial in respond(message, history):
        final_reply = partial
        yield partial
    log_turn(session_id, message, final_reply)


def build_ui() -> gr.ChatInterface:
    return gr.ChatInterface(
        fn=chat_fn,
        title="Chat with Greg's Digital Twin",
        description=(
            "Hi — I'm a chatbot that represents Greg. Ask me about his work in AI engineering."
        ),
        chatbot=gr.Chatbot(
            placeholder="Ready and listening — say hi 👋",
            height=480,
        ),
    )


if __name__ == "__main__":
    _check_env()
    indexed = rag.build_index()
    print(f"[startup] Knowledge base indexed: {indexed} chunks in '{rag.COLLECTION_NAME}'")

    ui = build_ui()
    ui.launch(
        server_name="0.0.0.0",
        server_port=int(os.environ.get("PORT", 7860)),
    )