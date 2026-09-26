import streamlit as st
import json
import random
import time
import os
import io
import re
import zipfile
import xml.etree.ElementTree as ET
import google.generativeai as genai

# 嘗試載入 PDF 自動切頁核心套件
try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# ================= 0. Turso 雲端 SQLite 連線設定 =================
TURSO_DB_URL = "libsql://quiz-db-tony080830-coder.aws-ap-northeast-1.turso.io"
TURSO_AUTH_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTA0MzMzNzYsImlkIjoiMDFhMGRiYTUtYmIwMS03MDkwLTgxM2UtNDUwODgwZGQ5MDdhIiwia2lkIjoiRG5HUXMycy13c0VfNkc5Szlnbms4cENlYWJ0NjZRcF9yUUhNYVU1aUhLSSIsInJpZCI6ImM0ZDI0Mjc1LTVmNzQtNGZkMi05M2Y5LTk1ZjRjMWExNWQ4MCJ9.RhhdrMA0SExmET39mWRfenDK7qMfhnqJUFN8zJSqap_daPCMFyOS08cl7HinR17U49Op5EqFBpYVbTPkvCo1CQ"

IS_CLOUD = False
CLOUD_ERROR = ""
db_conn = None

class RowDict(dict):
    def __getitem__(self, item):
        if isinstance(item, int):
            return list(self.values())[item]
        return super().__getitem__(item)

class CursorWrapper:
    def __init__(self, cursor, is_cloud):
        self.cursor = cursor
        self.is_cloud = is_cloud

    def execute(self, *args, **kwargs):
        new_args = list(args)
        if len(new_args) > 1:
            if isinstance(new_args[1], list):
                new_args[1] = tuple(new_args[1])
            elif new_args[1] is None:
                new_args[1] = ()
        if 'parameters' in kwargs:
            if isinstance(kwargs['parameters'], list):
                kwargs['parameters'] = tuple(kwargs['parameters'])
            elif kwargs['parameters'] is None:
                kwargs['parameters'] = ()
        try:
            self.cursor.execute(*new_args, **kwargs)
        except Exception as e:
            raise e
        return self

    def executemany(self, *args, **kwargs):
        new_args = list(args)
        if len(new_args) > 1 and isinstance(new_args[1], (list, tuple)):
            new_args[1] = [tuple(item) if isinstance(item, list) else item for item in new_args[1]]
        try:
            self.cursor.executemany(*new_args, **kwargs)
        except Exception as e:
            raise e
        return self

    def fetchone(self):
        try:
            row = self.cursor.fetchone()
        except Exception:
            return None
        if row is None:
            return None
        if hasattr(row, 'keys') or isinstance(row, dict):
            return row
        if self.cursor.description:
            cols = [col[0] for col in self.cursor.description]
            return RowDict(zip(cols, row))
        return row

    def fetchall(self):
        try:
            rows = self.cursor.fetchall()
        except Exception:
            return []
        if not rows:
            return []
        if hasattr(rows[0], 'keys') or isinstance(rows[0], dict):
            return rows
        if self.cursor.description:
            cols = [col[0] for col in self.cursor.description]
            return [RowDict(zip(cols, r)) for r in rows]
        return rows

class DBWrapper:
    def __init__(self, conn, is_cloud):
        self.conn = conn
        self.is_cloud = is_cloud
        if not is_cloud:
            import sqlite3
            self.conn.row_factory = sqlite3.Row

    def cursor(self):
        return CursorWrapper(self.conn.cursor(), self.is_cloud)

    def commit(self):
        try:
            return self.conn.commit()
        except Exception:
            pass

def init_connection():
    global IS_CLOUD, CLOUD_ERROR
    if TURSO_DB_URL.strip() and TURSO_AUTH_TOKEN.strip() and not TURSO_DB_URL.startswith("您的_"):
        try:
            import libsql_experimental as libsql
            raw_conn = libsql.connect(TURSO_DB_URL.strip(), auth_token=TURSO_AUTH_TOKEN.strip())
            test_cur = raw_conn.cursor()
            test_cur.execute("SELECT 1")
            test_cur.fetchall()
            IS_CLOUD = True
            return DBWrapper(raw_conn, is_cloud=True)
        except Exception as e:
            CLOUD_ERROR = str(e)
            import sqlite3
            raw_conn = sqlite3.connect('quiz_database.db', check_same_thread=False)
            IS_CLOUD = False
            return DBWrapper(raw_conn, is_cloud=False)
    else:
        import sqlite3
        raw_conn = sqlite3.connect('quiz_database.db', check_same_thread=False)
        IS_CLOUD = False
        return DBWrapper(raw_conn, is_cloud=False)

conn = init_connection()
c = conn.cursor()

