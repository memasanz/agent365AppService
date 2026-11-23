from pathlib import Path

import nbformat


def test_notebook_valid_compilable_and_no_execution_outputs():
    path = Path(__file__).resolve().parents[1] / "notebooks" / "01_register_with_agent365.ipynb"
    notebook = nbformat.read(path, as_version=4)
    nbformat.validate(notebook)
    for cell in notebook.cells:
        if cell.cell_type == "code":
            assert cell.execution_count is None
            assert cell.outputs == []
            compile(cell.source, f"{path.name}:{cell.id}", "exec")
