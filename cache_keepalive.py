#!/usr/bin/env python3
"""
Prompt Cache Keepalive

Anthropic's prompt cache TTL is 5 minutes. If you have a long system prompt or
conversation history, this script fires a minimal "heartbeat" request every ~5
minutes to keep the cache warm between real user turns — avoiding a cold-cache
penalty the next time the user sends a message.

How it works:
  1. Checks when the last real conversation message arrived (from Supabase).
  2. If idle for 3–5 minutes (cache is about to expire), sends a tiny request
     that carries the system prompt and recent history, each with
     `cache_control: {type: "ephemeral"}`.
  3. The response is discarded; only the cache write/read counts in `usage` matter.

Run via systemd timer or cron every 5 minutes.

Environment variables:
  ANTHROPIC_API_KEY      Your Anthropic API key (required)
  ANTHROPIC_API_BASE     API base URL (default: https://api.anthropic.com/v1)
  ANTHROPIC_MODEL        Model to use (default: claude-sonnet-4-5)
  SUPABASE_URL           Your Supabase project URL (required)
  SUPABASE_SERVICE_KEY   Your Supabase service role key (required)
  KEEPALIVE_STATE_FILE   Path to persist last-heartbeat state (default: /tmp/keepalive_state.json)
"""

import json, time, urllib.request, urllib.error, datetime, sys, os
from pathlib import Path

ANTHROPIC_KEY  = os.environ.get('ANTHROPIC_API_KEY', '')
API_BASE_URL   = os.environ.get('ANTHROPIC_API_BASE', 'https://api.anthropic.com/v1')
DEFAULT_MODEL  = os.environ.get('ANTHROPIC_MODEL', 'claude-sonnet-4-5')
SUPA_URL       = os.environ.get('SUPABASE_URL', '')
SUPA_KEY       = os.environ.get('SUPABASE_SERVICE_KEY', '')
STATE_FILE     = Path(os.environ.get('KEEPALIVE_STATE_FILE', '/tmp/keepalive_state.json'))

KEEPALIVE_AFTER_MINUTES = 3
MAX_IDLE_HOURS          = 24
HISTORY_LIMIT           = 60


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2))


