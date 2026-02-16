"""Tests for SSRF protection and path validation."""

from __future__ import annotations

import pytest

from microclaw.security import validate_path, validate_url


class TestValidateUrl:
    def test_normal_https_url(self):
        assert validate_url("https://example.com/api") is None

    def test_normal_http_url(self):
        assert validate_url("http://example.com") is None

    def test_blocks_ftp_scheme(self):
        err = validate_url("ftp://files.example.com")
        assert err is not None
        assert "scheme" in err.lower()

    def test_blocks_file_scheme(self):
        err = validate_url("file:///etc/passwd")
        assert err is not None

    def test_blocks_localhost(self):
        err = validate_url("http://localhost:8080")
        assert err is not None
        assert "Blocked" in err

    def test_blocks_127_0_0_1(self):
        err = validate_url("http://127.0.0.1/admin")
        assert err is not None

    def test_blocks_metadata_endpoint(self):
        err = validate_url("http://169.254.169.254/latest/meta-data/")
        assert err is not None

    def test_blocks_internal_domain(self):
        err = validate_url("http://service.internal/api")
        assert err is not None
        assert "internal" in err.lower()

    def test_blocks_local_domain(self):
        err = validate_url("http://printer.local")
        assert err is not None

    def test_empty_hostname(self):
        err = validate_url("http:///path")
        assert err is not None

    def test_blocks_private_ip(self):
        err = validate_url("http://10.0.0.1/admin")
        assert err is not None

    def test_blocks_192_168(self):
        err = validate_url("http://192.168.1.1/")
        assert err is not None


class TestValidatePath:
    def test_normal_path(self):
        assert validate_path("/tmp/somefile.txt") is None

    def test_blocks_traversal(self):
        err = validate_path("/tmp/../etc/shadow")
        assert err is not None
        assert ".." in err

    def test_blocks_etc_shadow(self):
        err = validate_path("/etc/shadow")
        assert err is not None

    def test_blocks_etc_passwd(self):
        err = validate_path("/etc/passwd")
        assert err is not None

    def test_allowed_root(self):
        assert validate_path("/home/user/test.txt", allowed_root="/home/user") is None

    def test_outside_allowed_root(self):
        err = validate_path("/etc/hosts", allowed_root="/home/user")
        assert err is not None
        assert "outside" in err.lower()
