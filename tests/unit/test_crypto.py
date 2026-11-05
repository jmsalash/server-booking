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


def test_key_file_appears_complete(tmp_path, monkeypatch):
    import os

    seen = []
    real_link = os.link

    def spy_link(src, dst, *a, **kw):
        # The key is fully written and private before it becomes visible under its name.
        seen.append(((tmp_path / "k").exists(), open(src, "rb").read(),
                     oct(os.stat(src).st_mode)[-3:]))
        return real_link(src, dst, *a, **kw)

    monkeypatch.setattr(os, "link", spy_link)
    key = load_or_create_key(tmp_path / "k")
    assert seen == [(False, key, "600")]
    assert sorted(p.name for p in tmp_path.iterdir()) == ["k"]  # temp file cleaned up


def test_racing_writer_wins(tmp_path, monkeypatch):
    from cryptography.fernet import Fernet

    theirs = Fernet.generate_key()
    real = Fernet.generate_key

    def racing_generate():
        # Another process creates the key file between our existence check and our write.
        (tmp_path / "k").write_bytes(theirs)
        return real()

    monkeypatch.setattr(Fernet, "generate_key", staticmethod(racing_generate))
    assert load_or_create_key(tmp_path / "k") == theirs
    assert (tmp_path / "k").read_bytes() == theirs
    assert sorted(p.name for p in tmp_path.iterdir()) == ["k"]
