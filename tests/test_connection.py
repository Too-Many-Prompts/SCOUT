import pytest


@pytest.mark.integration
def test_can_connect_and_query_dual(oracle_connection):
    cursor = oracle_connection.cursor()
    cursor.execute("SELECT 1 FROM dual")
    assert cursor.fetchone() == (1,)
