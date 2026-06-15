import math
import re
import subprocess
import sys
import threading
from collections import Counter
from datetime import datetime

# ── Lexical (BM25) ───────────────────────────────────────────────────────────

def _tok(text: str) -> list:
    return re.findall(r'\b[a-z]{2,}\b', text.lower())


def bm25_scores(results: list, query: str) -> list:
    """BM25 scores for each result, normalized to 0.0–1.0 (index-aligned).
    Returns all 1.0 if query has no terms or nothing scores above 0 (safe fallback).
    """
    if not results or not query.strip():
        return [1.0] * len(results)
    q_terms = Counter(_tok(query))
    if not q_terms:
        return [1.0] * len(results)
    corpus = [_tok(r.title) * 3 + _tok(r.snippet) for r in results]
    n      = len(corpus)
    avg_dl = sum(len(d) for d in corpus) / n
    df: Counter = Counter()
    for doc in corpus:
        for t in set(doc):
            df[t] += 1
    k1, b = 1.5, 0.75
    def _score(doc: list) -> float:
        dl = len(doc); tf = Counter(doc); s = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if not f: continue
            idf = math.log((n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1)
            s += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / max(avg_dl, 1)))
        return s
    raw = [_score(doc) for doc in corpus]
    mx = max(raw) if raw else 0.0
    if mx <= 0.0:
        return [1.0] * len(results)
    return [s / mx for s in raw]


def lexical_rerank(results: list, query: str) -> list:
    """BM25 rerank — title weighted 3×, snippet 1×."""
    if not results or not query.strip():
        return results

    q_terms = Counter(_tok(query))
    if not q_terms:
        return results

    corpus = [_tok(r.title) * 3 + _tok(r.snippet) for r in results]
    n      = len(corpus)
    avg_dl = sum(len(d) for d in corpus) / n

    df: Counter = Counter()
    for doc in corpus:
        for t in set(doc):
            df[t] += 1

    k1, b = 1.5, 0.75

    def _bm25(doc: list) -> float:
        dl = len(doc)
        tf = Counter(doc)
        s  = 0.0
        for term in q_terms:
            f = tf.get(term, 0)
            if not f:
                continue
            idf = math.log((n - df.get(term, 0) + 0.5) / (df.get(term, 0) + 0.5) + 1)
            s  += idf * f * (k1 + 1) / (f + k1 * (1 - b + b * dl / max(avg_dl, 1)))
        return s

    paired = sorted(zip(map(_bm25, corpus), results), key=lambda x: -x[0])
    return [r for _, r in paired]


# ── Semantic (sentence-transformers) ─────────────────────────────────────────

_sem_model      = None
_sem_model_name = None   # tracks which model is currently loaded

_install_status: dict = {"running": False, "done": False, "error": None, "log": ""}
_install_lock = threading.Lock()


def _load_sem_model(model_name: str = "all-MiniLM-L6-v2"):
    global _sem_model, _sem_model_name
    if _sem_model is not None and _sem_model_name == model_name:
        return _sem_model
    _sem_model = None
    _sem_model_name = None
    try:
        from sentence_transformers import SentenceTransformer
        _sem_model      = SentenceTransformer(model_name)
        _sem_model_name = model_name
        return _sem_model
    except Exception:
        return None


def semantic_rerank(results: list, query: str, model_name: str = "all-MiniLM-L6-v2",
                    cutoff: float = 0.0) -> list:
    """Rerank by cosine similarity to query using a local embedding model.

    cutoff: results with similarity below this threshold are dropped (0.0 = keep all).
    """
    if not results or not query.strip():
        return results
    model = _load_sem_model(model_name)
    if model is None:
        return results
    try:
        texts = [f"{r.title} {r.snippet}" for r in results]
        embs  = model.encode([query] + texts, normalize_embeddings=True)
        q_emb, doc_embs = embs[0], embs[1:]
        scored = sorted(
            ((float(d @ q_emb), r) for d, r in zip(doc_embs, results)),
            key=lambda x: -x[0],
        )
        if cutoff > 0.0:
            scored = [(s, r) for s, r in scored if s >= cutoff]
        return [r for _, r in scored]
    except Exception:
        return results


def semantic_available() -> bool:
    try:
        import sentence_transformers  # noqa: F401
        return True
    except ImportError:
        return False


# ── Installer ────────────────────────────────────────────────────────────────

def start_install() -> bool:
    """Start pip install in background. Returns False if already running."""
    if not _install_lock.acquire(blocking=False):
        return False
    _install_status.update(running=True, done=False, error=None, log="")

    def _run():
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", "pip", "install", "sentence-transformers"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
            )
            log = ""
            for line in proc.stdout:
                log += line
                _install_status["log"] = log
            proc.wait()
            if proc.returncode == 0:
                _install_status.update(running=False, done=True, error=None)
            else:
                _install_status.update(running=False, done=False,
                                       error=f"pip exited {proc.returncode}")
        except Exception as e:
            _install_status.update(running=False, done=False, error=str(e))
        finally:
            _install_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return True


def install_status() -> dict:
    return dict(_install_status)


# ── Freshness reranking ───────────────────────────────────────────────────────

_TEMPORAL_RE = re.compile(
    r'\b(today|yesterday|tonight|this\s+week|this\s+month|this\s+year|'
    r'latest|recent|recently|new|now|breaking|live|current|'
    r'update|updates|just|202[0-9]|201[5-9]|news)\b',
    re.IGNORECASE,
)


def freshness_rerank(results: list, query: str) -> list:
    """Blend original ranking with publication recency for time-sensitive queries.

    60% freshness weight, 40% original position. Undated results get a neutral
    freshness score (0.3) — slightly behind recent results but ahead of old ones.
    No-ops when query has no temporal signal.
    """
    if not results or not _TEMPORAL_RE.search(query):
        return results

    from datetime import timezone
    now = datetime.now(timezone.utc)
    n = len(results)
    _MAX_AGE_DAYS = 730.0  # 2 years = freshness 0.0

    def _fresh(r) -> float:
        if r.date is None:
            return 0.3
        age = max(0.0, (now - r.date).total_seconds() / 86400)
        return max(0.0, 1.0 - age / _MAX_AGE_DAYS)

    scored = [
        (0.25 * (1.0 - i / max(n - 1, 1)) + 0.75 * _fresh(r), r)
        for i, r in enumerate(results)
    ]
    scored.sort(key=lambda x: -x[0])
    return [r for _, r in scored]
