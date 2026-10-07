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
        if isinstance(item, int): return list(self.values())[item]
        return super().__getitem__(item)

class CursorWrapper:
    def __init__(self, cursor, is_cloud):
        self.cursor = cursor
        self.is_cloud = is_cloud

    def execute(self, *args, **kwargs):
        new_args = list(args)
        if len(new_args) > 1:
            if isinstance(new_args[1], list): new_args[1] = tuple(new_args[1])
            elif new_args[1] is None: new_args[1] = ()
        if 'parameters' in kwargs:
            if isinstance(kwargs['parameters'], list): kwargs['parameters'] = tuple(kwargs['parameters'])
            elif kwargs['parameters'] is None: kwargs['parameters'] = ()
        self.cursor.execute(*new_args, **kwargs)
        return self

    def executemany(self, *args, **kwargs):
        new_args = list(args)
        if len(new_args) > 1 and isinstance(new_args[1], (list, tuple)):
            new_args[1] = [tuple(item) if isinstance(item, list) else item for item in new_args[1]]
        self.cursor.executemany(*new_args, **kwargs)
        return self

    def fetchone(self):
        row = self.cursor.fetchone()
        if row is None: return None
        if hasattr(row, 'keys') or isinstance(row, dict): return row
        if self.cursor.description:
            cols = [col[0] for col in self.cursor.description]
            return RowDict(zip(cols, row))
        return row

    def fetchall(self):
        rows = self.cursor.fetchall()
        if not rows: return []
        if hasattr(rows[0], 'keys') or isinstance(rows[0], dict): return rows
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

    def cursor(self): return CursorWrapper(self.conn.cursor(), self.is_cloud)
    def commit(self):
        try: self.conn.commit()
        except: pass

@st.cache_resource
def get_db_connection():
    is_cloud = False
    cloud_err = ""
    if TURSO_DB_URL.strip() and TURSO_AUTH_TOKEN.strip() and not TURSO_DB_URL.startswith("您的_"):
        try:
            import libsql_experimental as libsql
            raw_conn = libsql.connect(TURSO_DB_URL.strip(), auth_token=TURSO_AUTH_TOKEN.strip())
            test_cur = raw_conn.cursor()
            test_cur.execute("SELECT 1")
            test_cur.fetchall()
            is_cloud = True
            return DBWrapper(raw_conn, is_cloud=True), is_cloud, cloud_err
        except Exception as e:
            cloud_err = str(e)
            import sqlite3
            raw_conn = sqlite3.connect('quiz_database.db', check_same_thread=False)
            return DBWrapper(raw_conn, is_cloud=False), False, cloud_err
    else:
        import sqlite3
        raw_conn = sqlite3.connect('quiz_database.db', check_same_thread=False)
        return DBWrapper(raw_conn, is_cloud=False), False, cloud_err

conn, IS_CLOUD, CLOUD_ERROR = get_db_connection()
c = conn.cursor()