# ================= 1. 資料庫初始化 & 自動升級 =================
try:
    c.execute('''CREATE TABLE IF NOT EXISTS questions
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  category TEXT, text TEXT, 
                  opt1 TEXT, opt2 TEXT, opt3 TEXT, opt4 TEXT, 
                  answer TEXT, wrong_count INTEGER DEFAULT 0)''')
    c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
    c.execute('''CREATE TABLE IF NOT EXISTS exam_history
                 (id INTEGER PRIMARY KEY AUTOINCREMENT, 
                  category TEXT, 
                  wrong_ids TEXT, 
                  correct_ids TEXT, 
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')

    c.execute("PRAGMA table_info(questions)")
    existing_cols = [col['name'] for col in c.fetchall()]
    if 'explanation' not in existing_cols:
        c.execute("ALTER TABLE questions ADD COLUMN explanation TEXT DEFAULT ''")
    if 'folder' not in existing_cols:
        c.execute("ALTER TABLE questions ADD COLUMN folder TEXT DEFAULT '未分類'")
    if 'is_starred' not in existing_cols:
        c.execute("ALTER TABLE questions ADD COLUMN is_starred INTEGER DEFAULT 0")
    if 'pdf_starred' not in existing_cols:
        c.execute("ALTER TABLE questions ADD COLUMN pdf_starred INTEGER DEFAULT 0")
    if 'options' not in existing_cols:
        c.execute("ALTER TABLE questions ADD COLUMN options TEXT DEFAULT ''")
    conn.commit()
except Exception:
    pass

def get_api_key():
    try:
        c.execute("SELECT value FROM settings WHERE key='gemini_api_key'")
        result = c.fetchone()
        return result['value'] if result else ""
    except Exception:
        return ""

def get_question_options(q):
    if 'options' in q and q['options'] and str(q['options']).strip():
        try:
            opts = json.loads(q['options'])
            if isinstance(opts, list) and len(opts) > 0:
                return [str(o).strip() for o in opts if str(o).strip()]
        except Exception:
            pass
    opts = []
    for col in ['opt1', 'opt2', 'opt3', 'opt4']:
        if col in q and q[col] and str(q[col]).strip():
            opts.append(str(q[col]).strip())
    return opts if opts else ["A", "B"]

# ================= 2. 檔案文字提取工具函式 =================
def extract_text_from_docx(file_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            if 'word/document.xml' not in z.namelist():
                return ""
            xml_content = z.read('word/document.xml')
            tree = ET.fromstring(xml_content)
            paragraphs = []
            for node in tree.iter():
                if node.tag.endswith('p'):
                    texts = [child.text for child in node.iter() if child.tag.endswith('t') and child.text]
                    if texts:
                        paragraphs.append("".join(texts))
            return "\n".join(paragraphs)
    except Exception as e:
        return f"[Word 提取錯誤: {e}]"

def extract_text_from_pptx(file_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            slide_files = [f for f in z.namelist() if 'ppt/slides/slide' in f and f.endswith('.xml')]
            def get_slide_num(s):
                m = re.search(r'slide(\d+)\.xml', s)
                return int(m.group(1)) if m else 0
            slide_files.sort(key=get_slide_num)
            
            slides_text = []
            for idx, sfile in enumerate(slide_files, start=1):
                xml_content = z.read(sfile)
                tree = ET.fromstring(xml_content)
                slide_paragraphs = []
                for node in tree.iter():
                    if node.tag.endswith('p'):
                        texts = [child.text for child in node.iter() if child.tag.endswith('t') and child.text]
                        if texts:
                            slide_paragraphs.append("".join(texts))
                if slide_paragraphs:
                    slides_text.append(f"--- Slide {idx} ---\n" + "\n".join(slide_paragraphs))
            return "\n\n".join(slides_text)
    except Exception as e:
        return f"[PPT 提取錯誤: {e}]"

# ================= 3. 狀態管理 =================
if 'current_q' not in st.session_state: st.session_state.current_q = None
if 'answered' not in st.session_state: st.session_state.answered = False
if 'is_correct' not in st.session_state: st.session_state.is_correct = False
if 'explanation' not in st.session_state: st.session_state.explanation = ""

if 'exam_active' not in st.session_state: st.session_state.exam_active = False
if 'exam_finished' not in st.session_state: st.session_state.exam_finished = False
if 'exam_questions' not in st.session_state: st.session_state.exam_questions = []
if 'exam_index' not in st.session_state: st.session_state.exam_index = 0
if 'exam_wrong_ids' not in st.session_state: st.session_state.exam_wrong_ids = []
if 'exam_correct_ids' not in st.session_state: st.session_state.exam_correct_ids = []
if 'exam_skipped_ids' not in st.session_state: st.session_state.exam_skipped_ids = []
if 'exam_tested_pdfs' not in st.session_state: st.session_state.exam_tested_pdfs = []

def check_answer(selected, correct, q_id):
    st.session_state.answered = True
    is_right = (selected.strip() == correct.strip())
    if not is_right and len(correct) == 1 and selected.startswith(correct):
        is_right = True
    if not is_right and len(selected) == 1 and correct.startswith(selected):
        is_right = True

    try:
        if is_right:
            st.session_state.is_correct = True
            c.execute("UPDATE questions SET wrong_count = MAX(0, wrong_count - 1) WHERE id=?", (q_id,))
        else:
            st.session_state.is_correct = False
            c.execute("UPDATE questions SET wrong_count = wrong_count + 1 WHERE id=?", (q_id,))
        conn.commit()
    except Exception:
        pass

# ================= 4. 網頁介面開始 =================
st.set_page_config(page_title="AI 錯題本", page_icon="📝", layout="centered")
st.title("📝 AI 專屬錯題本系統")

if IS_CLOUD:
    st.success("☁️ 連線狀態：已成功連線至 Turso 雲端資料庫！(重開機資料永不丟失)")
else:
    if CLOUD_ERROR:
        st.error(f"⚠️ Turso 雲端連線失敗原因：`{CLOUD_ERROR}`")
    else:
        st.caption("🖥️ 連線狀態：本地暫存模式 (請在 app.py 填寫 Turso URL 與 Token)")

tab_quiz, tab_review, tab_import, tab_settings = st.tabs(["🎯 開始測驗", "📖 錯題總覽", "📥 匯入題庫", "⚙️ 設定與管理"])

# ---------- 【測驗區】 ----------
with tab_quiz:
    try:
        c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL")
        folders = [row['folder'] for row in c.fetchall() if row['folder']]
    except Exception:
        folders = []
    
    if not folders:
        st.warning("題庫空空如也，請先到「匯入題庫」上傳考卷或簡報檔案！")
    else:
        if not st.session_state.exam_active and not st.session_state.exam_finished:
            col_f, col_st = st.columns([2, 1])
            with col_f:
                selected_folder = st.selectbox("📁 篩選資料夾：", ["全部資料夾"] + folders, key="quiz_folder")
            with col_st:
                st.write("")
                only_star_pdf = st.checkbox("⭐ 僅列星號考卷", value=False)
                only_star_q = st.checkbox("⭐ 只刷星號題目", value=False)

            pdf_query = "SELECT DISTINCT category, pdf_starred FROM questions WHERE 1=1"
            pdf_params = []
            if selected_folder != "全部資料夾":
                pdf_query += " AND folder=?"
                pdf_params.append(selected_folder)
            if only_star_pdf:
                pdf_query += " AND pdf_starred=1"
            
            c.execute(pdf_query, tuple(pdf_params))
            pdf_rows = c.fetchall()
            all_available_pdfs = [row['category'] for row in pdf_rows if row['category']]
            pdf_star_map = {row['category']: bool(row['pdf_starred']) for row in pdf_rows}

            selected_pdfs = st.multiselect(
                "📄 勾選要練習的考卷 / 講義（可複選混合出題）：",
                options=all_available_pdfs,
                default=all_available_pdfs[:1] if all_available_pdfs else [],
                format_func=lambda x: f"⭐ {x}" if pdf_star_map.get(x) else x
            )

            col_cnt, col_order = st.columns(2)
            with col_cnt:
                q_count_option = st.selectbox("⏱️ 練習題數（零碎時間快速刷）：", ["5 題", "10 題", "20 題", "全部題目"])
            with col_order:
                q_order = st.selectbox("🔀 出題順序：", ["隨機抽題（優先抽常錯題）", "照考卷順序"])

            if st.button("🚀 開始測驗", type="primary", use_container_width=True):
                if not selected_pdfs:
                    st.error("請至少勾選一份考卷或講義！")
                else:
                    placeholders = ','.join(['?'] * len(selected_pdfs))
                    query = f"SELECT * FROM questions WHERE category IN ({placeholders})"
                    params = list(selected_pdfs)
                    if only_star_q:
                        query += " AND is_starred=1"
                    
                    if q_order == "照考卷順序":
                        query += " ORDER BY category ASC, id ASC"
                    else:
                        query += " ORDER BY wrong_count DESC, id ASC"

                    c.execute(query, tuple(params))
                    q_pool = c.fetchall()

                    if not q_pool:
                        st.warning("所選條件下無任何題目！")
                    else:
                        if q_count_option != "全部題目":
                            pick_n = int(q_count_option.replace(" 題", ""))
                            actual_pool = q_pool[:max(pick_n * 2, len(q_pool))]
                            random.shuffle(actual_pool)
                            st.session_state.exam_questions = actual_pool[:pick_n]
                        else:
                            q_list = list(q_pool)
                            if q_order != "照考卷順序":
                                random.shuffle(q_list)
                            st.session_state.exam_questions = q_list

                        st.session_state.exam_index = 0
                        st.session_state.exam_wrong_ids = []
                        st.session_state.exam_correct_ids = []
                        st.session_state.exam_skipped_ids = []
                        st.session_state.exam_tested_pdfs = selected_pdfs
                        st.session_state.exam_active = True
                        st.session_state.exam_finished = False
                        st.session_state.answered = False
                        st.session_state.explanation = ""
                        st.rerun()

        elif st.session_state.exam_active and not st.session_state.exam_finished:
            total_q = len(st.session_state.exam_questions)
            idx = st.session_state.exam_index
            curr_q = st.session_state.exam_questions[idx]

            st.progress((idx) / total_q)
            col_prog, col_quit = st.columns([3, 1.2])
            with col_prog:
                st.caption(f"第 {idx + 1} / {total_q} 題 | 來源：{curr_q['category']} | 對：{len(st.session_state.exam_correct_ids)} / 錯：{len(st.session_state.exam_wrong_ids)} / 略過：{len(st.session_state.exam_skipped_ids)}")
            with col_quit:
                if st.button("⏹️ 隨時結算", help="隨時中斷並查看目前答題報告", use_container_width=True):
                    answered_so_far = len(st.session_state.exam_correct_ids) + len(st.session_state.exam_wrong_ids)
                    if answered_so_far > 0:
                        c.execute("INSERT INTO exam_history (category, wrong_ids, correct_ids) VALUES (?, ?, ?)",
                                  (", ".join(st.session_state.exam_tested_pdfs), json.dumps(st.session_state.exam_wrong_ids), json.dumps(st.session_state.exam_correct_ids)))
                        conn.commit()
                    st.session_state.exam_active = False
                    st.session_state.exam_finished = True
                    st.rerun()

            col_t, col_s = st.columns([4, 1.2])
            is_q_st = bool(curr_q['is_starred'])
            with col_t:
                st.subheader(curr_q['text'])
            with col_s:
                if st.button("⭐ 已收藏" if is_q_st else "☆ 收藏", key=f"ex_star_{curr_q['id']}", use_container_width=True):
                    new_star = 0 if is_q_st else 1
                    c.execute("UPDATE questions SET is_starred=? WHERE id=?", (new_star, curr_q['id']))
                    conn.commit()
                    c.execute("SELECT * FROM questions WHERE id=?", (curr_q['id'],))
                    st.session_state.exam_questions[idx] = c.fetchone()
                    st.rerun()

            options = get_question_options(curr_q)
            correct_ans = str(curr_q['answer']).strip()
            q_id = curr_q['id']

            if not st.session_state.answered:
                for opt_idx, opt in enumerate(options):
                    if st.button(opt, key=f"ex_btn_{q_id}_{opt_idx}", use_container_width=True):
                        check_answer(opt, correct_ans, q_id)
                        if st.session_state.is_correct:
                            if q_id not in st.session_state.exam_correct_ids:
                                st.session_state.exam_correct_ids.append(q_id)
                        else:
                            if q_id not in st.session_state.exam_wrong_ids:
                                st.session_state.exam_wrong_ids.append(q_id)
                        st.rerun()
                
                st.write("")
                if st.button("⏭️ 略過此題（非本次範圍 / 不計入成績）", key=f"skip_{q_id}", use_container_width=True):
                    if q_id not in st.session_state.exam_skipped_ids:
                        st.session_state.exam_skipped_ids.append(q_id)
                    
                    is_last = (idx + 1 >= total_q)
                    if is_last:
                        answered_so_far = len(st.session_state.exam_correct_ids) + len(st.session_state.exam_wrong_ids)
                        if answered_so_far > 0:
                            c.execute("INSERT INTO exam_history (category, wrong_ids, correct_ids) VALUES (?, ?, ?)",
                                      (", ".join(st.session_state.exam_tested_pdfs), json.dumps(st.session_state.exam_wrong_ids), json.dumps(st.session_state.exam_correct_ids)))
                            conn.commit()
                        st.session_state.exam_active = False
                        st.session_state.exam_finished = True
                    else:
                        st.session_state.exam_index += 1
                        st.session_state.answered = False
                        st.session_state.explanation = ""
                    st.rerun()

            else:
                if st.session_state.is_correct:
                    st.success("✅ 答對了！")
                else:
                    st.error(f"❌ 答錯了！正確答案是：{correct_ans}")

                col_next, col_exp = st.columns(2)
                with col_next:
                    is_last = (idx + 1 >= total_q)
                    btn_label = "🏁 完成並結算" if is_last else "👉 下一題"
                    if st.button(btn_label, type="primary", use_container_width=True):
                        if is_last:
                            c.execute("INSERT INTO exam_history (category, wrong_ids, correct_ids) VALUES (?, ?, ?)",
                                      (", ".join(st.session_state.exam_tested_pdfs), json.dumps(st.session_state.exam_wrong_ids), json.dumps(st.session_state.exam_correct_ids)))
                            conn.commit()
                            st.session_state.exam_active = False
                            st.session_state.exam_finished = True
                        else:
                            st.session_state.exam_index += 1
                            st.session_state.answered = False
                            st.session_state.explanation = ""
                        st.rerun()

                with col_exp:
                    has_exp = bool(curr_q['explanation'] and curr_q['explanation'].strip() and curr_q['explanation'] != '無提供詳解')
                    btn_exp_title = "📖 查看詳解" if has_exp else "🧠 AI 即時分析詳解"
                    if st.button(btn_exp_title, key=f"ex_exp_{q_id}", use_container_width=True):
                        if has_exp:
                            st.session_state.explanation = curr_q['explanation']
                        else:
                            api_key = get_api_key()
                            if not api_key:
                                st.error("請至「⚙️ 設定與管理」輸入 API Key！")
                            else:
                                with st.spinner("AI 正在為這題撰寫繁體中文深度詳解..."):
                                    try:
                                        genai.configure(api_key=api_key)
                                        model = genai.GenerativeModel('gemini-3.8-flash')
                                        prompt = (
                                            f"題目：{curr_q['text']}\n"
                                            f"選項：{options}\n"
                                            f"正確答案：{correct_ans}\n\n"
                                            "【作答規範】\n"
                                            "1. 無論題目原文是英文還是中文，詳解一律必須使用「繁體中文（台灣醫學常用語）」撰寫。\n"
                                            "2. 請點出關鍵核心考點與專有名詞中英對照。\n"
                                            "3. 詳細說明正解正確之原因，並逐一剖析其他錯誤選項錯在哪裡。"
                                        )
                                        resp = model.generate_content(prompt, request_options={"timeout": 120})
                                        new_exp = resp.text.strip()
                                        c.execute("UPDATE questions SET explanation=? WHERE id=?", (new_exp, q_id))
                                        conn.commit()
                                        st.session_state.explanation = new_exp
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"AI 生成詳解失敗：{e}")

                if st.session_state.explanation:
                    st.info(st.session_state.explanation)

        elif st.session_state.exam_finished:
            st.balloons()
            st.success("🎉 測驗已結束！本次練習總結如下：")

            curr_wrong = set(st.session_state.exam_wrong_ids)
            curr_correct = set(st.session_state.exam_correct_ids)
            skipped_cnt = len(st.session_state.exam_skipped_ids)
            actual_tested = len(curr_wrong) + len(curr_correct)
            score = (len(curr_correct) / actual_tested * 100) if actual_tested > 0 else 0

            col_m1, col_m2, col_m3, col_m4 = st.columns(4)
            col_m1.metric("實作答對率", f"{score:.1f}%")
            col_m2.metric("🟢 答對題數", f"{len(curr_correct)} 題")
            col_m3.metric("🔴 答錯題數", f"{len(curr_wrong)} 題")
            col_m4.metric("⏭️ 略過題數", f"{skipped_cnt} 題")

            if len(st.session_state.exam_tested_pdfs) == 1:
                single_pdf = st.session_state.exam_tested_pdfs[0]
                try:
                    c.execute("SELECT * FROM exam_history WHERE category=? ORDER BY id DESC LIMIT 2", (single_pdf,))
                    histories = c.fetchall()
                    if len(histories) >= 2:
                        prev_wrong = set(json.loads(histories[1]['wrong_ids']))
                        prev_correct = set(json.loads(histories[1]['correct_ids']))
                        persistent = curr_wrong.intersection(prev_wrong)
                        improved = prev_wrong.intersection(curr_correct)

                        st.markdown("#### 🔄 與上次同份考卷對比")
                        c_p1, c_p2 = st.columns(2)
                        c_p1.metric("🔴 兩次皆錯 (頑固題)", f"{len(persistent)} 題")
                        c_p2.metric("🟢 成功雪恥 (上次錯這次對)", f"{len(improved)} 題")
                except Exception:
                    pass

            st.divider()

            if curr_wrong:
                if st.button("⭐ 一鍵將本次答錯的題目加入星號收藏", type="primary"):
                    placeholders = ','.join(['?'] * len(curr_wrong))
                    c.execute(f"UPDATE questions SET is_starred=1 WHERE id IN ({placeholders})", tuple(curr_wrong))
                    conn.commit()
                    st.toast("✅ 本次錯題已全部加為星號收藏！")
                    time.sleep(0.5)

                st.subheader(f"❌ 本次答錯檢討 ({len(curr_wrong)} 題)")
                for qid in curr_wrong:
                    c.execute("SELECT * FROM questions WHERE id=?", (qid,))
                    q_data = c.fetchone()
                    if q_data:
                        opts_review = get_question_options(q_data)
                        with st.expander(f"[{q_data['category']}] {q_data['text'][:30]}..."):
                            st.markdown(f"**題目**：{q_data['text']}")
                            for i, opt_item in enumerate(opts_review):
                                label = chr(65 + i) if i < 26 else str(i + 1)
                                st.markdown(f"- ({label}) {opt_item}")
                            st.markdown(f"✅ **正確答案**：`{q_data['answer']}`")
                            exp = q_data['explanation'] if q_data['explanation'] else "尚未生成詳解"
                            st.caption(f"💡 解析：{exp}")
            else:
                st.info("太棒了！本次測驗全對，零錯題！")

            if st.button("🔄 繼續新的練習 / 重新開始", use_container_width=True):
                st.session_state.exam_active = False
                st.session_state.exam_finished = False
                st.session_state.exam_questions = []
                st.session_state.exam_index = 0
                st.session_state.exam_wrong_ids = []
                st.session_state.exam_correct_ids = []
                st.session_state.exam_skipped_ids = []
                st.rerun()

# ---------- 【錯題總覽區】 ----------
with tab_review:
    st.markdown("### 📖 各考卷 / 講義題目與解析總覽")
    try:
        c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL")
        folders = [row['folder'] for row in c.fetchall() if row['folder']]
    except Exception:
        folders = []
    
    if not folders:
        st.info("目前沒有題庫資料。")
    else:
        col_f, col_p, col_st = st.columns([1.5, 1.5, 1])
        with col_f:
            rev_folder = st.selectbox("📂 選擇資料夾：", ["全部資料夾"] + folders, key="rev_folder")
        with col_p:
            if rev_folder == "全部資料夾":
                c.execute("SELECT DISTINCT category, pdf_starred FROM questions")
            else:
                c.execute("SELECT DISTINCT category, pdf_starred FROM questions WHERE folder=?", (rev_folder,))
            rev_rows = c.fetchall()
            rev_pdfs = [row['category'] for row in rev_rows if row['category']]
            rev_star_map = {row['category']: bool(row['pdf_starred']) for row in rev_rows}
            rev_pdf = st.selectbox(
                "📄 選擇考卷 / 講義：", 
                ["全部考卷"] + rev_pdfs, 
                key="rev_pdf",
                format_func=lambda x: f"⭐ {x}" if rev_star_map.get(x) else x
            )
        with col_st:
            st.write("")
            rev_only_starred = st.checkbox("⭐ 僅看星號題目", value=False, key="rev_only_star")
            
        query = "SELECT * FROM questions WHERE 1=1"
        params = []
        if rev_folder != "全部資料夾":
            query += " AND folder=?"
            params.append(rev_folder)
        if rev_pdf != "全部考卷":
            query += " AND category=?"
            params.append(rev_pdf)
        if rev_only_starred:
            query += " AND is_starred=1"
            
        c.execute(query, tuple(params))
        questions_to_show = c.fetchall()
        
        if not questions_to_show:
            st.warning("此分類下沒有找到題目。")
        else:
            st.write(f"共找到 **{len(questions_to_show)}** 題：")
            st.divider()
            for idx, q in enumerate(questions_to_show):
                is_st = bool(q['is_starred'])
                star_tag = "⭐ " if is_st else ""
                q_opts = get_question_options(q)
                with st.expander(f"{star_tag}題目 {idx+1}: {q['text'][:30]}... (錯 {q['wrong_count']} 次)"):
                    st.markdown(f"**【題目】** {q['text']}")
                    for opt_i, opt_text in enumerate(q_opts):
                        opt_label = chr(65 + opt_i) if opt_i < 26 else str(opt_i + 1)
                        st.markdown(f"- ({opt_label}) {opt_text}")
                    st.markdown(f"✅ **正確答案**：`{q['answer']}`")
                    exp_text = q['explanation'] if q['explanation'] and q['explanation'].strip() and q['explanation'] != '無提供詳解' else "尚未生成詳解 (做題時可一鍵生成)"
                    st.markdown(f"💡 **解析**：{exp_text}")
                    st.markdown(f"📌 **星號狀態**：{'⭐ 已收藏' if is_st else '☆ 未收藏'}")

# ---------- 【匯入區：自動分批引擎 (徹底防禦 504)】 ----------
with tab_import:
    st.markdown("### 🤖 智慧題庫匯入")
    st.caption("⚡ 內建「長文件自動切頁批次引擎」，幾十頁至上百頁皆可無感全自動背景分段處理！")
    
    try:
        c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL")
        existing_folders = [row['folder'] for row in c.fetchall() if row['folder']]
    except Exception:
        existing_folders = []
    
    folder_choice = st.selectbox("📂 選擇目標資料夾：", ["-- ➕ 新增資料夾 --"] + existing_folders)
    if folder_choice == "-- ➕ 新增資料夾 --":
        target_folder = st.text_input("輸入新資料夾名稱", "生化")
    else:
        target_folder = folder_choice
        
    uploaded_file = st.file_uploader(
        "上傳題目檔案 (支援任意頁數的 PDF、Word、PPTX、TXT)", 
        type=["pdf", "docx", "pptx", "txt", "md"]
    )
    
    default_name = uploaded_file.name if uploaded_file else ""
    custom_name = st.text_input("📄 編輯匯入後的考卷/講義名稱：", value=default_name)
    
    if st.button("🚀 開始全自動匯入", type="primary") and uploaded_file:
        api_key = get_api_key()
        if not api_key: 
            st.error("請先到「⚙️ 設定與管理」輸入 API Key！")
        else:
            fname = uploaded_file.name.lower()
            file_bytes = uploaded_file.getvalue()
            final_name = custom_name.strip() if custom_name else uploaded_file.name
            
            genai.configure(api_key=api_key)
            model = genai.GenerativeModel('gemini-3.8-flash')
            
            parse_prompt = (
                "請從所提供內容中提取出所有的「選擇題」（包含單選、多選、A~E 或 A~F 多個選項、是非題等）。\n"
                "【嚴格提取規範】\n"
                "1. text（題目）與 options（選項陣列）：完整保留原文（若為英文請保留英文，切勿翻譯題幹）。\n"
                "2. options：請務必完整收錄該題的所有選項（若有 5 個選項 A~E 請列出 5 個，切勿省略）。\n"
                "3. answer：若內容中有標明正解請直接填入，若無請推導出正確選項字母或文字。\n"
                "4. explanation：若內容本身有附解答說明則填入簡要說明，若無請留空字串 \"\" 即可。\n"
                "5. 若本頁面中「沒有任何選擇題」，請直接回傳空陣列 [] 即可。\n"
                "6. 格式：嚴格以標準 JSON 陣列輸出，不要包含任何額外說明文字。\n"
                '範例格式：[{"text":"What is...","options":["A","B","C","D","E"],"answer":"A","explanation":""}]'
            )

            # ---------------- 檔案自動分批處理邏輯 ----------------
            batches = []
            
            if fname.endswith('.pdf'):
                if not HAS_PYPDF:
                    st.error("⚠️ 尚未安裝 `pypdf` 套件！請在 GitHub 的 `requirements.txt` 新增一行 `pypdf` 後重試。")
                    st.stop()
                
                reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                total_pages = len(reader.pages)
                PAGES_PER_BATCH = 5  # 🌟 5 頁一組，10~15 秒完成，徹底杜絕 504
                
                st.write(f"📖 偵測到 PDF 共 **{total_pages}** 頁，系統自動拆分為 **{(total_pages + PAGES_PER_BATCH - 1) // PAGES_PER_BATCH}** 批進行安全解析...")
                
                for start_p in range(0, total_pages, PAGES_PER_BATCH):
                    end_p = min(start_p + PAGES_PER_BATCH, total_pages)
                    
                    # 測試前幾頁是否能直接萃取純文字
                    batch_text = ""
                    for p_i in range(start_p, end_p):
                        t = reader.pages[p_i].extract_text() or ""
                        if t.strip():
                            batch_text += f"\n--- Page {p_i+1} ---\n" + t
                    
                    if len(batch_text.strip()) > 100:
                        # 有純文字，直接送文字（速度極快，約 3 秒）
                        batches.append({
                            "title": f"第 {start_p+1} ~ {end_p} 頁 (文字)",
                            "content": [parse_prompt, f"【以下為檔案第 {start_p+1} ~ {end_p} 頁文字內容】：\n\n{batch_text}"]
                        })
                    else:
                        # 圖片型 PDF，切出 5 頁 sub-pdf 發送
                        writer = pypdf.PdfWriter()
                        for p_i in range(start_p, end_p):
                            writer.add_page(reader.pages[p_i])
                        sub_buf = io.BytesIO()
                        writer.write(sub_buf)
                        batches.append({
                            "title": f"第 {start_p+1} ~ {end_p} 頁 (圖片排版)",
                            "content": [parse_prompt, {"mime_type": "application/pdf", "data": sub_buf.getvalue()}]
                        })
            
            elif fname.endswith('.docx'):
                doc_text = extract_text_from_docx(file_bytes)
                if not doc_text.strip(): st.error("Word 文件內容空白！"); st.stop()
                batches.append({"title": "Word 全文", "content": [parse_prompt, f"【Word 內容】：\n\n{doc_text}"]})
                
            elif fname.endswith('.pptx'):
                ppt_text = extract_text_from_pptx(file_bytes)
                if not ppt_text.strip(): st.error("PowerPoint 內容空白！"); st.stop()
                batches.append({"title": "PPT 全文", "content": [parse_prompt, f"【PPT 內容】：\n\n{ppt_text}"]})
                
            else:
                try: txt = file_bytes.decode('utf-8')
                except UnicodeDecodeError: txt = file_bytes.decode('big5', errors='ignore')
                batches.append({"title": "文字全文", "content": [parse_prompt, f"【文字筆記】：\n\n{txt}"]})

            # ---------------- 開始逐批安全呼叫與存庫 ----------------
            prog_bar = st.progress(0.0)
            status_box = st.empty()
            total_batches = len(batches)
            total_imported = 0
            
            for b_idx, b_info in enumerate(batches):
                status_box.markdown(f"⏳ **正在處理 [{b_idx+1}/{total_batches}] {b_info['title']}**（目前已成功抓取 **{total_imported}** 題）...")
                
                # 自動重試循環
                max_retries = 3
                response = None
                for attempt in range(max_retries):
                    try:
                        response = model.generate_content(b_info['content'], request_options={"timeout": 120})
                        break
                    except Exception as e:
                        if "429" in str(e) and attempt < max_retries - 1:
                            status_box.warning(f"⏳ 遇 API 頻率限制，冷卻 60 秒後重試批次... ({attempt+1}/{max_retries})")
                            time.sleep(60)
                        else:
                            st.warning(f"⚠️ {b_info['title']} 解析超時或失敗，跳過此批次繼續下一批。錯誤：{e}")
                            break
                
                if response and response.text:
                    try:
                        raw_text = response.text.strip()
                        match = re.search(r'\[\s*\{.*\}\s*\]', raw_text, re.DOTALL)
                        clean_json = match.group(0) if match else raw_text.replace('```json', '').replace('```', '').strip()
                        chunk_questions = json.loads(clean_json) if clean_json and clean_json != "[]" else []
                        
                        for nq in chunk_questions:
                            raw_opts = nq.get('options', [])
                            cleaned_opts = [str(o).strip() for o in raw_opts if str(o).strip()]
                            opts_json = json.dumps(cleaned_opts, ensure_ascii=False)
                            o1 = cleaned_opts[0] if len(cleaned_opts) > 0 else ""
                            o2 = cleaned_opts[1] if len(cleaned_opts) > 1 else ""
                            o3 = cleaned_opts[2] if len(cleaned_opts) > 2 else ""
                            o4 = cleaned_opts[3] if len(cleaned_opts) > 3 else ""
                            
                            c.execute(
                                "INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                                (target_folder, final_name, nq.get('text', ''), o1, o2, o3, o4, opts_json, str(nq.get('answer', '')), nq.get('explanation', ''))
                            )
                        conn.commit()
                        total_imported += len(chunk_questions)
                    except Exception:
                        pass
                
                prog_bar.progress((b_idx + 1) / total_batches)
                time.sleep(1.5)  # 批次間短暫冷卻，防止觸發 API 頻率保護
            
            status_box.empty()
            prog_bar.empty()
            if total_imported > 0:
                st.success(f"🎉 恭喜！整份文件已全數分析完畢，共成功匯入 **{total_imported}** 題至「{target_folder} / {final_name}」！" + (" (已即時儲存至 Turso 雲端)" if IS_CLOUD else ""))
            else:
                st.warning("處理完畢，但未在檔案中找到符合標準格式的選擇題。")

# ---------- 【設定與管理區】 ----------
with tab_settings:
    st.subheader("🔑 API Key 設定")
    current_key = get_api_key()
    new_key = st.text_input("輸入 Gemini API Key", value=current_key, type="password")
    if st.button("儲存設定"):
        c.execute("REPLACE INTO settings (key, value) VALUES ('gemini_api_key', ?)", (new_key,))
        conn.commit()
        st.success("設定已儲存！" + (" (已同步至雲端)" if IS_CLOUD else ""))

    if not IS_CLOUD:
        st.divider()
        st.subheader("💾 本地備份與還原 (未設定 Turso 時可用)")
        col_dl, col_ul = st.columns(2)
        with col_dl:
            if os.path.exists('quiz_database.db'):
                with open('quiz_database.db', "rb") as fp:
                    st.download_button(
                        label="📥 下載題庫備份檔 (.db)",
                        data=fp,
                        file_name="quiz_database.db",
                        mime="application/x-sqlite3",
                        use_container_width=True
                    )
        with col_ul:
            restore_file = st.file_uploader("選取 .db檔案以還原", type=["db"], label_visibility="collapsed")
            if restore_file:
                with open('quiz_database.db', "wb") as f:
                    f.write(restore_file.getvalue())
                st.success("✅ 題庫已成功還原！重新整理頁面中...")
                time.sleep(1)
                st.rerun()

    st.divider()
    st.subheader("📁 題庫管理 (名稱修改 / 移動 / 星號標記 / 刪除)")
    try:
        c.execute("SELECT folder, category, MAX(pdf_starred) as pdf_starred FROM questions GROUP BY folder, category")
        items = c.fetchall()
    except Exception:
        items = []
    
    if not items:
        st.info("目前沒有題庫資料。")
    else:
        try:
            c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL")
            all_folders = [row['folder'] for row in c.fetchall() if row['folder']]
        except Exception:
            all_folders = []

        for index, row in enumerate(items):
            f_name = row['folder']
            p_name = row['category']
            is_pdf_st = bool(row['pdf_starred'])
            
            exp_title = f"{'⭐ ' if is_pdf_st else ''}📂 {f_name} ＞ 📄 {p_name}"
            with st.expander(exp_title):
                new_p_name = st.text_input("修改名稱", value=p_name, key=f"p_rename_{index}")
                target_f = st.selectbox("移動至資料夾", all_folders, index=all_folders.index(f_name) if f_name in all_folders else 0, key=f"f_move_{index}")
                
                col_save, col_star_pdf, col_del = st.columns(3)
                if col_save.button("💾 儲存變更", key=f"save_{index}", use_container_width=True):
                    c.execute("UPDATE questions SET category=?, folder=? WHERE folder=? AND category=?", 
                              (new_p_name, target_f, f_name, p_name))
                    c.execute("UPDATE exam_history SET category=? WHERE category=?", (new_p_name, p_name))
                    conn.commit()
                    st.success("✅ 更新成功！")
                    time.sleep(0.5)
                    st.rerun()
                    
                star_btn_txt = "⭐ 取消考卷星號" if is_pdf_st else "☆ 標記為星號考卷"
                if col_star_pdf.button(star_btn_txt, key=f"star_pdf_{index}", use_container_width=True):
                    new_p_star = 0 if is_pdf_st else 1
                    c.execute("UPDATE questions SET pdf_starred=? WHERE folder=? AND category=?", 
                              (new_p_star, f_name, p_name))
                    conn.commit()
                    st.rerun()
                    
                if col_del.button("🗑️ 刪除此卷", key=f"del_{index}", use_container_width=True):
                    c.execute("DELETE FROM questions WHERE folder=? AND category=?", (f_name, p_name))
                    c.execute("DELETE FROM exam_history WHERE category=?", (p_name,))
                    conn.commit()
                    st.success("✅ 已刪除！")
                    time.sleep(0.5)
                    st.rerun()

        st.divider()
        st.subheader("✏️ 資料夾重新命名")
        if all_folders:
            old_folder_name = st.selectbox("選擇要改名的資料夾", all_folders, key="rename_folder_select")
            new_folder_name = st.text_input("輸入新的資料夾名稱", value=old_folder_name, key="rename_folder_input")
            if st.button("確認修改資料夾名稱"):
                if new_folder_name.strip():
                    c.execute("UPDATE questions SET folder=? WHERE folder=?", (new_folder_name.strip(), old_folder_name))
                    conn.commit()
                    st.success(f"✅ 資料夾已更名為「{new_folder_name.strip()}」！")
                    time.sleep(0.5)
                    st.rerun()

    st.divider()
    st.subheader("📊 錯題排行榜")
    try:
        c.execute("SELECT folder, category, text, wrong_count FROM questions WHERE wrong_count > 0 ORDER BY wrong_count DESC LIMIT 10")
        stats = c.fetchall()
        if stats:
            for s in stats: 
                st.write(f"❌ 錯 **{s['wrong_count']}** 次 | [{s['folder']}] {s['text'][:20]}...")
        else:
            st.write("目前沒有錯題紀錄！")
    except Exception:
        st.write("目前沒有錯題紀錄！")
