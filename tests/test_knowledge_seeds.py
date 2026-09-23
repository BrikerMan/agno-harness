from typer.testing import CliRunner

from agno_harness.cli.main import app as cli_app
from agno_harness.helpers.scaffold import copy_knowledge_seeds


def test_init_copies_seeds_and_leaves_edits(tmp_path) -> None:
    result = CliRunner().invoke(cli_app, ["init", str(tmp_path), "--channel", "web"])
    assert result.exit_code == 0

    live = tmp_path / "data" / "knowledge" / "memory.md"
    assert live.is_file()
    assert (tmp_path / "data" / "knowledge" / "leave.md").is_file()
    live.write_text("edited live note\n", encoding="utf-8")

    seed = tmp_path / "app" / "knowledge" / "policy.md"
    seed.write_text("# Policy\n", encoding="utf-8")
    copy_knowledge_seeds(tmp_path)

    assert live.read_text(encoding="utf-8") == "edited live note\n"
    assert (tmp_path / "data" / "knowledge" / "policy.md").read_text(encoding="utf-8") == "# Policy\n"
