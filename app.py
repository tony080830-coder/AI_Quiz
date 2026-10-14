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
TURSO_AUTH_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJhIjoicnciLCJpYXQiOjE3OTE4NzkzMDksImlkIjoiMDFhMGRiYTUtYmIwMS03MDkwLTgxM2UtNDUwODgwZGQ5MDdhIiwia2lkIjoiRG5HUXMycy13c0VfNkc5Szlnbms4cENlYWJ0NjZRcF9yUUhNYVU1aUhLSSIsInJpZCI6ImM0ZDI0Mjc1LTVmNzQtNGZkMi05M2Y5LTk1ZjRjMWExNWQ4MCJ9.6jTw0MrLdAEMzRubZj0lqOaZwS3X3oDzbgP707x6qEXXbuCXgxdgA9MmEjQfxYomwujYU5oW41kTUSKvE3e0Bw"

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
        c.execute("CREATE TABLE IF NOT EXISTS questions (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, text TEXT, opt1 TEXT, opt2 TEXT, opt3 TEXT, opt4 TEXT, answer TEXT, wrong_count INTEGER DEFAULT 0)")
        c.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        c.execute("CREATE TABLE IF NOT EXISTS exam_history (id INTEGER PRIMARY KEY AUTOINCREMENT, category TEXT, wrong_ids TEXT, correct_ids TEXT, timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)")
        c.execute("PRAGMA table_info(questions)")
        existing_cols = [col['name'] for col in c.fetchall()]
        for col_name in ['explanation', 'folder', 'is_starred', 'pdf_starred', 'options', 'explanation_image', 'text_en', 'options_en', 'text_zh', 'options_zh']:
            if col_name not in existing_cols:
                col_type = "INTEGER DEFAULT 0" if col_name in ['is_starred', 'pdf_starred'] else "TEXT DEFAULT ''"
                c.execute(f"ALTER TABLE questions ADD COLUMN {col_name} {col_type}")
        conn.commit()
        st.session_state['_db_schema_ready'] = True
    except: pass

def get_all_folders():
    try:
        c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL AND folder != ''")
        raw_rows = c.fetchall()
        result = [r[0] if isinstance(r, (tuple, list)) else (r.get('folder') if isinstance(r, dict) else r['folder']) for r in raw_rows]
        return sorted(list(set([str(v).strip() for v in result if v and str(v).strip()])))
    except: return []

def get_api_key():
    try:
        c.execute("SELECT value FROM settings WHERE key='gemini_api_key'")
        res = c.fetchone()
        return res['value'].strip() if res and res['value'] else ""
    except: return ""

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

