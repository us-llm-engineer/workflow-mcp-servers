# Copyright 2026 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Local .ipynb parsing for the load_notebook tool.

Deliberately kept free of any FastMCP/asyncio/browser-session dependency so
it can be unit tested as plain synchronous Python. Uses stdlib json only --
no nbformat dependency -- since Colab only ever produces modern nbformat-4
files and the shape we need (cells[].cell_type, cells[].source, notebook
language) is stable and simple enough to validate by hand.
"""

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class NormalizedCell:
    cell_type: str  # "code" or "markdown" only -- "raw"/unknown types are filtered out
    source: str  # already joined/normalized to a single string


def _normalize_source(source) -> str:
    if isinstance(source, list):
        return "".join(source)
    if isinstance(source, str):
        return source
    return str(source)


def _detect_language(notebook: dict) -> str:
    metadata = notebook.get("metadata") or {}
    kernelspec = metadata.get("kernelspec") or {}
    language = kernelspec.get("language")
    if language:
        return language
    language_info = metadata.get("language_info") or {}
    language = language_info.get("name")
    if language:
        return language
    return "python"


def load_ipynb_cells(path: str) -> tuple[list[NormalizedCell], str, int]:
    """Parse a local .ipynb file into normalized cells + notebook language.

    Returns (cells, language, skipped_raw_count):
      - cells: code/markdown cells in original notebook order, source
        normalized to a single string.
      - language: from metadata.kernelspec.language, falling back to
        metadata.language_info.name, falling back to "python".
      - skipped_raw_count: number of cells excluded because their
        cell_type was "raw" or otherwise not "code"/"markdown".

    Raises:
      FileNotFoundError: `path` does not exist.
      ValueError: the file is not valid JSON, is not a JSON object, or is
        missing a "cells" list.
    """
    with open(path, "r", encoding="utf-8") as f:
        try:
            notebook = json.load(f)
        except json.JSONDecodeError as exc:
            raise ValueError(f"Invalid JSON in {path!r}: {exc}") from exc

    if not isinstance(notebook, dict):
        raise ValueError(f"{path!r} does not contain a JSON object at the top level.")

    raw_cells = notebook.get("cells")
    if not isinstance(raw_cells, list):
        raise ValueError(f"{path!r} has no top-level \"cells\" list.")

    cells: list[NormalizedCell] = []
    skipped_raw = 0
    for cell in raw_cells:
        cell_type = cell.get("cell_type", "code") if isinstance(cell, dict) else "code"
        if cell_type not in ("code", "markdown"):
            skipped_raw += 1
            continue
        source = _normalize_source(cell.get("source", "") if isinstance(cell, dict) else "")
        cells.append(NormalizedCell(cell_type=cell_type, source=source))

    language = _detect_language(notebook)
    return cells, language, skipped_raw
