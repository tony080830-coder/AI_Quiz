import streamlit as st
import json
import random
import time
import os
import google.generativeai as genai

# ================= 0. Turso 雲端 SQLite 連線設定 =================
# 請將在 Turso 控制台複製的網址與 Token 貼在下方引號中
TURSO_DB_URL = "libsql://quiz-db-tony080830-coder.aws-ap-northeast-1.turso.io"      # 例："libsql://quiz-db-yourname.turso.io"
TURSO_AUTH_TOKEN = "eyJhbGciOiJFZERTQSIsInR5cCI6IkpXVCJ9.eyJqdGkiOiJDRUFwOHJsZ0VmR0R1MDdXRWFlUHR3Iiwib3JnX2lkIjoxMDAwMjU0OTEzfQ.Qa4_xPGT1uowZV9gdyj4hux4Kz5ESoQvIhEHbNoHI6ciad6h9dQrP-ekbJyrHJbjlWObzuhCMnCDiWP9CX6iBg"  # 你的 Turso 驗證 Token

try:
    if TURSO_DB_URL and TURSO_AUTH_TOKEN:
        import libsql_experimental as sqlite3
        conn = sqlite3.connect(TURSO_DB_URL, auth_token=TURSO_AUTH_TOKEN)
        IS_CLOUD = True
    else:
        import sqlite3
        conn = sqlite3.connect('quiz_database.db', check_same_thread=False)
        IS_CLOUD = False
except Exception as e:
    import sqlite3
    conn = sqlite3.connect('quiz_database.db', check_same_thread=False)
    IS_CLOUD = False

conn.row_factory = sqlite3.Row 
c = conn.cursor()

# ================= 1. 資料庫初始化 & 自動升級 =================
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
conn.commit()

def get_api_key():
    c.execute("SELECT value FROM settings WHERE key='gemini_api_key'")
    result = c.fetchone()
    return result['value'] if result else ""

# ================= 2. 狀態管理 =================
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
if 'exam_tested_pdfs' not in st.session_state: st.session_state.exam_tested_pdfs = []

def check_answer(selected, correct, q_id):
    st.session_state.answered = True
    if selected == correct:
        st.session_state.is_correct = True
        c.execute("UPDATE questions SET wrong_count = MAX(0, wrong_count - 1) WHERE id=?", (q_id,))
    else:
        st.session_state.is_correct = False
        c.execute("UPDATE questions SET wrong_count = wrong_count + 1 WHERE id=?", (q_id,))
    conn.commit()

# ================= 3. 網頁介面開始 =================
st.set_page_config(page_title="AI 錯題本", page_icon="📝", layout="centered")
st.title("📝 AI 專屬錯題本系統")

if IS_CLOUD:
    st.caption("☁️ 連線狀態：已連線至 Turso 雲端資料庫 (重啟永不丟失)")
else:
    st.caption("🖥️ 連線狀態：本地暫存模式 (填入 Turso URL 與 Token 即可啟用自動雲端保存)")

tab_quiz, tab_review, tab_import, tab_settings = st.tabs(["🎯 開始測驗", "📖 錯題總覽", "📥 匯入題庫", "⚙️ 設定與管理"])

