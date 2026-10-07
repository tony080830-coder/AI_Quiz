import streamlit as st
import json
import random
import time
import os
import io
import re
import base64
import zipfile
import xml.etree.ElementTree as ET
from PIL import Image
import google.generativeai as genai

try:
    import pypdf
    HAS_PYPDF = True
except ImportError:
    HAS_PYPDF = False

# ================= 0. Turso 雲端 SQLite 連線設定 =================
TURSO_DB_URL = "libsql://quiz-db-tony080830-coder.aws-ap-northeast-1.turso.io"
TURSO_AUTH_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTEzNTg1MTMsImlkIjoiMDFhMGRiYTUtYmIwMS03MDkwLTgxM2UtNDUwODgwZGQ5MDdhIiwia2lkIjoiRG5HUXMycy13c0VfNkc5Szlnbms4cENlYWJ0NjZRcF9yUUhNYVU1aUhLSSIsInJpZCI6ImM0ZDI0Mjc1LTVmNzQtNGZkMi05M2Y5LTk1ZjRjMWExNWQ4MCJ9.1rcO6LCJ8u5RcH55d5OjbDTn8eBH4L62KQMmEZ5VpQvexK4A_rJmDFgEsDBmEBrT4hCJFcTBZAXpcLyUBnjIAA"

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
    is_cloud = False
    cloud_err = ""
    if TURSO_DB_URL.strip() and TURSO_AUTH_TOKEN.strip() and not TURSO_DB_URL.startswith("您的_") and not TURSO_AUTH_TOKEN.startswith("你的_"):
        try:
            import libsql_experimental as libsql
            raw_conn = libsql.connect(TURSO_DB_URL.strip(), auth_token=TURSO_AUTH_TOKEN.strip())
            test_cur = raw_conn.cursor()
            test_cur.execute("SELECT 1")
            test_cur.fetchall()
            is_cloud = True
            wrapper = DBWrapper(raw_conn, is_cloud=True)
            return wrapper, is_cloud, cloud_err
        except Exception as e:
            cloud_err = str(e)
            import sqlite3
            raw_conn = sqlite3.connect('quiz_database.db', check_same_thread=False)
            return DBWrapper(raw_conn, is_cloud=False), False, cloud_err
    else:
        import sqlite3
        raw_conn = sqlite3.connect('quiz_database.db', check_same_thread=False)
        return DBWrapper(raw_conn, is_cloud=False), False, cloud_err

conn, IS_CLOUD, CLOUD_ERROR = init_connection()
c = conn.cursor()

# 🌟 資料庫結構升級（英中雙語儲存）
if '_db_schema_ready' not in st.session_state:
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
        for col_name in ['explanation', 'folder', 'is_starred', 'pdf_starred', 'options', 'explanation_image', 'text_en', 'options_en', 'text_zh', 'options_zh']:
            if col_name not in existing_cols:
                col_type = "INTEGER DEFAULT 0" if col_name in ['is_starred', 'pdf_starred'] else "TEXT DEFAULT ''"
                c.execute(f"ALTER TABLE questions ADD COLUMN {col_name} {col_type}")
        conn.commit()
        st.session_state['_db_schema_ready'] = True
    except Exception:
        pass

def get_cached_folders():
    if 'cached_folders' not in st.session_state or st.session_state['cached_folders'] is None:
        try:
            c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL AND folder != ''")
            raw_rows = c.fetchall()
            result = []
            for r in raw_rows:
                val = r[0] if isinstance(r, (tuple, list)) else (r.get('folder') if isinstance(r, dict) else r['folder'])
                if val and str(val).strip():
                    result.append(str(val).strip())
            st.session_state['cached_folders'] = sorted(list(set(result)))
        except Exception:
            st.session_state['cached_folders'] = []
    return st.session_state['cached_folders']

def get_cached_api_key():
    if 'cached_api_key' not in st.session_state or st.session_state['cached_api_key'] is None:
        try:
            c.execute("SELECT value FROM settings WHERE key='gemini_api_key'")
            res = c.fetchone()
            st.session_state['cached_api_key'] = res['value'] if res else ""
        except Exception:
            st.session_state['cached_api_key'] = ""
    return st.session_state['cached_api_key']

def get_categories_by_folder(folder=None, only_star=False):
    try:
        query = "SELECT DISTINCT category, pdf_starred FROM questions WHERE 1=1"
        params = []
        if folder and folder != "全部資料夾":
            query += " AND folder=?"
            params.append(folder)
        if only_star:
            query += " AND pdf_starred=1"
        c.execute(query, tuple(params))
        rows = c.fetchall()
        pdf_list = []
        star_map = {}
        for r in rows:
            cat = r[0] if isinstance(r, (tuple, list)) else (r.get('category') if isinstance(r, dict) else r['category'])
            star = r[1] if isinstance(r, (tuple, list)) and len(r) > 1 else (r.get('pdf_starred', 0) if isinstance(r, dict) else r.get('pdf_starred', 0))
            if cat and str(cat).strip():
                c_str = str(cat).strip()
                pdf_list.append(c_str)
                star_map[c_str] = bool(star)
        return sorted(list(set(pdf_list))), star_map
    except Exception:
        return [], {}

def compress_image_to_base64(uploaded_file, max_size=(800, 800), quality=70):
    try:
        img = Image.open(uploaded_file)
        if img.mode in ("RGBA", "P"):
            img = img.convert("RGB")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        encoded = base64.b64encode(buf.getvalue()).decode('utf-8')
        return f"data:image/jpeg;base64,{encoded}"
    except Exception:
        encoded = base64.b64encode(uploaded_file.getvalue()).decode('utf-8')
        return f"data:{uploaded_file.type};base64,{encoded}"

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

def is_primarily_english(text):
    zh_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    en_words = len(re.findall(r'[a-zA-Z]{2,}', text))
    return en_words >= zh_chars

