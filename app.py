import os
import json
import requests
from typing import List, Dict, Any

from flask import Flask, request, jsonify
from dotenv import load_dotenv

from azure.core.credentials import AzureKeyCredential
from azure.search.documents import SearchClient
from azure.search.documents.models import VectorQuery

# =========================
# Env & Flask
# =========================
load_dotenv()
app = Flask(__name__)

# ---- Azure OpenAI (REST) ----
# You can use either the regional endpoint (e.g., https://eastus.api.cognitive.microsoft.com)
# or the resource endpoint (e.g., https://aoai-<name>.openai.azure.com)
AOAI_ENDPOINT = os.environ["AZURE_OPENAI_ENDPOINT"].rstrip("/")
AOAI_KEY = os.environ["AZURE_OPENAI_API_KEY"]
AOAI_DEPLOYMENT = os.environ["AZURE_OPENAI_DEPLOYMENT"]               # e.g., gpt-4o-mini
AOAI_EMBED_DEPLOYMENT = os.environ.get("AZURE_OPENAI_EMBED_DEPLOYMENT")  # e.g., text-embedding-3-large (optional)
AOAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-08-01-preview")

CHAT_URL = f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_DEPLOYMENT}/chat/completions?api-version={AOAI_API_VERSION}"
EMBED_URL = (
    f"{AOAI_ENDPOINT}/openai/deployments/{AOAI_EMBED_DEPLOYMENT}/embeddings?api-version={AOAI_API_VERSION}"
    if AOAI_EMBED_DEPLOYMENT else None
)

