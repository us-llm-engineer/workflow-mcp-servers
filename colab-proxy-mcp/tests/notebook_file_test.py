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

import json

import pytest

from colab_mcp.notebook_file import NormalizedCell, load_ipynb_cells


def _write(tmp_path, name, obj):
    path = tmp_path / name
    path.write_text(json.dumps(obj), encoding="utf-8")
    return str(path)


def test_mixed_cells_string_and_list_source(tmp_path):
    nb = {
        "nbformat": 4,
        "cells": [
            {"cell_type": "code", "source": ["import sys\n", "print(sys.version)"]},
            {"cell_type": "markdown", "source": "# Title"},
        ],
        "metadata": {},
    }
    path = _write(tmp_path, "nb.ipynb", nb)
    cells, language, skipped = load_ipynb_cells(path)
    assert cells == [
        NormalizedCell(cell_type="code", source="import sys\nprint(sys.version)"),
        NormalizedCell(cell_type="markdown", source="# Title"),
    ]
    assert language == "python"
    assert skipped == 0


def test_raw_cell_skipped_and_counted(tmp_path):
    nb = {
        "cells": [
            {"cell_type": "code", "source": "a = 1"},
            {"cell_type": "raw", "source": "not code or markdown"},
            {"cell_type": "markdown", "source": "text"},
        ],
    }
    path = _write(tmp_path, "nb.ipynb", nb)
    cells, _language, skipped = load_ipynb_cells(path)
    assert [c.cell_type for c in cells] == ["code", "markdown"]
    assert skipped == 1


def test_missing_cells_key_raises_value_error(tmp_path):
    path = _write(tmp_path, "nb.ipynb", {"metadata": {}})
    with pytest.raises(ValueError):
        load_ipynb_cells(path)


def test_cells_not_a_list_raises_value_error(tmp_path):
    path = _write(tmp_path, "nb.ipynb", {"cells": {"not": "a list"}})
    with pytest.raises(ValueError):
        load_ipynb_cells(path)


def test_invalid_json_raises_value_error(tmp_path):
    path = tmp_path / "nb.ipynb"
    path.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_ipynb_cells(str(path))


def test_missing_file_raises_file_not_found_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_ipynb_cells(str(tmp_path / "does-not-exist.ipynb"))


def test_language_from_kernelspec(tmp_path):
    nb = {"cells": [], "metadata": {"kernelspec": {"language": "julia"}}}
    path = _write(tmp_path, "nb.ipynb", nb)
    _cells, language, _skipped = load_ipynb_cells(path)
    assert language == "julia"


def test_language_falls_back_to_language_info(tmp_path):
    nb = {"cells": [], "metadata": {"language_info": {"name": "r"}}}
    path = _write(tmp_path, "nb.ipynb", nb)
    _cells, language, _skipped = load_ipynb_cells(path)
    assert language == "r"


def test_language_defaults_to_python_with_no_metadata(tmp_path):
    nb = {"cells": []}
    path = _write(tmp_path, "nb.ipynb", nb)
    _cells, language, _skipped = load_ipynb_cells(path)
    assert language == "python"


def test_kernelspec_without_language_falls_back_to_language_info(tmp_path):
    nb = {
        "cells": [],
        "metadata": {
            "kernelspec": {"name": "python3", "display_name": "Python 3"},
            "language_info": {"name": "python"},
        },
    }
    path = _write(tmp_path, "nb.ipynb", nb)
    _cells, language, _skipped = load_ipynb_cells(path)
    assert language == "python"
