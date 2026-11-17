import pytest

def test_identity_is_open_gap():
    from src.statutewatch import identity
    with pytest.raises(NotImplementedError):
        identity.process({"id": "x"})

def test_diff_is_open_gap():
    from src.statutewatch import diff
    with pytest.raises(NotImplementedError):
        diff.process({"id": "x"})