# ---------- 【測驗區】 ----------
with tab_quiz:
    c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL")
    folders = [row['folder'] for row in c.fetchall() if row['folder']]
    
    if not folders:
        st.warning("題庫空空如也，請先到「匯入題庫」上傳題目！")
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
            c.execute(pdf_query, pdf_params)
            pdf_rows = c.fetchall()
            all_available_pdfs = [row['category'] for row in pdf_rows if row['category']]
            pdf_star_map = {row['category']: bool(row['pdf_starred']) for row in pdf_rows}

            selected_pdfs = st.multiselect(
                "📄 勾選要練習的 PDF 考卷（可複選混合出題）：",
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
                    st.error("請至少勾選一份 PDF 考卷！")
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

                    c.execute(query, params)
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

            answered_so_far = len(st.session_state.exam_correct_ids) + len(st.session_state.exam_wrong_ids)
            st.progress((idx) / total_q)
            col_prog, col_quit = st.columns([3, 1.2])
            with col_prog:
                st.caption(f"第 {idx + 1} / {total_q} 題 | 來源：{curr_q['category']} | 已答：對 {len(st.session_state.exam_correct_ids)} / 錯 {len(st.session_state.exam_wrong_ids)}")
            with col_quit:
                if st.button("⏹️ 隨時結算", help="隨時中斷並查看目前答題報告", use_container_width=True):
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

            options = [curr_q['opt1'], curr_q['opt2'], curr_q['opt3'], curr_q['opt4']]
            correct_ans = curr_q['answer']
            q_id = curr_q['id']

            if not st.session_state.answered:
                for opt in options:
                    if st.button(opt, key=f"ex_btn_{opt}", use_container_width=True):
                        check_answer(opt, correct_ans, q_id)
                        if st.session_state.is_correct:
                            if q_id not in st.session_state.exam_correct_ids:
                                st.session_state.exam_correct_ids.append(q_id)
                        else:
                            if q_id not in st.session_state.exam_wrong_ids:
                                st.session_state.exam_wrong_ids.append(q_id)
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
                    if not st.session_state.is_correct and st.button("📖 查看詳解" if has_exp else "🧠 AI 即時補寫詳解", key=f"ex_exp_{q_id}", use_container_width=True):
                        if has_exp:
                            st.session_state.explanation = curr_q['explanation']
                        else:
                            api_key = get_api_key()
                            if not api_key: st.error("請至設定輸入 API Key！")
                            else:
                                with st.spinner("AI 正在為這題撰寫詳解..."):
                                    try:
                                        genai.configure(api_key=api_key)
                                        model = genai.GenerativeModel('gemini-3.8-flash')
                                        prompt = f"題目：{curr_q['text']}\n選項：{options}\n正解：{correct_ans}\n請詳細解釋這題觀念，告訴我為什麼錯。"
                                        resp = model.generate_content(prompt)
                                        c.execute("UPDATE questions SET explanation=? WHERE id=?", (resp.text, q_id))
                                        conn.commit()
                                        st.session_state.explanation = resp.text
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"呼叫 AI 失敗：{e}")

                if st.session_state.explanation:
                    st.info(st.session_state.explanation)

        elif st.session_state.exam_finished:
            st.balloons()
            st.success("🎉 測驗已結束！本次練習總結如下：")

            curr_wrong = set(st.session_state.exam_wrong_ids)
            curr_correct = set(st.session_state.exam_correct_ids)
            total_tested = len(curr_wrong) + len(curr_correct)
            score = (len(curr_correct) / total_tested * 100) if total_tested > 0 else 0

            col_m1, col_m2, col_m3 = st.columns(3)
            col_m1.metric("答對率", f"{score:.1f}%")
            col_m2.metric("🟢 答對題數", f"{len(curr_correct)} 題")
            col_m3.metric("🔴 答錯題數", f"{len(curr_wrong)} 題")

            if len(st.session_state.exam_tested_pdfs) == 1:
                single_pdf = st.session_state.exam_tested_pdfs[0]
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

            st.divider()

            if curr_wrong:
                if st.button("⭐ 一鍵將本次答錯的題目加入星號收藏", type="primary"):
                    placeholders = ','.join(['?'] * len(curr_wrong))
                    c.execute(f"UPDATE questions SET is_starred=1 WHERE id IN ({placeholders})", list(curr_wrong))
                    conn.commit()
                    st.toast("✅ 本次錯題已全部加為星號收藏！")
                    time.sleep(0.5)

                st.subheader(f"❌ 本次答錯檢討 ({len(curr_wrong)} 題)")
                for qid in curr_wrong:
                    c.execute("SELECT * FROM questions WHERE id=?", (qid,))
                    q_data = c.fetchone()
                    if q_data:
                        with st.expander(f"[{q_data['category']}] {q_data['text'][:30]}..."):
                            st.markdown(f"**題目**：{q_data['text']}")
                            st.markdown(f"- (A) {q_data['opt1']}")
                            st.markdown(f"- (B) {q_data['opt2']}")
                            st.markdown(f"- (C) {q_data['opt3']}")
                            st.markdown(f"- (D) {q_data['opt4']}")
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
                st.rerun()

# ---------- 【錯題總覽區】 ----------
with tab_review:
    st.markdown("### 📖 各 PDF 完整題目與答案總覽")
    c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL")
    folders = [row['folder'] for row in c.fetchall() if row['folder']]
    
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
                "📄 選擇 PDF 考卷：", 
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
            
        c.execute(query, params)
        questions_to_show = c.fetchall()
        
        if not questions_to_show:
            st.warning("此分類下沒有找到題目。")
        else:
            st.write(f"共找到 **{len(questions_to_show)}** 題：")
            st.divider()
            for idx, q in enumerate(questions_to_show):
                is_st = bool(q['is_starred'])
                star_tag = "⭐ " if is_st else ""
                with st.expander(f"{star_tag}題目 {idx+1}: {q['text'][:30]}... (錯 {q['wrong_count']} 次)"):
                    st.markdown(f"**【題目】** {q['text']}")
                    st.markdown(f"- (A) {q['opt1']}")
                    st.markdown(f"- (B) {q['opt2']}")
                    st.markdown(f"- (C) {q['opt3']}")
                    st.markdown(f"- (D) {q['opt4']}")
                    st.markdown(f"✅ **正確答案**：`{q['answer']}`")
                    exp_text = q['explanation'] if q['explanation'] and q['explanation'].strip() and q['explanation'] != '無提供詳解' else "尚未生成詳解"
                    st.markdown(f"💡 **解析**：{exp_text}")
                    st.markdown(f"📌 **星號狀態**：{'⭐ 已收藏' if is_st else '☆ 未收藏'}")

