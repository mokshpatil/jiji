from jiji.core.serialization import canonicalize, sha256, compute_hash


class TestCanonicalize:
    def test_sorted_keys(self):
        result = canonicalize({"b": 2, "a": 1})
        assert result == b'{"a":1,"b":2}'

    def test_no_whitespace(self):
        result = canonicalize({"key": "value", "num": 42})
        assert b" " not in result
        assert b"\n" not in result

    def test_exclude_fields(self):
        result = canonicalize({"a": 1, "b": 2, "c": 3}, exclude_fields={"c"})
        assert result == b'{"a":1,"b":2}'

    def test_null_values(self):
        result = canonicalize({"a": None})
        assert result == b'{"a":null}'

    def test_bytes_auto_convert(self):
        result = canonicalize({"key": b"\x00\x01\x02"})
        assert b"000102" in result

    def test_deterministic(self):
        d = {"z": 1, "a": 2, "m": 3}
        assert canonicalize(d) == canonicalize(d)

    def test_empty_exclude(self):
        d = {"a": 1}
        assert canonicalize(d) == canonicalize(d, exclude_fields=set())


class TestSha256:
    def test_output_length(self):
        assert len(sha256(b"test")) == 32

    def test_deterministic(self):
        assert sha256(b"hello") == sha256(b"hello")

    def test_different_input_different_output(self):
        assert sha256(b"hello") != sha256(b"world")

    def test_empty_input(self):
        result = sha256(b"")
        assert len(result) == 32


class TestComputeHash:
    def test_combines_canonicalize_and_sha256(self):
        d = {"a": 1, "b": 2}
        expected = sha256(canonicalize(d))
        assert compute_hash(d) == expected

    def test_with_exclusion(self):
        d = {"a": 1, "sig": "xyz"}
        h1 = compute_hash(d, exclude_fields={"sig"})
        h2 = compute_hash({"a": 1})
        assert h1 == h2
