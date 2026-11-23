/* The read-only policy as a DuckDB extension on the stable C extension API.
 *
 * Same rules as ../policy.py. Built against duckdb_extension.h from DuckDB 2.0.0 (C API v1.5.6);
 * see build.sh. Functions, all in system.main:
 *
 *   exuvia_policy_configure(reader_token, reserved_csv) -> BOOLEAN   once, before quack_serve
 *   exuvia_policy_authn(connection_id, client_token, server_token) -> BOOLEAN   Quack's hook
 *   exuvia_policy_authz(connection_id, query) -> BOOLEAN                         Quack's hook
 *   exuvia_policy_role(connection_id) -> VARCHAR ('writer', 'reader' or NULL)
 *   exuvia_policy_forget(live_ids_csv, grace_seconds) -> BIGINT (roles dropped)
 *
 * Authentication binds the token's role to the server's connection id, as policy.py does: a
 * native function keeps state, which a SQL macro can't. Reader statements are parsed by
 * json_serialize_sql in a private in-memory instance (one connection, one prepared statement
 * behind a lock) and the JSON tree is checked here. Anything the checker doesn't recognise is
 * denied.
 */
#define DUCKDB_EXTENSION_NAME exuvia_policy
#include "duckdb_extension.h"

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

DUCKDB_EXTENSION_EXTERN

#ifdef _WIN32
#include <windows.h>
typedef SRWLOCK lock_t;
#define LOCK_INIT SRWLOCK_INIT
#define LOCK(l) AcquireSRWLockExclusive(l)
#define UNLOCK(l) ReleaseSRWLockExclusive(l)
static uint64_t now_ms(void) { return GetTickCount64(); }
#else
#include <pthread.h>
#include <time.h>
typedef pthread_mutex_t lock_t;
#define LOCK_INIT PTHREAD_MUTEX_INITIALIZER
#define LOCK(l) pthread_mutex_lock(l)
#define UNLOCK(l) pthread_mutex_unlock(l)
static uint64_t now_ms(void) {
	struct timespec ts;
	clock_gettime(CLOCK_MONOTONIC, &ts);
	return (uint64_t)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}
#endif

#define MAX_READER_SQL (64 * 1024)
#define MAX_DEPTH 256

/* ---------------- configuration and roles ---------------- */

enum { ROLE_NONE = 0, ROLE_WRITER = 1, ROLE_READER = 2 };

typedef struct {
	char *id;
	int role;
	uint64_t bound_ms;
} binding;

static lock_t roles_lock = LOCK_INIT;
static binding *roles;
static size_t n_roles, cap_roles;

static char *reader_token;   /* NULL until configured */
static char **reserved;      /* lower-case table and view names a CTE must not shadow */
static size_t n_reserved;

static int find_role(const char *id, size_t len) { /* caller holds roles_lock; returns index or -1 */
	for (size_t i = 0; i < n_roles; i++) {
		if (strlen(roles[i].id) == len && memcmp(roles[i].id, id, len) == 0) {
			return (int)i;
		}
	}
	return -1;
}

static int same_secret(const char *a, size_t na, const char *b, size_t nb) { /* constant time in a's length */
	unsigned char diff = (unsigned char)(na != nb);
	for (size_t i = 0; i < na; i++) {
		diff |= (unsigned char)(a[i] ^ (i < nb ? b[i] : 0));
	}
	return diff == 0;
}

/* ---------------- a small JSON reader (input: json_serialize_sql output) ---------------- */

enum { J_NULL, J_BOOL, J_NUM, J_STR, J_ARR, J_OBJ };

typedef struct jv jv;
struct jv {
	int type;
	const char *s; /* J_STR: raw contents between the quotes, escapes left in; J_BOOL: "t"/"f" */
	size_t n;      /* J_STR: length of s; J_ARR/J_OBJ: number of items */
	jv **items;    /* J_ARR, J_OBJ values */
	jv **keys;     /* J_OBJ keys (J_STR) */
};

