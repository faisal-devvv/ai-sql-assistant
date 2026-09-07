import streamlit as st
import pandas as pd
import mysql.connector
from google import genai
from dotenv import load_dotenv
from pathlib import Path
import os

env_path = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=env_path)

st.set_page_config(page_title="AI SQL Assistant", page_icon="🤖", layout="wide")

st.markdown("""
    <style>
    .main { padding-top: 2rem; }
    h1 { color: #4F8BF9; font-weight: 700; }
    div[data-testid="stTextInput"] input { border-radius: 8px; }
    </style>
""", unsafe_allow_html=True)

st.title("🤖 AI SQL Assistant")
st.caption("Upload any dataset, chat with it in plain English, get real answers.")
st.divider()

with st.sidebar:
    st.header("ℹ️ How it works")
    st.markdown("""
    1. **Upload** a CSV file
    2. We build a database table automatically
    3. **Chat** with your data in plain English
    4. AI writes SQL, runs it, and explains the result
    """)
    st.divider()
    st.caption("Built with Python, MySQL, Streamlit & Gemini")

MODEL_NAME = "gemini-3.6-flash"
DB_NAME = "ai_sql_db"

def get_admin_connection():
    return mysql.connector.connect(host="localhost", user="root", password=os.getenv("MYSQL_PASSWORD"))

def get_db_connection():
    return mysql.connector.connect(host="localhost", user="root", password=os.getenv("MYSQL_PASSWORD"), database=DB_NAME)

st.subheader("📁 Upload your data")
uploaded_file = st.file_uploader("Upload a CSV file", type=["csv"], label_visibility="collapsed")

