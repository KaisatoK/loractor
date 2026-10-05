import psycopg2
from psycopg2.extras import RealDictCursor
from psycopg2 import sql
import re

def get_conn():
    return psycopg2.connect("host=localhost dbname=postgres user=postgres")

def import_from_csv():
    conn = get_conn()
    cur = conn.cursor()
    
    cmd = sql.SQL(
        """CREATE TABLE users(
            id integer PRIMARY KEY,
            email text,
            name text,
            address text
        )
        """
    )
    
    