# 🌟 雙語切換處理引擎
def get_question_bilingual(q, target_lang="en"):
    orig_text = q.get('text', '')
    orig_opts = get_question_options(q)
    is_orig_en = is_primarily_english(orig_text)

    if target_lang == "en" and is_orig_en:
        return orig_text, orig_opts
    if target_lang == "zh" and not is_orig_en:
        return orig_text, orig_opts

    col_t = "text_en" if target_lang == "en" else "text_zh"
    col_o = "options_en" if target_lang == "en" else "options_zh"
    if q.get(col_t) and str(q[col_t]).strip():
        try:
            cached_opts = json.loads(q[col_o]) if q.get(col_o) else orig_opts
            return q[col_t], cached_opts
        except Exception:
            return q[col_t], orig_opts

    api_key = get_cached_api_key()
    if not api_key:
        return orig_text, orig_opts

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-3.8-flash')
        lang_target_desc = "專業醫學英文 (USMLE English)" if target_lang == "en" else "繁體中文（台灣醫學常用術語）"
        prompt = (
            f"請將以下醫學/生化選擇題翻譯為【{lang_target_desc}】。\n"
            f"題目：{orig_text}\n"
            f"選項：{orig_opts}\n\n"
            "【規範】以標準 JSON 格式輸出：\n"
            '{"text": "翻譯後的題目", "options": ["選項1", "選項2", ...]}'
        )
        resp = model.generate_content(prompt, request_options={"timeout": 45})
        raw = resp.text.strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        clean_json = match.group(0) if match else raw.replace('```json', '').replace('```', '').strip()
        parsed = json.loads(clean_json)
        
        trans_text = parsed.get('text', orig_text)
        trans_opts = parsed.get('options', orig_opts)
        
        c.execute(f"UPDATE questions SET {col_t}=?, {col_o}=? WHERE id=?", 
                  (trans_text, json.dumps(trans_opts, ensure_ascii=False), q['id']))
        conn.commit()
        q[col_t] = trans_text
        q[col_o] = json.dumps(trans_opts, ensure_ascii=False)
        return trans_text, trans_opts
    except Exception:
        return orig_text, orig_opts

def extract_text_from_docx(file_bytes):
    try:
        with zipfile.ZipFile(io.BytesIO(file_bytes)) as z:
            if 'word/document.xml' not in z.namelist(): return ""
            xml_content = z.read('word/document.xml')
            tree = ET.fromstring(xml_content)
            paragraphs = []
            for node in tree.iter():
                if node.tag.endswith('p'):
                    texts = [child.text for child in node.iter() if child.tag.endswith('t') and child.text]
                    if texts: paragraphs.append("".join(texts))
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
                        if texts: slide_paragraphs.append("".join(texts))
                if slide_paragraphs:
                    slides_text.append(f"--- Slide {idx} ---\n" + "\n".join(slide_paragraphs))
            return "\n\n".join(slides_text)
    except Exception as e:
        return f"[PPT 提取錯誤: {e}]"

# ================= 1. 測驗狀態管理 =================
if 'exam_active' not in st.session_state: st.session_state.exam_active = False
if 'exam_finished' not in st.session_state: st.session_state.exam_finished = False
if 'exam_questions' not in st.session_state: st.session_state.exam_questions = []
if 'exam_index' not in st.session_state: st.session_state.exam_index = 0
if 'exam_user_answers' not in st.session_state: st.session_state.exam_user_answers = {}
if 'exam_tested_pdfs' not in st.session_state: st.session_state.exam_tested_pdfs = []
if 'exam_start_time' not in st.session_state: st.session_state.exam_start_time = 0.0
if 'exam_time_limit_sec' not in st.session_state: st.session_state.exam_time_limit_sec = 0
if 'exam_total_time_str' not in st.session_state: st.session_state.exam_total_time_str = ""
if 'exam_wrong_ids' not in st.session_state: st.session_state.exam_wrong_ids = []
if 'exam_correct_ids' not in st.session_state: st.session_state.exam_correct_ids = []
if 'exam_lang_mode' not in st.session_state: st.session_state.exam_lang_mode = "en"
if 'exam_lang_overrides' not in st.session_state: st.session_state.exam_lang_overrides = {}
if 'ai_generated_temp' not in st.session_state: st.session_state.ai_generated_temp = []

def check_is_correct(chosen_idx, q_item):
    orig_opts = get_question_options(q_item)
    if chosen_idx is None or chosen_idx < 0 or chosen_idx >= len(orig_opts):
        return False
    chosen_str = orig_opts[chosen_idx].strip()
    correct_ans = str(q_item['answer']).strip()
    if chosen_str == correct_ans: return True
    opt_letter = chr(65 + chosen_idx) if chosen_idx < 26 else ""
    if correct_ans.upper() == opt_letter: return True
    if len(correct_ans) == 1 and chosen_str.startswith(correct_ans): return True
    if len(chosen_str) == 1 and correct_ans.startswith(chosen_str): return True
    return False

# ================= 2. 網頁介面開始 =================
st.set_page_config(page_title="AI 錯題本", page_icon="📝", layout="centered")
st.title("📝 AI 專屬錯題本系統")

if IS_CLOUD:
    st.success("☁️ 連線狀態：已成功連線至 Turso 雲端資料庫！(重開機資料永不丟失)")
else:
    if CLOUD_ERROR:
        st.error(f"⚠️ Turso 雲端連線失敗原因：`{CLOUD_ERROR}`")
    else:
        st.caption("🖥️ 連線狀態：本地暫存模式 (請在 app.py 填寫 Turso URL 與 Token)")

tab_quiz, tab_review, tab_ai_gen, tab_import, tab_settings = st.tabs([
    "🎯 開始測驗", "📖 錯題總覽", "🧠 AI 仿題出題", "📥 匯入題庫", "⚙️ 設定與管理"
])

