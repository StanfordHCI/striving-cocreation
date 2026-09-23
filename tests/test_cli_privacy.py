from argparse import Namespace

from tempo.cli import cmd_data_dir, cmd_purge


def test_data_dir_and_targeted_purge_commands(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TEMPO_DATA_DIR", str(tmp_path))
    screenshots = tmp_path / "screenshots"
    screenshots.mkdir()
    (screenshots / "capture.jpg").write_bytes(b"private")
    database = tmp_path / "tempo.db"
    database.write_bytes(b"db")

    cmd_data_dir(Namespace())
    assert capsys.readouterr().out.strip() == str(tmp_path.resolve())

    cmd_purge(Namespace(all=False, screenshots=True, yes=True))
    assert not screenshots.exists()
    assert database.exists()

    settings = tmp_path / "settings.json"
    settings.write_text("{}")
    cmd_purge(Namespace(all=True, screenshots=False, yes=True))
    assert tmp_path.exists()
    assert not list(tmp_path.iterdir())