# 🌟 單題專用深度剖析器（支援 a.~e. 5選項、【正確答案】、Correct Answer、【中文解析】、無空格黏連）
def parse_single_question_block(b):
    if not b or len(b.strip()) < 10:
        return None

    # 1. 抓取答案 (Answer)：支援 【正確答案】、o 【正確答案】、Correct Answer、解答、答案
    ans = ""
    ans_patterns = [
        r'(?:^[ \t]*[oO○•●\-\*]?\s*)?【\s*(?:正確答案|答案|解答|正解|Ans|Key)\s*】\s*[:：\s]*[\(（]?([A-Ea-e])\b',
        r'(?:Correct\s*Answer|Answer|解答|答案|正解|Ans|Key)\s*[:：\s]*[\(（]?([A-Ea-e])(?=[\)）\s\.\、\:]|$)',
        r'【\s*(?:正確答案|答案|解答|正解)\s*】\s*[:：\s]*[\(（]?([A-Ea-e])'
    ]
    m_ans = None
    for ap in ans_patterns:
        m = re.search(ap, b, re.IGNORECASE | re.MULTILINE)
        if m:
            ans = m.group(1).upper()
            m_ans = m
            break

    # 2. 抓取解析 (Explanation)：支援 【中文解析】、o 【中文解析】、中文簡析、解析、說明
    exp = ""
    exp_patterns = [
        r'(?:^[ \t]*[oO○•●\-\*]?\s*)?【\s*(?:(?:中文)?(?:解析|簡析|詳解|說明))\s*】\s*[:：\s]*([\s\S]*)$',
        r'(?:(?:中文)?(?:簡析|解析|詳解|說明)|Explanation)\s*[:：\s]*([\s\S]*)$'
    ]
    m_exp = None
    for ep in exp_patterns:
        m = re.search(ep, b, re.IGNORECASE | re.MULTILINE)
        if m:
            exp = m.group(1).strip()
            m_exp = m
            break

    # 3. 截除答案與解析，取得題目本體與選項
    pos_ans = m_ans.start() if m_ans else len(b)
    pos_exp = m_exp.start() if m_exp else len(b)
    earliest_meta = min(pos_ans, pos_exp)
    q_opts_part = b[:earliest_meta].strip()

    # 4. 尋找選項 A, B, C, D (及可選的 E)，支援大小寫 a.~e.
    def find_markers(text, letter):
        # 匹配 (A), A., A), A:, A、, (a), a., a)
        pat = r'(?:[\(（]' + letter + r'[\)）]|(?:(?<=[\s\n\r\.\,\;\:\)\]\"\'\>])|^|[a-zA-Z\)])' + letter + r'[\)）\.\、\:\：])\s*'
        return list(re.finditer(pat, text, re.IGNORECASE))

    A_matches = find_markers(q_opts_part, 'A')
    B_matches = find_markers(q_opts_part, 'B')
    C_matches = find_markers(q_opts_part, 'C')
    D_matches = find_markers(q_opts_part, 'D')
    E_matches = find_markers(q_opts_part, 'E')

    chosen_A = chosen_B = chosen_C = chosen_D = chosen_E = None

    for ma in A_matches:
        mb_cand = [mb for mb in B_matches if mb.start() > ma.end()]
        if not mb_cand: continue
        mb = mb_cand[0]

        mc_cand = [mc for mc in C_matches if mc.start() > mb.end()]
        if not mc_cand: continue
        mc = mc_cand[0]

        chosen_A, chosen_B, chosen_C = ma, mb, mc

        md_cand = [md for md in D_matches if md.start() > mc.end()]
        if md_cand:
            chosen_D = md_cand[0]
            me_cand = [me for me in E_matches if me.start() > chosen_D.end()]
            if me_cand:
                chosen_E = me_cand[0]
        break

    if not chosen_A or not chosen_B or not chosen_C:
        return None

    # 5. 提取題幹並清理開頭題號（保留【跨單元】等標籤）
    stem = q_opts_part[:chosen_A.start()].strip()
    stem = re.sub(r'^(?:(?:Question|question|Q|q)\s*\d+|第\s*\d+\s*題|\d+(?:[-_.]\d+)?\s*[\.\、\:\)）]|[\(（\[]\d+(?:[-_.]\d+)?[\)）\]])\s*', '', stem).strip()

    opt_A = q_opts_part[chosen_A.end() : chosen_B.start()].strip()
    opt_B = q_opts_part[chosen_B.end() : chosen_C.start()].strip()
    options = [f"(A) {opt_A}", f"(B) {opt_B}"]

    if chosen_D:
        opt_C = q_opts_part[chosen_C.end() : chosen_D.start()].strip()
        options.append(f"(C) {opt_C}")
        if chosen_E:
            opt_D = q_opts_part[chosen_D.end() : chosen_E.start()].strip()
            opt_E = q_opts_part[chosen_E.end():].strip()
            options.append(f"(D) {opt_D}")
            options.append(f"(E) {opt_E}")
        else:
            opt_D = q_opts_part[chosen_D.end():].strip()
            options.append(f"(D) {opt_D}")
    else:
        opt_C = q_opts_part[chosen_C.end():].strip()
        options.append(f"(C) {opt_C}")

    if not ans:
        ans = "A"

    return {
        "text": stem,
        "options": options,
        "answer": ans,
        "explanation": exp
    }

# 🌟 全文全自動切題引擎（精確辨別 3. 【跨單元】、Question 13、22-27. 等，支援長文無換行黏連）
def parse_questions_locally(raw_text):
    if not raw_text or not raw_text.strip():
        return []

    text = raw_text.replace('\r\n', '\n').replace('\r', '\n')

    # 正則匹配題目起點：
    # 1. Question 13
    # 2. 3. 【跨單元】 或 3. What is...
    # 3. 22-27.
    # 4. 第 1 題
    q_start_regex = r'(?:Question|question)\s*\d+|(?<=[\s\n\r。！？；，,\)\]」』])(?:[Qq]\s*\d+|第\s*\d+\s*題|\b\d+(?:[-_.]\d+)?\s*(?:[\.\、\:\)）]|(?=\s*【))|[\(（\[]\d+(?:[-_.]\d+)?[\)）\]])'
    splits = list(re.finditer(q_start_regex, text))

    blocks = []
    if len(splits) >= 2:
        if splits[0].start() > 0:
            pre_block = text[:splits[0].start()].strip()
            if pre_block:
                blocks.append(pre_block)

        for i in range(len(splits)):
            start = splits[i].start()
            end = splits[i+1].start() if i + 1 < len(splits) else len(text)
            blocks.append(text[start:end].strip())
    else:
        blocks = [b.strip() for b in text.split('\n\n') if len(b.strip()) > 10]
        if not blocks:
            blocks = [text.strip()]

    parsed_questions = []
    for b in blocks:
        item = parse_single_question_block(b)
        if item and item['text'] and len(item['options']) >= 2:
            parsed_questions.append(item)

    return parsed_questions

def extract_json_questions(raw_text):
    raw = raw_text.strip()
    try:
        data = json.loads(raw)
        return [data] if isinstance(data, dict) else data
    except: pass
    
    match_arr = re.search(r'\[\s*\{.*\}\s*\]', raw, re.DOTALL)
    if match_arr:
        try: return json.loads(match_arr.group(0))
        except: pass
            
    match_obj = re.search(r'\{.*\}', raw, re.DOTALL)
    if match_obj:
        try:
            data = json.loads(match_obj.group(0))
            return [data] if isinstance(data, dict) else data
        except: pass

    clean = re.sub(r'```(?:json)?', '', raw)
    clean = re.sub(r'```', '', clean).strip()
    try:
        data = json.loads(clean)
        return [data] if isinstance(data, dict) else data
    except: pass
        
    return []

