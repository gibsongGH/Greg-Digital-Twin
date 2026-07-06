import os
import re
import hashlib
from pathlib import Path

import chromadb
from openai import OpenAI

EMBED_MODEL = "text-embedding-3-small"
CHROMA_PATH = "./chroma_db"
COLLECTION_NAME = "greg_kb"
KB_DIR = Path("./knowledge_base")

# In production (e.g. the Hugging Face Space) set KB_DATASET_REPO to a private
# HF dataset repo id like "gibsongHF/digital_twin_docs" so the knowledge base
# is pulled from there instead of the local folder. Leave it unset for local
# testing to read from ./knowledge_base. HF_TOKEN must grant read access to the
# dataset when it is private.
KB_DATASET_REPO = os.getenv("KB_DATASET_REPO", "").strip()

# If any single paragraph is longer than this, split it on sentence boundaries.
MAX_PARAGRAPH_CHARS = 1500

_openai_client = None
_chroma_client = None
_collection = None


def _client() -> OpenAI:
    global _openai_client
    if _openai_client is None:
        _openai_client = OpenAI()
    return _openai_client


def _split_long_paragraph(text: str, max_chars: int = MAX_PARAGRAPH_CHARS) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    sentences = re.split(r"(?<=[.!?])\s+", text)
    chunks, current = [], ""
    for s in sentences:
        if len(current) + len(s) + 1 > max_chars and current:
            chunks.append(current.strip())
            current = s
        else:
            current = (current + " " + s).strip() if current else s
    if current:
        chunks.append(current.strip())
    return chunks


def _extract_section(lines_before: list[str]) -> str:
    for line in reversed(lines_before):
        m = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        if m:
            return m.group(2).strip()
    return ""


def chunk_markdown_by_paragraph(text: str) -> list[dict]:
    """Split a markdown doc into paragraph-level chunks with section metadata."""
    lines = text.split("\n")
    blocks = re.split(r"\n\s*\n+", text)

    chunks = []
    cursor = 0
    for block in blocks:
        block = block.strip()
        if not block:
            continue

        block_start = text.find(block, cursor)
        cursor = block_start + len(block)
        lines_before = text[:block_start].split("\n")
        section = _extract_section(lines_before)

        # Skip pure-heading blocks — they're navigation, not content
        if re.match(r"^#{1,6}\s+\S", block) and "\n" not in block:
            continue
        # Skip horizontal rules and other near-empty markdown noise
        if len(re.sub(r"[\s\-_*=#]", "", block)) < 20:
            continue

        for piece in _split_long_paragraph(block):
            chunks.append({"text": piece, "section": section})

    return chunks


def _doc_id(source: str, chunk_index: int, text: str) -> str:
    h = hashlib.sha1(text.encode("utf-8")).hexdigest()[:8]
    return f"{source}::{chunk_index}::{h}"


def _embed(texts: list[str]) -> list[list[float]]:
    resp = _client().embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]


def get_collection():
    global _chroma_client, _collection
    if _collection is None:
        _chroma_client = chromadb.PersistentClient(path=CHROMA_PATH)
        _collection = _chroma_client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"description": "Greg's digital twin knowledge base"},
        )
    return _collection


def _resolve_kb_dir() -> Path:
    """Return the directory to read knowledge-base markdown from.

    If KB_DATASET_REPO is set, download that (private) Hugging Face dataset and
    read from the local snapshot. Otherwise fall back to the local ./knowledge_base
    folder used during development.
    """
    if not KB_DATASET_REPO:
        return KB_DIR

    from huggingface_hub import snapshot_download

    snapshot_path = snapshot_download(
        repo_id=KB_DATASET_REPO,
        repo_type="dataset",
        token=os.getenv("HF_TOKEN") or None,
        allow_patterns=["*.md"],
    )
    return Path(snapshot_path)


def _kb_fingerprint(kb_dir: Path) -> str:
    """Hash of all KB markdown, so index rebuilds happen exactly when content changes."""
    h = hashlib.sha1()
    for md_path in sorted(kb_dir.glob("*.md")):
        h.update(md_path.name.encode("utf-8"))
        h.update(md_path.read_bytes())
    return h.hexdigest()


def build_index(force_rebuild: bool = False) -> int:
    """Read all .md files in knowledge_base/, chunk them by paragraph, embed, store in Chroma.
    Returns number of chunks indexed. Skips rebuild if the collection is already populated
    from identical KB content and force_rebuild is False."""
    collection = get_collection()

    kb_dir_early = _resolve_kb_dir()
    fingerprint = _kb_fingerprint(kb_dir_early) if kb_dir_early.exists() else ""

    if not force_rebuild and collection.count() > 0:
        indexed_fp = (collection.metadata or {}).get("kb_fingerprint")
        if indexed_fp == fingerprint:
            return collection.count()

    if collection.count() > 0:
        # Easiest reliable reset: drop the collection and recreate.
        global _collection
        _chroma_client.delete_collection(COLLECTION_NAME)
        _collection = _chroma_client.get_or_create_collection(
            name=COLLECTION_NAME,
            metadata={"description": "Greg's digital twin knowledge base"},
        )
        collection = _collection

    kb_dir = kb_dir_early
    if not kb_dir.exists():
        return 0

    all_chunks, all_ids, all_metadatas = [], [], []
    for md_path in sorted(kb_dir.glob("*.md")):
        text = md_path.read_text(encoding="utf-8")
        source = md_path.stem
        for i, chunk in enumerate(chunk_markdown_by_paragraph(text)):
            all_chunks.append(chunk["text"])
            all_ids.append(_doc_id(source, i, chunk["text"]))
            all_metadatas.append({
                "source": source,
                "section": chunk["section"],
                "chunk_index": i,
            })

    if not all_chunks:
        return 0

    embeddings = _embed(all_chunks)
    collection.add(
        ids=all_ids,
        embeddings=embeddings,
        documents=all_chunks,
        metadatas=all_metadatas,
    )
    collection.modify(metadata={
        "description": "Greg's digital twin knowledge base",
        "kb_fingerprint": fingerprint,
    })
    return len(all_chunks)


def retrieve(query: str, n_results: int = 4) -> str:
    """Embed query, fetch top-N relevant chunks, return as a formatted context string."""
    collection = get_collection()
    if collection.count() == 0:
        return ""

    query_embedding = _embed([query])[0]
    results = collection.query(
        query_embeddings=[query_embedding],
        n_results=n_results,
        include=["documents", "metadatas", "distances"],
    )

    docs = results["documents"][0]
    metas = results["metadatas"][0]
    if not docs:
        return ""

    parts = []
    for doc, meta in zip(docs, metas):
        header = f"[{meta.get('source', 'unknown')}"
        if meta.get("section"):
            header += f" — {meta['section']}"
        header += "]"
        parts.append(f"{header}\n{doc}")
    return "\n\n".join(parts)