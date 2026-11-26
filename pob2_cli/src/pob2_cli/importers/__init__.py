# SPDX-License-Identifier: GPL-3.0-only
"""PoE2 build importers package (L1): CN WeGame cookie-free share-link importer."""

from __future__ import annotations

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
    PASSIVE_JEWEL_SLOTS,
    ClassSpec,
    resolve_class_spec,
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
    "PASSIVE_JEWEL_SLOTS",
    "PII_FIELD_NAMES",
    "WEGAME_API_BASE",
    "WEGAME_METHODS",
    "ClassSpec",
    "EndpointPoster",
    "GemCandidate",
    "GemResolution",
    "WeGameImportResult",
    "WeGameTranslator",
    "clean_wegame_text",
    "convert_wegame_bundle_to_pob2_xml",
    "fetch_wegame_share",
    "has_shaman_bonded_marker",
    "is_progress_counter_stat",
    "is_wegame_share_url",
    "parse_leading_int",
    "parse_wegame_share_url",
    "resolve_class_spec",
    "sanitize_item_fields",
    "sanitize_talent_tree",
    "sanitize_wegame_responses",
]
