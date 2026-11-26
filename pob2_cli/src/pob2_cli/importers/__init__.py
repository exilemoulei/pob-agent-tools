# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 build importers package (L1): multi-source share URLs, post-load fixups, and CN WeGame importer."""

from __future__ import annotations

from ..share_codec import (
    MAX_SHARE_XML_BYTES,
    ShareCodeLimitError,
    inflate_capped,
    share_code_to_xml,
    xml_to_share_code,
)
from .post_load_fixups import apply_post_load_fixups, fetch_clean_notes, strip_pob_color_codes
from .share_urls import (
    ResolvedBuildSource,
    TextFetcher,
    load_build_into_worker,
    load_source_label,
    parse_share_url,
    resolve_poe2_build_source,
)
from .url_guard import MAX_REDIRECTS, check_public_https_url, get_public_https
from .wegame_convert import WeGameImportResult, convert_wegame_bundle_to_pob2_xml
from .wegame_fetch import (
    PII_FIELD_NAMES,
    WEGAME_API_BASE,
    WEGAME_METHODS,
    EndpointPoster,
    fetch_wegame_share,
    is_wegame_share_url,
    parse_wegame_share_url,
    sanitize_item_fields,
    sanitize_talent_tree,
    sanitize_wegame_responses,
)
from .wegame_tables import (
    CLASS_SPECS,
    DEFAULT_QUEST_BOOLEAN_KEYS,
    DEFAULT_QUEST_OPTION_KEYS,
    EQUIPMENT_SLOT_MAP,
    JEWEL_METADATA_ID_TO_BASE,
    PASSIVE_JEWEL_SLOTS,
    ClassSpec,
    resolve_class_spec,
    resolve_jewel_base_from_metadata_id,
)
from .wegame_translate import (
    GemCandidate,
    GemResolution,
    WeGameTranslator,
    clean_wegame_text,
    has_shaman_bonded_marker,
    is_progress_counter_stat,
    parse_leading_int,
)

__all__ = [
    "CLASS_SPECS",
    "DEFAULT_QUEST_BOOLEAN_KEYS",
    "DEFAULT_QUEST_OPTION_KEYS",
    "EQUIPMENT_SLOT_MAP",
    "JEWEL_METADATA_ID_TO_BASE",
    "MAX_REDIRECTS",
    "MAX_SHARE_XML_BYTES",
    "PASSIVE_JEWEL_SLOTS",
    "PII_FIELD_NAMES",
    "WEGAME_API_BASE",
    "WEGAME_METHODS",
    "ClassSpec",
    "EndpointPoster",
    "GemCandidate",
    "GemResolution",
    "ResolvedBuildSource",
    "ShareCodeLimitError",
    "TextFetcher",
    "WeGameImportResult",
    "WeGameTranslator",
    "apply_post_load_fixups",
    "check_public_https_url",
    "clean_wegame_text",
    "convert_wegame_bundle_to_pob2_xml",
    "fetch_clean_notes",
    "fetch_wegame_share",
    "get_public_https",
    "has_shaman_bonded_marker",
    "inflate_capped",
    "is_progress_counter_stat",
    "is_wegame_share_url",
    "load_build_into_worker",
    "load_source_label",
    "parse_leading_int",
    "parse_share_url",
    "parse_wegame_share_url",
    "resolve_class_spec",
    "resolve_jewel_base_from_metadata_id",
    "resolve_poe2_build_source",
    "sanitize_item_fields",
    "sanitize_talent_tree",
    "sanitize_wegame_responses",
    "share_code_to_xml",
    "strip_pob_color_codes",
    "xml_to_share_code",
]
