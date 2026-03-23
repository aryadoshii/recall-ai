# Recall AI 🧠
> Every word, remembered. — Powered by Qubrid AI × MiniMax M2.5

## What it does
A conversational assistant powered by MiniMax M2.5 that sustains perfect
memory across 100+ turns. It tracks your preferences, decisions, and
context — and refers back to earlier parts of the conversation naturally.

Four modes: Personal Assistant · Interview Coach · Customer Support · Study Partner

## What makes this different
Most chatbots forget after 10-20 messages. Recall AI leverages MiniMax M2.5's
massive context window to hold the ENTIRE conversation — every word — and
demonstrate what AI memory actually feels like at scale.

## Setup
```bash
uv venv
source .venv/bin/activate
uv sync
cp .env.example .env
streamlit run app.py
```

## Features
- 4 persona modes (Personal Assistant, Interview Coach, Support, Study)
- 100-turn memory showcase with live turn counter
- Full session history in SQLite sidebar
- Memory depth indicator + key topics extraction
- Load and resume any past session

## Powered by
- MiniMax M2.5 (MiniMax) via Qubrid AI API
- Streamlit · SQLite · uv
