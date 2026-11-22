"""Summarize what the logged reader/writer statements touch. Usage: analyze.py LOG.jsonl"""
import collections
import json
import sys

import duckdb

c = duckdb.connect()
rows = [json.loads(line) for line in open(sys.argv[1], encoding="utf-8")]


def walk(node, acc):
    if isinstance(node, dict):
        if "table_name" in node and node.get("type") == "BASE_TABLE":
            acc["tables"].add((node.get("catalog_name"), node.get("schema_name"), node["table_name"]))
        if node.get("type") == "TABLE_FUNCTION":
            fn = node["function"]
            acc["table_functions"].add((fn.get("catalog"), fn.get("schema"), fn.get("function_name")))
        if "function_name" in node:
            acc["functions"].add((node.get("catalog"), node.get("schema"), node["function_name"]))
        if "class" in node:
            acc["expr_classes"].add(node["class"])
        if str(node.get("type", "")).endswith("_NODE"):
            acc["query_nodes"].add(node["type"])
        if "sample" in node and "type" in node and "alias" in node:
            acc["table_refs"].add(node["type"])
        if "type" in node and "class" not in node and isinstance(node["type"], str):
            acc["aux_types"].add(node["type"])
        if node.get("class") == "OPERATOR" or node.get("class") in ("COMPARISON", "CONJUNCTION"):
            acc["operator_types"].add(node["type"])
        if "cte_map" in node:
            for e in node["cte_map"]["map"]:
                acc["ctes"].add(e["key"])
        for k, v in node.items():
            acc["keys"].add(k)
            walk(v, acc)
    elif isinstance(node, list):
        for v in node:
            walk(v, acc)


by_role = collections.defaultdict(lambda: collections.defaultdict(set))
nonselect = collections.Counter()
for r in rows:
    j = json.loads(c.execute("SELECT json_serialize_sql(?)", [r["sql"]]).fetchone()[0])
    acc = by_role[r["role"]]
    if j["error"]:
        nonselect[(r["role"], r["sql"].strip()[:90].replace("\n", " "))] += 1
        continue
    acc["n_statements_per_request"].add(len(j["statements"]))
    walk(j["statements"], acc)

for role, acc in by_role.items():
    print(f"===== {role}")
    for k in ("n_statements_per_request", "query_nodes", "table_refs", "aux_types", "expr_classes", "operator_types", "tables", "table_functions", "ctes"):
        print(f"{k}: {sorted(acc[k], key=str)}")
    fns = sorted({f for f in acc["functions"]} - {f for f in acc["table_functions"]}, key=str)
    print(f"scalar/aggregate functions: {fns}")
print("===== non-SELECT requests (role, sql prefix): count")
for k, v in sorted(nonselect.items(), key=str):
    print(v, k)
