from argparse import Namespace
from pathlib import Path
import os
import zipfile

def test_pre1980_timestamp_preserves_file_bytes(tmp_path, monkeypatch):
    """Portable ZIP dates preserve files with pre-1980 timestamps."""
    hermes_home = tmp_path / ".hermes"
    hermes_home.mkdir()
    (hermes_home / "config.yaml").write_text("model: test\n")

    # Create a file with epoch timestamp (1970-01-01)
    old_file = hermes_home / "ancient.txt"
    old_file.write_text("old data")
    os.utime(old_file, (0, 0))

    monkeypatch.setenv("HERMES_HOME", str(hermes_home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    out_zip = tmp_path / "out.zip"
    args = Namespace(output=str(out_zip))

    from hermes_cli.backup import run_backup
    run_backup(args)

    # Zip should still be created with the valid files
    assert out_zip.exists()
    with zipfile.ZipFile(out_zip, "r") as zf:
        names = zf.namelist()
        assert "config.yaml" in names
        # ZIP timestamp clamping must not drop the file.
        assert zf.read("ancient.txt") == b"old data"
        assert zf.getinfo("ancient.txt").date_time[0] == 1980