def get_valid_model_list(api_key):
    clean_key = api_key.strip()
    genai.configure(api_key=clean_key)
    supported_models = []
    try:
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                model_pure_name = m.name.replace('models/', '')
                supported_models.append(model_pure_name)
    except Exception as e:
        return [], f"API 連線失敗：{str(e)}"
    
    if not supported_models:
        return [], "您的 API Key 無法存取任何生成模型，請確認是否為有效金鑰。"
        
    ordered_models = []
    preferred = ['gemini-2.0-flash', 'gemini-1.5-flash-8b', 'gemini-1.5-flash', 'gemini-1.5-flash-latest', 'gemini-pro']
    for p in preferred:
        if p in supported_models and p not in ordered_models:
            ordered_models.append(p)
    for m in supported_models:
        if m not in ordered_models:
            ordered_models.append(m)
            
    return ordered_models, None

def call_gemini_guaranteed(content, api_key, status_box=None):
    clean_key = api_key.strip()
    candidate_models, err = get_valid_model_list(clean_key)
    if not candidate_models:
        return [], err

    last_error = ""
    for model_name in candidate_models:
        try:
            model = genai.GenerativeModel(model_name)
            resp = model.generate_content(content, request_options={"timeout": 90})
            if resp and resp.text:
                res = extract_json_questions(resp.text.strip())
                if res: return res, None
        except Exception as e:
            err_str = str(e)
            last_error = err_str
            if "429" in err_str or "quota" in err_str.lower():
                if status_box:
                    status_box.info(f"🔄 模型 {model_name} 額度受限，自動切換備援模型...")
                continue
            else:
                time.sleep(1)

    return [], f"此帳號所有可用模型今日額度均已滿，錯誤：{last_error}"

def call_gemini_text_fallback(prompt, api_key):
    clean_key = api_key.strip()
    models, _ = get_valid_model_list(clean_key)
    if not models: return None
    for m_name in models:
        try:
            m = genai.GenerativeModel(m_name)
            resp = m.generate_content(prompt, request_options={"timeout": 60})
            if resp and resp.text: return resp.text.strip()
        except: continue
    return None

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

    api_key = get_api_key()
    if not api_key: return orig_text, orig_opts

    try:
        lang_target_desc = "專業醫學英文 (USMLE English)" if target_lang == "en" else "繁體中文（台灣醫學常用術語）"
        prompt = f'請將以下題目翻譯為【{lang_target_desc}】。\n題目：{orig_text}\n選項：{orig_opts}\n\n以標準 JSON 格式輸出：\n{{"text": "...", "options": [...]}}'
        parsed_list, _ = call_gemini_guaranteed(prompt, api_key)
        parsed = parsed_list[0] if parsed_list else {}
        trans_text = parsed.get('text', orig_text)
        trans_opts = parsed.get('options', orig_opts)
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
    except Exception as e: return f"[Word 提取錯誤: {e}]"

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
    except Exception as e: return f"[PPT 提取錯誤: {e}]"

# ================= 1. 測驗與練習狀態管理 =================
if 'exam_active' not in st.session_state: st.session_state.exam_active = False
if 'exam_finished' not in st.session_state: st.session_state.exam_finished = False
if 'exam_mode' not in st.session_state: st.session_state.exam_mode = "practice"
if 'exam_questions_cache' not in st.session_state: st.session_state.exam_questions_cache = []
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

