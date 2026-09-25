"""Tests for WorkspaceToolkit in agno-harness."""

from __future__ import annotations

from pathlib import Path

from agno_harness.runtime.scope import bind_scope
from agno_harness.runtime.translator import make_run_scope
from agno_harness.tools.workspace import WorkspaceToolkit


def test_workspace_toolkit_initialization():
    toolkit_ro = WorkspaceToolkit(allow_write=False)
    assert set(toolkit_ro.functions.keys()) == {"read", "glob", "grep", "list_files"}
    assert "File modification tools are disabled" in str(toolkit_ro.instructions)

    toolkit_rw = WorkspaceToolkit(allow_write=True)
    assert set(toolkit_rw.functions.keys()) == {
        "read",
        "glob",
        "grep",
        "list_files",
        "write_file",
        "patch_file",
    }
    assert "write_file" in str(toolkit_rw.instructions)
    assert "patch_file" in str(toolkit_rw.instructions)


def test_workspace_resolution(tmp_path: Path):
    ws_dir = tmp_path / "workspaces" / "u1" / "t1"
    ws_dir.mkdir(parents=True)

    # 1. Path direct
    tk1 = WorkspaceToolkit(workspace_dir=ws_dir)
    assert tk1.resolve_workspace_root() == ws_dir.resolve()

    # 2. Template string with RunScope
    scope = make_run_scope(
        run_id="run-123",
        thread_id="t-abc",
        user_id="user-xyz",
    )
    with bind_scope(scope):
        tk2 = WorkspaceToolkit(
            workspace_dir=str(tmp_path / "workspaces" / "{user_id}" / "{thread_id}")
        )
        resolved = tk2.resolve_workspace_root()
        assert resolved == (tmp_path / "workspaces" / "user-xyz" / "t-abc").resolve()

    # 3. Callable resolution
    tk3 = WorkspaceToolkit(workspace_dir=lambda s: tmp_path / (s.thread_id if s else "default"))
    with bind_scope(scope):
        assert tk3.resolve_workspace_root() == (tmp_path / "t-abc").resolve()


def test_read_directory_and_files(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "doc.txt").write_text("line 1\nline 2\nline 3\nline 4\nline 5\n", encoding="utf-8")
    sub = root / "sub"
    sub.mkdir()
    (sub / "nested.md").write_text("# Title\nBody text\n", encoding="utf-8")
    (root / ".hidden").write_text("secret", encoding="utf-8")

    tk = WorkspaceToolkit(workspace_dir=root)

    # 1. Inspect root directory
    listing = tk.read("")
    assert "Directory listing for '.'" in listing
    assert "doc.txt" in listing
    assert "sub" in listing
    assert ".hidden" not in listing

    # 2. Inspect subdirectory
    sub_listing = tk.read("sub")
    assert "nested.md" in sub_listing

    # 3. Read slice
    slice_out = tk.read("doc.txt", offset=2, limit=2)
    assert "lines 2 to 3 of 5" in slice_out
    assert "line 2\nline 3" in slice_out

    # 4. Non-existent file
    assert "not found in workspace" in tk.read("missing.txt")

    # 5. Security escape
    escape_res = tk.read("../../etc/passwd")
    assert "Security error" in escape_res or "escapes workspace root" in escape_res


def test_file_resolver_hook(tmp_path: Path):
    root = tmp_path / "ws"
    reports_dir = root / "reports"
    reports_dir.mkdir(parents=True)
    (reports_dir / "target.md").write_text("Found via resolver hook!", encoding="utf-8")

    def custom_resolver(root_path: Path, rel_path: str) -> Path | None:
        direct = root_path / rel_path
        if direct.exists():
            return direct
        in_reports = root_path / "reports" / rel_path
        if in_reports.exists():
            return in_reports
        return None

    tk = WorkspaceToolkit(workspace_dir=root, file_resolver=custom_resolver)
    content = tk.read("target.md")
    assert "Found via resolver hook!" in content
    assert "reports/target.md" in content


def test_glob_and_grep(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "a.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
    (root / "b.py").write_text("def farewell():\n    return 'goodbye'\n", encoding="utf-8")
    (root / "c.txt").write_text("hello there\n", encoding="utf-8")

    tk = WorkspaceToolkit(workspace_dir=root)

    # Glob
    glob_py = tk.glob("*.py")
    assert "a.py" in glob_py
    assert "b.py" in glob_py
    assert "c.txt" not in glob_py

    # Grep substring
    grep_res = tk.grep("hello")
    assert "a.py:1: def hello():" in grep_res
    assert "c.txt:1: hello there" in grep_res
    assert "b.py" not in grep_res

    # Grep regex
    grep_re = tk.grep(r"return\s+'\w+'", is_regexp=True)
    assert "a.py:2:     return 'world'" in grep_re
    assert "b.py:2:     return 'goodbye'" in grep_re


def test_list_files(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()
    (root / "file1.txt").write_text("hello", encoding="utf-8")
    (root / "file2.md").write_text("# md", encoding="utf-8")

    tk = WorkspaceToolkit(workspace_dir=root)
    res = tk.list_files()
    assert "file1.txt" in res
    assert "file2.md" in res


def test_write_and_patch(tmp_path: Path):
    root = tmp_path / "ws"
    root.mkdir()

    # Read-only toolkit blocks writes
    ro_tk = WorkspaceToolkit(workspace_dir=root, allow_write=False)
    assert "disabled" in ro_tk.write_file("new.txt", "data")
    assert "disabled" in ro_tk.patch_file("new.txt", "old", "new")

    # Read-write toolkit
    rw_tk = WorkspaceToolkit(workspace_dir=root, allow_write=True)

    # 1. Write file
    w_res = rw_tk.write_file("sub/output.txt", "Initial line 1\nInitial line 2\n")
    assert "Successfully wrote" in w_res
    assert (root / "sub" / "output.txt").read_text(
        encoding="utf-8"
    ) == "Initial line 1\nInitial line 2\n"

    # 2. Append mode
    app_res = rw_tk.write_file("sub/output.txt", "Appended line 3\n", mode="append")
    assert "Successfully wrote" in app_res
    assert (root / "sub" / "output.txt").read_text(encoding="utf-8") == (
        "Initial line 1\nInitial line 2\nAppended line 3\n"
    )

    # 3. Patch file unique match
    p_res = rw_tk.patch_file("sub/output.txt", "Initial line 2", "Modified line 2")
    assert "Successfully patched" in p_res
    assert "Modified line 2" in (root / "sub" / "output.txt").read_text(encoding="utf-8")

    # 4. Patch file not found
    err_res = rw_tk.patch_file("sub/output.txt", "non_existent_text", "foo")
    assert "Error: `search_block` was not found" in err_res

    # 5. Security escape on write
    esc_write = rw_tk.write_file("../../hacked.txt", "bad")
    assert "Security error" in esc_write