# ---------- 【測驗區】 ----------
with tab_quiz:
    if not st.session_state.exam_active and not st.session_state.exam_finished:
        folders = get_cached_folders()
        if not folders:
            st.warning("題庫空空如也，請先到「匯入題庫」上傳考卷或簡報檔案！")
        else:
            col_f, col_st = st.columns([2, 1])
            with col_f:
                selected_folder = st.selectbox("📁 篩選資料夾：", ["全部資料夾"] + folders, key="quiz_folder")
            with col_st:
                st.write("")
                only_star_pdf = st.checkbox("⭐ 僅列星號考卷", value=False)
                only_star_q = st.checkbox("⭐ 只刷星號題目", value=False)

            all_available_pdfs, pdf_star_map = get_categories_by_folder(selected_folder, only_star_pdf)

            selected_pdfs = st.multiselect(
                "📄 勾選要練習的考卷 / 講義（可複選混合出題）：",
                options=all_available_pdfs,
                default=all_available_pdfs[:1] if all_available_pdfs else [],
                format_func=lambda x: f"⭐ {x}" if pdf_star_map.get(x) else x
            )

            col_order, col_cnt, col_lang = st.columns([1.5, 1.2, 1.3])
            with col_order:
                q_order = st.selectbox("🔀 出題模式：", ["隨機挑題（模擬考試）", "照考卷順序（循序練習）"])
            with col_cnt:
                # 🌟 加入 40 題選項
                q_count_option = st.selectbox("⏱️ 練習題數：", ["10 題", "20 題", "30 題", "40 題", "50 題", "自訂題數", "全部題目"], index=3)
            with col_lang:
                default_lang_choice = st.selectbox("🌐 預設題目語言：", ["🇺🇸 英文版 (考試專用)", "🇹🇼 中文版"])

            custom_pick_n = 40
            if q_count_option == "自訂題數":
                custom_pick_n = st.number_input("輸入想練習的題數：", min_value=1, value=40, step=1)

            col_time_type, col_time_min = st.columns(2)
            with col_time_type:
                timer_type = st.selectbox("⏳ 計時方式：", ["正數碼表（無時間限制）", "倒數計時（限時考試）"])
            with col_time_min:
                if timer_type.startswith("倒數"):
                    time_limit_min = st.selectbox("選擇時限：", [10, 15, 20, 30, 40, 50, 60, 90], index=4, format_func=lambda x: f"{x} 分鐘")
                else:
                    time_limit_min = 0
                    st.caption("💡 將從 00:00 開始記錄作答時間。")

            start_q_num = 1
            if q_order.startswith("照考卷"):
                start_q_num = st.number_input("📍 從第幾題開始做？（中斷接續）：", min_value=1, value=1, step=1)

            if st.button("🚀 開始全真測驗", type="primary", use_container_width=True):
                if not selected_pdfs:
                    st.error("請至少勾選一份考卷或講義！")
                else:
                    placeholders = ','.join(['?'] * len(selected_pdfs))
                    query = f"SELECT * FROM questions WHERE category IN ({placeholders})"
                    params = list(selected_pdfs)
                    if only_star_q:
                        query += " AND is_starred=1"
                    
                    if q_order.startswith("照考卷"):
                        query += " ORDER BY category ASC, id ASC"
                    else:
                        query += " ORDER BY id ASC"

                    c.execute(query, tuple(params))
                    q_pool = c.fetchall()

                    if not q_pool:
                        st.warning("所選條件下無任何題目！")
                    else:
                        q_pool_dicts = [dict(q) for q in q_pool]
                        total_found = len(q_pool_dicts)
                        
                        if q_order.startswith("隨機"):
                            random.shuffle(q_pool_dicts)
                            if q_count_option == "全部題目":
                                st.session_state.exam_questions = q_pool_dicts
                            elif q_count_option == "自訂題數":
                                st.session_state.exam_questions = q_pool_dicts[:min(int(custom_pick_n), total_found)]
                            else:
                                n = int(q_count_option.replace(" 題", ""))
                                st.session_state.exam_questions = q_pool_dicts[:min(n, total_found)]
                            st.session_state.exam_index = 0
                        else:
                            start_idx = max(0, min(int(start_q_num) - 1, total_found - 1))
                            if q_count_option == "全部題目":
                                st.session_state.exam_questions = q_pool_dicts
                                st.session_state.exam_index = start_idx
                            elif q_count_option == "自訂題數":
                                st.session_state.exam_questions = q_pool_dicts[start_idx : start_idx + int(custom_pick_n)]
                                st.session_state.exam_index = 0
                            else:
                                n = int(q_count_option.replace(" 題", ""))
                                st.session_state.exam_questions = q_pool_dicts[start_idx : start_idx + n]
                                st.session_state.exam_index = 0

                        st.session_state.exam_user_answers = {}
                        st.session_state.exam_tested_pdfs = selected_pdfs
                        st.session_state.exam_active = True
                        st.session_state.exam_finished = False
                        st.session_state.exam_start_time = time.time()
                        st.session_state.exam_time_limit_sec = time_limit_min * 60
                        st.session_state.exam_lang_mode = "en" if default_lang_choice.startswith("🇺🇸") else "zh"
                        st.session_state.exam_lang_overrides = {}
                        st.rerun()

    # ---------------- 測驗進行中 ----------------
    elif st.session_state.exam_active and not st.session_state.exam_finished:
        total_q = len(st.session_state.exam_questions)
        idx = st.session_state.exam_index
        curr_q = st.session_state.exam_questions[idx]
        q_id = curr_q['id']

        curr_lang = st.session_state.exam_lang_overrides.get(q_id, st.session_state.exam_lang_mode)
        disp_text, disp_options = get_question_bilingual(curr_q, curr_lang)

        elapsed_sec = int(time.time() - st.session_state.exam_start_time)
        m, s = divmod(elapsed_sec, 60)
        time_display = f"⏱️ 已用時：{m:02d}:{s:02d}"
        if st.session_state.exam_time_limit_sec > 0:
            remaining = st.session_state.exam_time_limit_sec - elapsed_sec
            if remaining <= 0:
                time_display = "🚨 時間到！請立即交卷"
            else:
                rm, rs = divmod(remaining, 60)
                time_display = f"⏳ 剩餘：{rm:02d}:{rs:02d}"

        answered_count = len(st.session_state.exam_user_answers)
        st.progress((idx + 1) / total_q)
        
        c_hdr1, c_hdr2, c_hdr3 = st.columns([1.8, 1.2, 1.2])
        c_hdr1.markdown(f"**第 {idx + 1} / {total_q} 題**（已答：{answered_count}/{total_q}）")
        c_hdr2.caption(time_display)
        with c_hdr3:
            if st.button("🏁 交卷並結算", type="primary", use_container_width=True):
                tot_elapsed = int(time.time() - st.session_state.exam_start_time)
                em, es = divmod(tot_elapsed, 60)
                st.session_state.exam_total_time_str = f"{em} 分 {es} 秒"
                
                curr_wrong = []
                curr_correct = []
                for q_item in st.session_state.exam_questions:
                    qid = q_item['id']
                    user_chosen_idx = st.session_state.exam_user_answers.get(qid, None)
                    if user_chosen_idx is not None and check_is_correct(user_chosen_idx, q_item):
                        curr_correct.append(qid)
                    else:
                        curr_wrong.append(qid)
                
                st.session_state.exam_wrong_ids = curr_wrong
                st.session_state.exam_correct_ids = curr_correct
                
                try:
                    c.execute("INSERT INTO exam_history (category, wrong_ids, correct_ids) VALUES (?, ?, ?)",
                              (", ".join(st.session_state.exam_tested_pdfs), json.dumps(curr_wrong), json.dumps(curr_correct)))
                    for qid in curr_wrong:
                        c.execute("UPDATE questions SET wrong_count = wrong_count + 1 WHERE id=?", (qid,))
                    for qid in curr_correct:
                        c.execute("UPDATE questions SET wrong_count = MAX(0, wrong_count - 1) WHERE id=?", (qid,))
                    conn.commit()
                except Exception:
                    pass
                
                st.session_state.exam_active = False
                st.session_state.exam_finished = True
                st.rerun()

        with st.expander("📋 題目導航盤（點擊快速跳至該題）", expanded=False):
            cols = st.columns(10)
            for i, q_it in enumerate(st.session_state.exam_questions):
                col_target = cols[i % 10]
                has_ans = q_it['id'] in st.session_state.exam_user_answers
                q_btn_label = f"🟢 {i+1}" if has_ans else f"⚪ {i+1}"
                if col_target.button(q_btn_label, key=f"nav_q_{i}", use_container_width=True):
                    st.session_state.exam_index = i
                    st.rerun()

        st.divider()

        col_t, col_lang_btn, col_s = st.columns([3.6, 1.4, 1])
        is_q_st = bool(curr_q.get('is_starred', 0))
        with col_t:
            st.subheader(f"Q{idx + 1}. {disp_text}")
        with col_lang_btn:
            # 🌟 隨時一鍵切換英文或中文
            toggle_lbl = "🇹🇼 翻成中文" if curr_lang == "en" else "🇺🇸 切換英文"
            if st.button(toggle_lbl, key=f"lang_btn_{q_id}_{idx}", use_container_width=True):
                st.session_state.exam_lang_overrides[q_id] = "zh" if curr_lang == "en" else "en"
                st.rerun()
        with col_s:
            if st.button("⭐ 已收藏" if is_q_st else "☆ 收藏", key=f"ex_star_{curr_q['id']}", use_container_width=True):
                new_star = 0 if is_q_st else 1
                c.execute("UPDATE questions SET is_starred=? WHERE id=?", (new_star, curr_q['id']))
                conn.commit()
                curr_q['is_starred'] = new_star
                st.rerun()

        current_chosen_idx = st.session_state.exam_user_answers.get(q_id, None)

        st.write("請選擇你的答案：")
        for opt_idx, opt_text in enumerate(disp_options):
            is_chosen = (current_chosen_idx == opt_idx)
            btn_txt = f"👉 【已選】{opt_text}" if is_chosen else f"  {opt_text}"
            btn_style = "primary" if is_chosen else "secondary"
            if st.button(btn_txt, key=f"opt_btn_{q_id}_{opt_idx}", type=btn_style, use_container_width=True):
                st.session_state.exam_user_answers[q_id] = opt_idx
                st.rerun()

        st.write("")
        col_prev, col_clear, col_next = st.columns([1.5, 1, 1.5])
        with col_prev:
            if st.button("⬅️ 上一題", disabled=(idx == 0), use_container_width=True):
                st.session_state.exam_index = max(0, idx - 1)
                st.rerun()
        with col_clear:
            if current_chosen_idx is not None:
                if st.button("🗑️ 取消本題選擇", use_container_width=True):
                    st.session_state.exam_user_answers.pop(q_id, None)
                    st.rerun()
        with col_next:
            if st.button("➡️ 下一題", disabled=(idx >= total_q - 1), use_container_width=True):
                st.session_state.exam_index = min(total_q - 1, idx + 1)
                st.rerun()

    # ---------------- 結算成果頁 ----------------
    elif st.session_state.exam_finished:
        st.balloons()
        st.success("🎉 測驗完成！成績統計如下：")

        curr_wrong = st.session_state.exam_wrong_ids
        curr_correct = st.session_state.exam_correct_ids
        total_questions = len(st.session_state.exam_questions)
        answered_cnt = len(st.session_state.exam_user_answers)
        unanswered_cnt = total_questions - answered_cnt
        score = (len(curr_correct) / total_questions * 100) if total_questions > 0 else 0

        col_m1, col_m2, col_m3, col_m4, col_m5 = st.columns(5)
        col_m1.metric("最終得分", f"{score:.1f} 分")
        col_m2.metric("🟢 答對題數", f"{len(curr_correct)} 題")
        col_m3.metric("🔴 答錯題數", f"{len(curr_wrong) - unanswered_cnt} 題")
        col_m4.metric("⚪ 未作答", f"{unanswered_cnt} 題")
        col_m5.metric("⏱️ 總耗時", st.session_state.exam_total_time_str)

        st.divider()

        rev_filter = st.radio("檢討題目範圍：", ["全部題目", "只看錯題與未作答", "只看答對題目"], horizontal=True)

        if curr_wrong:
            if st.button("⭐ 一鍵將本次答錯題目加入星號收藏", type="primary"):
                placeholders = ','.join(['?'] * len(curr_wrong))
                c.execute(f"UPDATE questions SET is_starred=1 WHERE id IN ({placeholders})", tuple(curr_wrong))
                conn.commit()
                st.toast("✅ 本次錯題已全部加入星號收藏！")

        for i, q_data in enumerate(st.session_state.exam_questions, 1):
            qid = q_data['id']
            user_chosen_idx = st.session_state.exam_user_answers.get(qid, None)
            is_right = check_is_correct(user_chosen_idx, q_data)

            if rev_filter == "只看錯題與未作答" and is_right: continue
            if rev_filter == "只看答對題目" and not is_right: continue

            rev_lang = st.session_state.exam_lang_overrides.get(qid, st.session_state.exam_lang_mode)
            q_txt, q_opts = get_question_bilingual(q_data, rev_lang)

            status_icon = "🟢" if is_right else ("⚪ 未作答" if user_chosen_idx is None else "🔴")
            with st.expander(f"{status_icon} 第 {i} 題：{q_txt[:35]}..."):
                c_q_head, c_q_lang = st.columns([4, 1.2])
                with c_q_head:
                    st.markdown(f"**【完整題目】**：{q_txt}")
                with c_q_lang:
                    toggle_rev_lbl = "🇹🇼 翻成中文" if rev_lang == "en" else "🇺🇸 切換英文"
                    if st.button(toggle_rev_lbl, key=f"rev_lang_btn_{qid}_{i}"):
                        st.session_state.exam_lang_overrides[qid] = "zh" if rev_lang == "en" else "en"
                        st.rerun()

                for opt_idx, opt_text in enumerate(q_opts):
                    is_this_ans = check_is_correct(opt_idx, q_data)
                    is_this_user = (user_chosen_idx == opt_idx)
                    if is_this_ans:
                        st.markdown(f"- **:green[✅ {opt_text} （正確答案）]**")
                    elif is_this_user:
                        st.markdown(f"- **:red[❌ {opt_text} （你的選擇）]**")
                    else:
                        st.markdown(f"- {opt_text}")
                
                if user_chosen_idx is None:
                    st.caption("⚠️ 本題未作答")

                exp = q_data.get('explanation', '') if q_data.get('explanation') else "尚未生成詳解"
                st.info(f"💡 解析：{exp}")
                if q_data.get('explanation_image'):
                    st.image(q_data['explanation_image'], caption="📸 筆記/解題截圖", use_container_width=True)

        if st.button("🔄 繼續新的測驗 / 重新開始", use_container_width=True):
            st.session_state.exam_active = False
            st.session_state.exam_finished = False
            st.session_state.exam_questions = []
            st.session_state.exam_user_answers = {}
            st.session_state.exam_index = 0
            st.rerun()