# ---------- 【🎓 分頁一：刷題練習】 ----------
with tab_practice:
    if st.session_state.exam_active and st.session_state.exam_mode == "test":
        st.info("⚠️ 正在進行模擬測驗，請前往「📝 模擬測驗」分頁繼續。")
    elif not st.session_state.exam_active:
        st.markdown("### 🎓 刷題練習模式（做一題、對一題、看詳解）")
        folders = get_all_folders()
        if not folders: 
            st.warning("題庫空空如也，請先到「📥 匯入題庫」上傳考卷或講義！")
            if st.button("🔄 重新整理 / 喚醒雲端資料庫", use_container_width=True, key="wake_p"):
                get_db_connection.clear()
                st.rerun()
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
                    query = f"SELECT id, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred, text_en, options_en, text_zh, options_zh FROM questions WHERE category IN ({placeholders})" + (" AND is_starred=1" if only_star_q else "") + (" ORDER BY category ASC, id ASC" if q_order == "照考卷順序" else "")
                    c.execute(query, tuple(list(selected_pdfs)))
                    q_pool = c.fetchall()
                    if not q_pool: st.warning("無符合題目！")
                    else:
                        q_pool_dicts = [dict(q) for q in q_pool]
                        tot_f = len(q_pool_dicts)
                        pick_amt = tot_f if q_count_option == "全部題目" else int(q_count_option.replace(" 題", ""))
                        if q_order == "隨機挑題":
                            random.shuffle(q_pool_dicts)
                            st.session_state.exam_questions_cache = q_pool_dicts[:min(pick_amt, tot_f)]
                        else:
                            s_idx = max(0, min(int(start_q_num) - 1, tot_f - 1))
                            st.session_state.exam_questions_cache = q_pool_dicts[s_idx : s_idx + pick_amt]

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

    elif st.session_state.exam_active and st.session_state.exam_mode == "practice":
        total_q = len(st.session_state.exam_questions_cache)
        if total_q == 0:
            st.session_state.exam_active = False
            st.rerun()
            
        st.session_state.exam_index = max(0, min(st.session_state.exam_index, total_q - 1))
        idx = st.session_state.exam_index
        curr_q = st.session_state.exam_questions_cache[idx]
        curr_q_id = curr_q['id']

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
                conn.commit()
                curr_q['is_starred'] = new_star
                st.rerun()

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
                    api_key = get_api_key()
                    if not api_key: st.error("請先輸入 API Key！")
                    else:
                        with st.spinner("撰寫中..."):
                            exp_prompt = f"題目：{disp_text}\n選項：{disp_options}\n正確答案：{curr_q['answer']}\n請用繁體中文詳解原因與機轉。"
                            ai_res = call_gemini_text_fallback(exp_prompt, api_key)
                            if ai_res:
                                c.execute("UPDATE questions SET explanation=? WHERE id=?", (ai_res, curr_q_id))
                                conn.commit()
                                curr_q['explanation'] = ai_res
                                st.rerun()
                            else: st.error("生成失敗，API 額度可能已滿。")

            c.execute("SELECT explanation_image FROM questions WHERE id=?", (curr_q_id,))
            img_row = c.fetchone()
            curr_img = img_row['explanation_image'] if img_row else ""
            if curr_img: st.image(curr_img, use_container_width=True)

            with st.expander("✏️ 編輯本題解析 / 上傳筆記截圖"):
                rev_exp_input = st.text_area("文字解析：", value=curr_q.get('explanation', ''), key=f"p_rev_txt_{curr_q_id}")
                rev_img_input = st.file_uploader("更換筆記截圖 (PNG, JPG)", type=["png", "jpg", "jpeg"], key=f"p_rev_img_{curr_q_id}")
                if st.button("💾 儲存筆記", key=f"p_save_note_{curr_q_id}", type="primary"):
                    new_img_b64 = curr_img
                    if rev_img_input: new_img_b64 = compress_image_to_base64(rev_img_input)
                    c.execute("UPDATE questions SET explanation=?, explanation_image=? WHERE id=?", (rev_exp_input.strip(), new_img_b64, curr_q_id))
                    conn.commit()
                    curr_q['explanation'] = rev_exp_input.strip()
                    st.toast("✅ 筆記已極速存入雲端！"); st.rerun()

            col_prev, col_next = st.columns(2)
            with col_prev:
                if st.button("⬅️ 看上一題", disabled=(idx == 0), use_container_width=True, key="p_prev"):
                    flush_pending_updates()
                    st.session_state.exam_index -= 1
                    check_prev_id = st.session_state.exam_questions_cache[st.session_state.exam_index]['id']
                    st.session_state.practice_answered = (check_prev_id in st.session_state.exam_user_answers)
                    st.rerun()
            with col_next:
                if st.button("🏁 完成" if idx >= total_q - 1 else "➡️ 下一題", type="primary", use_container_width=True, key="p_next"):
                    flush_pending_updates()
                    if idx >= total_q - 1: st.session_state.exam_active = False
                    else:
                        st.session_state.exam_index += 1
                        check_next_id = st.session_state.exam_questions_cache[st.session_state.exam_index]['id']
                        st.session_state.practice_answered = (check_next_id in st.session_state.exam_user_answers)
                    st.rerun()

# ---------- 【📝 分頁二：模擬測驗】 ----------
with tab_test:
    if st.session_state.exam_active and st.session_state.exam_mode == "practice":
        st.info("⚠️ 正在進行刷題練習，請前往「🎓 刷題練習」分頁繼續。")
    elif not st.session_state.exam_active and not st.session_state.exam_finished:
        st.markdown("### 📝 模擬測驗模式（全卷作答，最後統一給分結算）")
        folders = get_all_folders()
        if not folders: 
            st.warning("請先匯入考卷！")
            if st.button("🔄 重新整理 / 喚醒雲端資料庫", use_container_width=True, key="wake_t"):
                get_db_connection.clear()
                st.rerun()
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
                    query = f"SELECT id, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred, text_en, options_en, text_zh, options_zh FROM questions WHERE category IN ({placeholders})" + (" AND is_starred=1" if only_star_q else "")
                    c.execute(query, tuple(list(selected_pdfs)))
                    q_pool = c.fetchall()
                    if not q_pool: st.warning("無符合題目！")
                    else:
                        q_pool_dicts = [dict(q) for q in q_pool]
                        tot_f = len(q_pool_dicts)
                        pick_amt = tot_f if q_count_option == "全部題目" else int(q_count_option.replace(" 題", ""))
                        if q_order.startswith("隨機"): random.shuffle(q_pool_dicts)
                        st.session_state.exam_questions_cache = q_pool_dicts[:min(pick_amt, tot_f)]
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

    elif st.session_state.exam_active and st.session_state.exam_mode == "test":
        total_q = len(st.session_state.exam_questions_cache)
        if total_q == 0:
            st.session_state.exam_active = False
            st.rerun()
            
        st.session_state.exam_index = max(0, min(st.session_state.exam_index, total_q - 1))
        idx = st.session_state.exam_index
        curr_q = st.session_state.exam_questions_cache[idx]
        curr_q_id = curr_q['id']

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
                for q_item in st.session_state.exam_questions_cache:
                    qid = q_item['id']
                    u_idx = st.session_state.exam_user_answers.get(qid, None)
                    if u_idx is not None and check_is_correct(u_idx, q_item): curr_correct.append(qid)
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
                conn.commit()
                curr_q['is_starred'] = new_star
                st.rerun()

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

    elif st.session_state.exam_finished and st.session_state.exam_mode == "test":
        st.balloons()
        st.success("🎉 測驗完成！成績統計如下：")
        curr_wrong = st.session_state.exam_wrong_ids
        curr_correct = st.session_state.exam_correct_ids
        total_questions = len(st.session_state.exam_questions_cache)
        score = (len(curr_correct) / total_questions * 100) if total_questions > 0 else 0

        col_m1, col_m2, col_m3, col_m4 = st.columns(4)
        col_m1.metric("最終得分", f"{score:.1f} 分")
        col_m2.metric("🟢 答對", f"{len(curr_correct)} 題")
        col_m3.metric("🔴 答錯", f"{len(curr_wrong)} 題")
        col_m4.metric("⏱️ 總耗時", st.session_state.exam_total_time_str)

        if st.button("🔄 回到首頁 / 重新開始", use_container_width=True):
            st.session_state.exam_active, st.session_state.exam_finished = False, False
            st.session_state.exam_questions_cache, st.session_state.exam_user_answers = [], {}
            st.rerun()