# ---------- 【匯入區】 ----------
with tab_import:
    st.markdown("### 🤖 智慧 PDF 匯入")
    c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL")
    existing_folders = [row['folder'] for row in c.fetchall() if row['folder']]
    
    folder_choice = st.selectbox("📂 選擇目標資料夾：", ["-- ➕ 新增資料夾 --"] + existing_folders)
    if folder_choice == "-- ➕ 新增資料夾 --":
        target_folder = st.text_input("輸入新資料夾名稱", "新資料夾")
    else:
        target_folder = folder_choice
        
    uploaded_pdf = st.file_uploader("上傳考卷 PDF，AI 會自動切分並寫詳解！", type="pdf")
    default_pdf_name = uploaded_pdf.name if uploaded_pdf else ""
    custom_pdf_name = st.text_input("📄 編輯匯入後的 PDF 名稱：", value=default_pdf_name)
    
    if st.button("解析並匯入", type="primary") and uploaded_pdf:
        api_key = get_api_key()
        if not api_key: st.error("請先到設定頁面輸入 API Key！")
        else:
            with st.spinner("AI 努力閱讀並撰寫詳解中 (約需 15-30 秒)..."):
                try:
                    genai.configure(api_key=api_key)
                    model = genai.GenerativeModel('gemini-3.8-flash')
                    prompt = '請提取 PDF 中的「選擇題」。若無解答請補上正解，並為每一題撰寫詳細的解析。嚴格以 JSON 陣列格式輸出：[{"category":"分類","text":"題目","options":["A","B","C","D"],"answer":"正確選項","explanation":"詳細的解題觀念與原因"}]'
                    pdf_part = {"mime_type": "application/pdf", "data": uploaded_pdf.getvalue()}
                    
                    max_retries = 3
                    response = None
                    for attempt in range(max_retries):
                        try:
                            response = model.generate_content([prompt, pdf_part])
                            break
                        except Exception as e:
                            if "429" in str(e) and attempt < max_retries - 1:
                                st.warning(f"⏳ 觸發 API 限制，等待 60 秒後重試... ({attempt + 1}/{max_retries})")
                                time.sleep(60)
                            else:
                                raise e
                    
                    raw_text = response.text.replace('```json', '').replace('```', '').strip()
                    new_questions = json.loads(raw_text)
                    final_name = custom_pdf_name.strip() if custom_pdf_name else uploaded_pdf.name
                    
                    for nq in new_questions:
                        c.execute("INSERT INTO questions (folder, category, text, opt1, opt2, opt3, opt4, answer, explanation, is_starred, pdf_starred) VALUES (?,?,?,?,?,?,?,?,?,0,0)",
                                  (target_folder, final_name, nq['text'], nq['options'][0], nq['options'][1], nq['options'][2], nq['options'][3], nq['answer'], nq.get('explanation', '無提供詳解')))
                    conn.commit()
                    st.success(f"✅ 成功將 {len(new_questions)} 題匯入至「{target_folder} / {final_name}」！" + (" (已即時存入 Turso 雲端)" if IS_CLOUD else ""))
                except Exception as e:
                    st.error(f"解析失敗，詳細錯誤：{e}")

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
            restore_file = st.file_uploader("選取 .db 檔案以還原", type=["db"], label_visibility="collapsed")
            if restore_file:
                with open('quiz_database.db', "wb") as f:
                    f.write(restore_file.getvalue())
                st.success("✅ 題庫已成功還原！重新整理頁面中...")
                time.sleep(1)
                st.rerun()

    st.divider()
    st.subheader("📁 題庫管理 (名稱修改 / 移動 / 星號標記 / 刪除)")
    c.execute("SELECT folder, category, MAX(pdf_starred) as pdf_starred FROM questions GROUP BY folder, category")
    items = c.fetchall()
    
    if not items:
        st.info("目前沒有題庫資料。")
    else:
        c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL")
        all_folders = [row['folder'] for row in c.fetchall() if row['folder']]

        for index, row in enumerate(items):
            f_name = row['folder']
            p_name = row['category']
            is_pdf_st = bool(row['pdf_starred'])
            
            exp_title = f"{'⭐ ' if is_pdf_st else ''}📂 {f_name} ＞ 📄 {p_name}"
            with st.expander(exp_title):
                new_p_name = st.text_input("修改 PDF 名稱", value=p_name, key=f"p_rename_{index}")
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
                    
                if col_del.button("🗑️ 刪除考卷", key=f"del_{index}", use_container_width=True):
                    c.execute("DELETE FROM questions WHERE folder=? AND category=?", (f_name, p_name))
                    c.execute("DELETE FROM exam_history WHERE category=?", (p_name,))
                    conn.commit()
                    st.success("✅ 已刪除！")
                    time.sleep(0.5)
                    st.rerun()

        st.divider()
        st.subheader("✏️ 資料夾重新命名")
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
    c.execute("SELECT folder, category, text, wrong_count FROM questions WHERE wrong_count > 0 ORDER BY wrong_count DESC LIMIT 10")
    stats = c.fetchall()
    if stats:
        for s in stats: 
            st.write(f"❌ 錯 **{s['wrong_count']}** 次 | [{s['folder']}] {s['text'][:20]}...")
    else:
        st.write("目前沒有錯題紀錄！")
