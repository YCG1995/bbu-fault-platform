"""迁移包认证加密：scrypt + RFC 8439 ChaCha20-Poly1305，纯标准库实现。

仅用于离线迁移包。格式为固定 magic + 长度前缀 header + AEAD 密文 + 16 字节 tag；
header 同时作为 AEAD 的 AAD，因此修改 KDF 参数或 nonce 会直接校验失败。
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import struct
import time
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable

MAGIC = b"BBUMIG01"
HEADER_LIMIT = 8192
CHUNK = 256 * 1024
SCRYPT_N = 2 ** 15
SCRYPT_R = 8
SCRYPT_P = 1
KEY_LEN = 32
SALT_LEN = 16
NONCE_LEN = 12
TAG_LEN = 16
_PURE_CHACHA = "chacha20-poly1305"
_AES_GCM = "aes-256-gcm"


def _aes_gcm_available() -> bool:
    if os.environ.get("BBU_MIGRATION_PURE_CRYPTO", "").strip() in ("1", "true", "yes"):
        return False
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


class MigrationCryptoError(ValueError):
    pass


def _rotl(value: int, bits: int) -> int:
    return ((value << bits) & 0xFFFFFFFF) | (value >> (32 - bits))


def _quarter_round(state: list[int], a: int, b: int, c: int, d: int) -> None:
    state[a] = (state[a] + state[b]) & 0xFFFFFFFF
    state[d] = _rotl(state[d] ^ state[a], 16)
    state[c] = (state[c] + state[d]) & 0xFFFFFFFF
    state[b] = _rotl(state[b] ^ state[c], 12)
    state[a] = (state[a] + state[b]) & 0xFFFFFFFF
    state[d] = _rotl(state[d] ^ state[a], 8)
    state[c] = (state[c] + state[d]) & 0xFFFFFFFF
    state[b] = _rotl(state[b] ^ state[c], 7)


def chacha20_block(key: bytes, counter: int, nonce: bytes) -> bytes:
    if len(key) != 32 or len(nonce) != 12:
        raise MigrationCryptoError("ChaCha20 key/nonce 长度非法")
    if counter < 0 or counter > 0xFFFFFFFF:
        raise MigrationCryptoError("ChaCha20 counter 越界")
    constants = (0x61707865, 0x3320646E, 0x79622D32, 0x6B206574)
    initial = list(constants) + list(struct.unpack("<8I", key)) + [counter] + list(struct.unpack("<3I", nonce))
    state = initial.copy()
    for _ in range(10):
        _quarter_round(state, 0, 4, 8, 12)
        _quarter_round(state, 1, 5, 9, 13)
        _quarter_round(state, 2, 6, 10, 14)
        _quarter_round(state, 3, 7, 11, 15)
        _quarter_round(state, 0, 5, 10, 15)
        _quarter_round(state, 1, 6, 11, 12)
        _quarter_round(state, 2, 7, 8, 13)
        _quarter_round(state, 3, 4, 9, 14)
    state = [(state[i] + initial[i]) & 0xFFFFFFFF for i in range(16)]
    return struct.pack("<16I", *state)


def _xor_bytes(data: bytes, stream: bytes) -> bytes:
    return bytes(a ^ b for a, b in zip(data, stream))


def _poly1305_tag(key: bytes, message: bytes) -> bytes:
    r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
    s = int.from_bytes(key[16:], "little")
    p = (1 << 130) - 5
    acc = 0
    for offset in range(0, len(message), 16):
        block = message[offset:offset + 16]
        acc = ((acc + int.from_bytes(block + b"\x01", "little")) * r) % p
    return ((acc + s) & ((1 << 128) - 1)).to_bytes(16, "little")


def _pad16(data: bytes) -> bytes:
    remain = len(data) % 16
    return data + (b"\x00" * (16 - remain) if remain else b"")


class _Poly1305:
    """增量 Poly1305：避免纯 Python 回退时把整个密文留在内存。"""

    def __init__(self, key: bytes) -> None:
        self.r = int.from_bytes(key[:16], "little") & 0x0FFFFFFC0FFFFFFC0FFFFFFC0FFFFFFF
        self.s = int.from_bytes(key[16:], "little")
        self.p = (1 << 130) - 5
        self.acc = 0
        self.buffer = b""

    def _block(self, block: bytes) -> None:
        self.acc = ((self.acc + int.from_bytes(block + b"\x01", "little")) * self.r) % self.p

    def update(self, data: bytes) -> None:
        if not data:
            return
        buffer = self.buffer + data
        while len(buffer) >= 16:
            self._block(buffer[:16])
            buffer = buffer[16:]
        self.buffer = buffer

    def tag(self, aad_len: int, ciphertext_len: int) -> bytes:
        if self.buffer:
            self._block(self.buffer + b"\x00" * (16 - len(self.buffer)))
            self.buffer = b""
        self._block(struct.pack("<QQ", aad_len, ciphertext_len))
        return ((self.acc + self.s) & ((1 << 128) - 1)).to_bytes(16, "little")


class _AeadBase:
    def __init__(self, key: bytes, nonce: bytes, aad: bytes) -> None:
        if len(key) != 32 or len(nonce) != 12:
            raise MigrationCryptoError("AEAD key/nonce 长度非法")
        self.key = key
        self.nonce = nonce
        self.aad = aad
        self.mac_key = chacha20_block(key, 0, nonce)[:32]
        self._mac = _Poly1305(self.mac_key)
        self._mac.update(_pad16(aad))
        self.counter = 1
        self.ciphertext_len = 0
        self._keystream = b""

    def _stream(self, length: int) -> bytes:
        if length <= 0:
            return b""
        out = bytearray()
        while len(out) < length:
            if not self._keystream:
                if self.counter > 0xFFFFFFFF:
                    raise MigrationCryptoError("迁移包过大，ChaCha20 counter 溢出")
                self._keystream = chacha20_block(self.key, self.counter, self.nonce)
                self.counter += 1
            take = min(length - len(out), len(self._keystream))
            out.extend(self._keystream[:take])
            self._keystream = self._keystream[take:]
        return bytes(out)

    def _tag(self) -> bytes:
        return self._mac.tag(len(self.aad), self.ciphertext_len)


class Encryptor(_AeadBase):
    def __init__(self, key: bytes, nonce: bytes, aad: bytes) -> None:
        super().__init__(key, nonce, aad)

    def update(self, plaintext: bytes) -> bytes:
        stream = self._stream(len(plaintext))
        cipher = _xor_bytes(plaintext, stream)
        self._mac.update(cipher)
        self.ciphertext_len += len(cipher)
        return cipher

    def finalize(self) -> bytes:
        return self._tag()


class Decryptor(_AeadBase):
    def __init__(self, key: bytes, nonce: bytes, aad: bytes) -> None:
        super().__init__(key, nonce, aad)

    def update(self, ciphertext: bytes) -> bytes:
        self._mac.update(ciphertext)
        self.ciphertext_len += len(ciphertext)
        return _xor_bytes(ciphertext, self._stream(len(ciphertext)))

    def verify(self, tag: bytes) -> None:
        if len(tag) != TAG_LEN:
            raise MigrationCryptoError("迁移包 tag 长度非法")
        expected = self._tag()
        import hmac
        if not hmac.compare_digest(expected, tag):
            raise MigrationCryptoError("迁移包口令错误或文件已被篡改")


class _AesEncryptor:
    def __init__(self, key: bytes, nonce: bytes, aad: bytes) -> None:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        self.ciphertext_len = 0
        self._enc = Cipher(algorithms.AES(key), modes.GCM(nonce)).encryptor()
        self._enc.authenticate_additional_data(aad)

    def update(self, plaintext: bytes) -> bytes:
        result = self._enc.update(plaintext)
        self.ciphertext_len += len(plaintext)
        return result

    def finalize(self) -> bytes:
        return self._enc.finalize() + self._enc.tag


class _AesDecryptor:
    def __init__(self, key: bytes, nonce: bytes, aad: bytes, tag: bytes) -> None:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

        self.ciphertext_len = 0
        self._dec = Cipher(algorithms.AES(key), modes.GCM(nonce, tag)).decryptor()
        self._dec.authenticate_additional_data(aad)

    def update(self, ciphertext: bytes) -> bytes:
        result = self._dec.update(ciphertext)
        self.ciphertext_len += len(ciphertext)
        return result

    def verify(self) -> None:
        try:
            self._dec.finalize()
        except Exception as exc:  # noqa: BLE001 - cryptography.InvalidTag
            raise MigrationCryptoError("迁移包口令错误或文件已被篡改") from exc


def _derive_key(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    if not password:
        raise MigrationCryptoError("迁移口令不能为空")
    if n < 2 ** 12 or n > 2 ** 18 or n & (n - 1):
        raise MigrationCryptoError("迁移包 scrypt N 参数非法")
    if r not in (8,) or p not in (1,):
        raise MigrationCryptoError("迁移包 scrypt 参数非法")
    return hashlib.scrypt(password.encode("utf-8"), salt=salt, n=n, r=r, p=p, dklen=KEY_LEN,
                          maxmem=128 * 1024 * 1024)


def _header_bytes(header: dict[str, Any]) -> tuple[bytes, bytes]:
    raw = json.dumps(header, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    if len(raw) > HEADER_LIMIT:
        raise MigrationCryptoError("迁移包 header 过大")
    length = struct.pack(">I", len(raw))
    return raw, MAGIC + length + raw


def _read_header(source: BinaryIO) -> tuple[dict[str, Any], bytes]:
    head = source.read(len(MAGIC))
    if head != MAGIC:
        raise MigrationCryptoError("不是有效的 BBU 加密迁移包")
    length_raw = source.read(4)
    if len(length_raw) != 4:
        raise MigrationCryptoError("迁移包 header 不完整")
    length = struct.unpack(">I", length_raw)[0]
    if length <= 0 or length > HEADER_LIMIT:
        raise MigrationCryptoError("迁移包 header 长度非法")
    raw = source.read(length)
    if len(raw) != length:
        raise MigrationCryptoError("迁移包 header 不完整")
    try:
        header = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise MigrationCryptoError("迁移包 header 解析失败") from exc
    if header.get("format") != "bbu-migration" or header.get("version") != 1:
        raise MigrationCryptoError("不支持的迁移包格式版本")
    if header.get("cipher") not in (_PURE_CHACHA, _AES_GCM):
        raise MigrationCryptoError("不支持的迁移包加密算法")
    return header, MAGIC + length_raw + raw


def _new_header(plain_size: int, password: str) -> tuple[dict[str, Any], bytes, bytes]:
    salt = secrets.token_bytes(SALT_LEN)
    nonce = secrets.token_bytes(NONCE_LEN)
    # 迁移包必须在无 cryptography 的目标实例上也能解密，因此统一使用 ChaCha20-Poly1305。
    # AES-GCM 仅保留解密兼容：早期/手工生成的 AES 包仍可导入。
    cipher = _PURE_CHACHA
    header = {
        "format": "bbu-migration",
        "version": 1,
        "cipher": cipher,
        "kdf": {"name": "scrypt", "n": SCRYPT_N, "r": SCRYPT_R, "p": SCRYPT_P,
                "salt": base64.b64encode(salt).decode("ascii")},
        "nonce": base64.b64encode(nonce).decode("ascii"),
        "plain_size": int(plain_size),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    _, aad = _header_bytes(header)
    return header, _derive_key(password, salt, SCRYPT_N, SCRYPT_R, SCRYPT_P), aad


def encrypt_file(source: Path, destination: Path, password: str,
                 progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """流式加密文件；明文不写入目标目录以外的临时位置。返回包级元信息。"""
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise MigrationCryptoError("待加密文件不存在")
    total = source.stat().st_size
    header, key, aad = _new_header(total, password)
    nonce = base64.b64decode(header["nonce"])
    enc = _AesEncryptor(key, nonce, aad) if header["cipher"] == _AES_GCM else Encryptor(key, nonce, aad)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + ".part")
    try:
        with open(source, "rb") as src, open(temp, "wb") as out:
            out.write(aad)
            done = 0
            while True:
                chunk = src.read(CHUNK)
                if not chunk:
                    break
                out.write(enc.update(chunk))
                done += len(chunk)
                if progress:
                    progress(done, total)
            out.write(enc.finalize())
        os.chmod(temp, 0o600)
        os.replace(temp, destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    digest = file_sha256(destination)
    return {"size": destination.stat().st_size, "plain_size": total, "sha256": digest,
            "cipher": header["cipher"]}


def decrypt_file(source: Path, destination: Path, password: str,
                 progress: Callable[[int, int], None] | None = None) -> dict[str, Any]:
    """流式解密并校验 tag；失败时不留下明文目标文件。"""
    source = Path(source)
    destination = Path(destination)
    if not source.is_file():
        raise MigrationCryptoError("加密迁移包不存在")
    temp = destination.with_name(destination.name + ".part")
    temp.parent.mkdir(parents=True, exist_ok=True)
    try:
        with open(source, "rb") as src:
            header, aad = _read_header(src)
            salt = base64.b64decode(header["kdf"]["salt"])
            nonce = base64.b64decode(header["nonce"])
            key = _derive_key(password, salt, int(header["kdf"]["n"]),
                              int(header["kdf"]["r"]), int(header["kdf"]["p"]))
            plain_size = int(header.get("plain_size") or -1)
            if plain_size < 0:
                raise MigrationCryptoError("迁移包原始大小非法")
            data_start = len(aad)
            cipher = header["cipher"]
            expected_size = data_start + plain_size + TAG_LEN
            if source.stat().st_size != expected_size:
                raise MigrationCryptoError("迁移包长度与 header 不一致")
            if cipher == _AES_GCM:
                if not _aes_gcm_available():
                    raise MigrationCryptoError("当前运行环境缺少 AES-GCM 支持，无法解密该迁移包")
                src.seek(expected_size - TAG_LEN)
                tag = src.read(TAG_LEN)
                src.seek(data_start)
                dec = _AesDecryptor(key, nonce, aad, tag)
                with open(temp, "wb") as out:
                    remaining = plain_size
                    done = 0
                    while remaining:
                        chunk = src.read(min(CHUNK, remaining))
                        if not chunk:
                            raise MigrationCryptoError("迁移包密文不完整")
                        out.write(dec.update(chunk))
                        remaining -= len(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, plain_size)
                    dec.verify()
            else:
                with open(temp, "wb") as out:
                    dec = Decryptor(key, nonce, aad)
                    remaining = plain_size
                    done = 0
                    while remaining:
                        chunk = src.read(min(CHUNK, remaining))
                        if not chunk:
                            raise MigrationCryptoError("迁移包密文不完整")
                        out.write(dec.update(chunk))
                        remaining -= len(chunk)
                        done += len(chunk)
                        if progress:
                            progress(done, plain_size)
                    tag = src.read(TAG_LEN)
                    if len(tag) != TAG_LEN:
                        raise MigrationCryptoError("迁移包缺少认证 tag")
                    if src.read(1):
                        raise MigrationCryptoError("迁移包包含多余数据")
                    dec.verify(tag)
        os.chmod(temp, 0o600)
        os.replace(temp, destination)
    except Exception:
        temp.unlink(missing_ok=True)
        raise
    return {"size": destination.stat().st_size, "plain_size": plain_size,
            "header": header, "data_start": len(aad)}


def encrypt_bytes(data: bytes, password: str) -> bytes:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.bin"
        dst = Path(tmp) / "dst.bbumig"
        src.write_bytes(data)
        encrypt_file(src, dst, password)
        return dst.read_bytes()


def decrypt_bytes(data: bytes, password: str) -> bytes:
    import tempfile
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "src.bbumig"
        dst = Path(tmp) / "dst.bin"
        src.write_bytes(data)
        decrypt_file(src, dst, password)
        return dst.read_bytes()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()
