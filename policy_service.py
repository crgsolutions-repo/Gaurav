import logging
import re
from functools import lru_cache
from pathlib import Path

from config import Config
from supabase_client import rag_supabase


logger = logging.getLogger(__name__)
WORD_PATTERN = re.compile(r"[a-z0-9]+")
SENSITIVE_OR_POLICY_TERMS = (
    "harass",
    "bully",
    "discriminat",
    "retaliat",
    "unsafe",
    "threat",
    "grievance",
    "whistleblow",
    "manager is on leave",
    "manager on leave",
    "alternate approver",
    "higher authority",
    "escalate",
    "company policy",
    "hr policy",
    "code of conduct",
    "passed away",
    "died",
    "bereavement",
    "wife is due",
    "child was born",
    "getting married",
    "medical emergency",
    "forgot to punch out",
    "workplace complaint",
)


def tokens(value):
    return set(WORD_PATTERN.findall(str(value or "").lower()))


def should_use_copilot(message):
    text = str(message or "").strip().lower()
    if not text:
        return False
    if any(term in text for term in SENSITIVE_OR_POLICY_TERMS):
        return True
    return any(
        phrase in text
        for phrase in (
            "what should i do",
            "what are my options",
            "who should i contact",
            "where can i report",
            "is this allowed",
            "is this covered",
        )
    )


def split_markdown(path):
    text = path.read_text(encoding="utf-8")
    title = path.stem.replace("_", " ").title()
    current_heading = title
    buffer = []
    chunks = []

    def flush():
        content = "\n".join(buffer).strip()
        if content:
            chunks.append(
                {
                    "title": title,
                    "section": current_heading,
                    "content": content,
                    "source_path": str(path),
                }
            )
        buffer.clear()

    for line in text.splitlines():
        if line.startswith("# "):
            flush()
            title = line[2:].strip()
            current_heading = title
        elif line.startswith("## "):
            flush()
            current_heading = line[3:].strip()
        else:
            buffer.append(line)
    flush()
    return chunks


@lru_cache(maxsize=1)
def local_policy_chunks():
    source_dir = Path(Config.POLICY_SOURCE_DIR)
    if not source_dir.exists():
        return []
    chunks = []
    for path in sorted(source_dir.glob("*.md")):
        chunks.extend(split_markdown(path))
    return chunks


def local_policy_search(query, match_count=5):
    query_tokens = tokens(query)
    if not query_tokens:
        return []
    scored = []
    for chunk in local_policy_chunks():
        heading_tokens = tokens(chunk["title"] + " " + chunk["section"])
        content_tokens = tokens(chunk["content"])
        score = len(query_tokens & content_tokens) + (2 * len(query_tokens & heading_tokens))
        if score:
            result = dict(chunk)
            result["similarity"] = score / max(len(query_tokens), 1)
            scored.append(result)
    return sorted(scored, key=lambda row: row["similarity"], reverse=True)[:match_count]


def embed_text(text, task_type="retrieval_query", title=None):
    import google.generativeai as genai

    genai.configure(api_key=Config.GEMINI_API_KEY)
    kwargs = {
        "model": Config.GEMINI_EMBEDDING_MODEL,
        "content": text,
        "task_type": task_type,
        "output_dimensionality": Config.GEMINI_EMBEDDING_DIMENSION,
        "request_options": {"timeout": Config.GEMINI_TIMEOUT_SECONDS},
    }
    if title and task_type == "retrieval_document":
        kwargs["title"] = title
    response = genai.embed_content(**kwargs)
    return response["embedding"]


def vector_policy_search(query, match_count=5, threshold=0.45):
    if not Config.RAG_ENABLED or not Config.SUPABASE_SERVICE_ROLE_KEY:
        return []
    try:
        query_embedding = embed_text(query, "retrieval_query")
        response = rag_supabase.rpc(
            "match_hr_policy_chunks",
            {
                "query_embedding": query_embedding,
                "match_count": match_count,
                "match_threshold": threshold,
            },
        ).execute()
        return response.data or []
    except Exception as exc:
        logger.warning("Vector policy retrieval unavailable; using local retrieval: %s", exc)
        return []


def retrieve_policy_chunks(query, match_count=5):
    vector_results = vector_policy_search(query, match_count=match_count)
    return vector_results or local_policy_search(query, match_count=match_count)


def format_policy_context(chunks):
    sections = []
    for chunk in chunks:
        title = chunk.get("title") or chunk.get("document_title") or "HR Policy"
        section = chunk.get("section") or chunk.get("section_title") or "Relevant section"
        content = str(chunk.get("content") or "").strip()
        if content:
            sections.append(f"Source: {title} | Section: {section}\n{content}")
    return "\n\n".join(sections)


def retrieve_policy_context(query, match_count=5):
    return format_policy_context(retrieve_policy_chunks(query, match_count=match_count))