# ---------- 【錯題總覽區】 ----------
with tab_review:
    if st.session_state.exam_active or st.session_state.exam_finished:
        st.info("⚡ 測驗或查看報告中，背景查詢已自動暫停以確保刷題零卡頓。完成後點擊「重新開始」即可查閱完整題庫。")
    else:
        st.markdown("### 📖 各考卷 / 講義題目與解析總覽")
        folders = get_cached_folders()
        if not folders:
            st.info("目前沒有題庫資料。")
        else:
            col_f, col_p, col_st = st.columns([1.5, 1.5, 1])
            with col_f:
                rev_folder = st.selectbox("📂 選擇資料夾：", ["全部資料夾"] + folders, key="rev_folder")
            with col_p:
                rev_pdfs, rev_star_map = get_categories_by_folder(rev_folder)
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
                    is_st = bool(q.get('is_starred', 0))
                    star_tag = "⭐ " if is_st else ""
                    q_opts = get_question_options(q)
                    with st.expander(f"{star_tag}題目 {idx+1}: {q['text'][:30]}... (錯 {q['wrong_count']} 次)"):
                        st.markdown(f"**【題目】** {q['text']}")
                        for opt_i, opt_text in enumerate(q_opts):
                            opt_label = chr(65 + opt_i) if opt_i < 26 else str(opt_i + 1)
                            st.markdown(f"- ({opt_label}) {opt_text}")
                        st.markdown(f"✅ **正確答案**：`{q['answer']}`")
                        exp_text = q['explanation'] if q.get('explanation') and q['explanation'].strip() and q['explanation'] != '無提供詳解' else "尚未填寫詳解"
                        st.markdown(f"💡 **解析**：{exp_text}")
                        if q.get('explanation_image'):
                            st.image(q['explanation_image'], caption="📸 解題筆記/截圖", use_container_width=True)
                        st.markdown(f"📌 **星號狀態**：{'⭐ 已收藏' if is_st else '☆ 未收藏'}")

                        with st.expander("✏️ 編輯此題詳解 / 更新筆記截圖"):
                            rev_exp_input = st.text_area("修改文字解析：", value=q['explanation'] if q.get('explanation') and q['explanation'] != '無提供詳解' else "", key=f"rev_txt_{q['id']}")
                            rev_img_input = st.file_uploader("更換筆記截圖 (PNG, JPG)", type=["png", "jpg", "jpeg"], key=f"rev_img_{q['id']}")
                            col_sv_b, col_rm_img = st.columns(2)
                            with col_sv_b:
                                if st.button("💾 儲存修改", key=f"rev_save_btn_{q['id']}", use_container_width=True):
                                    new_img_b64 = q.get('explanation_image', '')
                                    if rev_img_input:
                                        new_img_b64 = compress_image_to_base64(rev_img_input)
                                    c.execute("UPDATE questions SET explanation=?, explanation_image=? WHERE id=?", (rev_exp_input.strip(), new_img_b64, q['id']))
                                    conn.commit()
                                    st.toast("✅ 詳解已極速更新！")
                                    st.rerun()
                            with col_rm_img:
                                if st.button("🗑️ 清除既有截圖", key=f"rev_rm_img_{q['id']}", use_container_width=True):
                                    c.execute("UPDATE questions SET explanation_image='' WHERE id=?", (q['id'],))
                                    conn.commit()
                                    st.toast("✅ 筆記截圖已移除！")
                                    st.rerun()

