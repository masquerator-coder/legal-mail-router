"""
Crypto 模块 — 加解密测试
"""
import pytest
from app.crypto import encrypt, decrypt, reload_fernet


class TestEncryption:
    """Fernet 加解密"""

    def test_encrypt_decrypt_roundtrip(self):
        original = "sensitive data"
        encrypted = encrypt(original)
        # 加密结果不应是明文
        assert encrypted != original
        assert original not in encrypted
        # 解密后恢复
        decrypted = decrypt(encrypted)
        assert decrypted == original

    def test_different_ciphertexts(self):
        """相同明文每次加密结果不同（IV 变化）"""
        msg = "hello"
        e1 = encrypt(msg)
        e2 = encrypt(msg)
        assert e1 != e2

    def test_empty_string(self):
        assert decrypt(encrypt("")) == ""

    def test_unicode(self):
        original = "中文测试 🔐 with emoji"
        assert decrypt(encrypt(original)) == original

    def test_long_string(self):
        original = "A" * 10000
        assert decrypt(encrypt(original)) == original

    def test_reload_fernet(self):
        """reload_fernet 不抛出异常，新密钥下加密可用"""
        reload_fernet()
        new = encrypt("data after reload")
        assert decrypt(new) == "data after reload"
