from jevapan.cli import main


def test_main_returns_int() -> None:
    assert main([]) == 2
