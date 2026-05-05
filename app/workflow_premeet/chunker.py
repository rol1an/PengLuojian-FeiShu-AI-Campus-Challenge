"""Document chunking for vector embedding.

Splits document content into text chunks before embedding,
using different strategies per document type.
"""
import re

_MAX_CHUNK_CHARS = 1500  # prevent exceeding Doubao embedding token limit

# Chunking parameters per doc type: (chunk_size_chars, overlap_chars)
_CHUNK_PARAMS: dict[str, tuple[int, int]] = {
    "docx": (800, 100),
    "doc": (800, 100),
    "slides": (500, 50),
    "sheet": (300, 0),
}
_DEFAULT_PARAMS = (800, 100)  # fallback for unknown types


def chunk_doc(title: str, excerpt: str, doc_type: str, full_content: str = "") -> list[str]:
    """Split document into text chunks for embedding.

    Each chunk is prefixed with the document title for semantic completeness.
    Falls back to a single chunk (title + excerpt) when full_content is empty.
    """
    prefix = f"【{title}】\n"
    if not full_content.strip():
        text = f"{prefix}{excerpt}".strip()
        return [text[:_MAX_CHUNK_CHARS]]

    raw_chunks = _split_content(full_content, doc_type)
    result: list[str] = []
    for chunk in raw_chunks:
        chunk = chunk.strip()
        if not chunk:
            continue
        text = f"{prefix}{chunk}"
        # Hard truncate to avoid Doubao token limit
        result.append(text[:_MAX_CHUNK_CHARS])
    return result if result else [f"{prefix}{excerpt}".strip()[:_MAX_CHUNK_CHARS]]


def _split_content(content: str, doc_type: str) -> list[str]:
    doc_type = doc_type.lower()
    if doc_type == "sheet":
        return _split_lines(content, chunk_size=300, overlap=0)
    if doc_type in {"slides", "slide"}:
        return _split_slides(content, chunk_size=500, overlap=50)
    # docx / unknown: split by headings and blank lines
    return _split_docx(content, chunk_size=800, overlap=100)


def _split_docx(content: str, chunk_size: int, overlap: int) -> list[str]:
    """Split by Markdown headings (#, ##, ###) and double newlines."""
    # Split on heading lines or double blank lines
    parts = re.split(r"(?m)(?=^#{1,3}\s)|(?:\n\s*){2,}", content)
    return _merge_chunks(parts, chunk_size, overlap)


def _split_slides(content: str, chunk_size: int, overlap: int) -> list[str]:
    """Split on slide separators (---, ===) or 3+ consecutive blank lines."""
    parts = re.split(r"(?m)^(?:---|===)$|(?:\n\s*){3,}", content)
    return _merge_chunks(parts, chunk_size, overlap)


def _split_lines(content: str, chunk_size: int, overlap: int) -> list[str]:
    """Split line by line (for sheet/table content)."""
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    chunks: list[str] = []
    buf: list[str] = []
    buf_len = 0
    for line in lines:
        if buf_len + len(line) > chunk_size and buf:
            chunks.append("\n".join(buf))
            buf = []
            buf_len = 0
        buf.append(line)
        buf_len += len(line) + 1
    if buf:
        chunks.append("\n".join(buf))
    return chunks


def _merge_chunks(parts: list[str], chunk_size: int, overlap: int) -> list[str]:
    """Merge small parts into chunks of ~chunk_size chars with overlap."""
    chunks: list[str] = []
    buf = ""
    for part in parts:
        part = part.strip()
        if not part:
            continue
        if len(buf) + len(part) > chunk_size and buf:
            chunks.append(buf)
            # Keep tail of previous chunk for overlap
            buf = buf[-overlap:] + "\n" + part if overlap else part
        else:
            buf = (buf + "\n" + part).strip() if buf else part
    if buf.strip():
        chunks.append(buf.strip())
    return chunks
