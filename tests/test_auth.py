"""
Auth 模块 — 密码哈希、频率限制测试
"""
import pytest
from app.auth import hash_password, verify_password


class TestPasswordHashing:
    """PBKDF2 密码哈希"""

    def test_hash_and_verify(self):
        hashed = hash_password("mypassword123")
        assert verify_password("mypassword123", hashed) is True

    def test_wrong_password_fails(self):
        hashed = hash_password("correct")
        assert verify_password("wrong", hashed) is False

    def test_different_salts(self):
        h1 = hash_password("password")
        h2 = hash_password("password")
        # 相同密码每次 salt 不同，哈希不同
        assert h1 != h2

    def test_verify_empty_password(self):
        hashed = hash_password("somepass")
        assert verify_password("", hashed) is False

    def test_verify_against_empty_stored(self):
        assert verify_password("any", "") is False

    def test_verify_against_garbage_stored(self):
        assert verify_password("any", "not-a-valid-hash") is False

    def test_compat_old_iterations(self):
        """向后兼容：旧 100K 迭代的哈希仍可验证"""
        import hashlib, os
        salt = os.urandom(32)
        old_hash = salt.hex() + ":" + hashlib.pbkdf2_hmac(
            "sha256", "oldpass".encode("utf-8"), salt, 100_000
        ).hex()
        assert verify_password("oldpass", old_hash) is True

    def test_hash_format(self):
        hashed = hash_password("test")
        parts = hashed.split(":")
        assert len(parts) == 2
        # salt 是 32 字节 = 64 hex chars
        assert len(parts[0]) == 64
