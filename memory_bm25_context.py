#!/usr/bin/env python3
"""
memory_bm25_context.py <query>

Runs BM25 search over a memory corpus and outputs a [related_memories] block,
or exits silently if no relevant memories are found.

Intended to be called from a message gateway before forwarding messages to the AI,
injecting relevant memory context inline.

Environment variables:
  SUPABASE_URL          Your Supabase project URL
  SUPABASE_SERVICE_KEY  Your Supabase service role key
"""
import sys, json, time, os, urllib.request

CACHE_FILE = '/tmp/memory_bm25_cache.json'
CACHE_TTL = 300        # 5-minute local cache
MIN_LEN = 7            # minimum query character length
MIN_UNIQUE_TOKENS = 2  # minimum unique jieba tokens
THRESHOLD = 9.0        # BM25 top-score threshold

TYPE_THRESHOLD = {'anchor': 6.0, 'treasure': 7.5}


def get_supabase():
    url = os.environ.get('SUPABASE_URL', '')
    key = os.environ.get('SUPABASE_SERVICE_KEY', '')
    if not url or not key:
        raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
    return url, key


def load_memories():
    try:
        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE) as f:
                cache = json.load(f)
            if time.time() - cache.get('ts', 0) < CACHE_TTL:
                return cache['data']
    except Exception:
        pass

    url, key = get_supabase()
    req = urllib.request.Request(
        url + '/rest/v1/memories?archived=eq.false&select=id,type,importance,content,tags&limit=2000',
        headers={'apikey': key, 'Authorization': 'Bearer ' + key}
    )
    with urllib.request.urlopen(req, timeout=10) as r:
        memories = json.loads(r.read())

    try:
        with open(CACHE_FILE, 'w') as f:
            json.dump({'ts': time.time(), 'data': memories}, f)
    except Exception:
        pass

    return memories


def tokenize(text: str) -> list:
    import jieba
    jieba.setLogLevel(50)
    return list(jieba.cut(text.lower()))


def main():
    if len(sys.argv) < 2:
        sys.exit(0)
    query = sys.argv[1].strip()

    if len(query) < MIN_LEN:
        sys.exit(0)

    try:
        from rank_bm25 import BM25Okapi

        tokens = tokenize(query)
        if len(set(tokens)) < MIN_UNIQUE_TOKENS:
            sys.exit(0)

        memories = load_memories()
        if not memories:
            sys.exit(0)

        corpus = []
        for m in memories:
            tags_str = ' '.join(m.get('tags') or [])
            text = f"{m.get('content', '')} {tags_str} {m.get('type', '')}"
            corpus.append(tokenize(text))

        bm25 = BM25Okapi(corpus)
        scores = bm25.get_scores(tokens)
        min_threshold = min(THRESHOLD, min(TYPE_THRESHOLD.values()))

        if max(scores) < min_threshold:
            sys.exit(0)

        ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
        lines = ['[related_memories]']
        count = 0
        for idx, score in ranked:
            if count >= 3:
                break
            if score < min_threshold:
                break
            m = memories[idx]
            effective_threshold = TYPE_THRESHOLD.get(m.get('type', ''), THRESHOLD)
            if score >= effective_threshold:
                snippet = m.get('content', '')[:100]
                lines.append(f"#{m['id']} {snippet}")
                count += 1

        if count == 0:
            sys.exit(0)

        print('\n'.join(lines))

    except Exception as e:
        sys.stderr.write(f"[memory_bm25] error: {e}\n")
        sys.exit(0)


if __name__ == '__main__':
    main()
