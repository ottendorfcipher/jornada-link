from pathlib import Path

from jornada.state import clear_state, read_state, write_state


def test_state_roundtrip_and_clear(tmp_path: Path):
    path = tmp_path / "nested" / "conn.json"
    assert read_state(path) is None
    write_state(path, {"ip": "1.2.3.4", "n": 1})
    assert read_state(path) == {"ip": "1.2.3.4", "n": 1}
    assert not list(path.parent.glob(".connection-*"))  # temp file renamed away
    clear_state(path)
    assert read_state(path) is None
    clear_state(path)  # idempotent


def test_state_ignores_garbage(tmp_path: Path):
    path = tmp_path / "conn.json"
    path.write_text("not json")
    assert read_state(path) is None
    path.write_text("[1, 2]")
    assert read_state(path) is None
