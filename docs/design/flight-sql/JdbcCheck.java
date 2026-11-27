// Drive the Flight SQL prototype (server.py) with the Arrow Flight SQL JDBC
// driver, making the calls DBeaver makes when it connects, browses a schema
// and runs a query. jdbc_check.py starts the server, makes the data, and runs
// this with the driver jar:
//
//     java --add-opens=java.base/java.nio=ALL-UNNAMED -cp flight-sql-jdbc-driver-<v>.jar \
//         JdbcCheck.java 'jdbc:arrow-flight-sql://127.0.0.1:7461/?useEncryption=false&token=exd_...'
//
// Every call is tried and reported; it doesn't stop at the first failure.
// A line starting "FAIL" is a call that threw; "note" lines are answers worth
// reading but not wrong. It exits 1 if any required call failed.

import java.sql.*;
import java.util.*;

public class JdbcCheck {
    static int failures = 0;
    static List<String> optional = new ArrayList<>();

    interface Call { Object run() throws Exception; }

    // A call DBeaver needs to work: a failure here breaks connecting, browsing or querying.
    static Object need(String what, Call c) {
        try {
            Object out = c.run();
            System.out.println("  ok   " + what + (out == null ? "" : ": " + show(out)));
            return out;
        } catch (Throwable e) {
            failures++;
            System.out.println("  FAIL " + what + ": " + first(e));
            return null;
        }
    }

    // A call DBeaver makes but copes without (it catches the error and shows less).
    static Object may(String what, Call c) {
        try {
            Object out = c.run();
            System.out.println("  ok   " + what + (out == null ? "" : ": " + show(out)));
            return out;
        } catch (Throwable e) {
            optional.add(what);
            System.out.println("  --   " + what + " (optional): " + first(e));
            return null;
        }
    }

    static String first(Throwable e) {
        String m = e.getClass().getSimpleName() + ": " + e.getMessage();
        m = m.split("\n")[0];
        return m.length() > 220 ? m.substring(0, 220) + "..." : m;
    }

    static String show(Object o) {
        String s = String.valueOf(o);
        return s.length() > 600 ? s.substring(0, 600) + "..." : s;
    }

    static List<List<Object>> rows(ResultSet rs, String... cols) throws SQLException {
        List<List<Object>> out = new ArrayList<>();
        try (rs) {
            while (rs.next()) {
                List<Object> row = new ArrayList<>();
                for (String c : cols) row.add(rs.getObject(c));
                out.add(row);
            }
        }
        return out;
    }

    static List<String> columns(ResultSet rs) throws SQLException {
        ResultSetMetaData md = rs.getMetaData();
        List<String> out = new ArrayList<>();
        for (int i = 1; i <= md.getColumnCount(); i++) out.add(md.getColumnLabel(i));
        return out;
    }