# ---------- 【📖 分頁三：錯題總覽區 (支援單題與批次刪除)】 ----------
with tab_review:
    if st.session_state.exam_active or st.session_state.exam_finished:
        st.info("⚡ 系統運作中，背景查詢已自動暫停。")
    else:
        st.markdown("### 📖 各考卷題目總覽與管理")
        folders = get_all_folders()
        if not folders: 
            st.info("目前沒有題庫資料。")
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

                # 批次刪除誤傳題目
                with st.expander("🧹 批次勾選刪除題目（一次刪多題）", expanded=False):
                    q_select_options = {f"ID {q['id']} - {q['text'][:35]}...": q['id'] for q in questions_to_show}
                    selected_q_to_delete = st.multiselect("勾選欲刪除的題目：", list(q_select_options.keys()))
                    if selected_q_to_delete:
                        if st.button("🚨 確認刪除選中的題目", type="primary", use_container_width=True):
                            delete_ids = [q_select_options[k] for k in selected_q_to_delete]
                            placeholders = ','.join(['?'] * len(delete_ids))
                            c.execute(f"DELETE FROM questions WHERE id IN ({placeholders})", tuple(delete_ids))
                            conn.commit()
                            st.success(f"✅ 成功刪除 {len(delete_ids)} 道題目！")
                            time.sleep(1)
                            st.rerun()

                st.divider()
                for idx, q in enumerate(questions_to_show):
                    is_st = bool(q.get('is_starred', 0))
                    q_opts = get_question_options(q)
                    with st.expander(f"{'⭐ ' if is_st else ''}第 {idx+1} 題 (ID: {q['id']}): {q['text'][:30]}... (錯 {q['wrong_count']} 次)"):
                        st.markdown(f"**【題目】** {q['text']}")
                        for opt_i, opt_text in enumerate(q_opts):
                            st.markdown(f"- ({chr(65 + opt_i) if opt_i < 26 else str(opt_i + 1)}) {opt_text}")
                        st.markdown(f"✅ **正確答案**：`{q['answer']}`")
                        exp_text = q.get('explanation', '') if q.get('explanation') and q['explanation'].strip() and q['explanation'] != '無提供詳解' else "尚未填寫詳解"
                        st.markdown(f"💡 **解析**：{exp_text}")
                        if q.get('explanation_image'): st.image(q['explanation_image'], use_container_width=True)
                        
                        col_edt, col_del_single = st.columns([3, 1])
                        with col_del_single:
                            if st.button("🗑️ 刪除此題", key=f"del_single_q_{q['id']}", use_container_width=True):
                                c.execute("DELETE FROM questions WHERE id=?", (q['id'],))
                                conn.commit()
                                st.toast(f"✅ 已刪除題目 ID: {q['id']}")
                                time.sleep(0.5)
                                st.rerun()

                        with st.expander("✏️ 編輯詳解 / 更換筆記截圖"):
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
    st.markdown("### 🧠 AI 模擬出題")
    st.info("💡 仿題出題功能需耗費 API 額度。若當前額度耗盡，請至「⚙️ 設定」換上全新 Gmail 申請的 API Key。")

