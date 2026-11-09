"""Reproduce fixed, previously validated native-library compatibility changes.

Recipes are protected by the app manifest. Both input and output are hash-pinned;
only staged copies change. No compiler, Homebrew, linker tool or system mutation.
"""
import base64
import hashlib


def compatible_bytes(data, recipe, expected):
    if (len(data) != recipe['source_bytes'] or hashlib.sha256(data).hexdigest() != recipe['source_sha256']
            or recipe['target_bytes'] != expected['bytes'] or recipe['target_sha256'] != expected['sha256']):
        raise ValueError('原生兼容组件的输入或目标不匹配')
    length = expected['bytes']
    if length > 128 * 1024 * 1024 or len(recipe['blocks']) > 10_000:
        raise ValueError('原生兼容组件超过大小限制')
    result = bytearray(data[:length])
    result.extend(b'\0' * (length - len(result)))
    end = 0
    for block in recipe['blocks']:
        offset = block['offset']
        replacement = base64.b64decode(block['data'], validate=True)
        if offset < end or not replacement or offset + len(replacement) > length:
            raise ValueError('原生兼容组件的范围无效')
        result[offset:offset + len(replacement)] = replacement
        end = offset + len(replacement)
    if hashlib.sha256(result).hexdigest() != expected['sha256']:
        raise ValueError('原生兼容组件校验失败')
    return result
