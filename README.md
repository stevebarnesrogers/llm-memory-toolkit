# llm-memory-toolkit

A collection of lightweight Python services that give a conversational AI system contextual awareness, persistent memory retrieval, multi-channel messaging, and prompt cache efficiency.

These components were built to support a long-running, stateful AI assistant with a Supabase-backed memory store and multiple interaction channels (a messaging bot and a custom web frontend).

---

## Modules

### 1. BM25 Memory Injection

**Files:** `memory_bm25_server.py`, `memory_bm25_context.py`

At-query-time retrieval of semantically relevant memories using BM25 ranking over a Supabase-backed corpus. Before a message is forwarded to the AI, the gateway runs BM25 search and injects a `[related_memories]` block into the prompt — so the AI always has relevant context without relying on a full vector database.

Key design decisions:
- **Chinese-aware tokenization** via `jieba`, with a curated stopword list
- **Tag boosting**: memory tags are repeated in the corpus to increase their BM25 weight
- **Type-specific thresholds**: high-importance memory types (`anchor`, `treasure`) use lower score cutoffs to resist IDF dilution as the corpus grows
- **Non-blocking corpus refresh**: the HTTP server (`memory_bm25_server.py`) is always ready to serve; corpus rebuilds happen in a background thread and take effect atomically
- **Local 5-minute cache** (`memory_bm25_context.py`) to avoid hammering Supabase on every message

**Setup:**
```bash
pip install rank-bm25 jieba
export SUPABASE_URL=https://your-project.supabase.co
export SUPABASE_SERVICE_KEY=your_service_role_key

# HTTP server mode (integrate with message gateway)
python memory_bm25_server.py

# CLI mode (call per-message from a shell gateway script)
python memory_bm25_context.py "what did we talk about last weekend"
```

**Expected Supabase table schema (`memories`):**
```sql
CREATE TABLE memories (
  id          bigint primary key generated always as identity,
  type        text,        -- e.g. 'anchor', 'treasure', 'diary', 'message'
  importance  integer,
  content     text,
  tags        text[],
  archived    boolean default false
);
```

---

### 2. Web Channel Bridge

**File:** `web_channel_bridge.py`

A minimal HTTP bridge that connects a web frontend sending messages via POST to a Claude Code plugin that polls for new messages and delivers AI replies via SSE (Server-Sent Events).

Architecture:
```
Browser (fetch + EventSource)
    │  POST /api/chat { request_id, text }
    │  ← SSE stream of { type, text } events
    ▼
web_channel_bridge.py
    │  GET  /internal/next          ← Web Channel Plugin polls here
    │  POST /internal/reply         ← Plugin delivers Claude's response
    ▼
Web Channel Plugin → Claude Code session
```

No secrets, no external dependencies beyond the standard library.

**Setup:**
```bash
python web_channel_bridge.py
```

---

### 3. Mobile Screen Context Awareness

**File:** `peek_receiver.py`

An HTTP endpoint that receives screenshots POSTed from a mobile device (e.g. triggered by MacroDroid on Android), stores them locally, optionally forwards them to a messaging bot, and injects a signal into a `tmux` session so the AI session can read and respond to the screenshot.

This enables the AI to proactively understand what the user is currently doing on their phone and provide contextually-aware responses — without any manual sharing step from the user.

Security: all uploads require a shared secret sent as the `X-Secret` header. A 30-second cooldown prevents flooding.

**Setup:**
```bash
export PEEK_SECRET=your_shared_secret
export NOTIFY_BOT_TOKEN=your_bot_token      # optional: forward screenshots to a messaging bot
export NOTIFY_CHAT_ID=your_chat_id          # optional: required if NOTIFY_BOT_TOKEN is set
export PEEK_TMUX_SESSION=your_session_name  # default: main
export PEEK_PORT=8766                       # default
export PEEK_SAVE_DIR=/tmp/peek              # default

python peek_receiver.py
```

**Mobile side (MacroDroid example):**
- Trigger: notification received from target app
- Action: HTTP POST to `http://your-server:8766/peek` with header `X-Secret: <value>` and body = screenshot JPEG

---

### 4. Prompt Cache Keepalive

**File:** `cache_keepalive.py`

> **Note:** This module is relevant only when accessing Claude via the **Anthropic API directly**
> (i.e. you manage your own API key and make raw HTTP calls). It does not apply to the web
> interface at claude.ai, which handles caching internally.

Anthropic's prompt cache TTL is 5 minutes. For long system prompts or extended conversation histories, this script fires a minimal heartbeat API request every ~5 minutes when the user is idle — keeping the cache warm so the next real user message does not pay a cold-cache penalty.

The heartbeat replicates the exact cache breakpoint positions that the production chat proxy uses (BP1 on the system prompt, BP2 on the second-to-last user message), so the cached segments stay aligned.

The heartbeat message (`__cache_keepalive__`) is never written to the conversation history in Supabase.

**Setup:**
```bash
export ANTHROPIC_API_KEY=your_key
export SUPABASE_URL=https://your-project.supabase.co
export SUPABASE_SERVICE_KEY=your_service_role_key
# Optional:
export ANTHROPIC_MODEL=claude-sonnet-4-5
export ANTHROPIC_API_BASE=https://api.anthropic.com/v1
export KEEPALIVE_STATE_FILE=/tmp/keepalive_state.json

# Run via cron or systemd timer every 5 minutes:
# */5 * * * * /path/to/python /path/to/cache_keepalive.py
python cache_keepalive.py
```

**Expected Supabase tables:**
- `chat_messages` — columns: `role`, `content`, `created_at`
- `profiles` — columns: `profile_type`, `content` (one row with `profile_type = 'system_profile'` holds the system prompt)

---

## Requirements

```
rank-bm25
jieba
```

All other imports are from the Python standard library.

## License

MIT