    public static void main(String[] args) throws Exception {
        String url = args[0];
        String narrowUrl = args.length > 1 ? args[1] : null;

        System.out.println("1. connecting, as DBeaver does");
        Connection conn = (Connection) need("DriverManager.getConnection", () -> DriverManager.getConnection(url));
        if (conn == null) System.exit(1);
        DatabaseMetaData md = (DatabaseMetaData) need("getMetaData", conn::getMetaData);
        need("isValid", () -> conn.isValid(5));
        need("getDatabaseProductName", md::getDatabaseProductName);
        need("getDatabaseProductVersion", md::getDatabaseProductVersion);
        need("getDriverName / version", () -> md.getDriverName() + " " + md.getDriverVersion());
        need("getIdentifierQuoteString is a double quote", () -> {
            String q = md.getIdentifierQuoteString();
            if (!"\"".equals(q)) throw new SQLException("got '" + q + "'");
            return q;
        });
        need("getCatalogSeparator", md::getCatalogSeparator);
        need("getCatalogTerm / getSchemaTerm", () -> md.getCatalogTerm() + " / " + md.getSchemaTerm());
        need("isReadOnly", md::isReadOnly);
        need("supportsCatalogsInTableDefinitions", md::supportsCatalogsInTableDefinitions);
        need("supportsSchemasInTableDefinitions", md::supportsSchemasInTableDefinitions);
        need("supportsTransactions", md::supportsTransactions);
        need("supportsMixedCaseQuotedIdentifiers", md::supportsMixedCaseQuotedIdentifiers);
        need("storesLowerCaseIdentifiers", md::storesLowerCaseIdentifiers);
        need("getMaxColumnNameLength", md::getMaxColumnNameLength);
        need("getSQLKeywords", md::getSQLKeywords);
        need("getNumericFunctions", md::getNumericFunctions);
        need("getStringFunctions", md::getStringFunctions);
        need("getSearchStringEscape", md::getSearchStringEscape);
        need("getExtraNameCharacters", md::getExtraNameCharacters);
        need("getDefaultTransactionIsolation", md::getDefaultTransactionIsolation);
        need("Connection.getAutoCommit", conn::getAutoCommit);
        may("Connection.setAutoCommit(true)", () -> { conn.setAutoCommit(true); return null; });
        may("Connection.setReadOnly(true)", () -> { conn.setReadOnly(true); return null; });
        need("Connection.getTransactionIsolation", conn::getTransactionIsolation);
        need("Connection.getCatalog", conn::getCatalog);
        need("Connection.getSchema", conn::getSchema);
        need("Connection.getWarnings", conn::getWarnings);

        System.out.println("2. the navigator: catalogs, schemas, tables, columns");
        need("getCatalogs", () -> rows(md.getCatalogs(), "TABLE_CAT"));
        need("getSchemas()", () -> rows(md.getSchemas(), "TABLE_CATALOG", "TABLE_SCHEM"));
        need("getSchemas(\"sales\", null)", () -> rows(md.getSchemas("sales", null), "TABLE_CATALOG", "TABLE_SCHEM"));
        need("getTableTypes", () -> rows(md.getTableTypes(), "TABLE_TYPE"));
        need("getTables(\"sales\", \"main\", \"%\", null)",
             () -> rows(md.getTables("sales", "main", "%", null), "TABLE_CAT", "TABLE_SCHEM", "TABLE_NAME", "TABLE_TYPE"));
        need("getTables(null, null, null, {VIEW})",
             () -> rows(md.getTables(null, null, null, new String[] {"VIEW"}), "TABLE_CAT", "TABLE_NAME"));
        need("getTables(null, null, null, {TABLE})  (DBeaver's default filter)",
             () -> rows(md.getTables(null, null, null, new String[] {"TABLE"}), "TABLE_CAT", "TABLE_NAME"));
        need("getColumns(\"crm\", \"main\", \"accounts\", \"%\")",
             () -> rows(md.getColumns("crm", "main", "accounts", "%"), "COLUMN_NAME", "DATA_TYPE", "TYPE_NAME", "NULLABLE", "ORDINAL_POSITION"));
        need("getColumns(\"types\", \"main\", \"typed\", \"%\")",
             () -> rows(md.getColumns("types", "main", "typed", "%"), "COLUMN_NAME", "DATA_TYPE", "TYPE_NAME"));
        need("getPrimaryKeys", () -> rows(md.getPrimaryKeys("crm", "main", "accounts"), "COLUMN_NAME"));
        need("getImportedKeys", () -> rows(md.getImportedKeys("crm", "main", "accounts"), "FKCOLUMN_NAME"));
        need("getExportedKeys", () -> rows(md.getExportedKeys("crm", "main", "accounts"), "PKCOLUMN_NAME"));
        may("getIndexInfo", () -> rows(md.getIndexInfo("crm", "main", "accounts", false, true), "INDEX_NAME"));
        may("getTypeInfo", () -> rows(md.getTypeInfo(), "TYPE_NAME", "DATA_TYPE"));
        may("getProcedures", () -> rows(md.getProcedures("crm", "main", "%"), "PROCEDURE_NAME"));
        may("getFunctions", () -> rows(md.getFunctions("crm", "main", "%"), "FUNCTION_NAME"));
        may("Connection.setCatalog(\"crm\")  (DBeaver: choosing a default catalog)", () -> { conn.setCatalog("crm"); return conn.getCatalog(); });

        System.out.println("3. queries: the SQL editor and the data viewer");
        String join = "select a.name, sum(o.amount) as total from \"sales\".orders o join crm.accounts a using (account) group by a.name order by a.name";
        need("Statement.executeQuery (cross-project join)", () -> {
            try (Statement st = conn.createStatement()) {
                return rows(st.executeQuery(join), "name", "total");
            }
        });
        for (String sql : new String[] {
                "SELECT * FROM crm.main.accounts",  // what DBeaver's data viewer writes for a table
                "SELECT * FROM \"crm\".\"main\".\"accounts\"",
                "SELECT COUNT(*) FROM (SELECT * FROM crm.main.accounts) dbvrcnt",  // its row count
        }) {
            need("executeQuery " + sql, () -> {
                try (Statement st = conn.createStatement(); ResultSet rs = st.executeQuery(sql)) {
                    int n = 0;
                    while (rs.next()) n++;
                    return n + " rows";
                }
            });
        }
        need("Statement.execute + getResultSet", () -> {
            try (Statement st = conn.createStatement()) {
                if (!st.execute(join)) throw new SQLException("execute returned false for a SELECT");
                return rows(st.getResultSet(), "name", "total");
            }
        });
        need("PreparedStatement, no parameters", () -> {
            try (PreparedStatement ps = conn.prepareStatement("select * from crm.accounts order by account")) {
                ResultSetMetaData pmd = ps.getMetaData();  // DBeaver reads it before running
                String cols = pmd == null ? "no metadata" : pmd.getColumnCount() + " columns";
                return cols + " " + rows(ps.executeQuery(), "account", "name");
            }
        });
        need("setMaxRows(200) on 2,000 rows  (the data viewer's page)", () -> {
            try (Statement st = conn.createStatement()) {
                st.setMaxRows(200);
                st.setFetchSize(200);
                ResultSet rs = st.executeQuery("select * from big.many");
                int n = 0;
                while (rs.next()) n++;
                rs.close();
                if (n != 200) throw new SQLException("got " + n + " rows, expected 200");
                return n + " rows";
            }
        });
        need("all 2,000 rows", () -> {
            try (Statement st = conn.createStatement(); ResultSet rs = st.executeQuery("select * from big.many")) {
                int n = 0;
                while (rs.next()) n++;
                if (n != 2000) throw new SQLException("got " + n);
                return n + " rows";
            }
        });
        need("ResultSetMetaData of the typed table", () -> {
            try (Statement st = conn.createStatement(); ResultSet rs = st.executeQuery("select * from types.typed")) {
                ResultSetMetaData rmd = rs.getMetaData();
                List<String> out = new ArrayList<>();
                for (int i = 1; i <= rmd.getColumnCount(); i++)
                    out.add(rmd.getColumnLabel(i) + ":" + rmd.getColumnTypeName(i) + "/" + rmd.getColumnType(i)
                            + (rmd.getColumnClassName(i) == null ? "" : ""));
                return out;
            }
        });
        need("every value of the typed table, by getObject and getString", () -> {
            try (Statement st = conn.createStatement(); ResultSet rs = st.executeQuery("select * from types.typed order by i")) {
                ResultSetMetaData rmd = rs.getMetaData();
                List<String> out = new ArrayList<>();
                while (rs.next())
                    for (int i = 1; i <= rmd.getColumnCount(); i++) {
                        Object o = rs.getObject(i);
                        String s = rs.getString(i);
                        if (rs.getRow() == 1) out.add(rmd.getColumnLabel(i) + "=" + s + " (" + (o == null ? "null" : o.getClass().getSimpleName()) + ")");
                    }
                return out;
            }
        });
        need("typed getters: getDate, getTimestamp, getBigDecimal, getLong, getBoolean", () -> {
            try (Statement st = conn.createStatement(); ResultSet rs = st.executeQuery("select * from types.typed order by i")) {
                rs.next();
                return List.of(rs.getDate("d"), rs.getTimestamp("ts"), rs.getTimestamp("tstz"), rs.getBigDecimal("amount"),
                               rs.getLong("i"), rs.getBoolean("flag"));
            }
        });
        need("an empty result keeps its columns", () -> {
            try (Statement st = conn.createStatement(); ResultSet rs = st.executeQuery("select * from crm.accounts where false")) {
                if (rs.next()) throw new SQLException("expected no rows");
                return columns(rs);
            }
        });
        may("Statement.cancel (nothing running)", () -> {
            try (Statement st = conn.createStatement()) { st.cancel(); return null; }
        });
        need("a bad query fails with a readable message", () -> {
            try (Statement st = conn.createStatement()) {
                st.executeQuery("select nope from crm.accounts");
                throw new IllegalStateException("it ran");
            } catch (SQLException e) {
                return first(e);
            }
        });
        need("a write is refused", () -> {
            try (Statement st = conn.createStatement()) {
                st.execute("drop view crm.accounts");
                throw new IllegalStateException("it ran");
            } catch (SQLException e) {
                return first(e);
            }
        });
        need("the connection still answers after those", () -> {
            try (Statement st = conn.createStatement()) {
                return rows(st.executeQuery("select count(*) as n from crm.accounts"), "n");
            }
        });
        need("Connection.close", () -> { conn.close(); return null; });

        System.out.println("4. the key rules over JDBC");
        need("no token is refused", () -> {
            try (Connection c = DriverManager.getConnection(url.replaceAll("[&?]token=[^&]*", ""));
                 Statement st = c.createStatement()) {
                st.executeQuery("select 1");
                throw new IllegalStateException("it connected");
            } catch (SQLException e) {
                return first(e);
            }
        });
        if (narrowUrl != null) {
            need("a narrower key sees only its catalog", () -> {
                try (Connection c = DriverManager.getConnection(narrowUrl)) {
                    List<List<Object>> cats = rows(c.getMetaData().getCatalogs(), "TABLE_CAT");
                    if (!cats.equals(List.of(List.of("sales")))) throw new SQLException("saw " + cats);
                    return cats;
                }
            });
            need("... and can't query another project", () -> {
                try (Connection c = DriverManager.getConnection(narrowUrl); Statement st = c.createStatement()) {
                    st.executeQuery("select * from crm.accounts");
                    throw new IllegalStateException("it ran");
                } catch (SQLException e) {
                    return first(e);
                }
            });
        }

        System.out.println();
        System.out.println(failures == 0 ? "all required calls passed" : failures + " required calls failed");
        if (!optional.isEmpty()) System.out.println("optional calls that failed: " + String.join(", ", optional));
        System.exit(failures == 0 ? 0 : 1);
    }
}