if '_db_schema_ready' not in st.session_state:
    try:
        c.execute('''CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, text TEXT, opt1 TEXT, opt2 TEXT, opt3 TEXT, opt4 TEXT, answer TEXT, wrong_count INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS exam_history (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, wrong_ids TEXT, correct_ids TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
        c.execute("PRAGMA table_info(questions)")
        existing_cols = [col['name'] for col in c.fetchall()]
        for col_name in ['explanation', 'folder', 'is_starred', 'pdf_starred', 'options', 'explanation_image', 'text_en', 'options_en', 'text_zh', 'options_zh']:
            if col_name not in existing_cols:
                col_type = "INTEGER DEFAULT 0" if col_name in ['is_starred', 'pdf_starred'] else "TEXT DEFAULT ''"
                c.execute(f"ALTER TABLE questions ADD COLUMN {col_name} {col_type}")
        conn.commit()
        st.session_state['_db_schema_ready'] = True
    except: pass

def get_cached_folders():
    if 'cached_folders' not in st.session_state or st.session_state['cached_folders'] is None:
        try:
            c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL AND folder != ''")
            raw_rows = c.fetchall()
            result = [r[0] if isinstance(r, (tuple, list)) else (r.get('folder') if isinstance(r, dict) else r['folder']) for r in raw_rows]
            st.session_state['cached_folders'] = sorted(list(set([str(v).strip() for v in result if v and str(v).strip()])))
        except: st.session_state['cached_folders'] = []
    return st.session_state['cached_folders']

def get_cached_api_key():
    if 'cached_api_key' not in st.session_state or st.session_state['cached_api_key'] is None:
        try:
            c.execute("SELECT value FROM settings WHERE key='gemini_api_key'")
            res = c.fetchone()
            st.session_state['cached_api_key'] = res['value'] if res else ""
        except: st.session_state['cached_api_key'] = ""
    return st.session_state['cached_api_key']

def get_categories_by_folder(folder=None, only_star=False):
    try:
        query = "SELECT DISTINCT category, pdf_starred FROM questions WHERE 1=1"
        params = []
        if folder and folder != "全部資料夾":
            query += " AND folder=?"
            params.append(folder)
        if only_star: query += " AND pdf_starred=1"
        c.execute(query, tuple(params))
        rows = c.fetchall()
        pdf_list, star_map = [], {}
        for r in rows:
            cat = r[0] if isinstance(r, (tuple, list)) else (r.get('category') if isinstance(r, dict) else r['category'])
            star = r[1] if isinstance(r, (tuple, list)) and len(r) > 1 else (r.get('pdf_starred', 0) if isinstance(r, dict) else r.get('pdf_starred', 0))
            if cat and str(cat).strip():
                c_str = str(cat).strip()
                pdf_list.append(c_str)
                star_map[c_str] = bool(star)
        return sorted(list(set(pdf_list))), star_map
    except: return [], {}

def compress_image_to_base64(uploaded_file, max_size=(800, 800), quality=70):
    try:
        img = Image.open(uploaded_file).convert("RGB")
        img.thumbnail(max_size, Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality, optimize=True)
        return f"data:image/jpeg;base64,{base64.b64encode(buf.getvalue()).decode('utf-8')}"
    except:
        return f"data:{uploaded_file.type};base64,{base64.b64encode(uploaded_file.getvalue()).decode('utf-8')}"

def get_question_options(q):
    if 'options' in q and q['options'] and str(q['options']).strip():
        try:
            opts = json.loads(q['options'])
            if isinstance(opts, list) and len(opts) > 0: return [str(o).strip() for o in opts if str(o).strip()]
        except: pass
    opts = []
    for col in ['opt1', 'opt2', 'opt3', 'opt4']:
        if col in q and q[col] and str(q[col]).strip(): opts.append(str(q[col]).strip())
    return opts if opts else ["A", "B"]

def is_primarily_english(text):
    zh_chars = len(re.findall(r'[\u4e00-\u9fff]', text))
    en_words = len(re.findall(r'[a-zA-Z]{2,}', text))
    return en_words >= zh_chars

def get_question_bilingual(q, target_lang="en"):
    orig_text = q.get('text', '')
    orig_opts = get_question_options(q)
    is_orig_en = is_primarily_english(orig_text)

    if (target_lang == "en" and is_orig_en) or (target_lang == "zh" and not is_orig_en):
        return orig_text, orig_opts

    col_t = "text_en" if target_lang == "en" else "text_zh"
    col_o = "options_en" if target_lang == "en" else "options_zh"
    if q.get(col_t) and str(q[col_t]).strip():
        try: return q[col_t], (json.loads(q[col_o]) if q.get(col_o) else orig_opts)
        except: return q[col_t], orig_opts

    api_key = get_cached_api_key()
    if not api_key: return orig_text, orig_opts

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel('gemini-3.8-flash')
        lang_target_desc = "專業醫學英文 (USMLE English)" if target_lang == "en" else "繁體中文（台灣醫學常用術語）"
        prompt = f'請將以下醫學/生化選擇題翻譯為【{lang_target_desc}】。\n題目：{orig_text}\n選項：{orig_opts}\n\n【規範】以標準 JSON 格式輸出：\n{{"text": "翻譯後的題目", "options": ["選項1", "選項2", ...]}}'
        resp = model.generate_content(prompt, request_options={"timeout": 45})
        raw = resp.text.strip()
        match = re.search(r'\{.*\}', raw, re.DOTALL)
        clean_json = match.group(0) if match else raw.replace('```json', '').replace('```', '').strip()
        parsed = json.loads(clean_json)
        
        trans_text, trans_opts = parsed.get('text', orig_text), parsed.get('options', orig_opts)
        c.execute(f"UPDATE questions SET {col_t}=?, {col_o}=? WHERE id=?", (trans_text, json.dumps(trans_opts, ensure_ascii=False), q['id']))
        conn.commit()
        q[col_t], q[col_o] = trans_text, json.dumps(trans_opts, ensure_ascii=False)
        return trans_text, trans_opts
    except: return orig_text, orig_opts

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

# ================= 1. 測驗與練習狀態管理 =================
if 'exam_active' not in st.session_state: st.session_state.exam_active = False
if 'exam_finished' not in st.session_state: st.session_state.exam_finished = False
if 'exam_mode' not in st.session_state: st.session_state.exam_mode = "practice" 
if 'exam_q_ids' not in st.session_state: st.session_state.exam_q_ids = []
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
if 'practice_answered' not in st.session_state: st.session_state.practice_answered = False
if 'pending_db_updates' not in st.session_state: st.session_state.pending_db_updates = {}
if 'ai_generated_temp' not in st.session_state: st.session_state.ai_generated_temp = []

def check_is_correct(chosen_idx, q_item):
    orig_opts = get_question_options(q_item)
    if chosen_idx is None or chosen_idx < 0 or chosen_idx >= len(orig_opts): return False
    chosen_str, correct_ans = orig_opts[chosen_idx].strip(), str(q_item['answer']).strip()
    if chosen_str == correct_ans: return True
    opt_letter = chr(65 + chosen_idx) if chosen_idx < 26 else ""
    if correct_ans.upper() == opt_letter: return True
    if len(correct_ans) == 1 and chosen_str.startswith(correct_ans): return True
    if len(chosen_str) == 1 and correct_ans.startswith(chosen_str): return True
    return False

def flush_pending_updates():
    if st.session_state.pending_db_updates:
        for qid, delta in st.session_state.pending_db_updates.items():
            if delta < 0: c.execute("UPDATE questions SET wrong_count = MAX(0, wrong_count - 1) WHERE id=?", (qid,))
            else: c.execute("UPDATE questions SET wrong_count = wrong_count + 1 WHERE id=?", (qid,))
        conn.commit()
        st.session_state.pending_db_updates = {}

# ================= 2. 網頁介面開始 =================
st.set_page_config(page_title="AI 錯題本", page_icon="📝", layout="centered")
st.title("📝 AI 專屬錯題本系統")

if IS_CLOUD: st.success("☁️ 已極速連線至 Turso 雲端資料庫！(重開機資料永不丟失)")
else: st.caption("🖥️ 本地暫存模式")

tab_practice, tab_test, tab_review, tab_ai_gen, tab_import, tab_settings = st.tabs([
    "🎓 刷題練習", "📝 模擬測驗", "📖 錯題總覽", "🧠 AI 仿題出題", "📥 匯入題庫", "⚙️ 設定"
])

# ---------- 【🎓 分頁一：刷題練習 (做一題解答一題)】 ----------
with tab_practice:
    if st.session_state.exam_active and st.session_state.exam_mode == "test":
        st.info("⚠️ 正在進行模擬測驗，請前往「📝 模擬測驗」分頁繼續。")
    elif not st.session_state.exam_active:
        st.markdown("### 🎓 刷題練習模式（做一題、對一題、看詳解）")
        folders = get_cached_folders()
        if not folders: st.warning("題庫空空如也，請先匯入考卷！")
        else:
            col_f, col_st = st.columns([2, 1])
            with col_f: selected_folder = st.selectbox("📁 篩選資料夾：", ["全部資料夾"] + folders, key="p_fold")
            with col_st:
                st.write("")
                only_star_q = st.checkbox("⭐ 只刷星號題目", value=False, key="p_star")

            all_available_pdfs, pdf_star_map = get_categories_by_folder(selected_folder)
            selected_pdfs = st.multiselect("📄 勾選要練習的考卷 / 講義：", options=all_available_pdfs, format_func=lambda x: f"⭐ {x}" if pdf_star_map.get(x) else x, key="p_pdfs")

            col_order, col_cnt, col_lang = st.columns([1.5, 1.2, 1.3])
            with col_order: q_order = st.selectbox("🔀 出題順序：", ["照考卷順序", "隨機挑題"], key="p_ord")
            with col_cnt: q_count_option = st.selectbox("⏱️ 練習題數：", ["10 題", "20 題", "40 題", "全部題目"], index=1, key="p_cnt")
            with col_lang: default_lang_choice = st.selectbox("🌐 預設語言：", ["🇺🇸 英文版", "🇹🇼 中文版"], key="p_lang")

            start_q_num = 1
            if q_order == "照考卷順序": start_q_num = st.number_input("📍 從第幾題開始做？", min_value=1, value=1, step=1, key="p_start")

            if st.button("🚀 開始刷題", type="primary", use_container_width=True, key="p_start_btn"):
                if not selected_pdfs: st.error("請勾選考卷！")
                else:
                    placeholders = ','.join(['?'] * len(selected_pdfs))
                    query = f"SELECT id FROM questions WHERE category IN ({placeholders})" + (" AND is_starred=1" if only_star_q else "") + (" ORDER BY category ASC, id ASC" if q_order == "照考卷順序" else "")
                    c.execute(query, tuple(list(selected_pdfs)))
                    q_pool = c.fetchall()
                    if not q_pool: st.warning("無符合題目！")
                    else:
                        q_ids = [r['id'] for r in q_pool]
                        tot_f = len(q_ids)
                        pick_amt = tot_f if q_count_option == "全部題目" else int(q_count_option.replace(" 題", ""))
                        
                        if q_order == "隨機挑題":
                            random.shuffle(q_ids)
                            st.session_state.exam_q_ids = q_ids[:min(pick_amt, tot_f)]
                        else:
                            s_idx = max(0, min(int(start_q_num) - 1, tot_f - 1))
                            st.session_state.exam_q_ids = q_ids[s_idx : s_idx + pick_amt]

                        st.session_state.exam_index = 0
                        st.session_state.exam_mode = "practice"
                        st.session_state.exam_user_answers = {}
                        st.session_state.practice_answered = False
                        st.session_state.pending_db_updates = {}
                        st.session_state.exam_lang_mode = "en" if default_lang_choice.startswith("🇺🇸") else "zh"
                        st.session_state.exam_lang_overrides = {}
                        st.session_state.exam_active = True
                        st.session_state.exam_start_time = time.time()
                        st.rerun()

    # --- 練習進行中 ---
    elif st.session_state.exam_active and st.session_state.exam_mode == "practice":
        total_q = len(st.session_state.exam_q_ids)
        if total_q == 0:
            st.session_state.exam_active = False
            st.rerun()
            
        # 🌟 核心防護：防止網頁連點導致的 Index 超出範圍
        st.session_state.exam_index = max(0, min(st.session_state.exam_index, total_q - 1))
        idx = st.session_state.exam_index
        curr_q_id = st.session_state.exam_q_ids[idx]
        
        c.execute("SELECT * FROM questions WHERE id=?", (curr_q_id,))
        curr_q = dict(c.fetchone())

        curr_lang = st.session_state.exam_lang_overrides.get(curr_q_id, st.session_state.exam_lang_mode)
        disp_text, disp_options = get_question_bilingual(curr_q, curr_lang)

        st.progress((idx + 1) / total_q)
        c_hdr1, c_hdr2 = st.columns([3, 1])
        c_hdr1.markdown(f"**第 {idx + 1} / {total_q} 題** (練習模式)")
        with c_hdr2:
            if st.button("⏹️ 結束練習", type="primary", use_container_width=True, key="p_end"):
                flush_pending_updates()
                st.session_state.exam_active = False
                st.rerun()

        st.divider()

        col_t, col_lang_btn, col_s = st.columns([3.6, 1.4, 1])
        with col_t: st.subheader(f"Q{idx + 1}. {disp_text}")
        with col_lang_btn:
            if st.button("🇹🇼 翻中文" if curr_lang == "en" else "🇺🇸 切英文", key=f"p_lang_{curr_q_id}", use_container_width=True):
                st.session_state.exam_lang_overrides[curr_q_id] = "zh" if curr_lang == "en" else "en"
                st.rerun()
        with col_s:
            is_q_st = bool(curr_q.get('is_starred', 0))
            if st.button("⭐ 已收藏" if is_q_st else "☆ 收藏", key=f"p_star_{curr_q_id}", use_container_width=True):
                new_star = 0 if is_q_st else 1
                c.execute("UPDATE questions SET is_starred=? WHERE id=?", (new_star, curr_q_id))
                conn.commit(); st.rerun()

        current_chosen_idx = st.session_state.exam_user_answers.get(curr_q_id, None)

        if not st.session_state.practice_answered and current_chosen_idx is None:
            st.write("請選擇你的答案：")
            for opt_idx, opt_text in enumerate(disp_options):
                if st.button(f"  {opt_text}", key=f"p_opt_{curr_q_id}_{opt_idx}", use_container_width=True):
                    st.session_state.exam_user_answers[curr_q_id] = opt_idx
                    st.session_state.practice_answered = True
                    is_right = check_is_correct(opt_idx, curr_q)
                    st.session_state.pending_db_updates[curr_q_id] = -1 if is_right else 1
                    st.rerun()
        else:
            is_right = check_is_correct(current_chosen_idx, curr_q)
            if is_right: st.success("✅ 答對了！")
            else: st.error(f"❌ 答錯了！正確答案是：**{curr_q['answer']}**")

            st.markdown("#### 📋 題目選項完整對照：")
            for opt_idx, opt_text in enumerate(disp_options):
                is_this_ans = check_is_correct(opt_idx, curr_q)
                is_this_user = (current_chosen_idx == opt_idx)
                if is_this_ans: st.markdown(f"- **:green[✅ {opt_text} （正解）]**")
                elif is_this_user: st.markdown(f"- **:red[❌ {opt_text} （你的選擇）]**")
                else: st.markdown(f"- <span style='color: gray;'>{opt_text}</span>", unsafe_allow_html=True)

            st.divider()
            exp = curr_q.get('explanation', '')
            if exp and exp.strip() and exp != '無提供詳解': st.info(f"💡 解析：{exp}")
            else:
                if st.button("🧠 AI 即時分析詳解", key=f"p_ai_exp_{curr_q_id}"):
                    api_key = get_cached_api_key()
                    if not api_key: st.error("請先輸入 API Key！")
                    else:
                        with st.spinner("撰寫中..."):
                            try:
                                genai.configure(api_key=api_key)
                                resp = genai.GenerativeModel('gemini-3.8-flash').generate_content(f"題目：{disp_text}\n選項：{disp_options}\n正確答案：{curr_q['answer']}\n請用繁體中文詳解原因與機轉。", request_options={"timeout": 60})
                                c.execute("UPDATE questions SET explanation=? WHERE id=?", (resp.text.strip(), curr_q_id))
                                conn.commit(); st.rerun()
                            except: st.error("AI 生成失敗")

            if curr_q.get('explanation_image'): st.image(curr_q['explanation_image'], use_container_width=True)

            with st.expander("✏️ 編輯本題解析 / 上傳筆記截圖 (極速秒存)"):
                rev_exp_input = st.text_area("文字解析：", value=curr_q.get('explanation', ''), key=f"p_rev_txt_{curr_q_id}")
                rev_img_input = st.file_uploader("更換筆記截圖 (PNG, JPG)", type=["png", "jpg", "jpeg"], key=f"p_rev_img_{curr_q_id}")
                if st.button("💾 儲存筆記", key=f"p_save_note_{curr_q_id}", type="primary"):
                    new_img_b64 = curr_q.get('explanation_image', '')
                    if rev_img_input: new_img_b64 = compress_image_to_base64(rev_img_input)
                    c.execute("UPDATE questions SET explanation=?, explanation_image=? WHERE id=?", (rev_exp_input.strip(), new_img_b64, curr_q_id))
                    conn.commit()
                    st.toast("✅ 筆記已極速存入雲端！"); st.rerun()

            col_prev, col_next = st.columns(2)
            with col_prev:
                if st.button("⬅️ 看上一題", disabled=(idx == 0), use_container_width=True, key="p_prev"):
                    flush_pending_updates()
                    st.session_state.exam_index -= 1
                    # 重新檢查上一題是否已作答
                    check_prev_id = st.session_state.exam_q_ids[st.session_state.exam_index]
                    st.session_state.practice_answered = (check_prev_id in st.session_state.exam_user_answers)
                    st.rerun()
            with col_next:
                if st.button("🏁 完成" if idx >= total_q - 1 else "➡️ 下一題", type="primary", use_container_width=True, key="p_next"):
                    flush_pending_updates()
                    if idx >= total_q - 1: st.session_state.exam_active = False
                    else:
                        st.session_state.exam_index += 1
                        check_next_id = st.session_state.exam_q_ids[st.session_state.exam_index]
                        st.session_state.practice_answered = (check_next_id in st.session_state.exam_user_answers)
                    st.rerun()

# ---------- 【📝 分頁二：模擬測驗 (大考模式)】 ----------
with tab_test:
    if st.session_state.exam_active and st.session_state.exam_mode == "practice":
        st.info("⚠️ 正在進行刷題練習，請前往「🎓 刷題練習」分頁繼續。")
    elif not st.session_state.exam_active and not st.session_state.exam_finished:
        st.markdown("### 📝 模擬測驗模式（全卷作答，最後統一給分結算）")
        folders = get_cached_folders()
        if not folders: st.warning("請先匯入考卷！")
        else:
            col_f, col_st = st.columns([2, 1])
            with col_f: selected_folder = st.selectbox("📁 篩選資料夾：", ["全部資料夾"] + folders, key="t_fold")
            with col_st:
                st.write("")
                only_star_q = st.checkbox("⭐ 只考星號題目", value=False, key="t_star")

            all_available_pdfs, pdf_star_map = get_categories_by_folder(selected_folder)
            selected_pdfs = st.multiselect("📄 勾選要測驗的考卷：", options=all_available_pdfs, format_func=lambda x: f"⭐ {x}" if pdf_star_map.get(x) else x, key="t_pdfs")

            col_order, col_cnt, col_lang = st.columns([1.5, 1.2, 1.3])
            with col_order: q_order = st.selectbox("🔀 出題順序：", ["隨機抽題（全真模擬）", "照考卷順序"], key="t_ord")
            with col_cnt: q_count_option = st.selectbox("⏱️ 測驗題數：", ["10 題", "20 題", "40 題", "50 題", "全部題目"], index=2, key="t_cnt")
            with col_lang: default_lang_choice = st.selectbox("🌐 預設語言：", ["🇺🇸 英文版", "🇹🇼 中文版"], key="t_lang")

            col_time_type, col_time_min = st.columns(2)
            with col_time_type: timer_type = st.selectbox("⏳ 計時方式：", ["倒數計時（限時考試）", "正數碼表"], key="t_time")
            with col_time_min:
                time_limit_min = st.selectbox("選擇時限：", [10, 20, 30, 40, 50, 60, 90], index=3, format_func=lambda x: f"{x} 分鐘", key="t_min") if timer_type.startswith("倒數") else 0

            if st.button("🚀 開始測驗", type="primary", use_container_width=True, key="t_start_btn"):
                if not selected_pdfs: st.error("請勾選考卷！")
                else:
                    placeholders = ','.join(['?'] * len(selected_pdfs))
                    query = f"SELECT id FROM questions WHERE category IN ({placeholders})" + (" AND is_starred=1" if only_star_q else "")
                    c.execute(query, tuple(list(selected_pdfs)))
                    q_pool = c.fetchall()
                    if not q_pool: st.warning("無符合題目！")
                    else:
                        q_ids = [r['id'] for r in q_pool]
                        tot_f = len(q_ids)
                        pick_amt = tot_f if q_count_option == "全部題目" else int(q_count_option.replace(" 題", ""))
                        
                        if q_order.startswith("隨機"): random.shuffle(q_ids)
                        st.session_state.exam_q_ids = q_ids[:min(pick_amt, tot_f)]
                        
                        st.session_state.exam_index = 0
                        st.session_state.exam_mode = "test"
                        st.session_state.exam_user_answers = {}
                        st.session_state.exam_tested_pdfs = selected_pdfs
                        st.session_state.exam_active = True
                        st.session_state.exam_finished = False
                        st.session_state.exam_start_time = time.time()
                        st.session_state.exam_time_limit_sec = time_limit_min * 60
                        st.session_state.exam_lang_mode = "en" if default_lang_choice.startswith("🇺🇸") else "zh"
                        st.session_state.exam_lang_overrides = {}
                        st.rerun()

    # --- 測驗進行中 ---
    elif st.session_state.exam_active and st.session_state.exam_mode == "test":
        total_q = len(st.session_state.exam_q_ids)
        if total_q == 0:
            st.session_state.exam_active = False
            st.rerun()
            
        # 🌟 核心防護：防止網頁連點導致的 Index 超出範圍
        st.session_state.exam_index = max(0, min(st.session_state.exam_index, total_q - 1))
        idx = st.session_state.exam_index
        curr_q_id = st.session_state.exam_q_ids[idx]
        
        c.execute("SELECT * FROM questions WHERE id=?", (curr_q_id,))
        curr_q = dict(c.fetchone())

        curr_lang = st.session_state.exam_lang_overrides.get(curr_q_id, st.session_state.exam_lang_mode)
        disp_text, disp_options = get_question_bilingual(curr_q, curr_lang)

        elapsed_sec = int(time.time() - st.session_state.exam_start_time)
        m, s = divmod(elapsed_sec, 60)
        time_display = f"⏱️ 已用時：{m:02d}:{s:02d}"
        if st.session_state.exam_time_limit_sec > 0:
            rem = st.session_state.exam_time_limit_sec - elapsed_sec
            time_display = "🚨 時間到！" if rem <= 0 else f"⏳ 剩餘：{rem//60:02d}:{rem%60:02d}"

        answered_count = len(st.session_state.exam_user_answers)
        st.progress((idx + 1) / total_q)
        
        c_hdr1, c_hdr2, c_hdr3 = st.columns([1.8, 1.2, 1.2])
        c_hdr1.markdown(f"**第 {idx + 1} / {total_q} 題**（已答：{answered_count}/{total_q}）")
        c_hdr2.caption(time_display)
        with c_hdr3:
            if st.button("🏁 交卷並結算", type="primary", use_container_width=True, key="t_submit"):
                st.session_state.exam_total_time_str = f"{elapsed_sec//60} 分 {elapsed_sec%60} 秒"
                curr_wrong, curr_correct = [], []
                
                for qid in st.session_state.exam_q_ids:
                    c.execute("SELECT options, answer FROM questions WHERE id=?", (qid,))
                    chk_q = dict(c.fetchone())
                    u_idx = st.session_state.exam_user_answers.get(qid, None)
                    if u_idx is not None and check_is_correct(u_idx, chk_q): curr_correct.append(qid)
                    else: curr_wrong.append(qid)
                
                st.session_state.exam_wrong_ids, st.session_state.exam_correct_ids = curr_wrong, curr_correct
                try:
                    c.execute("INSERT INTO exam_history (category, wrong_ids, correct_ids) VALUES (?, ?, ?)", (", ".join(st.session_state.exam_tested_pdfs), json.dumps(curr_wrong), json.dumps(curr_correct)))
                    for qid in curr_wrong: c.execute("UPDATE questions SET wrong_count = wrong_count + 1 WHERE id=?", (qid,))
                    for qid in curr_correct: c.execute("UPDATE questions SET wrong_count = MAX(0, wrong_count - 1) WHERE id=?", (qid,))
                    conn.commit()
                except: pass
                
                st.session_state.exam_active, st.session_state.exam_finished = False, True
                st.rerun()

        with st.expander("📋 題目導航盤（點擊快速跳至該題）", expanded=False):
            cols = st.columns(10)
            for i, qid in enumerate(st.session_state.exam_q_ids):
                has_ans = qid in st.session_state.exam_user_answers
                if cols[i % 10].button(f"{'🟢' if has_ans else '⚪'} {i+1}", key=f"t_nav_{i}", use_container_width=True):
                    st.session_state.exam_index = i
                    st.rerun()

        st.divider()

        col_t, col_lang_btn, col_s = st.columns([3.6, 1.4, 1])
        with col_t: st.subheader(f"Q{idx + 1}. {disp_text}")
        with col_lang_btn:
            if st.button("🇹🇼 翻中文" if curr_lang == "en" else "🇺🇸 切英文", key=f"t_lang_{curr_q_id}", use_container_width=True):
                st.session_state.exam_lang_overrides[curr_q_id] = "zh" if curr_lang == "en" else "en"
                st.rerun()
        with col_s:
            is_q_st = bool(curr_q.get('is_starred', 0))
            if st.button("⭐ 已收藏" if is_q_st else "☆ 收藏", key=f"t_star_{curr_q_id}", use_container_width=True):
                new_star = 0 if is_q_st else 1
                c.execute("UPDATE questions SET is_starred=? WHERE id=?", (new_star, curr_q_id))
                conn.commit(); st.rerun()

        current_chosen_idx = st.session_state.exam_user_answers.get(curr_q_id, None)

        st.write("請選擇你的答案：")
        for opt_idx, opt_text in enumerate(disp_options):
            is_chosen = (current_chosen_idx == opt_idx)
            if st.button(f"👉 【已選】{opt_text}" if is_chosen else f"  {opt_text}", type="primary" if is_chosen else "secondary", key=f"t_opt_{curr_q_id}_{opt_idx}", use_container_width=True):
                st.session_state.exam_user_answers[curr_q_id] = opt_idx
                st.rerun()

        st.write("")
        col_prev, col_clear, col_next = st.columns([1.5, 1, 1.5])
        with col_prev:
            if st.button("⬅️ 上一題", disabled=(idx == 0), use_container_width=True, key="t_prev"):
                st.session_state.exam_index -= 1
                st.rerun()
        with col_clear:
            if current_chosen_idx is not None and st.button("🗑️ 取消選擇", use_container_width=True, key="t_clear"):
                st.session_state.exam_user_answers.pop(curr_q_id, None)
                st.rerun()
        with col_next:
            if st.button("➡️ 下一題", disabled=(idx >= total_q - 1), use_container_width=True, key="t_next"):
                st.session_state.exam_index += 1
                st.rerun()

    # --- 測驗結算頁 ---
    elif st.session_state.exam_finished and st.session_state.exam_mode == "test":
        st.balloons()
        st.success("🎉 測驗完成！成績統計如下：")

        curr_wrong = st.session_state.exam_wrong_ids
        curr_correct = st.session_state.exam_correct_ids
        total_questions = len(st.session_state.exam_q_ids)
        answered_cnt = len(st.session_state.exam_user_answers)
        unanswered_cnt = total_questions - answered_cnt
        score = (len(curr_correct) / total_questions * 100) if total_questions > 0 else 0

        col_m1, col_m2, col_m3, col_m4, col_m5 = st.columns(5)
        col_m1.metric("最終得分", f"{score:.1f} 分")
        col_m2.metric("🟢 答對", f"{len(curr_correct)} 題")
        col_m3.metric("🔴 答錯", f"{len(curr_wrong) - unanswered_cnt} 題")
        col_m4.metric("⚪ 未作答", f"{unanswered_cnt} 題")
        col_m5.metric("⏱️ 總耗時", st.session_state.exam_total_time_str)

        st.divider()

        rev_filter = st.radio("檢討範圍：", ["全部題目", "只看錯題與未作答", "只看答對題目"], horizontal=True)

        if curr_wrong:
            if st.button("⭐ 一鍵將本次錯題加入星號", type="primary"):
                placeholders = ','.join(['?'] * len(curr_wrong))
                c.execute(f"UPDATE questions SET is_starred=1 WHERE id IN ({placeholders})", tuple(curr_wrong))
                conn.commit(); st.toast("✅ 本次錯題已加入星號！")

        for i, qid in enumerate(st.session_state.exam_q_ids, 1):
            c.execute("SELECT * FROM questions WHERE id=?", (qid,))
            q_data = dict(c.fetchone())
            u_idx = st.session_state.exam_user_answers.get(qid, None)
            is_right = check_is_correct(u_idx, q_data)

            if rev_filter == "只看錯題與未作答" and is_right: continue
            if rev_filter == "只看答對題目" and not is_right: continue

            rev_lang = st.session_state.exam_lang_overrides.get(qid, st.session_state.exam_lang_mode)
            q_txt, q_opts = get_question_bilingual(q_data, rev_lang)

            status_icon = "🟢" if is_right else ("⚪ 未作答" if u_idx is None else "🔴")
            with st.expander(f"{status_icon} 第 {i} 題：{q_txt[:35]}..."):
                c_q_head, c_q_lang = st.columns([4, 1.2])
                with c_q_head: st.markdown(f"**【完整題目】**：{q_txt}")
                with c_q_lang:
                    if st.button("🇹🇼 翻中文" if rev_lang == "en" else "🇺🇸 切英文", key=f"res_lang_{qid}"):
                        st.session_state.exam_lang_overrides[qid] = "zh" if rev_lang == "en" else "en"
                        st.rerun()

                for opt_idx, opt_text in enumerate(q_opts):
                    is_this_ans = check_is_correct(opt_idx, q_data)
                    is_this_user = (u_idx == opt_idx)
                    if is_this_ans: st.markdown(f"- **:green[✅ {opt_text} （正解）]**")
                    elif is_this_user: st.markdown(f"- **:red[❌ {opt_text} （你的選擇）]**")
                    else: st.markdown(f"- {opt_text}")
                
                exp = q_data.get('explanation', '')
                st.info(f"💡 解析：{exp if exp else '尚未生成詳解'}")
                if q_data.get('explanation_image'): st.image(q_data['explanation_image'], use_container_width=True)

        if st.button("🔄 回到首頁 / 重新開始", use_container_width=True):
            st.session_state.exam_active, st.session_state.exam_finished = False, False
            st.session_state.exam_q_ids, st.session_state.exam_user_answers = [], {}
            st.rerun()

# ---------- 【📖 分頁三：錯題總覽區 (測驗期間休眠)】 ----------
with tab_review:
    if st.session_state.exam_active or st.session_state.exam_finished:
        st.info("⚡ 系統運作中，背景查詢已自動暫停以確保刷題零卡頓。")
    else:
        st.markdown("### 📖 各考卷 / 講義題目與解析總覽")
        folders = get_cached_folders()
        if not folders: st.info("目前沒有題庫資料。")
        else:
            col_f, col_p, col_st = st.columns([1.5, 1.5, 1])
            with col_f: rev_folder = st.selectbox("📂 選擇資料夾：", ["全部資料夾"] + folders)
            with col_p:
                rev_pdfs, rev_star_map = get_categories_by_folder(rev_folder)
                rev_pdf = st.selectbox("📄 選擇考卷 / 講義：", ["全部考卷"] + rev_pdfs, format_func=lambda x: f"⭐ {x}" if rev_star_map.get(x) else x)
            with col_st:
                st.write("")
                rev_only_starred = st.checkbox("⭐ 僅看星號題目", value=False)
                
            query = "SELECT * FROM questions WHERE 1=1"
            params = []
            if rev_folder != "全部資料夾": query += " AND folder=?"; params.append(rev_folder)
            if rev_pdf != "全部考卷": query += " AND category=?"; params.append(rev_pdf)
            if rev_only_starred: query += " AND is_starred=1"
                
            c.execute(query, tuple(params))
            questions_to_show = c.fetchall()
            
            if not questions_to_show: st.warning("此分類下沒有找到題目。")
            else:
                st.write(f"共找到 **{len(questions_to_show)}** 題：")
                st.divider()
                for idx, q in enumerate(questions_to_show):
                    is_st = bool(q.get('is_starred', 0))
                    q_opts = get_question_options(q)
                    with st.expander(f"{'⭐ ' if is_st else ''}題目 {idx+1}: {q['text'][:30]}... (錯 {q['wrong_count']} 次)"):
                        st.markdown(f"**【題目】** {q['text']}")
                        for opt_i, opt_text in enumerate(q_opts):
                            st.markdown(f"- ({chr(65 + opt_i) if opt_i < 26 else str(opt_i + 1)}) {opt_text}")
                        st.markdown(f"✅ **正確答案**：`{q['answer']}`")
                        exp_text = q.get('explanation', '') if q.get('explanation') and q['explanation'].strip() and q['explanation'] != '無提供詳解' else "尚未填寫詳解"
                        st.markdown(f"💡 **解析**：{exp_text}")
                        if q.get('explanation_image'): st.image(q['explanation_image'], use_container_width=True)
                        
                        with st.expander("✏️ 編輯此題詳解 / 更新筆記截圖"):
                            rev_exp_input = st.text_area("修改文字解析：", value=q['explanation'] if q.get('explanation') and q['explanation'] != '無提供詳解' else "", key=f"rev_txt_{q['id']}")
                            rev_img_input = st.file_uploader("更換筆記截圖 (PNG, JPG)", type=["png", "jpg", "jpeg"], key=f"rev_img_{q['id']}")
                            col_sv_b, col_rm_img = st.columns(2)
                            with col_sv_b:
                                if st.button("💾 儲存修改", key=f"rev_save_btn_{q['id']}", use_container_width=True):
                                    new_img_b64 = q.get('explanation_image', '')
                                    if rev_img_input: new_img_b64 = compress_image_to_base64(rev_img_input)
                                    c.execute("UPDATE questions SET explanation=?, explanation_image=? WHERE id=?", (rev_exp_input.strip(), new_img_b64, q['id']))
                                    conn.commit()
                                    st.toast("✅ 詳解已極速更新！"); st.rerun()
                            with col_rm_img:
                                if st.button("🗑️ 清除截圖", key=f"rev_rm_img_{q['id']}", use_container_width=True):
                                    c.execute("UPDATE questions SET explanation_image='' WHERE id=?", (q['id'],))
                                    conn.commit()
                                    st.toast("✅ 筆記截圖已移除！"); st.rerun()

# ---------- 【🧠 分頁四：AI 仿題出題區】 ----------
with tab_ai_gen:
    if st.session_state.exam_active or st.session_state.exam_finished:
        st.info("⚡ 刷題進行中，此面板已自動休眠。")
    else:
        st.markdown("### 🧠 AI 模擬出題（從特定 PDF/講義深度模仿）")
        all_gen_folders = get_cached_folders()
        if not all_gen_folders: st.info("題庫內目前尚無講義，請先前往「📥 匯入題庫」上傳 PDF！")
        else:
            col_g1, col_g2 = st.columns(2)
            with col_g1: gen_folder = st.selectbox("📂 選擇範本所屬資料夾：", all_gen_folders)
            with col_g2:
                gen_pdfs, _ = get_categories_by_folder(gen_folder)
                gen_target_pdf = st.selectbox("📄 選擇模仿範本：", gen_pdfs)

            col_g_n, col_g_diff = st.columns(2)
            with col_g_n: gen_q_count = st.selectbox("🎯 欲生成的題數：", [3, 5, 8, 10], index=1, format_func=lambda x: f"{x} 題")
            with col_g_diff: gen_diff = st.selectbox("🎓 題目風格難度：", ["USMLE Step 1 / 全英風格", "國考臨床結合概念題", "基礎生化代謝途徑專題"])

            if st.button("✨ 分析講義並生成全新仿題", type="primary", use_container_width=True):
                api_key = get_cached_api_key()
                if not api_key: st.error("請輸入 API Key！")
                elif not gen_target_pdf: st.error("請選取範本！")
                else:
                    c.execute("SELECT text, options, answer FROM questions WHERE category=? ORDER BY RANDOM() LIMIT 8", (gen_target_pdf,))
                    sample_rows = c.fetchall()
                    if not sample_rows: st.error("範本不足！")
                    else:
                        sample_context = "\n".join([f"Q: {s['text']} | Opts: {', '.join(get_question_options(s))} | Ans: {s['answer']}" for s in sample_rows])
                        with st.spinner(f"AI 正在撰寫 {gen_q_count} 道全英文仿題..."):
                            try:
                                genai.configure(api_key=api_key)
                                ai_prompt = f"Analyze these questions from '{gen_target_pdf}':\n{sample_context}\n\nTASK: Generate {gen_q_count} brand-new multiple-choice questions in English. Style: {gen_diff}.\nRULES:\n1. Options in English (4 or 5 choices).\n2. Provide best answer.\n3. Explanation in Traditional Chinese.\n4. Output JSON array only:\n" + '[{"text": "...", "options": ["A. ...", "B. ..."], "answer": "A", "explanation": "..."}]'
                                resp = genai.GenerativeModel('gemini-3.8-flash').generate_content(ai_prompt, request_options={"timeout": 120})
                                raw = resp.text.strip()
                                match = re.search(r'\[\s*\{.*\}\s*\]', raw, re.DOTALL)
                                st.session_state.ai_generated_temp = json.loads(match.group(0) if match else raw.replace('```json', '').replace('```', '').strip())
                                st.toast("🎉 生成完畢！")
                            except Exception as e: st.error(f"失敗：{e}")

            if st.session_state.ai_generated_temp:
                st.divider()
                st.subheader("📋 預覽")
                for g_i, g_q in enumerate(st.session_state.ai_generated_temp, 1):
                    with st.expander(f"仿題 {g_i}：{g_q.get('text', '')[:40]}...", expanded=True):
                        st.markdown(f"**【題幹】**：{g_q.get('text', '')}")
                        for o in g_q.get('options', []): st.markdown(f"- {o}")
                        st.markdown(f"✅ **正解**：`{g_q.get('answer', '')}`")
                
                save_cat_name = f"[AI仿題] {gen_target_pdf}"
                if st.button(f"📥 一鍵加入題庫（儲存至 {save_cat_name}）", type="primary", use_container_width=True):
                    for g_item in st.session_state.ai_generated_temp:
                        opts_clean = [str(x).strip() for x in g_item.get('options', []) if str(x).strip()]
                        opts_json = json.dumps(opts_clean, ensure_ascii=False)
                        c.execute("INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred, text_en, options_en) VALUES (?,?,?,?,?,?,?,?,?,?,0,0,?,?)", (gen_folder, save_cat_name, g_item.get('text', ''), opts_clean[0] if len(opts_clean)>0 else "", opts_clean[1] if len(opts_clean)>1 else "", opts_clean[2] if len(opts_clean)>2 else "", opts_clean[3] if len(opts_clean)>3 else "", opts_json, str(g_item.get('answer', '')), g_item.get('explanation', ''), g_item.get('text', ''), opts_json))
                    conn.commit()
                    st.session_state['cached_folders'] = None
                    st.session_state.ai_generated_temp = []
                    st.success("🎉 存入雲端成功！"); st.rerun()

# ---------- 【📥 分頁五：匯入區 (測驗期間休眠)】 ----------
with tab_import:
    if st.session_state.exam_active or st.session_state.exam_finished:
        st.info("⚡ 刷題進行中，此面板已自動休眠。")
    else:
        st.markdown("### 🤖 智慧題庫匯入")
        existing_folders = get_cached_folders()
        folder_choice = st.selectbox("📂 目標資料夾：", ["-- ➕ 新增資料夾 --"] + existing_folders)
        target_folder = st.text_input("新資料夾名稱", "生化") if folder_choice == "-- ➕ 新增資料夾 --" else folder_choice
            
        import_mode = st.radio("匯入方式：", ["📁 模式一：檔案上傳", "📋 模式二：純文字貼上"])
        parse_prompt = "請提取所有的選擇題。嚴格輸出 JSON 陣列：\n" + '[{"text":"...","options":["A","B","C"],"answer":"A","explanation":""}]'

        if import_mode.startswith("📁"):
            uploaded_file = st.file_uploader("上傳檔案", type=["pdf", "docx", "pptx", "txt", "md"])
            col_n, col_p = st.columns([2, 1])
            with col_n: custom_name = st.text_input("📄 考卷名稱：", value=uploaded_file.name if uploaded_file else "")
            with col_p: page_range_str = st.text_input("🎯 頁數範圍 (例如 1-10)：", placeholder="留空代表全份")
            
            if st.button("🚀 開始全自動匯入", type="primary") and uploaded_file:
                api_key = get_cached_api_key()
                if not api_key: st.error("請輸入 API Key！")
                else:
                    fname = uploaded_file.name.lower()
                    file_bytes = uploaded_file.getvalue()
                    final_name = custom_name.strip() if custom_name else uploaded_file.name
                    genai.configure(api_key=api_key)
                    model = genai.GenerativeModel('gemini-3.8-flash')
                    batches = []
                    
                    if fname.endswith('.pdf'):
                        if not HAS_PYPDF: st.stop()
                        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                        start_p, end_p = 0, len(reader.pages)
                        if page_range_str.strip():
                            try:
                                parts = page_range_str.strip().split('-')
                                start_p, end_p = max(0, int(parts[0].strip())-1), min(len(reader.pages), int(parts[1].strip()))
                            except: pass

                        for sp in range(start_p, end_p, 10):
                            ep = min(sp + 10, end_p)
                            batch_text = "".join([(reader.pages[i].extract_text() or "") for i in range(sp, ep)])
                            if len(batch_text.strip()) > 100: batches.append({"title": f"第 {sp+1}~{ep} 頁", "content": [parse_prompt, batch_text]})
                            else:
                                writer = pypdf.PdfWriter()
                                for i in range(sp, ep): writer.add_page(reader.pages[i])
                                sub_buf = io.BytesIO()
                                writer.write(sub_buf)
                                batches.append({"title": f"第 {sp+1}~{ep} 頁 (圖文)", "content": [parse_prompt, {"mime_type": "application/pdf", "data": sub_buf.getvalue()}]})
                    else:
                        txt = extract_text_from_docx(file_bytes) if fname.endswith('.docx') else (extract_text_from_pptx(file_bytes) if fname.endswith('.pptx') else file_bytes.decode('utf-8', errors='ignore'))
                        batches.append({"title": "全文提取", "content": [parse_prompt, txt]})

                    prog_bar, status_box = st.progress(0.0), st.empty()
                    total_imported = 0
                    for b_idx, b_info in enumerate(batches):
                        status_box.markdown(f"⏳ 處理 {b_info['title']}...")
                        for attempt in range(3):
                            try:
                                resp = model.generate_content(b_info['content'], request_options={"timeout": 90})
                                raw = resp.text.strip()
                                match = re.search(r'\[\s*\{.*\}\s*\]', raw, re.DOTALL)
                                chunk_questions = json.loads(match.group(0) if match else raw.replace('```json', '').replace('```', '').strip())
                                for nq in chunk_questions:
                                    opts_clean = [str(o).strip() for o in nq.get('options', []) if str(o).strip()]
                                    opts_json = json.dumps(opts_clean, ensure_ascii=False)
                                    c.execute("INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)", (target_folder, final_name, nq.get('text', ''), opts_clean[0] if len(opts_clean)>0 else "", opts_clean[1] if len(opts_clean)>1 else "", opts_clean[2] if len(opts_clean)>2 else "", opts_clean[3] if len(opts_clean)>3 else "", opts_json, str(nq.get('answer', '')), nq.get('explanation', '')))
                                conn.commit()
                                total_imported += len(chunk_questions)
                                break
                            except: time.sleep(5)
                        prog_bar.progress((b_idx + 1) / len(batches))
                    st.session_state['cached_folders'] = None
                    if total_imported > 0: st.success(f"🎉 成功存入 {total_imported} 題！")
        else:
            ptext = st.text_area("請在此貼上題目文字：", height=250)
            if st.button("🚀 解析貼上內容", type="primary") and ptext.strip():
                api_key = get_cached_api_key()
                if api_key:
                    with st.spinner("提取中..."):
                        try:
                            genai.configure(api_key=api_key)
                            resp = genai.GenerativeModel('gemini-3.8-flash').generate_content([parse_prompt, ptext], request_options={"timeout": 60})
                            raw = resp.text.strip()
                            match = re.search(r'\[\s*\{.*\}\s*\]', raw, re.DOTALL)
                            new_questions = json.loads(match.group(0) if match else raw.replace('```json', '').replace('```', '').strip())
                            for nq in new_questions:
                                opts_clean = [str(o).strip() for o in nq.get('options', []) if str(o).strip()]
                                opts_json = json.dumps(opts_clean, ensure_ascii=False)
                                c.execute("INSERT INTO questions (folder, category, text, options, answer, explanation, is_starred, pdf_starred) VALUES (?,?,?,?,?,?,0,0)", (target_folder, "貼上匯入", nq.get('text', ''), opts_json, str(nq.get('answer', '')), nq.get('explanation', '')))
                            conn.commit()
                            st.session_state['cached_folders'] = None
                            st.success(f"🎉 成功存入 {len(new_questions)} 題！")
                        except Exception as e: st.error(f"失敗：{e}")

# ---------- 【⚙️ 分頁六：設定與管理區】 ----------
with tab_settings:
    if st.session_state.exam_active or st.session_state.exam_finished:
        st.info("⚡ 刷題進行中，此面板已自動休眠。")
    else:
        st.subheader("🔑 API Key 設定")
        new_key = st.text_input("輸入 Gemini API Key", value=get_cached_api_key(), type="password")
        if st.button("儲存設定"):
            c.execute("REPLACE INTO settings (key, value) VALUES ('gemini_api_key', ?)", (new_key,))
            conn.commit()
            st.session_state['cached_api_key'] = new_key
            st.success("設定已儲存！")

        st.divider()
        st.subheader("📁 題庫管理 (名稱修改 / 移動 / 刪除)")
        try:
            c.execute("SELECT folder, category, MAX(pdf_starred) as pdf_starred FROM questions GROUP BY folder, category")
            items = c.fetchall()
            all_folders = get_cached_folders()
            for index, row in enumerate(items):
                f_name, p_name = row['folder'], row['category']
                with st.expander(f"{'⭐ ' if bool(row['pdf_starred']) else ''}📂 {f_name} ＞ 📄 {p_name}"):
                    new_p_name = st.text_input("修改名稱", value=p_name, key=f"p_rename_{index}")
                    target_f = st.selectbox("移動至資料夾", all_folders, index=all_folders.index(f_name) if f_name in all_folders else 0, key=f"f_move_{index}")
                    col_save, col_star_pdf, col_del = st.columns(3)
                    if col_save.button("💾 儲存變更", key=f"save_{index}", use_container_width=True):
                        c.execute("UPDATE questions SET category=?, folder=? WHERE folder=? AND category=?", (new_p_name, target_f, f_name, p_name))
                        c.execute("UPDATE exam_history SET category=? WHERE category=?", (new_p_name, p_name))
                        conn.commit(); st.session_state['cached_folders'] = None; st.rerun()
                    if col_star_pdf.button("⭐ 標記星號" if not bool(row['pdf_starred']) else "☆ 取消星號", key=f"star_pdf_{index}", use_container_width=True):
                        c.execute("UPDATE questions SET pdf_starred=? WHERE folder=? AND category=?", (0 if bool(row['pdf_starred']) else 1, f_name, p_name))
                        conn.commit(); st.rerun()
                    if col_del.button("🗑️ 刪除", key=f"del_{index}", use_container_width=True):
                        c.execute("DELETE FROM questions WHERE folder=? AND category=?", (f_name, p_name))
                        conn.commit(); st.session_state['cached_folders'] = None; st.rerun()
        except: pass
