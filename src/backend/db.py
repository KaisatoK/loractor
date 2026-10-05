import psycopg2
from pathlib import Path


def get_conn():
    return psycopg2.connect("host=localhost dbname=postgres user=postgres password=postgres")


def import_from_csv(relative_file_path: str, table_name: str):
    conn = get_conn()
    cursor = conn.cursor()
    base_dir = Path(__file__).resolve().parent
    absolute_file_path = base_dir / relative_file_path

    try:
        create_table_query = f"""
            CREATE TABLE IF NOT EXISTS {table_name} (
                line_id INT PRIMARY KEY,
                log_record TEXT,
                log_date DATE,
                log_time TIME,
                pid INT,
                level VARCHAR(50),
                component TEXT,
                addr TEXT,
                content TEXT,
                event_id VARCHAR(50),
                event_template TEXT
            );
        """

        cursor.execute(create_table_query)
        print(f"Table '{table_name}' created successfully.")

        copy_query = f"""
            COPY {table_name} (
                line_id, log_record, log_date, log_time, pid, 
                level, component, addr, content, event_id, event_template
            )
            FROM STDIN
            WITH CSV HEADER DELIMITER ','
        """

        with open(absolute_file_path, "r", encoding="utf-8") as f:
            cursor.copy_expert(copy_query, f)

        conn.commit()
        print("Import completed successfully.")
    except Exception as e:
        conn.rollback()
        print(e)
    finally:
        cursor.close()
        conn.close()


if __name__ == "__main__":
    import_from_csv("csv/OpenStack_2k.log_structured.csv", "LOG")
