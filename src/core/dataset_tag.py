"""
Shared dataset-tag derivation (T6, ticket t6-config-generator-fix_07052026_0043).

Single source of truth for stripping data-file basenames into registry
dataset tags. Byte-for-byte the logic that lived (duplicated) in
``gcp/vm_e2e_pipeline.py`` at HEAD 2a8311f (:655-661 and :733-740):

  1. legacy ``bk_`` rule fires FIRST (``cl-5m_bk_HourSet_13A`` -> ``HourSet_13A``),
  2. else a case-INSENSITIVE ``{symbol}_`` prefix match with a
     case-PRESERVING slice (``CL_HourSet_14B`` -> ``HourSet_14B``,
     ``es_hourset_01b`` -> ``hourset_01b``),
  3. else passthrough (``HourSet_09`` -> ``HourSet_09``).

Both consumers (``agent/generate_ensemble_artifacts.py`` and
``gcp/vm_e2e_pipeline.py``) must import THIS function — tests pin the
identity of the function object so divergence-by-copy (the root cause of
the dead ``E2E_{sym}_*`` model_path regression, audit section 1) is
structurally impossible.

Stdlib-only leaf: safe to import from the VM pipeline, the generator, and
the live engine alike.
"""

from __future__ import annotations

import re


def derive_dataset_tag(data_basename: str, symbol: str) -> str:
    """Strip legacy ``bk_`` or modern ``{symbol}_`` prefixes for cleaner
    bundle names.

    Args:
        data_basename: the data filename WITHOUT directory or extension
            (callers apply ``os.path.splitext(os.path.basename(path))[0]``).
        symbol: the instrument symbol whose ``{symbol}_`` prefix is stripped.

    Returns:
        The dataset tag (e.g. ``HourSet_14B``) used to build
        ``E2E_{tag}_{direction}_{metric}`` registry bundle names.
    """
    match = re.search(r'bk_(.+)$', data_basename)
    if match:
        dataset_tag = match.group(1)
    elif data_basename.upper().startswith(symbol.upper() + "_"):
        dataset_tag = data_basename[len(symbol) + 1:]
    else:
        dataset_tag = data_basename
    return dataset_tag
