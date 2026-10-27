-- Synthetic fixture for SCOUT tests. Ground truth lives in tests/db/expected.py;
-- keep the two in sync. &1 = password for SCOUT_READER.
SET DEFINE ON
SET VERIFY OFF

-- Schema owners cannot log in; they only hold data.
CREATE USER scout_hr     NO AUTHENTICATION QUOTA UNLIMITED ON users;
CREATE USER scout_docs   NO AUTHENTICATION QUOTA UNLIMITED ON users;
-- scout_docs owns a CONTEXT (Oracle Text) index, which creates DR$ tables in its schema
GRANT CREATE TABLE, CTXAPP TO scout_docs;
CREATE USER scout_odd    NO AUTHENTICATION QUOTA UNLIMITED ON users;
CREATE USER scout_hidden NO AUTHENTICATION QUOTA UNLIMITED ON users;
CREATE USER scout_broken NO AUTHENTICATION QUOTA UNLIMITED ON users;
CREATE USER scout_virt   NO AUTHENTICATION QUOTA UNLIMITED ON users;

-- Low-privilege searcher: sees hr/docs/odd via ALL_*, never scout_hidden.
CREATE USER scout_reader IDENTIFIED BY "&1";
GRANT CREATE SESSION TO scout_reader;

---------------------------------------------------------------- SCOUT_HR
CREATE TABLE scout_hr.employees (
  id         NUMBER,
  first_name VARCHAR2(50),
  last_name  VARCHAR2(50),
  notes      VARCHAR2(400),
  status     CHAR(10),
  hired      DATE,
  salary     NUMBER
);
INSERT INTO scout_hr.employees VALUES (1, 'Alice', 'Anderson', 'Excellent communicator and team player', 'ACTIVE', DATE '2020-01-15', 5000);
INSERT INTO scout_hr.employees VALUES (2, 'Bob',   'Baker',    'Handles the quarterly audit report', 'ACTIVE', DATE '2019-03-01', 5200);
INSERT INTO scout_hr.employees VALUES (3, 'Carol', 'Chen',     'Prefers remote work; the report is due Friday', 'LEAVE', DATE '2018-07-20', 6100);
INSERT INTO scout_hr.employees VALUES (4, 'Dave',  'Davis',    'Reportedly late on deliverables', 'ACTIVE', DATE '2021-11-05', 4800);
INSERT INTO scout_hr.employees VALUES (5, 'Eve',   'O''Brien', 'Said 100% done; check user_name field', 'ACTIVE', DATE '2022-02-14', 5500);
INSERT INTO scout_hr.employees VALUES (6, 'Frank', 'Foster',   'REPORT in uppercase for case tests', 'ACTIVE', DATE '2017-09-09', 7000);
INSERT INTO scout_hr.employees VALUES (7, 'Grace', 'Green',    'username lookup is separate', 'ACTIVE', DATE '2023-05-30', 5100);
INSERT INTO scout_hr.employees VALUES (8, 'Hal', 'Hill', 'Albert met Mr Albertson', 'ACTIVE', DATE '2016-04-04', 4000);
INSERT INTO scout_hr.employees VALUES (9, 'Ivy', 'Ito', 'Gilbert and Talbert', 'ACTIVE', DATE '2015-08-08', 4100);
INSERT INTO scout_hr.employees VALUES (10, 'Jon', 'Jay', 'Albertina', 'LEAVE', DATE '2014-02-02', 4200);
-- empty table: must be searched without error and yield nothing
CREATE TABLE scout_hr.empty_log (msg VARCHAR2(100));

-------------------------------------------------------------- SCOUT_DOCS
CREATE TABLE scout_docs.documents (
  doc_id NUMBER,
  title  VARCHAR2(200),
  body   CLOB,
  author NVARCHAR2(100)
);
INSERT INTO scout_docs.documents VALUES (1, 'Fox story', 'The quick brown fox jumps over the lazy dog', N'Zoë Müller');
INSERT INTO scout_docs.documents VALUES (2, 'Short note', 'A quick and lazy afternoon', N'Ana Silva');
INSERT INTO scout_docs.documents VALUES (3, 'Filler', 'Nothing relevant here', N'Li Wei');
INSERT INTO scout_docs.documents VALUES (4, 'Adverb', 'Quickly lazy', N'Sam Poe');
INSERT INTO scout_docs.documents VALUES (5, 'The QUICK brown FOX club', 'Unrelated body text', N'Ola Nordmann');
-- views duplicate base-table data; SCOUT skips them
CREATE VIEW scout_docs.doc_titles AS SELECT title FROM scout_docs.documents;

