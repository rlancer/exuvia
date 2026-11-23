"""Append DuckDB's loadable-extension metadata footer. Usage: append_footer.py LIB OUT C_API_VERSION

DuckDB reads the last 512 bytes of an extension file on LOAD: a Wasm custom-section header
(so Wasm builds stay valid), eight 32-byte fields stored last-to-first, then 256 bytes for a
signature. This build is unsigned, so the serving instance needs allow_unsigned_extensions.
Same layout as extension-ci-tools/scripts/append_extension_metadata.py.
"""
import shutil
import sys

import duckdb

lib, out, c_api_version = sys.argv[1:4]
platform = duckdb.connect().execute("SELECT platform FROM pragma_platform()").fetchone()[0]


def field(value):
    raw = value.encode("ascii")
    assert len(raw) <= 32, value
    return raw + b"\x00" * (32 - len(raw))


# custom section id 0, LEB128 length 531, name length 16, name, LEB128 payload length 512
header = bytes([0, 0x93, 0x04, 16]) + b"duckdb_signature" + bytes([0x80, 0x04])
fields = [
    "", "", "",             # FIELD8..6, unused
    "C_STRUCT",             # FIELD5 abi type: the stable C struct API
    "0.1.0",                # FIELD4 extension version
    c_api_version,          # FIELD3 for C_STRUCT: the minimum C API version
    platform,               # FIELD2
    "4",                    # FIELD1 magic value
]
shutil.copyfile(lib, out)
with open(out, "ab") as f:
    f.write(header + b"".join(field(v) for v in fields) + b"\x00" * 256)
print(f"{out}: platform {platform}, C API {c_api_version}")