typedef struct {
	char **blocks;
	size_t n_blocks, cap_blocks;
	char *cur;
	size_t left;
} arena;

static void *arena_alloc(arena *a, size_t size) {
	size = (size + 15) & ~(size_t)15;
	if (size > a->left) {
		size_t block = size > 65536 ? size : 65536;
		if (a->n_blocks == a->cap_blocks) {
			size_t cap = a->cap_blocks ? 2 * a->cap_blocks : 16;
			char **b = realloc(a->blocks, cap * sizeof(char *));
			if (!b) return NULL;
			a->blocks = b;
			a->cap_blocks = cap;
		}
		char *mem = malloc(block);
		if (!mem) return NULL;
		a->blocks[a->n_blocks++] = mem;
		a->cur = mem;
		a->left = block;
	}
	void *p = a->cur;
	a->cur += size;
	a->left -= size;
	return p;
}

static void arena_free(arena *a) {
	for (size_t i = 0; i < a->n_blocks; i++) free(a->blocks[i]);
	free(a->blocks);
}

typedef struct {
	const char *p, *end;
	arena *a;
	int depth;
} parser;

static void skip_ws(parser *ps) {
	while (ps->p < ps->end && (*ps->p == ' ' || *ps->p == '\n' || *ps->p == '\r' || *ps->p == '\t')) ps->p++;
}

static jv *parse_value(parser *ps);

static jv *parse_string(parser *ps) {
	if (ps->p >= ps->end || *ps->p != '"') return NULL;
	const char *start = ++ps->p;
	while (ps->p < ps->end && *ps->p != '"') {
		if (*ps->p == '\\') {
			ps->p++;
			if (ps->p >= ps->end) return NULL;
		}
		ps->p++;
	}
	if (ps->p >= ps->end) return NULL;
	jv *v = arena_alloc(ps->a, sizeof(jv));
	if (!v) return NULL;
	memset(v, 0, sizeof(*v));
	v->type = J_STR;
	v->s = start;
	v->n = (size_t)(ps->p - start);
	ps->p++;
	return v;
}

static jv *parse_container(parser *ps, int is_obj) {
	char close = is_obj ? '}' : ']';
	if (++ps->depth > MAX_DEPTH) return NULL;
	ps->p++;
	size_t cap = 8, n = 0;
	jv **items = malloc(cap * sizeof(jv *)), **keys = is_obj ? malloc(cap * sizeof(jv *)) : NULL;
	jv *result = NULL;
	if (!items || (is_obj && !keys)) goto done;
	skip_ws(ps);
	if (ps->p < ps->end && *ps->p == close) {
		ps->p++;
	} else {
		for (;;) {
			jv *k = NULL;
			if (is_obj) {
				skip_ws(ps);
				if (!(k = parse_string(ps))) goto done;
				skip_ws(ps);
				if (ps->p >= ps->end || *ps->p != ':') goto done;
				ps->p++;
			}
			jv *v = parse_value(ps);
			if (!v) goto done;
			if (n == cap) {
				cap *= 2;
				jv **ni = realloc(items, cap * sizeof(jv *));
				if (!ni) goto done;
				items = ni;
				if (is_obj) {
					jv **nk = realloc(keys, cap * sizeof(jv *));
					if (!nk) goto done;
					keys = nk;
				}
			}
			items[n] = v;
			if (is_obj) keys[n] = k;
			n++;
			skip_ws(ps);
			if (ps->p < ps->end && *ps->p == ',') {
				ps->p++;
				continue;
			}
			if (ps->p < ps->end && *ps->p == close) {
				ps->p++;
				break;
			}
			goto done;
		}
	}
	result = arena_alloc(ps->a, sizeof(jv));
	if (!result) goto done;
	memset(result, 0, sizeof(*result));
	result->type = is_obj ? J_OBJ : J_ARR;
	result->n = n;
	/* copy the item arrays into the arena so the whole tree is freed at once */
	result->items = n ? arena_alloc(ps->a, n * sizeof(jv *)) : NULL;
	if (n && !result->items) {
		result = NULL;
		goto done;
	}
	if (n) memcpy(result->items, items, n * sizeof(jv *));
	if (is_obj) {
		result->keys = n ? arena_alloc(ps->a, n * sizeof(jv *)) : NULL;
		if (n && !result->keys) {
			result = NULL;
			goto done;
		}
		if (n) memcpy(result->keys, keys, n * sizeof(jv *));
	}
done:
	free(items);
	free(keys);
	ps->depth--;
	return result;
}

