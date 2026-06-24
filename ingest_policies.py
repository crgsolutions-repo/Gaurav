import argparse
import hashlib
import mimetypes
import time
from pathlib import Path

from config import Config
from policy_service import embed_text, split_markdown
from supabase_client import rag_supabase


SUPPORTED_SUFFIXES = {".md", ".txt", ".pdf"}


def parse_args():
    parser = argparse.ArgumentParser(description="Index HR policy documents into Supabase vector search.")
    parser.add_argument("--source", default=Config.POLICY_SOURCE_DIR, help="Policy source directory")
    parser.add_argument("--delay", type=float, default=1.0, help="Delay between embedding requests")
    parser.add_argument("--dry-run", action="store_true", help="Parse and chunk without API/database writes")
    return parser.parse_args()


def sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def category_for(path, text):
    for line in text.splitlines()[:12]:
        if line.lower().startswith("category:"):
            return line.split(":", 1)[1].strip()
    return path.stem.split("_", 1)[-1].replace("_", " ").title()


def title_for(path, text):
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return path.stem.replace("_", " ").title()


def text_chunks(path):
    if path.suffix.lower() == ".md":
        return split_markdown(path)
    if path.suffix.lower() == ".txt":
        text = path.read_text(encoding="utf-8")
        return [{"title": title_for(path, text), "section": "Policy", "content": text, "source_path": str(path)}]
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise RuntimeError("PDF ingestion requires pypdf. Install dependencies from requirements.txt.") from exc

    chunks = []
    reader = PdfReader(str(path))
    title = path.stem.replace("_", " ").title()
    for page_number, page in enumerate(reader.pages, start=1):
        content = (page.extract_text() or "").strip()
        if content:
            chunks.append(
                {
                    "title": title,
                    "section": f"Page {page_number}",
                    "content": content,
                    "source_page": page_number,
                    "source_path": str(path),
                }
            )
    return chunks


def split_large_chunks(chunks, max_chars=2400):
    output = []
    for chunk in chunks:
        content = chunk["content"].strip()
        if len(content) <= max_chars:
            output.append(chunk)
            continue
        paragraphs = [part.strip() for part in content.split("\n\n") if part.strip()]
        buffer = []
        size = 0
        for paragraph in paragraphs:
            if buffer and size + len(paragraph) > max_chars:
                result = dict(chunk)
                result["content"] = "\n\n".join(buffer)
                output.append(result)
                buffer = []
                size = 0
            buffer.append(paragraph)
            size += len(paragraph)
        if buffer:
            result = dict(chunk)
            result["content"] = "\n\n".join(buffer)
            output.append(result)
    return output


def upload_source(path):
    data = path.read_bytes()
    storage_path = f"source/{path.name}"
    bucket = rag_supabase.storage.from_("hr-policies")
    content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    try:
        bucket.upload(storage_path, data, {"content-type": content_type, "upsert": "true"})
    except TypeError:
        bucket.upload(storage_path, data, file_options={"content-type": content_type, "upsert": "true"})
    return storage_path


def index_file(path, delay=1.0, dry_run=False):
    raw = path.read_bytes()
    text = path.read_text(encoding="utf-8") if path.suffix.lower() in {".md", ".txt"} else ""
    chunks = split_large_chunks(text_chunks(path))
    title = title_for(path, text) if text else path.stem.replace("_", " ").title()
    category = category_for(path, text)
    result = {"file": path.name, "title": title, "category": category, "chunks": len(chunks)}
    if dry_run:
        return result
    if not Config.SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is required for policy ingestion.")

    storage_path = upload_source(path)
    document_payload = {
        "title": title,
        "category": category,
        "storage_path": storage_path,
        "source_filename": path.name,
        "source_hash": sha256_bytes(raw),
        "status": "published",
        "metadata": {"source_type": path.suffix.lower().lstrip(".")},
    }
    document_response = (
        rag_supabase.table("hr_policy_documents")
        .upsert(document_payload, on_conflict="source_hash")
        .execute()
    )
    document = (document_response.data or [None])[0]
    if not document:
        lookup = (
            rag_supabase.table("hr_policy_documents")
            .select("*")
            .eq("source_hash", document_payload["source_hash"])
            .limit(1)
            .execute()
        )
        document = (lookup.data or [None])[0]
    if not document:
        raise RuntimeError(f"Could not create policy document record for {path.name}")

    rag_supabase.table("hr_policy_chunks").delete().eq("document_id", document["id"]).execute()
    rows = []
    for index, chunk in enumerate(chunks):
        embedding = embed_text(chunk["content"], "retrieval_document", title=title)
        rows.append(
            {
                "document_id": document["id"],
                "chunk_index": index,
                "section_title": chunk.get("section"),
                "content": chunk["content"],
                "token_count": max(1, len(chunk["content"].split())),
                "source_page": chunk.get("source_page"),
                "embedding": embedding,
                "metadata": {"source_path": chunk.get("source_path")},
            }
        )
        if delay:
            time.sleep(delay)
    if rows:
        rag_supabase.table("hr_policy_chunks").insert(rows).execute()
    return result


def main():
    args = parse_args()
    source = Path(args.source)
    files = [path for path in sorted(source.rglob("*")) if path.is_file() and path.suffix.lower() in SUPPORTED_SUFFIXES]
    if not files:
        raise SystemExit(f"No supported policy files found under {source}")
    total_chunks = 0
    for path in files:
        result = index_file(path, delay=max(args.delay, 0), dry_run=args.dry_run)
        total_chunks += result["chunks"]
        print(f"{result['file']}: {result['chunks']} chunks ({result['category']})")
    print(f"Processed {len(files)} documents and {total_chunks} chunks.")


if __name__ == "__main__":
    main()
