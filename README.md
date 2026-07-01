---
title: Greg's Digital Twin
emoji: 🤖
colorFrom: blue
colorTo: purple
sdk: docker
app_port: 7860
pinned: false
---

![Digital Twin Demo](Digital_Twin.png)

# Greg's Digital Twin

> Live demo available on Hugging Face Spaces:  [Digital Twin](https://huggingface.co/spaces/gibsongHF/Greg_Digital_Twin)

An AI-powered chatbot that answers questions about my professional background using my own documents and project history
and books 15-minute intro calls onto my Cal.com calendar via Claude tool 

Built as a practical application of Retrieval-Augmented Generation (RAG), this project turns static career materials into a searchable, conversational system.

It’s essentially a structured, queryable version of a resume — with context.

## What it can do

- Answer questions about my work history and roles
- Explain projects and technical decisions
- Summarize skills and tools across different domains
- Retrieve relevant context from source documents
- Generate clear, conversational responses based on that context

## Why I built it

I wanted to move beyond static portfolios and build something interactive that demonstrates how AI systems actually work.

This project reflects how I think about AI engineering:
- structured inputs
- controlled retrieval
- explainable outputs
- real-world usefulness over novelty

## How the chatbot works

1. Personal documents are chunked and embedded
2. Embeddings are stored in a vector database
3. User queries are converted into embeddings
4. Relevant chunks are retrieved
5. An LLM generates a response using that context

## How the booking flow works

1. Visitor expresses interest in talking → bot decides to invoke tools.
2. Bot calls `list_available_slots(timezone)` → Cal.com API returns open 15-min slots.
3. Bot shows 3-5 options in chat, asks visitor to pick one + confirms name/email.
4. Bot calls `create_booking(start_iso, name, email, timezone, topic)`.
5. Cal.com sends the visitor a calendar invite + the meeting link, and emails Greg.

## Stack
- **Chat:** Anthropic Claude Sonnet 4.6 (`claude-sonnet-4-6`) with tool use
- **Embeddings:** OpenAI `text-embedding-3-small`
- **Vector store:** ChromaDB (local, persisted to `chroma_db/`)
- **UI:** Gradio `ChatInterface` (Gradio 6.x — messages format)
- **Booking:** Cal.com v2 API
- **Notifications:** Pushover
- **Deploy:** Docker → Hugging Face Spaces

## Project layout
```
app.py                  # Gradio entrypoint
twin/
  prompts.py            # System prompt
  rag.py                # Paragraph chunking + Chroma + embeddings
  tools.py              # OpenAI tool schemas + dispatch
  calcom.py             # Cal.com API client (v2)
  llm.py                # OpenAI client + tool-call loop
  convolog.py           # Conversation logger (JSONL)
knowledge_base/
  about_greg.md         # Knowledge base — drop more .md files here
  resume.md
logs/
  conversations.jsonl   # One JSON object per turn (gitignored)
Dockerfile
requirements.txt
.env
```

## Links
- Hugging Face Space: [Live demo](https://huggingface.co/spaces/gibsongHF/Greg_Digital_Twin)
- Portfolio: [Website](https://gibson-ai.com)


Original project via SuperDataScience, expansions including calendar booking credit JeanWeng01