static jv *parse_value(parser *ps) {
	skip_ws(ps);
	if (ps->p >= ps->end) return NULL;
	char c = *ps->p;
	if (c == '{') return parse_container(ps, 1);
	if (c == '[') return parse_container(ps, 0);
	if (c == '"') return parse_string(ps);
	jv *v = arena_alloc(ps->a, sizeof(jv));
	if (!v) return NULL;
	memset(v, 0, sizeof(*v));
	size_t left = (size_t)(ps->end - ps->p);
	if (left >= 4 && memcmp(ps->p, "null", 4) == 0) {
		v->type = J_NULL;
		ps->p += 4;
	} else if (left >= 4 && memcmp(ps->p, "true", 4) == 0) {
		v->type = J_BOOL;
		v->s = "t";
		ps->p += 4;
	} else if (left >= 5 && memcmp(ps->p, "false", 5) == 0) {
		v->type = J_BOOL;
		v->s = "f";
		ps->p += 5;
	} else if (c == '-' || (c >= '0' && c <= '9')) {
		v->type = J_NUM;
		while (ps->p < ps->end && strchr("+-.eE0123456789", *ps->p)) ps->p++;
	} else {
		return NULL;
	}
	return v;
}

static jv *get(jv *obj, const char *key) {
	if (!obj || obj->type != J_OBJ) return NULL;
	size_t kn = strlen(key);
	for (size_t i = 0; i < obj->n; i++) {
		if (obj->keys[i]->n == kn && memcmp(obj->keys[i]->s, key, kn) == 0) return obj->items[i];
	}
	return NULL;
}

static int str_is(jv *v, const char *s) {
	return v && v->type == J_STR && v->n == strlen(s) && memcmp(v->s, s, v->n) == 0;
}

/* ASCII lower-case copy of a JSON string into buf (NUL-terminated); 0 if it doesn't fit or isn't a string */
static int lower_copy(jv *v, char *buf, size_t size) {
	if (!v || v->type != J_STR || v->n >= size) return 0;
	for (size_t i = 0; i < v->n; i++) {
		char c = v->s[i];
		buf[i] = (c >= 'A' && c <= 'Z') ? (char)(c - 'A' + 'a') : c;
	}
	buf[v->n] = 0;
	return 1;
}

static int in_list(const char *s, const char *const *list) {
	for (; *list; list++) {
		if (strcmp(s, *list) == 0) return 1;
	}
	return 0;
}

/* ---------------- the checker (mirrors check_tree in policy.py) ---------------- */

static const char *const QUERY_NODES[] = {"SELECT_NODE", "SET_OPERATION_NODE", "RECURSIVE_CTE_NODE", NULL};
static const char *const TABLE_REFS[] = {"BASE_TABLE", "EMPTY", "JOIN", "SUBQUERY", "TABLE_FUNCTION", NULL};
static const char *const EXPRESSION_CLASSES[] = {"BETWEEN", "CASE",     "CAST", "COLUMN_REF", "COMPARISON", "CONJUNCTION",
                                                  "CONSTANT", "FUNCTION", "OPERATOR", "STAR", "SUBQUERY", "TYPE", NULL};
static const char *const AUX_TYPES[] = {"ORDER_MODIFIER", "LIMIT_MODIFIER", "DISTINCT_MODIFIER",
                                         "ASCENDING",      "DESCENDING",     "ORDER_DEFAULT", NULL};
