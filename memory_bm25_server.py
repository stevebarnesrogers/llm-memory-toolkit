#!/usr/bin/env python3
"""
BM25 Memory Retrieval Service
Serves BM25-ranked memory search over HTTP on a configurable port.
- Non-blocking: corpus is built asynchronously in a background thread
- ThreadingHTTPServer: corpus refresh does not block incoming requests
- Returns empty string when corpus is not yet ready (fail-safe)

Environment variables:
  SUPABASE_URL          Your Supabase project URL
  SUPABASE_SERVICE_KEY  Your Supabase service role key
  BM25_PORT             Port to listen on (default: 18765)
"""
import os
from http.server import BaseHTTPRequestHandler, HTTPServer
from socketserver import ThreadingMixIn
from urllib.parse import urlparse, parse_qs
import json, time, threading, urllib.request

PORT = int(os.environ.get('BM25_PORT', 18765))
CACHE_TTL = 1800  # refresh corpus every 30 minutes

_lock = threading.Lock()
_corpus_cache = {'ts': 0.0, 'memories': [], 'bm25': None, 'building': False}

MIN_LEN = 7
MIN_UNIQUE_TOKENS = 1
THRESHOLD = 8.5
TAG_BOOST = 3  # repeat tags in corpus to boost tag-match weight

# Lower thresholds for high-importance types to prevent IDF dilution
TYPE_THRESHOLD = {'anchor': 6.5, 'treasure': 7.5}

STOPWORDS = {
    '我','你','他','她','它','我们','你们','他们','她们','的','了','是','在','有','和','也','就','都','而','及',
    '与','这','那','个','一','不','没','很','太','好','说','去','来','看','想','知','可','会','还','呢','啊',
    '嗯','哦','哈','呀','啦','嗒','哎','喔','嘿','诶','嘛','吧','哇','噢','唉','额','哟','哈哈','嗯嗯',
    '今天','明天','昨天','下午','上午','早上','晚上','现在','时候','一下','什么','怎么','为什么','如何',
    '这个','那个','这里','那里','这样','那样','然后','但是','所以','因为','虽然','如果','已经','还是',
    '只是','一直','一起','一点','有点','感觉','觉得','应该','需要','可以','可能','其实','真的','确实',
    '吃饭','睡觉','起来','过来','出去','进去','回来','走了','好了','完了','行了','对了','ok','okay',
    '难道','尽管','虽然','既然','居然','竟然','果然','原来','于是','因此','总之','毕竟',
    '否则','似乎','简直','以为','告诉','知道','以及','关于','对于','根据','通过','由于',
    '然而','不过','况且','何况','甚至','宁可','宁愿','除非','只要','只有','无论','不管',
}


def tokenize(text: str) -> list:
    import jieba, re
    return [t for t in jieba.cut(text.lower()) if re.search(r'\w', t) and t not in STOPWORDS]


def get_supabase():
    url = os.environ.get('SUPABASE_URL', '')
    key = os.environ.get('SUPABASE_SERVICE_KEY', '')
    if not url or not key:
        raise RuntimeError('SUPABASE_URL and SUPABASE_SERVICE_KEY must be set')
    return url, key


def _do_refresh():
    try:
        from rank_bm25 import BM25Okapi
        url, key = get_supabase()
        req = urllib.request.Request(
            url + '/rest/v1/memories?archived=eq.false&select=id,type,importance,content,tags&limit=2000',
            headers={'apikey': key, 'Authorization': 'Bearer ' + key}
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            memories = json.loads(r.read())
        tag_sep = ' '
        corpus = [
            tokenize(m.get('content', '') + ' ' + (tag_sep.join(m.get('tags') or []) + ' ') * TAG_BOOST)
            for m in memories
        ]
        bm25 = BM25Okapi(corpus)
        with _lock:
            _corpus_cache['memories'] = memories
            _corpus_cache['bm25'] = bm25
            _corpus_cache['ts'] = time.time()
            _corpus_cache['building'] = False
        print(f"[bm25] corpus ready: {len(memories)} memories", flush=True)
    except Exception as e:
        with _lock:
            _corpus_cache['building'] = False
        print(f"[bm25] corpus build failed: {e}", flush=True)


def ensure_corpus():
    with _lock:
        age = time.time() - _corpus_cache['ts']
        stale = _corpus_cache['bm25'] is None or age >= CACHE_TTL
        building = _corpus_cache['building']
        memories = _corpus_cache['memories']
        bm25 = _corpus_cache['bm25']

    if stale and not building:
        with _lock:
            _corpus_cache['building'] = True
        t = threading.Thread(target=_do_refresh, daemon=True)
        t.start()

    return memories, bm25


def search(query: str) -> str:
    if len(query) < MIN_LEN:
        return ''
    tokens = tokenize(query)
    unique = len(set(tokens))
    if unique < MIN_UNIQUE_TOKENS:
        return ''
    memories, bm25 = ensure_corpus()
    if bm25 is None:
        return ''
    threshold = THRESHOLD if unique >= 3 else THRESHOLD * 0.7
    min_threshold = min(threshold, min(TYPE_THRESHOLD.values()))
    scores = bm25.get_scores(tokens)
    if not len(scores) or max(scores) < min_threshold:
        return ''
    ranked = sorted(enumerate(scores), key=lambda x: x[1], reverse=True)
    lines = ['[related_memories]']
    count = 0
    for idx, score in ranked:
        if count >= 3:
            break
        if score < min_threshold:
            break
        m = memories[idx]
        effective_threshold = TYPE_THRESHOLD.get(m.get('type', ''), threshold)
        if score >= effective_threshold:
            lines.append(f"#{m['id']} {m.get('content', '')[:100]}")
            count += 1
    return '\n'.join(lines) if count > 0 else ''


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)
        q = qs.get('q', [''])[0]
        result = search(q)
        body = result.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class ThreadingHTTPServer(ThreadingMixIn, HTTPServer):
    daemon_threads = True


if __name__ == '__main__':
    import jieba
    jieba.setLogLevel(50)
    print("[bm25] warming jieba dict...", flush=True)
    list(jieba.cut("warmup"))
    print(f"[bm25] starting server on :{PORT}", flush=True)
    server = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    with _lock:
        _corpus_cache['building'] = True
    threading.Thread(target=_do_refresh, daemon=True).start()
    print(f"[bm25] listening, corpus building in background...", flush=True)
    server.serve_forever()