if uploaded_file is not None:
    if uploaded_file.size > 10 * 1024 * 1024:
        st.error("❌ File too large. Please upload a CSV under 10MB.")
        st.stop()

    try:
        df = pd.read_csv(uploaded_file)
        df.columns = [str(c).strip() for c in df.columns]
    except Exception:
        st.error("❌ Couldn't read this file. Please make sure it's a valid CSV.")
        st.stop()

    if df.empty:
        st.warning("⚠️ This CSV has no data rows. Please upload a file with data.")
        st.stop()

    st.write("**Preview of your data:**")
    m1, m2 = st.columns(2)
    m1.metric("Rows", f"{len(df):,}")
    m2.metric("Columns", len(df.columns))
    st.dataframe(df.head(10), use_container_width=True)
    st.caption(f"Showing first {min(10, len(df))} of {len(df):,} rows")

    file_id = (uploaded_file.name, uploaded_file.size)
    if st.session_state.get("file_id") != file_id:
        try:
            admin_conn = get_admin_connection()
            admin_cursor = admin_conn.cursor()
            admin_cursor.execute(f"CREATE DATABASE IF NOT EXISTS {DB_NAME}")
            admin_conn.close()

            conn = get_db_connection()
            cursor = conn.cursor()

            table_name = "uploaded_data"
            cursor.execute(f"DROP TABLE IF EXISTS {table_name}")

            columns_sql = []
            schema_description = []
            for col in df.columns:
                if pd.api.types.is_integer_dtype(df[col]):
                    col_type = "INT"
                elif pd.api.types.is_float_dtype(df[col]):
                    col_type = "FLOAT"
                else:
                    col_type = "TEXT"
                columns_sql.append(f"`{col}` {col_type}")
                schema_description.append(f"{col} ({col_type})")

            create_query = f"CREATE TABLE {table_name} ({', '.join(columns_sql)})"
            cursor.execute(create_query)

            clean_df = df.astype(object).where(pd.notnull(df), None)
            placeholders = ", ".join(["%s"] * len(df.columns))
            insert_query = f"INSERT INTO {table_name} VALUES ({placeholders})"
            cursor.executemany(insert_query, clean_df.values.tolist())
            conn.commit()
            conn.close()

            st.session_state.file_id = file_id
            st.session_state.table_name = table_name
            st.session_state.row_count = len(df)
            st.session_state.schema_text = f"Table: {table_name}\nColumns: {', '.join(schema_description)}"
            st.session_state.sample_data = df.head(3).to_string()
            st.session_state.chat_history = []
        except mysql.connector.Error as e:
            st.error(f"⚠️ Couldn't connect to MySQL. Is the server running? ({e})")
            st.stop()

    st.success(f"✅ Table ready with {st.session_state.row_count:,} rows — ask away!")
    st.divider()
    st.subheader("💬 Chat with your data")

    if "chat_history" not in st.session_state:
        st.session_state.chat_history = []

    for msg in st.session_state.chat_history:
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("sql") or msg.get("result_df") is not None:
                with st.expander("🔍 View generated SQL & data"):
                    if msg.get("sql"):
                        st.code(msg["sql"], language="sql")
                    if msg.get("result_df") is not None:
                        st.dataframe(msg["result_df"], use_container_width=True)

    question = st.chat_input("Ask a question about your data...")

    if question:
        st.session_state.chat_history.append({"role": "user", "content": question})
        with st.chat_message("user"):
            st.markdown(question)

        history_lines = []
        for m in st.session_state.chat_history[-7:-1]:
            if m["role"] == "user":
                history_lines.append(f"User asked: {m['content']}")
            elif m.get("sql"):
                history_lines.append(f"AI used SQL: {m['sql']}")
        history_text = "\n".join(history_lines) if history_lines else "(no earlier messages)"

        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))

        with st.chat_message("assistant"):
            prompt_sql = f"""
You are an expert SQL analyst helping a non-technical user query their data.

Table: {st.session_state.table_name}
Columns: {st.session_state.schema_text}

Sample rows (for reference on exact formatting/casing of values):
{st.session_state.sample_data}

Recent conversation for context:
{history_text}

Rules:
1. Only use column names exactly as listed above — never invent columns.
2. For text/category filters, use LOWER(column) LIKE LOWER('%value%') so casing and partial matches don't cause missed results, unless an exact match is clearly needed.
3. If the question refers back to something from the conversation above (e.g. "what about her", "and in sales"), use that context.
4. If the question truly cannot be answered from this schema, respond with exactly: NO_SQL: <short reason>
5. Otherwise, respond with ONLY the raw MySQL query — no explanation, no markdown.

Current question: {question}
"""
            try:
                with st.spinner("🤖 Thinking..."):
                    resp_sql = client.models.generate_content(model=MODEL_NAME, contents=prompt_sql)
                ai_output = resp_sql.text.strip()
            except Exception as e:
                if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                    reply_text = "⏳ Hit the API rate limit for a moment. Wait a few seconds and try again."
                else:
                    reply_text = f"⚠️ AI service error: {e}"
                st.markdown(reply_text)
                st.session_state.chat_history.append({"role": "assistant", "content": reply_text})
                st.stop()

            if ai_output.startswith("NO_SQL"):
                reply_text = f"🤔 I can't answer that from this data. {ai_output.replace('NO_SQL:', '').strip()}"
                st.markdown(reply_text)
                st.session_state.chat_history.append({"role": "assistant", "content": reply_text})
            else:
                sql_query = ai_output.replace("```sql", "").replace("```", "").strip()

                forbidden = ["DROP", "DELETE", "UPDATE", "INSERT", "ALTER", "TRUNCATE"]
                if any(w in sql_query.upper() for w in forbidden):
                    reply_text = "⚠️ That question would modify the database, which isn't allowed."
                    st.markdown(reply_text)
                    st.session_state.chat_history.append({"role": "assistant", "content": reply_text, "sql": sql_query})
                else:
                    rows = None
                    try:
                        conn = get_db_connection()
                        cursor = conn.cursor()
                        cursor.execute(sql_query)
                        rows = cursor.fetchall()
                        col_names = [d[0] for d in cursor.description]
                        conn.close()
                    except mysql.connector.Error as e:
                        reply_text = f"⚠️ Couldn't run that query: {e}"
                        st.markdown(reply_text)
                        st.session_state.chat_history.append({"role": "assistant", "content": reply_text, "sql": sql_query})

                    if rows is not None:
                        if not rows:
                            reply_text = "📭 No results found for that question."
                            st.markdown(reply_text)
                            st.session_state.chat_history.append({"role": "assistant", "content": reply_text, "sql": sql_query})
                        else:
                            result_df = pd.DataFrame(rows, columns=col_names)

                            try:
                                result_sample = result_df.head(10).to_string()
                                prompt_answer = f"""
You are a helpful, conversational data assistant.
The user asked: "{question}"
The database returned this exact data:
{result_sample}

Answer the user's question directly and naturally, based only on this data. Keep it short and conversational, like a knowledgeable colleague would.
"""
                                with st.spinner("💬 Writing your answer..."):
                                    resp_ans = client.models.generate_content(model=MODEL_NAME, contents=prompt_answer)
                                reply_text = resp_ans.text.strip()
                            except Exception:
                                reply_text = "Here's what I found:"

                            st.markdown(reply_text)

                            wants_chart = any(k in question.lower() for k in ["chart", "graph", "plot", "visual", "compare"])
                            if wants_chart:
                                try:
                                    numeric_cols = result_df.select_dtypes(include="number").columns
                                    if len(numeric_cols) > 0:
                                        st.bar_chart(result_df.set_index(result_df.columns[0])[numeric_cols])
                                except Exception:
                                    pass

                            with st.expander("🔍 View generated SQL & data"):
                                st.code(sql_query, language="sql")
                                st.dataframe(result_df, use_container_width=True)

                            st.session_state.chat_history.append({
                                "role": "assistant", "content": reply_text,
                                "sql": sql_query, "result_df": result_df
                            })
else:
    st.info("👆 Upload a CSV file to get started")