static const char *const SCALAR_FUNCTIONS[] = {"-",           "count_star", "current_database", "length", "list",
                                                "list_append", "list_value", "max", "min", "struct_pack", "sum",
                                                "array_agg",   NULL};
static const char *const TABLE_FUNCTIONS[] = {"duckdb_functions", "duckdb_schemas", "duckdb_tables", "duckdb_views",
                                               NULL};
static const char *const RESERVED_PREFIXES[] = {"ducklake_", "duckdb_", "pragma_", "sqlite_", "pg_", NULL};

#define NAME_MAX_LEN 256
#define MAX_CTES 256

typedef struct {
	char reason[160];
	char ctes[MAX_CTES][NAME_MAX_LEN];
	size_t n_ctes;
} check;

static int deny(check *c, const char *what, jv *name) {
	if (name && name->type == J_STR) {
		int n = name->n > 60 ? 60 : (int)name->n;
		snprintf(c->reason, sizeof(c->reason), "%s %.*s", what, n, name->s);
	} else {
		snprintf(c->reason, sizeof(c->reason), "%s", what);
	}
	return 0;
}

static int add_cte(check *c, jv *name) {
	if (c->n_ctes == MAX_CTES) return deny(c, "too many CTEs", NULL);
	if (!lower_copy(name, c->ctes[c->n_ctes], NAME_MAX_LEN)) return deny(c, "CTE name", name);
	c->n_ctes++;
	return 1;
}

static int collect_ctes(check *c, jv *v) {
	if (!v) return 1;
	if (v->type == J_OBJ) {
		jv *cm = get(v, "cte_map");
		if (cm) {
			jv *map = get(cm, "map");
			if (!map || map->type != J_ARR) return deny(c, "cte_map form", NULL);
			for (size_t i = 0; i < map->n; i++) {
				if (!add_cte(c, get(map->items[i], "key"))) return 0;
			}
		}
		if (str_is(get(v, "type"), "RECURSIVE_CTE_NODE") && !add_cte(c, get(v, "cte_name"))) return 0;
	}
	if (v->type == J_OBJ || v->type == J_ARR) {
		for (size_t i = 0; i < v->n; i++) {
			if (!collect_ctes(c, v->items[i])) return 0;
		}
	}
	return 1;
}