# ---------- 【全新：🧠 AI 仿題出題區】 ----------
with tab_ai_gen:
    st.markdown("### 🧠 AI 模擬出題（從特定 PDF/講義深度模仿）")
    st.caption("AI 會分析您所選 PDF 的核心機轉與出題邏輯，生成全新且不重複的全英文 USMLE / 國考級模擬題！")

    all_gen_folders = get_cached_folders()
    if not all_gen_folders:
        st.info("題庫內目前尚無講義，請先前往「📥 匯入題庫」上傳 PDF！")
    else:
        col_g1, col_g2 = st.columns(2)
        with col_g1:
            gen_folder = st.selectbox("📂 選擇範本所屬資料夾：", all_gen_folders, key="gen_folder_sel")
        with col_g2:
            gen_pdfs, _ = get_categories_by_folder(gen_folder)
            gen_target_pdf = st.selectbox("📄 選擇模仿範本考卷/講義：", gen_pdfs, key="gen_pdf_sel")

        col_g_n, col_g_diff = st.columns(2)
        with col_g_n:
            gen_q_count = st.selectbox("🎯 欲生成的題目數量：", [3, 5, 8, 10], index=1, format_func=lambda x: f"{x} 題")
        with col_g_diff:
            gen_diff = st.selectbox("🎓 題目風格難度：", ["USMLE Step 1 / 醫學院期中考全英風格", "國考臨床結合概念題", "基礎生化代謝途徑專題"])

        if st.button("✨ 立即分析該講義並生成全新仿題", type="primary", use_container_width=True):
            api_key = get_cached_api_key()
            if not api_key:
                st.error("請先前往「⚙️ 設定與管理」輸入 Gemini API Key！")
            elif not gen_target_pdf:
                st.error("請選取要模仿的考卷/講義！")
            else:
                # 從該 PDF 抽樣 6~8 題作為範本
                c.execute("SELECT text, options, answer FROM questions WHERE category=? ORDER BY RANDOM() LIMIT 8", (gen_target_pdf,))
                sample_rows = c.fetchall()
                if not sample_rows:
                    st.error("該 PDF 下尚未抓到足夠的題目範本，請先確認是否已成功匯入！")
                else:
                    sample_texts = []
                    for idx_s, s in enumerate(sample_rows, 1):
                        opts_str = ", ".join(get_question_options(s))
                        sample_texts.append(f"Sample {idx_s}:\nQuestion: {s['text']}\nOptions: {opts_str}\nKey Answer: {s['answer']}")
                    sample_context = "\n\n".join(sample_texts)

                    with st.spinner(f"AI 正在精讀「{gen_target_pdf}」考點並撰寫 {gen_q_count} 道全英文高階仿題..."):
                        try:
                            genai.configure(api_key=api_key)
                            model = genai.GenerativeModel('gemini-3.8-flash')
                            ai_prompt = (
                                f"You are a leading medical professor and examination board question creator. "
                                f"Analyze the following real exam questions from the lecture '{gen_target_pdf}':\n\n"
                                f"【SAMPLE QUESTIONS FROM LECTURE】:\n{sample_context}\n\n"
                                f"【TASK】:\n"
                                f"Generate {gen_q_count} BRAND-NEW, UNIQUE, high-yield multiple-choice questions in ENGLISH. "
                                f"Style/Difficulty: {gen_diff}.\n"
                                f"【RULES】:\n"
                                f"1. Do NOT duplicate the sample questions word-for-word. Synthesize clinical vignettes or biochemical mechanisms testing the exact same conceptual topics.\n"
                                f"2. Options MUST be in English, listing 4 or 5 plausible choices (A-D or A-E).\n"
                                f"3. Provide the single best correct answer string or letter.\n"
                                f"4. Provide a thorough, high-yield explanation written in Traditional Chinese (繁體中文台灣醫學用語) explaining the mechanism and why wrong choices are incorrect.\n"
                                f"5. Strictly output a standard JSON array of objects without Markdown wrappers:\n"
                                f'[{{\n'
                                f'  "text": "A 45-year-old male presents with...",\n'
                                f'  "options": ["A. Choice 1", "B. Choice 2", "C. Choice 3", "D. Choice 4", "E. Choice 5"],\n'
                                f'  "answer": "A",\n'
                                f'  "explanation": "繁體中文核心考點機轉詳解..."\n'
                                f'}}]\n'
                            )
                            resp = model.generate_content(ai_prompt, request_options={"timeout": 120})
                            raw = resp.text.strip()
                            match = re.search(r'\[\s*\{.*\}\s*\]', raw, re.DOTALL)
                            clean_json = match.group(0) if match else raw.replace('```json', '').replace('```', '').strip()
                            st.session_state.ai_generated_temp = json.loads(clean_json)
                            st.toast("🎉 AI 仿題已生成完畢！請在下方檢視確認。")
                        except Exception as e:
                            st.error(f"AI 模擬出題失敗：{e}")

        # 預覽與一鍵入庫
        if st.session_state.ai_generated_temp:
            st.divider()
            st.subheader(f"📋 模擬仿題預覽（共 {len(st.session_state.ai_generated_temp)} 題）")
            
            for g_i, g_q in enumerate(st.session_state.ai_generated_temp, 1):
                with st.expander(f"仿題 {g_i}：{g_q.get('text', '')[:40]}...", expanded=True):
                    st.markdown(f"**【英文題幹】**：{g_q.get('text', '')}")
                    for o in g_q.get('options', []):
                        st.markdown(f"- {o}")
                    st.markdown(f"✅ **正解**：`{g_q.get('answer', '')}`")
                    st.caption(f"💡 解析：{g_q.get('explanation', '')}")

            save_cat_name = f"[AI仿題] {gen_target_pdf}"
            col_save_btn, col_clear_btn = st.columns([3, 1])
            with col_save_btn:
                if st.button(f"📥 一鍵將這 {len(st.session_state.ai_generated_temp)} 題加入題庫（保存至 {gen_folder} ＞ {save_cat_name}）", type="primary", use_container_width=True):
                    for g_item in st.session_state.ai_generated_temp:
                        opts_clean = [str(x).strip() for x in g_item.get('options', []) if str(x).strip()]
                        opts_json = json.dumps(opts_clean, ensure_ascii=False)
                        o1 = opts_clean[0] if len(opts_clean) > 0 else ""
                        o2 = opts_clean[1] if len(opts_clean) > 1 else ""
                        o3 = opts_clean[2] if len(opts_clean) > 2 else ""
                        o4 = opts_clean[3] if len(opts_clean) > 3 else ""
                        
                        c.execute(
                            "INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred, text_en, options_en) VALUES (?,?,?,?,?,?,?,?,?,?,0,0,?,?)",
                            (gen_folder, save_cat_name, g_item.get('text', ''), o1, o2, o3, o4, opts_json, str(g_item.get('answer', '')), g_item.get('explanation', ''), g_item.get('text', ''), opts_json)
                        )
                    conn.commit()
                    st.session_state['cached_folders'] = None
                    st.session_state.ai_generated_temp = []
                    st.success(f"🎉 成功存入雲端！現在即可前往「🎯 開始測驗」勾選「{save_cat_name}」進行全真練習！")
                    st.rerun()
            with col_clear_btn:
                if st.button("🗑️ 清除預覽", use_container_width=True):
                    st.session_state.ai_generated_temp = []
                    st.rerun()