-- Same text with and without an Oracle Text index: SCOUT must find identical matches in both.
CREATE TABLE scout_docs.indexed_notes (note_id NUMBER, body VARCHAR2(400));
CREATE TABLE scout_docs.plain_notes   (note_id NUMBER, body VARCHAR2(400));
INSERT INTO scout_docs.indexed_notes VALUES (1, 'A zebra crossing ahead');
INSERT INTO scout_docs.indexed_notes VALUES (2, 'Zebras graze at dawn');
INSERT INTO scout_docs.indexed_notes VALUES (3, 'Nothing striped here');
INSERT INTO scout_docs.plain_notes SELECT * FROM scout_docs.indexed_notes;
CREATE INDEX scout_docs.indexed_notes_ctx ON scout_docs.indexed_notes (body)
  INDEXTYPE IS CTXSYS.CONTEXT;

--------------------------------------------------------------- SCOUT_ODD
-- Quoted, mixed-case, space-containing identifiers, plus a table with no string columns.
CREATE TABLE scout_odd."MixedCase Table" (
  "Row Id"   NUMBER,
  "Weird Col" VARCHAR2(100)
);
INSERT INTO scout_odd."MixedCase Table" VALUES (1, 'colour');
INSERT INTO scout_odd."MixedCase Table" VALUES (2, 'color');
INSERT INTO scout_odd."MixedCase Table" VALUES (3, 'Colorado');
INSERT INTO scout_odd."MixedCase Table" VALUES (4, 'The colors are bright');
INSERT INTO scout_odd."MixedCase Table" VALUES (5, 'it''s a ''quoted'' value');
-- Characters Oracle's case-insensitive matching treats as equal to plain letters (Kelvin sign
-- = k, long s = s, sharp s = ss, ligatures = fi/ffi/...). Built with UNISTR so the file's
-- encoding cannot change them. 'narrow' holds exactly one character.
CREATE TABLE scout_odd."Unicode Edge" (id NUMBER, txt VARCHAR2(80 CHAR), narrow VARCHAR2(1 CHAR));
INSERT INTO scout_odd."Unicode Edge" VALUES (1, 'Kelvin ' || UNISTR('\212A') || ' sign', UNISTR('\212A'));
INSERT INTO scout_odd."Unicode Edge" VALUES (2, 'long ' || UNISTR('\017F') || 'afe harbour', UNISTR('\017F'));
INSERT INTO scout_odd."Unicode Edge" VALUES (3, 'Stra' || UNISTR('\00DF') || 'e', UNISTR('\00DF'));
INSERT INTO scout_odd."Unicode Edge" VALUES (4, UNISTR('\FB01') || 'ne print', UNISTR('\FB01'));
INSERT INTO scout_odd."Unicode Edge" VALUES (5, 'o' || UNISTR('\FB03') || 'ce supplies', UNISTR('\FB03'));
INSERT INTO scout_odd."Unicode Edge" VALUES (6, UNISTR('\0130') || 'stanbul', UNISTR('\0130'));
INSERT INTO scout_odd."Unicode Edge" VALUES (7, 'plain ascii: office, fine, strasse, kelvin, safe', 'x');

-- the only hit is the last of 200 rows: locate mode must still report this column
CREATE TABLE scout_odd.late_hit (id NUMBER, txt VARCHAR2(100));
INSERT INTO scout_odd.late_hit
  SELECT LEVEL, CASE WHEN LEVEL = 200 THEN 'the lateneedle arrives' ELSE 'filler text row' END
    FROM dual CONNECT BY LEVEL <= 200;
-- Oracle reserved words as table and column names: only quoting keeps the generated SQL valid.
CREATE TABLE scout_odd."ORDER" (id NUMBER, "GROUP" VARCHAR2(100), "LEVEL" VARCHAR2(100));
INSERT INTO scout_odd."ORDER" VALUES (1, 'reservedneedle in a group', 'plain level');
INSERT INTO scout_odd."ORDER" VALUES (2, 'nothing here', 'reservedneedle at a level');
CREATE TABLE scout_odd."TABLE" (id NUMBER, "COMMENT" VARCHAR2(100));
INSERT INTO scout_odd."TABLE" VALUES (1, 'a reservedneedle comment');

