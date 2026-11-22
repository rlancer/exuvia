"""Shared helpers: load the PyPI extension wheels by file path, no autoinstall."""
import glob
import importlib
import os

import duckdb

WRITER_TOKEN = "writer-token-0123456789"
READER_TOKEN = "reader-token-0123456789"


def ext_path(name):
    m = importlib.import_module(f"duckdb_extension_{name}")
    return glob.glob(os.path.join(os.path.dirname(m.__file__), "extensions", "*", f"{name}.duckdb_extension"))[0]


def connect(path=":memory:", exts=("httpfs", "ducklake", "quack")):
    c = duckdb.connect(path)
    c.execute("SET autoinstall_known_extensions=false; SET autoload_known_extensions=false")
    for n in exts:
        c.execute(f"LOAD '{ext_path(n)}'")
    return c


def lake_client(port, token, data_path, read_only=False, alias="lake"):
    c = connect()
    c.execute(f"CREATE SECRET (TYPE quack, TOKEN '{token}')")
    opts = [f"DATA_PATH '{data_path}'"]
    if read_only:
        opts.append("READ_ONLY")
    c.execute(f"ATTACH 'ducklake:quack:quack://127.0.0.1:{port}' AS {alias} ({', '.join(opts)})")
    return c