def _chat(messages, max_tokens=700, temperature=0.1) -> str:
    """Call Azure OpenAI Chat via REST and return the assistant content."""
    headers = {"api-key": AOAI_KEY, "Content-Type": "application/json"}
    payload = {"messages": messages, "max_tokens": max_tokens, "temperature": temperature}
    r = requests.post(CHAT_URL, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

def embed(text: str) -> List[float]:
    """Return an embedding vector or [] if embeddings are not configured/available."""
    if not EMBED_URL:
        return []
    try:
        headers = {"api-key": AOAI_KEY, "Content-Type": "application/json"}
        payload = {"input": text}
        r = requests.post(EMBED_URL, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()["data"][0]["embedding"]
    except Exception:
        return []

# ---- Azure AI Search ----
SEARCH_ENDPOINT = os.environ["AZURE_SEARCH_ENDPOINT"]
SEARCH_KEY = os.environ["AZURE_SEARCH_API_KEY"]
SEARCH_INDEX = os.environ["AZURE_SEARCH_INDEX"]
TOPK = int(os.environ.get("TOPK", "5"))

search_client = SearchClient(
    endpoint=SEARCH_ENDPOINT,
    index_name=SEARCH_INDEX,
    credential=AzureKeyCredential(SEARCH_KEY),
)

# =========================
# Prompting & schema
# =========================
SYSTEM_PROMPT = (
    "You are a cybersecurity assistant. Use ONLY the provided context. "
    "Return a STRICT JSON object with keys: "
    "summary (array of exactly 3 short strings), "
    "iocs (array of objects {type,value,evidence}), "
    "mitigations (array of 5 short strings). "
    "If unknown, use empty arrays but keep the schema. Do NOT include any text outside JSON."
)

RESPONSE_SCHEMA_EXAMPLE = {
    "summary": ["...", "...", "..."],
    "iocs": [{"type": "IP Address", "value": "x.x.x.x", "evidence": "from doc ..."}],
    "mitigations": ["...", "...", "...", "...", "..."]
}

SELECT_FIELDS = ["metadata_storage_path", "metadata_storage_name", "content"]

def _map_doc(doc) -> Dict[str, Any]:
    return {
        "id": doc.get("metadata_storage_path"),
        "source": doc.get("metadata_storage_name"),
        "chunk_id": "",
        "content": doc.get("content"),
    }

def retrieve_semantic(query: str, k: int) -> List[Dict[str, Any]]:
    """Semantic (or classic) search over the index."""
    try:
        results = search_client.search(
            search_text=query,
            query_type="semantic",
            semantic_configuration_name="default",
            select=SELECT_FIELDS,
            top=k,
        )
        return [_map_doc(d) for d in results]
    except Exception:
        # fallback to classic
        results = search_client.search(search_text=query, select=SELECT_FIELDS, top=k)
        return [_map_doc(d) for d in results]

def retrieve_vector(query: str, k: int) -> List[Dict[str, Any]]:
    """Vector search if we have embeddings and a 'contentVector' field in the index."""
    vec = embed(query)
    if not vec:
        return []
    try:
        r = search_client.search(
            search_text="",
            vectors=[VectorQuery(value=vec, k=k, fields="contentVector")],
            select=SELECT_FIELDS,
            top=k,
        )
        return [_map_doc(d) for d in r]
    except Exception:
        return []

def dedupe_keep_order(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for d in items:
        key = d["id"]
        if key not in seen:
            seen.add(key)
            out.append(d)
    return out[:TOPK]

def build_context(snippets: List[Dict[str, Any]]) -> str:
    parts = []
    for s in snippets:
        src = s.get("source") or "unknown"
        content = (s.get("content") or "")[:2000]
        parts.append(f"[{src}] {content}")
    return "\n\n".join(parts)

def coerce_json(text: str) -> Dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end+1])
            except Exception:
                pass
        return {"summary": ["unknown", "unknown", "unknown"], "iocs": [], "mitigations": []}

# =========================
# API
# =========================
@app.post("/ask")
def ask():
    data = request.get_json(force=True, silent=True) or {}
    question = (data.get("question") or "").strip()
    strategy = (data.get("strategy") or "auto").lower()  # 'semantic' | 'vector' | 'auto'
    if not question:
        return jsonify(error="Provide 'question'"), 400

    try:
        # Diagnostic: retrieval-only
        if question.startswith("[diag]"):
            q = question.replace("[diag]", "", 1).strip() or "test"
            sem = retrieve_semantic(q, TOPK)
            vec = retrieve_vector(q, TOPK)
            combined = dedupe_keep_order(vec + sem) if strategy in ("auto", "hybrid") else (vec if strategy=="vector" else sem)
            return jsonify({
                "answer": {"summary":["diagnostic mode","AOAI skipped","showing top snippets"],"iocs":[],"mitigations":[]},
                "citations":[{"source":s["source"],"id":s["id"],"chunk_id":s["chunk_id"]} for s in combined],
                "snippets":[{"source":s["source"],"preview":(s["content"] or "")[:300]} for s in combined],
                "strategy": strategy
            }), 200

        # Retrieval
        if strategy == "semantic":
            snippets = retrieve_semantic(question, TOPK)
        elif strategy == "vector":
            snippets = retrieve_vector(question, TOPK)
        else:  # auto
            v = retrieve_vector(question, TOPK)
            s = retrieve_semantic(question, TOPK)
            snippets = dedupe_keep_order(v + s)

        if not snippets:
            return jsonify(error="No docs retrieved from Search; verify index name, key, and network access"), 502

        # Prompt
        context = build_context(snippets)
        user_prompt = f"""
Context:
{context}

Question: {question}

Return ONLY valid JSON matching this schema:
{json.dumps(RESPONSE_SCHEMA_EXAMPLE, ensure_ascii=False, indent=2)}
"""

        # LLM (REST)
        raw = _chat(
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.1,
            max_tokens=700,
        )
        payload = coerce_json(raw)

        return jsonify({
            "answer": payload,
            "citations":[{"source":s["source"],"id":s["id"],"chunk_id":s["chunk_id"]} for s in snippets],
            "strategy": strategy
        }), 200

    except requests.HTTPError as http_err:
        # Surface useful AOAI error details
        try:
            detail = http_err.response.json()
        except Exception:
            detail = http_err.response.text
        return jsonify(error=f"HTTPError: {http_err}", detail=detail), http_err.response.status_code
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500

@app.get("/")
def health():
    return "OK", 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000"))) 