-- 60 string columns: more than one scan covers (MAX_COLUMNS_PER_SCAN = 20), so the groups are split
-- at the boundaries. 'widehit' sits in columns 1, 20, 21, 40, 41 and 60.
CREATE TABLE scout_odd.wide_60 (
  id NUMBER,
  c01 VARCHAR2(60),
  c02 VARCHAR2(60),
  c03 VARCHAR2(60),
  c04 VARCHAR2(60),
  c05 VARCHAR2(60),
  c06 VARCHAR2(60),
  c07 VARCHAR2(60),
  c08 VARCHAR2(60),
  c09 VARCHAR2(60),
  c10 VARCHAR2(60),
  c11 VARCHAR2(60),
  c12 VARCHAR2(60),
  c13 VARCHAR2(60),
  c14 VARCHAR2(60),
  c15 VARCHAR2(60),
  c16 VARCHAR2(60),
  c17 VARCHAR2(60),
  c18 VARCHAR2(60),
  c19 VARCHAR2(60),
  c20 VARCHAR2(60),
  c21 VARCHAR2(60),
  c22 VARCHAR2(60),
  c23 VARCHAR2(60),
  c24 VARCHAR2(60),
  c25 VARCHAR2(60),
  c26 VARCHAR2(60),
  c27 VARCHAR2(60),
  c28 VARCHAR2(60),
  c29 VARCHAR2(60),
  c30 VARCHAR2(60),
  c31 VARCHAR2(60),
  c32 VARCHAR2(60),
  c33 VARCHAR2(60),
  c34 VARCHAR2(60),
  c35 VARCHAR2(60),
  c36 VARCHAR2(60),
  c37 VARCHAR2(60),
  c38 VARCHAR2(60),
  c39 VARCHAR2(60),
  c40 VARCHAR2(60),
  c41 VARCHAR2(60),
  c42 VARCHAR2(60),
  c43 VARCHAR2(60),
  c44 VARCHAR2(60),
  c45 VARCHAR2(60),
  c46 VARCHAR2(60),
  c47 VARCHAR2(60),
  c48 VARCHAR2(60),
  c49 VARCHAR2(60),
  c50 VARCHAR2(60),
  c51 VARCHAR2(60),
  c52 VARCHAR2(60),
  c53 VARCHAR2(60),
  c54 VARCHAR2(60),
  c55 VARCHAR2(60),
  c56 VARCHAR2(60),
  c57 VARCHAR2(60),
  c58 VARCHAR2(60),
  c59 VARCHAR2(60),
  c60 VARCHAR2(60)
);
INSERT INTO scout_odd.wide_60 (id, c01, c20, c21, c40, c41, c60)
  VALUES (1, 'widehit one', 'widehit twenty', 'widehit twenty-one', 'widehit forty', 'widehit forty-one', 'widehit sixty');
INSERT INTO scout_odd.wide_60 (id, c01, c21, c60) VALUES (2, 'second widehit', 'widehit again', 'last widehit');

CREATE TABLE scout_odd.nums (n NUMBER, d DATE);
INSERT INTO scout_odd.nums VALUES (42, DATE '2024-01-01');

----------------------------------------------------------- SCOUT_HIDDEN
CREATE TABLE scout_hidden.secrets (id NUMBER, note VARCHAR2(200));
INSERT INTO scout_hidden.secrets VALUES (1, 'confidential audit report');

----------------------------------------------------------- SCOUT_BROKEN
-- Visible in the catalog but fails when queried (external table whose file is missing):
-- SCOUT must skip it, keep going, and report it.
CREATE OR REPLACE DIRECTORY scout_missing_dir AS '/nonexistent/scout';
CREATE TABLE scout_broken.ext_missing (txt VARCHAR2(100))
  ORGANIZATION EXTERNAL (TYPE ORACLE_LOADER DEFAULT DIRECTORY scout_missing_dir
                         LOCATION ('missing.dat'))
  REJECT LIMIT UNLIMITED;

-- (own schema, kept out of expected.SCHEMAS like SCOUT_BROKEN) One column that raises when the table is read (ORA-01476 on row 3) next to a healthy one: a scan of
-- the whole table fails, but SCOUT must still search NOTE and skip only V.
CREATE TABLE scout_virt.virt_err (
  id   NUMBER,
  note VARCHAR2(100),
  v    VARCHAR2(40) GENERATED ALWAYS AS (TO_CHAR(10 / (id - 3))) VIRTUAL
);
INSERT INTO scout_virt.virt_err (id, note) VALUES (1, 'virtneedle first');
INSERT INTO scout_virt.virt_err (id, note) VALUES (2, 'nothing here');
INSERT INTO scout_virt.virt_err (id, note) VALUES (3, 'virtneedle third');
INSERT INTO scout_virt.virt_err (id, note) VALUES (4, 'virtneedle fourth');

COMMIT;

-- reader gets everything except scout_hidden
GRANT SELECT ON scout_hr.employees           TO scout_reader;
GRANT SELECT ON scout_hr.empty_log           TO scout_reader;
GRANT SELECT ON scout_docs.documents         TO scout_reader;
GRANT SELECT ON scout_docs.doc_titles        TO scout_reader;
GRANT SELECT ON scout_docs.indexed_notes     TO scout_reader;
GRANT SELECT ON scout_docs.plain_notes       TO scout_reader;
GRANT SELECT ON scout_odd."MixedCase Table"  TO scout_reader;
GRANT SELECT ON scout_odd."ORDER"            TO scout_reader;
GRANT SELECT ON scout_odd."TABLE"            TO scout_reader;
GRANT SELECT ON scout_odd.nums               TO scout_reader;
GRANT SELECT ON scout_odd.wide_60            TO scout_reader;
GRANT SELECT ON scout_odd.late_hit           TO scout_reader;
GRANT SELECT ON scout_odd."Unicode Edge"   TO scout_reader;
GRANT SELECT ON scout_broken.ext_missing     TO scout_reader;
GRANT SELECT ON scout_virt.virt_err        TO scout_reader;