def supa_get(path):
    req = urllib.request.Request(
        f"{SUPA_URL}{path}",
        headers={'apikey': SUPA_KEY, 'Authorization': f'Bearer {SUPA_KEY}'}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def get_last_message_time():
    try:
        data = supa_get('/rest/v1/chat_messages?select=created_at,role&order=created_at.desc&limit=1')
        if data and data[0].get('created_at'):
            ts = data[0]['created_at'].replace('Z', '+00:00')
            return datetime.datetime.fromisoformat(ts)
    except Exception as e:
        print(f'[keepalive] failed to fetch last message time: {e}')
    return None


def get_system_prompt():
    try:
        data = supa_get('/rest/v1/profiles?profile_type=eq.system_profile&select=content&limit=1')
        if data and data[0].get('content'):
            return data[0]['content']
    except Exception as e:
        print(f'[keepalive] failed to fetch system prompt: {e}')
    return ''


def get_recent_history(limit=HISTORY_LIMIT):
    try:
        data = supa_get(
            f'/rest/v1/chat_messages?select=role,content&order=created_at.desc&limit={limit}'
        )
        data.reverse()
        return [
            {'role': row['role'], 'content': row['content']}
            for row in data
            if row.get('role') in ('user', 'assistant') and row.get('content')
        ]
    except Exception as e:
        print(f'[keepalive] failed to fetch history: {e}')
    return []


def send_keepalive(system_prompt, history_messages):
    system_blocks = [
        {
            "type": "text",
            "text": system_prompt,
            "cache_control": {"type": "ephemeral"}
        }
    ]

    messages = list(history_messages)

    # Mark the second-to-last user message as a cache breakpoint (BP2),
    # matching the position the real chat proxy uses.
    user_indices = [
        i for i, m in enumerate(messages)
        if m.get('role') == 'user'
        and not (
            isinstance(m.get('content'), list)
            and m['content']
            and isinstance(m['content'][0], dict)
            and m['content'][0].get('type') == 'tool_result'
        )
    ]
    cache_idx = user_indices[-2] if len(user_indices) >= 2 else -1
    if cache_idx >= 0:
        cv = messages[cache_idx]['content']
        if isinstance(cv, str):
            messages[cache_idx] = {
                'role': 'user',
                'content': [{'type': 'text', 'text': cv, 'cache_control': {'type': 'ephemeral'}}]
            }
        elif isinstance(cv, list):
            nc = list(cv)
            for bi in range(len(nc) - 1, -1, -1):
                if nc[bi].get('type') == 'text':
                    nc[bi] = {**nc[bi], 'cache_control': {'type': 'ephemeral'}}
                    break
            messages[cache_idx] = {**messages[cache_idx], 'content': nc}

    # Append the heartbeat turn — this never appears in the chat log
    messages.append({
        "role": "user",
        "content": "__cache_keepalive__\nReply with OK only."
    })

    payload = {
        'model': DEFAULT_MODEL,
        'max_tokens': 8,
        'stream': False,
        'system': system_blocks,
        'messages': messages,
    }

    req = urllib.request.Request(
        API_BASE_URL + '/messages',
        data=json.dumps(payload).encode(),
        headers={
            'x-api-key': ANTHROPIC_KEY,
            'anthropic-version': '2023-06-01',
            'anthropic-beta': 'prompt-caching-2024-07-31',
            'content-type': 'application/json',
        },
        method='POST'
    )

    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
        return result.get('usage', {})


def main():
    if not ANTHROPIC_KEY:
        print('[keepalive] ANTHROPIC_API_KEY not set, exiting')
        sys.exit(1)
    if not SUPA_URL or not SUPA_KEY:
        print('[keepalive] SUPABASE_URL and SUPABASE_SERVICE_KEY must be set, exiting')
        sys.exit(1)

    now = datetime.datetime.now(datetime.timezone.utc)
    state = load_state()

    last_msg_time = get_last_message_time()
    if not last_msg_time:
        print('[keepalive] cannot determine last message time, skipping')
        return

    idle_minutes = (now - last_msg_time).total_seconds() / 60
    print(f'[keepalive] idle {idle_minutes:.1f} min since last message')

    if idle_minutes > MAX_IDLE_HOURS * 60:
        print(f'[keepalive] idle > {MAX_IDLE_HOURS}h, skipping')
        return

    if idle_minutes < KEEPALIVE_AFTER_MINUTES:
        print(f'[keepalive] still active (< {KEEPALIVE_AFTER_MINUTES} min), skipping')
        return

    last_touch = state.get('last_cache_touch_at')
    if last_touch:
        touch_ago = (now - datetime.datetime.fromisoformat(last_touch)).total_seconds() / 60
        if touch_ago < 4:
            print(f'[keepalive] last heartbeat {touch_ago:.1f} min ago, skipping')
            return

    system_prompt = get_system_prompt()
    if not system_prompt:
        print('[keepalive] system prompt empty, skipping')
        return

    history = get_recent_history()
    print(f'[keepalive] fetched {len(history)} history messages')
    print('[keepalive] sending heartbeat...')

    usage = None
    last_err = None
    for attempt in range(3):
        try:
            usage = send_keepalive(system_prompt, history)
            break
        except urllib.error.HTTPError as e:
            last_err = f'HTTP {e.code}: {e.read().decode()}'
            print(f'[keepalive] attempt {attempt+1} failed: {last_err}')
            if attempt < 2:
                time.sleep(5)
        except Exception as e:
            last_err = str(e)
            print(f'[keepalive] attempt {attempt+1} error: {e}')
            if attempt < 2:
                time.sleep(5)

    if usage is None:
        print(f'[keepalive] all retries failed: {last_err}')
        return

    cache_read  = usage.get('cache_read_input_tokens', 0)
    cache_write = usage.get('cache_creation_input_tokens', 0)
    status = 'hit' if cache_read > 0 else 'write' if cache_write > 0 else 'miss'
    print(
        f'[keepalive] {status} | input={usage.get("input_tokens",0)} '
        f'output={usage.get("output_tokens",0)} '
        f'cache_read={cache_read} cache_write={cache_write}'
    )

    state['last_cache_touch_at'] = now.isoformat()
    state['last_status'] = status
    state['last_usage'] = usage
    save_state(state)


if __name__ == '__main__':
    main()