static int safe_cte_name(const char *s) {
	if (!(s[0] >= 'a' && s[0] <= 'z')) return 0;
	for (const char *p = s; *p; p++) {
		if (!((*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') || *p == '_')) return 0;
	}
	for (const char *const *pre = RESERVED_PREFIXES; *pre; pre++) {
		if (strncmp(s, *pre, strlen(*pre)) == 0) return 0;
	}
	for (size_t i = 0; i < n_reserved; i++) {
		if (strcmp(s, reserved[i]) == 0) return 0;
	}
	return 1;
}

static int is_metadata_table(const char *s) { /* ducklake_[a-z0-9_]+ */
	if (strncmp(s, "ducklake_", 9) != 0 || !s[9]) return 0;
	for (const char *p = s + 9; *p; p++) {
		if (!((*p >= 'a' && *p <= 'z') || (*p >= '0' && *p <= '9') || *p == '_')) return 0;
	}
	return 1;
}

static int check_function(check *c, jv *fn, const char *const *allowed, const char *what) {
	jv *name = get(fn, "function_name");
	char lname[NAME_MAX_LEN];
	if (!lower_copy(name, lname, sizeof(lname))) return deny(c, what, name);
	jv *cat = get(fn, "catalog"), *sch = get(fn, "schema");
	if ((cat && !str_is(cat, "")) || (sch && !str_is(sch, ""))) return deny(c, "qualified", name);
	jv *qn = get(fn, "qualified_name");
	if (qn) {
		jv *path = get(qn, "path");
		if (!path || path->type != J_ARR || path->n != 1 || path->items[0]->type != J_STR ||
		    path->items[0]->n != name->n || memcmp(path->items[0]->s, name->s, name->n) != 0) {
			return deny(c, "qualified", name);
		}
	}
	if (!in_list(lname, allowed)) return deny(c, what, name);
	return 1;
}

static int check_table(check *c, jv *ref) {
	jv *cat = get(ref, "catalog_name"), *sch = get(ref, "schema_name"), *tn = get(ref, "table_name");
	char name[NAME_MAX_LEN];
	if (!cat || !sch || cat->type != J_STR || sch->type != J_STR) return deny(c, "table form", NULL);
	if (!str_is(cat, "")) return deny(c, "catalog", cat);
	if (!lower_copy(tn, name, sizeof(name))) return deny(c, "table", tn);
	if (str_is(sch, "")) {
		for (size_t i = 0; i < c->n_ctes; i++) {
			if (strcmp(name, c->ctes[i]) == 0) return 1;
		}
	}
	if ((str_is(sch, "") || str_is(sch, "main")) && is_metadata_table(name)) return 1;
	if (str_is(sch, "information_schema") && strcmp(name, "tables") == 0) return 1;
	return deny(c, "table", tn);
}

static int walk(check *c, jv *v, int depth) {
	if (!v || depth > MAX_DEPTH) return v ? deny(c, "too deep", NULL) : 1;
	if (v->type == J_ARR) {
		for (size_t i = 0; i < v->n; i++) {
			if (!walk(c, v->items[i], depth + 1)) return 0;
		}
		return 1;
	}
	if (v->type != J_OBJ) return 1;
	jv *cls = get(v, "class"), *type = get(v, "type");
	char buf[64];
	if (cls) {
		if (cls->type != J_STR || cls->n >= sizeof(buf)) return deny(c, "expression", NULL);
		memcpy(buf, cls->s, cls->n);
		buf[cls->n] = 0;
		if (!in_list(buf, EXPRESSION_CLASSES)) return deny(c, "expression", cls);
		if (strcmp(buf, "FUNCTION") == 0 && !check_function(c, v, SCALAR_FUNCTIONS, "function")) return 0;
	} else if (type && type->type == J_STR) {
		if (type->n >= sizeof(buf)) return deny(c, "node type", NULL);
		memcpy(buf, type->s, type->n);
		buf[type->n] = 0;
		size_t n = type->n;
		if (n >= 5 && strcmp(buf + n - 5, "_NODE") == 0) {
			if (!in_list(buf, QUERY_NODES)) return deny(c, "query node", type);
		} else if (in_list(buf, TABLE_REFS)) {
			if (strcmp(buf, "BASE_TABLE") == 0) {
				if (!check_table(c, v)) return 0;
			} else if (strcmp(buf, "TABLE_FUNCTION") == 0) {
				jv *fn = get(v, "function");
				if (!str_is(get(fn, "class"), "FUNCTION")) return deny(c, "table function form", NULL);
				if (!check_function(c, fn, TABLE_FUNCTIONS, "table function")) return 0;
				jv *args = get(fn, "arguments");
				if (!args || args->type != J_ARR || args->n != 0) return deny(c, "table function arguments", NULL);
				for (size_t i = 0; i < v->n; i++) { /* the call is checked: walk the rest of the ref */
					if (!str_is(v->keys[i], "function") && !walk(c, v->items[i], depth + 1)) return 0;
				}
				return 1;
			}
		} else if (!in_list(buf, AUX_TYPES)) {
			return deny(c, "node type", type);
		}
	}
	for (size_t i = 0; i < v->n; i++) {
		if (!walk(c, v->items[i], depth + 1)) return 0;
	}
	return 1;
}

/* Returns 1 if a reader may run the tree, else 0 with c->reason set. */
static int check_tree(check *c, jv *tree) {
	jv *err = get(tree, "error");
	if (!err || err->type != J_BOOL || err->s[0] != 'f') return deny(c, "not a SELECT", NULL);
	jv *stmts = get(tree, "statements");
	if (!stmts || stmts->type != J_ARR || stmts->n != 1) return deny(c, "one statement per request", NULL);
	if (!collect_ctes(c, stmts)) return 0;
	for (size_t i = 0; i < c->n_ctes; i++) {
		if (!safe_cte_name(c->ctes[i])) {
			snprintf(c->reason, sizeof(c->reason), "CTE name '%.60s'", c->ctes[i]);
			return 0;
		}
	}
	jv *node = get(stmts->items[0], "node");
	if (!node) return deny(c, "statement form", NULL);
	return walk(c, node, 0);
}

/* ---------------- parsing through json_serialize_sql ---------------- */

static lock_t parse_lock = LOCK_INIT;
static duckdb_database parse_db;
static duckdb_connection parse_con;
static duckdb_prepared_statement parse_stmt;

/* Returns a malloc'd JSON string (caller frees with duckdb_free), or NULL. */
static char *serialize(const char *sql, size_t len) {
	char *copy = malloc(len + 1), *json = NULL;
	if (!copy) return NULL;
	memcpy(copy, sql, len);
	copy[len] = 0;
	LOCK(&parse_lock);
	duckdb_result res;
	if (parse_stmt && duckdb_bind_varchar(parse_stmt, 1, copy) == DuckDBSuccess &&
	    duckdb_execute_prepared(parse_stmt, &res) == DuckDBSuccess) {
		json = duckdb_value_varchar(&res, 0, 0);
		duckdb_destroy_result(&res);
	}
	UNLOCK(&parse_lock);
	free(copy);
	return json;
}

/* NULL if a reader may run `sql`, else writes the reason into `reason`. */
static int reader_allowed(const char *sql, size_t len, char *reason, size_t reason_size) {
	if (len > MAX_READER_SQL) {
		snprintf(reason, reason_size, "statement too long");
		return 0;
	}
	char *json = serialize(sql, len);
	if (!json) {
		snprintf(reason, reason_size, "checker error: serialize");
		return 0;
	}
	arena a = {0};
	parser ps = {json, json + strlen(json), &a, 0};
	jv *tree = parse_value(&ps);
	int ok = 0;
	check *c = calloc(1, sizeof(check));
	if (!tree || !c) {
		snprintf(reason, reason_size, "checker error: json");
	} else {
		ok = check_tree(c, tree);
		if (!ok) snprintf(reason, reason_size, "%s", c->reason);
	}
	free(c);
	arena_free(&a);
	duckdb_free(json);
	return ok;
}

/* ---------------- scalar functions ---------------- */

static const char *arg(duckdb_data_chunk in, idx_t col, idx_t row, uint32_t *len) {
	duckdb_vector v = duckdb_data_chunk_get_vector(in, col);
	duckdb_string_t *data = (duckdb_string_t *)duckdb_vector_get_data(v);
	*len = duckdb_string_t_length(data[row]);
	return duckdb_string_t_data(&data[row]);
}

static void fn_configure(duckdb_function_info info, duckdb_data_chunk in, duckdb_vector out) {
	bool *res = (bool *)duckdb_vector_get_data(out);
	idx_t n = duckdb_data_chunk_get_size(in);
	for (idx_t row = 0; row < n; row++) {
		uint32_t tl, rl;
		const char *tok = arg(in, 0, row, &tl), *csv = arg(in, 1, row, &rl);
		LOCK(&roles_lock);
		if (reader_token) {
			UNLOCK(&roles_lock);
			duckdb_scalar_function_set_error(info, "exuvia_policy is already configured");
			return;
		}
		reader_token = malloc(tl + 1);
		memcpy(reader_token, tok, tl);
		reader_token[tl] = 0;
		/* reserved names: comma-separated, lower-cased by the caller */
		size_t cap = 16;
		reserved = malloc(cap * sizeof(char *));
		const char *p = csv, *end = csv + rl;
		while (p < end) {
			const char *comma = memchr(p, ',', (size_t)(end - p));
			const char *stop = comma ? comma : end;
			if (n_reserved == cap) reserved = realloc(reserved, (cap *= 2) * sizeof(char *));
			reserved[n_reserved] = malloc((size_t)(stop - p) + 1);
			memcpy(reserved[n_reserved], p, (size_t)(stop - p));
			reserved[n_reserved][stop - p] = 0;
			n_reserved++;
			p = stop + 1;
		}
		UNLOCK(&roles_lock);
		res[row] = true;
	}
}

static void fn_authn(duckdb_function_info info, duckdb_data_chunk in, duckdb_vector out) {
	bool *res = (bool *)duckdb_vector_get_data(out);
	idx_t n = duckdb_data_chunk_get_size(in);
	for (idx_t row = 0; row < n; row++) {
		uint32_t il, pl, sl;
		const char *id = arg(in, 0, row, &il), *presented = arg(in, 1, row, &pl), *server = arg(in, 2, row, &sl);
		int role = ROLE_NONE;
		if (same_secret(presented, pl, server, sl)) {
			role = ROLE_WRITER;
		} else if (reader_token && same_secret(presented, pl, reader_token, strlen(reader_token))) {
			role = ROLE_READER;
		}
		res[row] = false;
		if (role == ROLE_NONE) continue;
		LOCK(&roles_lock);
		if (find_role(id, il) < 0) { /* server ids are random: a repeat is a bug, deny it */
			if (n_roles == cap_roles) {
				cap_roles = cap_roles ? 2 * cap_roles : 64;
				roles = realloc(roles, cap_roles * sizeof(binding));
			}
			roles[n_roles].id = malloc(il + 1);
			memcpy(roles[n_roles].id, id, il);
			roles[n_roles].id[il] = 0;
			roles[n_roles].role = role;
			roles[n_roles].bound_ms = now_ms();
			n_roles++;
			res[row] = true;
		}
		UNLOCK(&roles_lock);
	}
}

static void fn_authz(duckdb_function_info info, duckdb_data_chunk in, duckdb_vector out) {
	bool *res = (bool *)duckdb_vector_get_data(out);
	idx_t n = duckdb_data_chunk_get_size(in);
	for (idx_t row = 0; row < n; row++) {
		uint32_t il, ql;
		const char *id = arg(in, 0, row, &il), *sql = arg(in, 1, row, &ql);
		LOCK(&roles_lock);
		int i = find_role(id, il), role = i < 0 ? ROLE_NONE : roles[i].role;
		UNLOCK(&roles_lock);
		if (role == ROLE_WRITER) {
			res[row] = true;
			continue;
		}
		if (role != ROLE_READER) {
			duckdb_scalar_function_set_error(info, "unknown connection");
			return;
		}
		char reason[200], msg[240];
		if (!reader_allowed(sql, ql, reason, sizeof(reason))) {
			snprintf(msg, sizeof(msg), "read-only token: %s", reason);
			duckdb_scalar_function_set_error(info, msg);
			return;
		}
		res[row] = true;
	}
}

static void fn_role(duckdb_function_info info, duckdb_data_chunk in, duckdb_vector out) {
	idx_t n = duckdb_data_chunk_get_size(in);
	for (idx_t row = 0; row < n; row++) {
		uint32_t il;
		const char *id = arg(in, 0, row, &il);
		LOCK(&roles_lock);
		int i = find_role(id, il), role = i < 0 ? ROLE_NONE : roles[i].role;
		UNLOCK(&roles_lock);
		if (role == ROLE_NONE) {
			duckdb_vector_ensure_validity_writable(out);
			duckdb_validity_set_row_invalid(duckdb_vector_get_validity(out), row);
		} else {
			duckdb_vector_assign_string_element(out, row, role == ROLE_WRITER ? "writer" : "reader");
		}
	}
}

/* Drop roles of connections not in live_csv that were bound more than grace_seconds ago. */
static void fn_forget(duckdb_function_info info, duckdb_data_chunk in, duckdb_vector out) {
	int64_t *res = (int64_t *)duckdb_vector_get_data(out);
	double *grace = (double *)duckdb_vector_get_data(duckdb_data_chunk_get_vector(in, 1));
	idx_t n = duckdb_data_chunk_get_size(in);
	for (idx_t row = 0; row < n; row++) {
		uint32_t ll;
		const char *live = arg(in, 0, row, &ll);
		uint64_t cutoff = now_ms() - (uint64_t)(grace[row] * 1000);
		int64_t dropped = 0;
		LOCK(&roles_lock);
		for (size_t i = 0; i < n_roles;) {
			size_t idl = strlen(roles[i].id);
			int alive = 0;
			for (const char *p = live, *end = live + ll; p < end;) {
				const char *comma = memchr(p, ',', (size_t)(end - p));
				const char *stop = comma ? comma : end;
				if ((size_t)(stop - p) == idl && memcmp(p, roles[i].id, idl) == 0) alive = 1;
				p = stop + 1;
			}
			if (!alive && roles[i].bound_ms < cutoff) {
				free(roles[i].id);
				roles[i] = roles[--n_roles];
				dropped++;
			} else {
				i++;
			}
		}
		UNLOCK(&roles_lock);
		res[row] = dropped;
	}
}

static void add(duckdb_connection con, const char *name, duckdb_scalar_function_t fn, duckdb_type ret, int nargs,
                const duckdb_type *args) {
	duckdb_scalar_function f = duckdb_create_scalar_function();
	duckdb_scalar_function_set_name(f, name);
	for (int i = 0; i < nargs; i++) {
		duckdb_logical_type t = duckdb_create_logical_type(args[i]);
		duckdb_scalar_function_add_parameter(f, t);
		duckdb_destroy_logical_type(&t);
	}
	duckdb_logical_type r = duckdb_create_logical_type(ret);
	duckdb_scalar_function_set_return_type(f, r);
	duckdb_destroy_logical_type(&r);
	duckdb_scalar_function_set_function(f, fn);
	duckdb_scalar_function_set_volatile(f); /* has side effects: never constant-folded or cached */
	duckdb_register_scalar_function(con, f);
	duckdb_destroy_scalar_function(&f);
}

DUCKDB_EXTENSION_ENTRYPOINT(duckdb_connection connection, duckdb_extension_info info,
                            struct duckdb_extension_access *access) {
	/* A private in-memory instance of the same DuckDB build does the parsing. */
	if (duckdb_open(NULL, &parse_db) != DuckDBSuccess || duckdb_connect(parse_db, &parse_con) != DuckDBSuccess ||
	    duckdb_prepare(parse_con, "SELECT json_serialize_sql($1)", &parse_stmt) != DuckDBSuccess) {
		access->set_error(info, "exuvia_policy: could not open its parser instance");
		return false;
	}
	const duckdb_type V = DUCKDB_TYPE_VARCHAR;
	const duckdb_type two[] = {V, V}, three[] = {V, V, V}, one[] = {V}, forget[] = {V, DUCKDB_TYPE_DOUBLE};
	add(connection, "exuvia_policy_configure", fn_configure, DUCKDB_TYPE_BOOLEAN, 2, two);
	add(connection, "exuvia_policy_authn", fn_authn, DUCKDB_TYPE_BOOLEAN, 3, three);
	add(connection, "exuvia_policy_authz", fn_authz, DUCKDB_TYPE_BOOLEAN, 2, two);
	add(connection, "exuvia_policy_role", fn_role, DUCKDB_TYPE_VARCHAR, 1, one);
	add(connection, "exuvia_policy_forget", fn_forget, DUCKDB_TYPE_BIGINT, 2, forget);
	return true;
}
