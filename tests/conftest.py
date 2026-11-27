import os
import uuid

import psycopg
import pytest
from check_web import serialized_migration
from psycopg import sql
from psycopg.conninfo import make_conninfo


@pytest.fixture
def database():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("requires a disposable PostgreSQL cluster")
    db = "contract_" + uuid.uuid4().hex
    roles = [db + "_p", db + "_f"]
    with psycopg.connect(url, autocommit=True) as cluster:
        cluster.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(db)))
        try:
            dsn = make_conninfo(url, dbname=db)
            with psycopg.connect(dsn, autocommit=True) as admin:
                serialized_migration(cluster, admin)
                for role, aud in zip(roles, ("public", "family"), strict=True):
                    admin.execute(
                        sql.SQL("CREATE ROLE {} LOGIN PASSWORD 'workshop'").format(
                            sql.Identifier(role)
                        )
                    )
                    admin.execute(
                        sql.SQL("GRANT {} TO {}").format(
                            sql.Identifier("genealogy_" + aud + "_ro"), sql.Identifier(role)
                        )
                    )
                yield admin, [make_conninfo(dsn, user=r, password="workshop") for r in roles]
        finally:
            cluster.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(db)))
            for role in roles:
                cluster.execute(sql.SQL("DROP ROLE IF EXISTS {}").format(sql.Identifier(role)))
