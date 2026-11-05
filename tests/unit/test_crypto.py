import pytest

from app.crypto import SecretBox, load_or_create_key


def test_key_created_once(tmp_path):
    k1 = load_or_create_key(tmp_path / "k")
    k2 = load_or_create_key(tmp_path / "k")
    assert k1 == k2 and oct((tmp_path / "k").stat().st_mode)[-3:] == "600"


def test_secretbox_roundtrip(tmp_path):
    box = SecretBox(load_or_create_key(tmp_path / "k"))
    token = box.encrypt("123:abc")
    assert token != "123:abc" and box.decrypt(token) == "123:abc"


def test_secretbox_wrong_key_fails(tmp_path):
    token = SecretBox(load_or_create_key(tmp_path / "a")).encrypt("x")
    with pytest.raises(Exception):
        SecretBox(load_or_create_key(tmp_path / "b")).decrypt(token)
