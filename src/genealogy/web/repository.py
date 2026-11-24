"""Parameterized reads against one audience's security-barrier views only."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal

import psycopg
from psycopg import sql

Audience = Literal["public", "family"]
Record = dict[str, Any]


class Repository:
    def __init__(self, connection: Any, audience: Audience) -> None:
        self.connection = connection
        self.schema = sql.Identifier("site_" + audience)

    def rows(self, query: str, params: tuple[Any, ...] = ()) -> list[Record]:
        with self.connection.cursor() as cursor:
            cursor.execute(sql.SQL(query).format(ns=self.schema), params)
            return [dict(row[0]) for row in cursor.fetchall()]

    def get(self, record_id: str, categories: list[str]) -> Record | None:
        rows = self.rows(
            """SELECT CASE WHEN category IN ('assertions','alternatives','history')
            THEN document || jsonb_build_object('collection',category) ELSE document END
            FROM {ns}.entity WHERE id=%s AND category=ANY(%s)""",
            (record_id, categories),
        )
        return rows[0] if rows else None

    def claims(self, ids: list[str]) -> list[Record]:
        return self.rows(
            """SELECT DISTINCT e.document || jsonb_build_object('collection',e.category)
            FROM {ns}.entity e JOIN {ns}.assertion_subject s ON s.assertion_id=e.id
            WHERE s.ref_id=ANY(%s) ORDER BY 1""",
            (ids,),
        )

    def search(self, query: str, offset: int, scope: str = "all") -> list[Record]:
        # Every token must match authorized names/record names, dates or places.
        # No source prose, relationship text, private counts, or raw corpus query.
        return self.rows(
            """WITH subjects AS (
              SELECT id AS person_id,id AS subject_id FROM {ns}.person
              UNION
              SELECT p.id, CASE WHEN a.document->>'subject_id'=p.id
                THEN a.document->'value'->>'other_id' ELSE a.document->>'subject_id' END
              FROM {ns}.person p JOIN {ns}.assertion_subject s ON s.ref_id=p.id
              JOIN {ns}.assertion a ON a.id=s.assertion_id
              WHERE a.document->>'assertion_type'='identity.same_person'
            ), searchable AS (
              SELECT p.id, coalesce(string_agg(
                CASE WHEN e.document->>'assertion_type'='person.name'
                  THEN concat_ws(' ', e.document->'value'->>'raw',
                    e.document->'value'->>'normalized') ELSE concat_ws(' ',
                    e.document->'value'->'date'->>'raw',
                    e.document->'value'->'date'->>'normalized',
                    e.document->'value'->>'raw_place') END,' '),'') AS words
              FROM {ns}.person p LEFT JOIN subjects s ON s.person_id=p.id
              LEFT JOIN {ns}.entity e ON e.document->>'subject_id'=s.subject_id
                AND e.category IN ('assertions','alternatives')
              GROUP BY p.id
            ) SELECT p.document FROM {ns}.person p JOIN searchable s USING(id)
            LEFT JOIN {ns}.assertion display
              ON display.id=p.document->>'display_name_assertion_id'
            WHERE (%s = 'all' OR p.document->>'family_scope' = 'tree')
              AND NOT EXISTS (SELECT 1 FROM unnest(%s::text[]) token
                              WHERE strpos(lower(s.words),lower(token))=0)
            ORDER BY lower(coalesce(display.document->'value'->>'raw','Name not yet established')),p.id
            LIMIT 26 OFFSET %s""",
            (scope, query.split(), offset),
        )

    def sources(self, query: str, kind: str, offset: int) -> list[Record]:
        # Bind kind twice: an empty value selects all types; otherwise require equality.
        return self.rows(
            """SELECT document FROM {ns}.source
            WHERE (%s = '' OR document->>'source_kind' = %s)
              AND NOT EXISTS (SELECT 1 FROM unnest(%s::text[]) token
                WHERE strpos(lower(document->>'title'),lower(token))=0)
            ORDER BY lower(document->>'title'),id LIMIT 26 OFFSET %s""",
            (kind, kind, query.split(), offset),
        )

    def source_kind_counts(self, query: str) -> list[Record]:
        # Deliberately omit the selected kind: options reflect title matches.
        return self.rows(
            """SELECT jsonb_build_object('value',document->>'source_kind','count',count(*))
            FROM {ns}.source
            WHERE NOT EXISTS (SELECT 1 FROM unnest(%s::text[]) token
                WHERE strpos(lower(document->>'title'),lower(token))=0)
            GROUP BY document->>'source_kind'""",
            (query.split(),),
        )

    def listing_token(self) -> str:
        with self.connection.cursor() as cursor:
            cursor.execute(sql.SQL("SELECT token FROM {ns}.listing_token").format(ns=self.schema))
            row = cursor.fetchone()
            if row is None:
                raise RuntimeError("Serving projection unavailable")
            return str(row[0])

    def source_items(self, source_id: str, category: str) -> list[Record]:
        return self.rows(
            "SELECT document FROM {ns}.entity WHERE category=%s AND document->>'source_id'=%s ORDER BY id",
            (category, source_id),
        )

    def narratives(self, person_id: str) -> list[Record]:
        return self.rows(
            "SELECT document FROM {ns}.narrative WHERE document->'subjects' ? %s ORDER BY id",
            (person_id,),
        )

    def source_claims(self, source_id: str) -> list[Record]:
        return self.rows(
            """SELECT DISTINCT a.document
            FROM {ns}.assertion a
            CROSS JOIN LATERAL jsonb_array_elements(
                coalesce(a.document->'evidence','[]'::jsonb)) e
            JOIN {ns}.citation c ON c.id=e->>'citation_id'
            WHERE c.document->>'source_id'=%s""",
            (source_id,),
        )


class PostgresStore:
    def __init__(self, public_dsn: str, family_dsn: str | None = None) -> None:
        self.dsns = {"public": public_dsn, "family": family_dsn}

    @contextmanager
    def open(self, audience: Audience) -> Iterator[Repository]:
        dsn = self.dsns[audience]
        if not dsn:
            raise RuntimeError("Serving connection unavailable")
        with psycopg.connect(dsn, connect_timeout=5) as connection:
            # Reject privileged/misconfigured web credentials. A public connection
            # must not possess family grants, even before SET ROLE.
            row = connection.execute(
                """SELECT rolsuper OR rolcreaterole OR rolcreatedb OR rolreplication OR rolbypassrls,
                has_schema_privilege(session_user,'genealogy','USAGE'),
                has_schema_privilege(session_user,'site_family','USAGE')
                FROM pg_roles WHERE rolname=session_user"""
            ).fetchone()
            if row is None or row[0] or row[1] or (audience == "public" and row[2]):
                raise RuntimeError("Serving credentials must be least privilege")
            connection.commit()
            with connection.transaction():
                connection.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
                connection.execute("SET LOCAL statement_timeout='5s'")
                connection.execute(
                    sql.SQL("SET LOCAL ROLE {}").format(
                        sql.Identifier("genealogy_" + audience + "_ro")
                    )
                )
                yield Repository(connection, audience)