# ---------- 【📥 分頁五：匯入區 (全面支援極速本地正則引擎)】 ----------
with tab_import:
    if st.session_state.exam_active or st.session_state.exam_finished:
        st.info("⚡ 刷題進行中，此面板已自動休眠。")
    else:
        st.markdown("### 📥 題庫匯入區")
        existing_folders = get_all_folders()
        folder_choice = st.selectbox("📂 目標資料夾：", ["-- ➕ 新增資料夾 --"] + existing_folders)
        target_folder = st.text_input("輸入新資料夾名稱：", "大三/生化") if folder_choice == "-- ➕ 新增資料夾 --" else folder_choice

        engine_type = st.radio(
            "⚙️ 選擇匯入引擎：",
            ["⚡ 本地免 AI 極速解析（推薦！零額度消耗，永遠不怕被鎖）", "🤖 AI 智慧解析（需 API 額度）"]
        )

        import_mode = st.radio("資料來源：", ["📁 檔案上傳 (PDF, Word, PPT)", "📋 純文字直接貼上"])

        if import_mode.startswith("📁"):
            uploaded_file = st.file_uploader("上傳試卷檔案", type=["pdf", "docx", "pptx", "txt", "md"])
            custom_name = st.text_input("📄 考卷自訂名稱：", value=uploaded_file.name if uploaded_file else "")
            
            if st.button("🚀 開始匯入", type="primary") and uploaded_file:
                final_name = custom_name.strip() if custom_name else uploaded_file.name
                file_bytes = uploaded_file.getvalue()
                fname = uploaded_file.name.lower()

                full_text = ""
                if fname.endswith('.pdf'):
                    if HAS_PYPDF:
                        reader = pypdf.PdfReader(io.BytesIO(file_bytes))
                        full_text = "\n".join([(page.extract_text() or "") for page in reader.pages])
                elif fname.endswith('.docx'): full_text = extract_text_from_docx(file_bytes)
                elif fname.endswith('.pptx'): full_text = extract_text_from_pptx(file_bytes)
                else: full_text = file_bytes.decode('utf-8', errors='ignore')

                if not full_text.strip():
                    st.error("⚠️ 無法讀取文字，請確認檔案不是純掃描圖片。")
                else:
                    if engine_type.startswith("⚡ 本地"):
                        with st.spinner("正在執行本地正則極速分析切題..."):
                            questions = parse_questions_locally(full_text)
                            if questions:
                                for nq in questions:
                                    opts_clean = [str(o).strip() for o in nq.get('options', []) if str(o).strip()]
                                    opts_json = json.dumps(opts_clean, ensure_ascii=False)
                                    c.execute("INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                                              (target_folder, final_name, nq['text'], opts_clean[0] if len(opts_clean)>0 else "", opts_clean[1] if len(opts_clean)>1 else "", opts_clean[2] if len(opts_clean)>2 else "", opts_clean[3] if len(opts_clean)>3 else "", opts_json, nq['answer'], nq['explanation']))
                                conn.commit()
                                st.success(f"🎉 本地免 AI 引擎成功切分並存入 {len(questions)} 題！（完全不耗 API 額度）")
                                time.sleep(1)
                                st.rerun()
                            else:
                                st.warning("⚠️ 本地規則未能切分出題目，請確認檔案是否包含題號與選項。")
                    else:
                        api_key = get_api_key()
                        if not api_key:
                            st.error("請先至「⚙️ 設定」輸入 Gemini API Key！")
                        else:
                            parse_prompt = "請從文本中提取所有選擇題，嚴格輸出 JSON 陣列：[{\"text\":\"...\",\"options\":[\"(A)...\",\"(B)...\"],\"answer\":\"A\",\"explanation\":\"\"}]"
                            with st.spinner("AI 解析中..."):
                                questions, err_msg = call_gemini_guaranteed([parse_prompt, full_text[:12000]], api_key)
                                if questions:
                                    for nq in questions:
                                        opts_clean = [str(o).strip() for o in nq.get('options', []) if str(o).strip()]
                                        opts_json = json.dumps(opts_clean, ensure_ascii=False)
                                        c.execute("INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                                                  (target_folder, final_name, nq.get('text', ''), opts_clean[0] if len(opts_clean)>0 else "", opts_clean[1] if len(opts_clean)>1 else "", opts_clean[2] if len(opts_clean)>2 else "", opts_clean[3] if len(opts_clean)>3 else "", opts_json, str(nq.get('answer', '')), nq.get('explanation', '')))
                                    conn.commit()
                                    st.success(f"🎉 AI 成功存入 {len(questions)} 題！")
                                    time.sleep(1)
                                    st.rerun()
                                else: st.error(err_msg)
        else:
            paste_category_name = st.text_input("📄 考卷/講義名稱：", value="未命名考卷")
            ptext = st.text_area("請在此貼上題目文字：", height=250)
            if st.button("🚀 開始匯入", type="primary") and ptext.strip():
                if engine_type.startswith("⚡ 本地"):
                    questions = parse_questions_locally(ptext)
                    if questions:
                        for nq in questions:
                            opts_clean = [str(o).strip() for o in nq.get('options', []) if str(o).strip()]
                            opts_json = json.dumps(opts_clean, ensure_ascii=False)
                            c.execute("INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                                      (target_folder, paste_category_name.strip(), nq['text'], opts_clean[0] if len(opts_clean)>0 else "", opts_clean[1] if len(opts_clean)>1 else "", opts_clean[2] if len(opts_clean)>2 else "", opts_clean[3] if len(opts_clean)>3 else "", opts_json, nq['answer'], nq['explanation']))
                        conn.commit()
                        st.success(f"🎉 本地免 AI 引擎成功切分並存入 {len(questions)} 題！（完全不耗 API 額度）")
                        time.sleep(1)
                        st.rerun()
                    else: st.warning("⚠️ 未能辨識題目格式，請確認是否包含題號與 (A)(B)(C)(D) 選項。")
                else:
                    api_key = get_api_key()
                    if not api_key: st.error("請先前往「⚙️ 設定」輸入 Gemini API Key！")
                    else:
                        parse_prompt = "請從文本中提取所有選擇題，嚴格輸出 JSON 陣列：[{\"text\":\"...\",\"options\":[\"(A)...\",\"(B)...\"],\"answer\":\"A\",\"explanation\":\"\"}]"
                        with st.spinner("AI 解析中..."):
                            questions, err_msg = call_gemini_guaranteed([parse_prompt, ptext], api_key)
                            if questions:
                                for nq in questions:
                                    opts_clean = [str(o).strip() for o in nq.get('options', []) if str(o).strip()]
                                    opts_json = json.dumps(opts_clean, ensure_ascii=False)
                                    c.execute("INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, options, answer, explanation, is_starred, pdf_starred) VALUES (?,?,?,?,?,?,?,?,?,?,0,0)",
                                              (target_folder, paste_category_name.strip(), nq.get('text', ''), opts_clean[0] if len(opts_clean)>0 else "", opts_clean[1] if len(opts_clean)>1 else "", opts_clean[2] if len(opts_clean)>2 else "", opts_clean[3] if len(opts_clean)>3 else "", opts_json, str(nq.get('answer', '')), nq.get('explanation', '')))
                                conn.commit()
                                st.success(f"🎉 AI 成功存入 {len(questions)} 題！")
                                time.sleep(1)
                                st.rerun()
                            else: st.error(err_msg)

# ---------- 【⚙️ 分頁六：設定與資料夾完整管理區】 ----------
with tab_settings:
    if st.session_state.exam_active or st.session_state.exam_finished:
        st.info("⚡ 刷題進行中，此面板已自動休眠。")
    else:
        st.subheader("🔑 API Key 狀態與設定")
        current_saved_key = get_api_key()
        if current_saved_key:
            st.info(f"當前生效中的 API Key 後四碼為：`...{current_saved_key[-4:]}`")
        else:
            st.warning("⚠️ 目前尚未設定 API Key")

        col_k1, col_k2 = st.columns([3, 1])
        with col_k1:
            new_key = st.text_input("輸入 Gemini API Key", value="", type="password", placeholder="貼上全新 Gmail 申請的 API Key")
        with col_k2:
            st.write("")
            st.write("")
            if st.button("💾 儲存 Key", type="primary", use_container_width=True):
                clean_k = new_key.strip()
                if clean_k:
                    c.execute("REPLACE INTO settings (key, value) VALUES ('gemini_api_key', ?)", (clean_k,))
                    conn.commit()
                    st.success("✅ 已儲存至雲端資料庫！")
                    time.sleep(1)
                    st.rerun()

        if current_saved_key and st.button("🧪 測試當前 API Key 能否正常通訊", use_container_width=True):
            with st.spinner("連線 Google 測試中..."):
                valid_models, test_err = get_valid_model_list(current_saved_key)
                if not valid_models:
                    st.error(f"❌ 驗證失敗：{test_err}")
                else:
                    try:
                        m = genai.GenerativeModel(valid_models[0])
                        res = m.generate_content("Ping")
                        st.success(f"✅ 連線成功！此 Key 完全正常，首選模型：`{valid_models[0]}`")
                    except Exception as e:
                        st.error(f"❌ 呼叫受限（此 Google 帳號今日額度可能已滿）：{e}")

        st.divider()

        # 批次管理考卷
        st.subheader("📦 批次管理考卷 (多選移動 / 刪除)")
        try:
            c.execute("SELECT folder, category FROM questions GROUP BY folder, category")
            all_items = c.fetchall()
            all_folders = get_all_folders()
            
            item_options = [f"📂 {r['folder']} ＞ 📄 {r['category']}" for r in all_items]
            selected_batch = st.multiselect("✅ 勾選你要處理的考卷 (支援多選)：", item_options)
            
            if selected_batch:
                st.info(f"已選取 {len(selected_batch)} 份考卷，請選擇下方動作：")
                col_b1, col_b2 = st.columns(2)
                with col_b1:
                    batch_target = st.selectbox("🎯 批次移動至資料夾：", ["-- 手動輸入全新路徑 (自由建立上下層) --"] + all_folders, key="batch_move_sel")
                    batch_final_folder = batch_target
                    if batch_target == "-- 手動輸入全新路徑 (自由建立上下層) --":
                        batch_final_folder = st.text_input("✏️ 輸入全新路徑 (用 / 分隔，例如：大三/生化/期中)：", key="batch_move_txt")
                    
                    if st.button("🚀 執行批次移動", type="primary", use_container_width=True):
                        if batch_final_folder.strip():
                            clean_dst = batch_final_folder.strip().replace('//', '/')
                            for sel in selected_batch:
                                f_part, c_part = sel.split(" ＞ 📄 ")
                                orig_f = f_part.replace("📂 ", "")
                                orig_c = c_part
                                c.execute("UPDATE questions SET folder=? WHERE folder=? AND category=?", (clean_dst, orig_f, orig_c))
                            conn.commit()
                            st.success(f"✅ 成功將 {len(selected_batch)} 份考卷移動至「{clean_dst}」！")
                            time.sleep(1)
                            st.rerun()
                        else: st.error("請輸入目標資料夾名稱！")
                            
                with col_b2:
                    st.write("")
                    st.write("")
                    if st.button("🗑️ 批次刪除選取的考卷", use_container_width=True):
                        for sel in selected_batch:
                            f_part, c_part = sel.split(" ＞ 📄 ")
                            orig_f = f_part.replace("📂 ", "")
                            orig_c = c_part
                            c.execute("DELETE FROM questions WHERE folder=? AND category=?", (orig_f, orig_c))
                            c.execute("DELETE FROM exam_history WHERE category=?", (orig_c,))
                        conn.commit()
                        st.success(f"✅ 成功刪除 {len(selected_batch)} 份考卷！")
                        time.sleep(1)
                        st.rerun()
        except Exception as e: pass

        st.divider()

        # 單一考卷詳細管理
        st.subheader("📄 單一考卷詳細管理 (更名 / 星號 / 移動 / 刪除)")
        st.caption("💡 直接修改路徑即可自由建立上下層資料夾，例如在前面加上 `大三/` 就能包進母資料夾。")
        try:
            c.execute("SELECT folder, category, MAX(pdf_starred) as pdf_starred FROM questions GROUP BY folder, category")
            items = c.fetchall()
            for index, row in enumerate(items):
                f_name, p_name = row['folder'], row['category']
                with st.expander(f"{'⭐ ' if bool(row['pdf_starred']) else ''}📂 {f_name} ＞ 📄 {p_name}"):
                    new_p_name = st.text_input("📄 考卷名稱", value=p_name, key=f"p_rename_{index}")
                    new_f_name = st.text_input("📂 所屬資料夾 (用 / 分隔建立多層級)", value=f_name, key=f"p_folder_{index}")

                    col_save, col_star_pdf, col_del = st.columns(3)
                    if col_save.button("💾 儲存變更", key=f"save_{index}", use_container_width=True):
                        clean_folder = new_f_name.strip().replace('//', '/')
                        c.execute("UPDATE questions SET category=?, folder=? WHERE folder=? AND category=?", (new_p_name.strip(), clean_folder, f_name, p_name))
                        c.execute("UPDATE exam_history SET category=? WHERE category=?", (new_p_name.strip(), p_name))
                        conn.commit(); st.rerun()
                    if col_star_pdf.button("⭐ 標記星號" if not bool(row['pdf_starred']) else "☆ 取消星號", key=f"star_pdf_{index}", use_container_width=True):
                        c.execute("UPDATE questions SET pdf_starred=? WHERE folder=? AND category=?", (0 if bool(row['pdf_starred']) else 1, f_name, p_name))
                        conn.commit(); st.rerun()
                    if col_del.button("🗑️ 刪除此卷", key=f"del_{index}", use_container_width=True):
                        c.execute("DELETE FROM questions WHERE folder=? AND category=?", (f_name, p_name))
                        conn.commit(); st.rerun()
        except: pass

        st.divider()

        # 整個資料夾階層更名與移動
        st.subheader("🗂️ 整個資料夾階層更名與移動")
        st.caption("💡 想要把整個 `生化` 移進 `大三` 變成 `大三/生化`？直接在下方修改完整路徑即可，所有子資料夾會自動跟隨！")
        try:
            all_folders_rename = get_all_folders()
            if all_folders_rename:
                old_folder_name = st.selectbox("📦 選擇要修改的資料夾：", all_folders_rename, key="rename_folder_select")
                new_folder_name = st.text_input("✏️ 編輯完整路徑 (隨意增減上下層次)：", value=old_folder_name, key="rename_folder_input")
                if st.button("🚀 執行結構修改", use_container_width=True):
                    if new_folder_name.strip() and new_folder_name.strip() != old_folder_name:
                        new_path_clean = new_folder_name.strip().replace('//', '/')
                        c.execute("SELECT DISTINCT folder FROM questions WHERE folder = ? OR folder LIKE ?", (old_folder_name, f"{old_folder_name}/%"))
                        folders_to_update = [row[0] for row in c.fetchall()]
                        for f in folders_to_update:
                            if f == old_folder_name:
                                c.execute("UPDATE questions SET folder=? WHERE folder=?", (new_path_clean, f))
                            else:
                                sub_path = f[len(old_folder_name):] 
                                c.execute("UPDATE questions SET folder=? WHERE folder=?", (new_path_clean + sub_path, f))
                        conn.commit()
                        st.success(f"✅ 整個資料夾結構已成功更新為「{new_path_clean}」！")
                        time.sleep(1)
                        st.rerun()
                    elif new_folder_name.strip() == old_folder_name:
                        st.warning("路徑沒有變更喔！")
                    else: st.warning("請輸入新路徑！")
        except: pass
