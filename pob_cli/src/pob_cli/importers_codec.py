"""PoB share-code codec: URL-safe Base64 over zlib, with a cap on what a code may inflate to."""

from __future__ import annotations

import base64
import binascii
import re
import zlib

# A real build XML is a few MiB at most; anything past this is a decompression bomb.
MAX_SHARE_XML_BYTES = 64 * 1024 * 1024


def _inflate_capped(raw_bytes: bytes, wbits: int) -> bytes:
    """`zlib.decompress` that stops at MAX_SHARE_XML_BYTES instead of filling memory."""
    inflater = zlib.decompressobj(wbits)
    out = inflater.decompress(raw_bytes, MAX_SHARE_XML_BYTES + 1)
    if len(out) <= MAX_SHARE_XML_BYTES and not inflater.unconsumed_tail:
        out += inflater.flush()
    if len(out) > MAX_SHARE_XML_BYTES or inflater.unconsumed_tail:
        raise ValueError(f"decompressed build XML exceeds {MAX_SHARE_XML_BYTES // (1024 * 1024)} MiB")
    if not inflater.eof:
        raise zlib.error("incomplete or truncated stream")
    return out


def xml_to_share_code(xml_text: str) -> str:
    """Compress raw PoB XML string into a URL-safe Base64 PoB share code."""
    clean_xml = xml_text.lstrip("\ufeff")
    compressed = zlib.compress(clean_xml.encode("utf-8"), level=9)
    return base64.urlsafe_b64encode(compressed).decode("ascii")


def share_code_to_xml(code: str) -> str:
    """Decompress a URL-safe Base64 PoB share code into raw PoB XML text.

    Characters outside the Base64 alphabet are dropped, as PoB's own decoder does, so every code
    the engine would accept decodes here too and the PoE2 guardrail sees what the engine gets.
    """
    cleaned = re.sub(r"\s+", "", code.lstrip("\ufeff")).replace("-", "+").replace("_", "/")
    if not cleaned:
        raise ValueError("Invalid PoB share code: empty share code.")
    pad = (-len(cleaned)) % 4
    if pad:
        cleaned += "=" * pad
    try:
        raw_bytes = base64.b64decode(cleaned, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"Invalid PoB share code: {exc}") from exc
    try:
        try:
            decompressed = _inflate_capped(raw_bytes, zlib.MAX_WBITS)
        except zlib.error:
            decompressed = _inflate_capped(raw_bytes, -zlib.MAX_WBITS)
    except (zlib.error, ValueError) as exc:
        raise ValueError(f"Invalid PoB share code: {exc}") from exc
    return decompressed.decode("utf-8-sig", errors="replace").lstrip("\ufeff")