# ---------- 【匯入區 (測驗進行中休眠)】 ----------
with tab_import:
    if st.session_state.exam_active or st.session_state.exam_finished:
        st.info("⚡ 測驗或查看報告中，匯入面板已自動休眠以保持刷題零卡頓。")
    else:
        st.markdown("### 🤖 智慧題庫匯入")
        existing_folders = get_cached_folders()
        
        folder_choice = st.selectbox("📂 選擇目標資料夾：", ["-- ➕ 新增資料夾 --"] + existing_folders)
        if folder_choice == "-- ➕ 新增資料夾 --":
            target_folder = st.text_input("輸入新資料夾名稱", "生化")
        else:
            target_folder = folder_choice
            
        import_mode = st.radio("選擇匯入方式：", ["📁 模式一：檔案直接上傳 (PDF, Word, PPTX, TXT)", "📋 模式二：直接貼上題目純文字 (快速補抓少量題目)"])
        
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

        if import_mode.startswith("📁 模式一"):
            uploaded_file = st.file_uploader(
                "上傳題目檔案 (支援任意頁數的 PDF、Word、PPTX、TXT)", 
                type=["pdf", "docx", "pptx", "txt", "md"]
            )
            
            default_name = uploaded_file.name if uploaded_file else ""
            col_n, col_p = st.columns([2, 1])
            with col_n:
                custom_name = st.text_input("📄 編輯匯入後的考卷/講義名稱：", value=default_name)
            with col_p:
                page_range_str = st.text_input("🎯 指定頁數範圍 (選填)", placeholder="例如：1-10 或 11-20", help="平常留空代表解析整份文件；若某幾頁被略過，在此填寫即可只補跑該區間並自動合併！")
            
            if st.button("🚀 開始全自動匯入", type="primary") and uploaded_file:
                api_key = get_cached_api_key()
                if not api_key: 
                    st.error("請先到「⚙️ 設定與管理」輸入 API Key！")
                else:
                    fname = uploaded_file.name.lower()
                    file_bytes = uploaded_file.getvalue()
                    final_name = custom_name.strip() if custom_name else uploaded_file.name
                    
                    genai.configure(api_key=api_key)
                    model = genai.GenerativeModel('gemini-3.8-flash')
                    
                    batches = []
                    
                    if fname.endswith('.pdf'):
                        if not HAS_PYPDF:
                            st.error("⚠️ 尚未安裝 `pypdf` 套件！請在 GitHub 的 `requirements.txt` 新增一行 `pypdf`。")
                            st.stop()
                        
                        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                        total_pdf_pages = len(reader.pages)
                        
                        start_page_limit = 0
                        end_page_limit = total_pdf_pages
                        if page_range_str.strip():
                            try:
                                parts = page_range_str.strip().split('-')
                                start_page_limit = max(0, int(parts[0].strip()) - 1)
                                end_page_limit = min(total_pdf_pages, int(parts[1].strip()))
                            except Exception:
                                st.warning("⚠️ 頁數格式不正確（例如請填寫：1-10），將自動解析全部頁數。")
                                start_page_limit = 0
                                end_page_limit = total_pdf_pages

                        actual_page_count = end_page_limit - start_page_limit
                        PAGES_PER_BATCH = 10
                        
                        st.info(f"📖 目標範圍：第 **{start_page_limit + 1} ~ {end_page_limit}** 頁（共 {actual_page_count} 頁），系統以每批 10 頁極速安全解析中...")
                        
                        for start_p in range(start_page_limit, end_page_limit, PAGES_PER_BATCH):
                            end_p = min(start_p + PAGES_PER_BATCH, end_page_limit)
                            
                            batch_text = ""
                            for p_i in range(start_p, end_p):
                                t = reader.pages[p_i].extract_text() or ""
                                if t.strip():
                                    batch_text += f"\n--- Page {p_i+1} ---\n" + t
                            
                            if len(batch_text.strip()) > 100:
                                batches.append({
                                    "title": f"第 {start_p+1} ~ {end_p} 頁 (文字極速提取)",
                                    "content": [parse_prompt, f"【以下為檔案第 {start_p+1} ~ {end_p} 頁文字內容】：\n\n{batch_text}"]
                                })
                            else:
                                writer = pypdf.PdfWriter()
                                for p_i in range(start_p, end_p):
                                    writer.add_page(reader.pages[p_i])
                                sub_buf = io.BytesIO()
                                writer.write(sub_buf)
                                batches.append({
                                    "title": f"第 {start_p+1} ~ {end_p} 頁 (圖文光學解析)",
                                    "content": [parse_prompt, {"mime_type": "application/pdf", "data": sub_buf.getvalue()}]
                                })
                    
                    elif fname.endswith('.docx'):
                        doc_text = extract_text_from_docx(file_bytes)
                        if not doc_text.strip(): st.error("Word 文件空白！"); st.stop()
                        batches.append({"title": "Word 全文", "content": [parse_prompt, f"【Word 內容】：\n\n{doc_text}"]})
                        
                    elif fname.endswith('.pptx'):
                        ppt_text = extract_text_from_pptx(file_bytes)
                        if not ppt_text.strip(): st.error("PPT 簡報空白！"); st.stop()
                        batches.append({"title": "PPT 全文", "content": [parse_prompt, f"【PPT 內容】：\n\n{ppt_text}"]})
                        
                    else:
                        try: txt = file_bytes.decode('utf-8')
                        except UnicodeDecodeError: txt = file_bytes.decode('big5', errors='ignore')
                        batches.append({"title": "文字全文", "content": [parse_prompt, f"【文字內容】：\n\n{txt}"]})

                    prog_bar = st.progress(0.0)
                    status_box = st.empty()
                    total_batches = len(batches)
                    total_imported = 0
                    all_imported_summary = []
                    
                    for b_idx, b_info in enumerate(batches):
                        status_box.markdown(f"⏳ **正在處理 [{b_idx+1}/{total_batches}] {b_info['title']}**（目前已成功抓取 **{total_imported}** 題）...")
                        
                        max_retries = 3
                        response = None
                        for attempt in range(max_retries):
                            try:
                                response = model.generate_content(b_info['content'], request_options={"timeout": 90})
                                break
                            except Exception as e:
                                if "429" in str(e) and attempt < max_retries - 1:
                                    status_box.warning(f"⏳ 遇 API 頻率限制，等待 60 秒... ({attempt+1}/{max_retries})")
                                    time.sleep(60)
                                elif ("504" in str(e) or "Deadline" in str(e)) and attempt < max_retries - 1:
                                    status_box.warning(f"⏳ 遇伺服器暫態延遲，重試該批次中... ({attempt+1}/{max_retries})")
                                    time.sleep(3)
                                else:
                                    st.warning(f"⚠️ {b_info['title']} 略過：{e}")
                                    break
                        
                        if response and response.text:
                            try:
                                raw_text = response.text.strip()
                                match = re.search(r'\[\s*\{.*\}\s*\]', raw_text, re.DOTALL)
                                clean_json = match.group(0) if match else raw_text.replace('```json', '').replace('```', '').strip()
                                chunk_questions = json.loads(clean_json) if clean_json and clean_json != "[]" else []
                                
                                if chunk_questions:
                                    with st.expander(f"📋 {b_info['title']} 成功抓取 {len(chunk_questions)} 題 (點此展開看明細)", expanded=True):
                                        for q_sub_idx, nq in enumerate(chunk_questions, 1):
                                            current_q_num = total_imported + q_sub_idx
                                            q_txt = nq.get('text', '')
                                            raw_opts = nq.get('options', [])
                                            cleaned_opts = [str(o).strip() for o in raw_opts if str(o).strip()]
                                            ans_txt = str(nq.get('answer', '')).strip()
                                            
                                            st.markdown(f"**第 {current_q_num} 題**：{q_txt}")
                                            st.caption(f"選項：{', '.join(cleaned_opts)}  |  正解：`{ans_txt}`")
                                            
                                            opts_json = json.dumps(cleaned_opts, ensure_ascii=False)
                                            o1 = cleaned_opts[0] if len(cleaned_opts) > 0 else ""
                                            o2 = cleaned_opts[1] if len(cleaned_opts) > 1 else ""
                                            o3 = cleaned_opts[2] if len(cleaned_opts) > 2 else ""
                                            o4 = cleaned_opts[3] if len(cleaned_opts) > 3 else ""
                                            
                                            c.execute(
                                                "INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                                                (target_folder, final_name, q_txt, o1, o2, o3, o4, opts_json, ans_txt, nq.get('explanation', ''))
                                            )
                                            all_imported_summary.append({"num": current_q_num, "text": q_txt, "answer": ans_txt})
                                    
                                    conn.commit()
                                    total_imported += len(chunk_questions)
                            except Exception:
                                pass
                        
                        prog_bar.progress((b_idx + 1) / total_batches)
                        time.sleep(1.5)
                    
                    status_box.empty()
                    prog_bar.empty()
                    st.session_state['cached_folders'] = None
                    if total_imported > 0:
                        st.success(f"🎉 處理完畢！成功將 **{total_imported}** 題追加存入「{target_folder} / {final_name}」！" + (" (已即時存入 Turso 雲端)" if IS_CLOUD else ""))
                        with st.expander("🔍 檢視本次完整匯入題目總覽清單", expanded=False):
                            for item in all_imported_summary:
                                st.write(f"**第 {item['num']} 題**：{item['text']} (答案：`{item['answer']}`)")
                    else:
                        st.warning("處理完畢，但未在檔案所選範圍中找到符合標準格式的選擇題。")

        else:
            st.info("💡 **文字貼上模式**：在 PDF 中選取未匯入的頁數文字直接貼上，免重傳檔案，瞬間追加！")
            pname = st.text_input("📄 考卷/講義名稱 (務必填寫相同名稱以自動追加)：", value="CH15.pdf")
            ptext = st.text_area("請在此貼上題目文字：", height=250, placeholder="例如：\n1. What is...\nA. ...\nB. ...")
            
            if st.button("🚀 解析貼上內容並匯入", type="primary") and ptext.strip():
                api_key = get_cached_api_key()
                if not api_key:
                    st.error("請至「⚙️ 設定與管理」輸入 API Key！")
                else:
                    with st.spinner("AI 正在提取純文字題目中 (約需 5~10 秒)..."):
                        try:
                            genai.configure(api_key=api_key)
                            model = genai.GenerativeModel('gemini-3.8-flash')
                            response = model.generate_content([parse_prompt, f"【以下為題目文字】：\n\n{ptext}"], request_options={"timeout": 120})
                            
                            raw_text = response.text.strip()
                            match = re.search(r'\[\s*\{.*\}\s*\]', raw_text, re.DOTALL)
                            clean_json = match.group(0) if match else raw_text.replace('```json', '').replace('```', '').strip()
                            new_questions = json.loads(clean_json)
                            final_pname = pname.strip() if pname.strip() else "未命名考卷"
                            
                            if new_questions:
                                with st.expander(f"📋 成功抓取 {len(new_questions)} 題明細", expanded=True):
                                    for q_idx, nq in enumerate(new_questions, 1):
                                        q_txt = nq.get('text', '')
                                        raw_opts = nq.get('options', [])
                                        cleaned_opts = [str(o).strip() for o in raw_opts if str(o).strip()]
                                        ans_txt = str(nq.get('answer', '')).strip()
                                        
                                        st.markdown(f"**第 {q_idx} 題**：{q_txt}")
                                        st.caption(f"選項：{', '.join(cleaned_opts)}  |  正解：`{ans_txt}`")
                                        
                                        opts_json = json.dumps(cleaned_opts, ensure_ascii=False)
                                        o1 = cleaned_opts[0] if len(cleaned_opts) > 0 else ""
                                        o2 = cleaned_opts[1] if len(cleaned_opts) > 1 else ""
                                        o3 = cleaned_opts[2] if len(cleaned_opts) > 2 else ""
                                        o4 = cleaned_opts[3] if len(cleaned_opts) > 3 else ""
                                        
                                        c.execute(
                                            "INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                                            (target_folder, final_pname, q_txt, o1, o2, o3, o4, opts_json, ans_txt, nq.get('explanation', ''))
                                        )
                                conn.commit()
                                st.session_state['cached_folders'] = None
                                st.success(f"🎉 成功將 {len(new_questions)} 題追加匯入至「{target_folder} / {final_pname}」！" + (" (已即時存入 Turso 雲端)" if IS_CLOUD else ""))
                        except Exception as e:
                            st.error(f"解析失敗，詳細錯誤：{e}")

# ---------- 【設定與管理區 (測驗進行中休眠)】 ----------
with tab_settings:
    if st.session_state.exam_active or st.session_state.exam_finished:
        st.info("⚡ 測驗或查看報告中，管理面板已自動休眠以保持刷題零卡頓。")
    else:
        st.subheader("🔑 API Key 設定")
        current_key = get_cached_api_key()
        new_key = st.text_input("輸入 Gemini API Key", value=current_key, type="password")
        if st.button("儲存設定"):
            c.execute("REPLACE INTO settings (key, value) VALUES ('gemini_api_key', ?)", (new_key,))
            conn.commit()
            st.session_state['cached_api_key'] = new_key
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
                    st.session_state['cached_folders'] = None
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
        
        if items:
            all_folders = get_cached_folders()
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
                        st.session_state['cached_folders'] = None
                        st.success("✅ 更新成功！")
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
                        st.session_state['cached_folders'] = None
                        st.success("✅ 已刪除！")
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
                        st.session_state['cached_folders'] = None
                        st.success(f"✅ 資料夾已更名為「{new_folder_name.strip()}」！")
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
    else:
        st.caption("⚡ 測驗或查看報告中，管理面板休眠以保持頁面極速反應。")
