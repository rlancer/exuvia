"""Serve a DuckLake catalog over Quack and log every statement with the role of its token."""
import json
import sys
import threading
import time

from common import READER_TOKEN, WRITER_TOKEN, connect

catalog, port, log_path = sys.argv[1], int(sys.argv[2]), sys.argv[3]
roles = {}
lock = threading.Lock()
log = open(log_path, "a", encoding="utf-8")


def authn(conn_id, presented, server_token):
    role = {WRITER_TOKEN: "writer", READER_TOKEN: "reader"}.get(presented)
    if role:
        roles[conn_id] = role
    return role is not None


def authz(conn_id, sql):
    with lock:
        log.write(json.dumps({"role": roles.get(conn_id), "sql": sql}) + "\n")
        log.flush()
    return True


s = connect(catalog)
s.create_function("x_authn", authn, ["VARCHAR", "VARCHAR", "VARCHAR"], "BOOLEAN")
s.create_function("x_authz", authz, ["VARCHAR", "VARCHAR"], "BOOLEAN")
s.execute("SET GLOBAL quack_authentication_function = 'x_authn'")
s.execute("SET GLOBAL quack_authorization_function = 'x_authz'")
s.execute(f"SELECT * FROM quack_serve('quack://127.0.0.1:{port}', token := '{WRITER_TOKEN}')").fetchall()
print("serving", flush=True)
while True:
    time.sleep(1)
