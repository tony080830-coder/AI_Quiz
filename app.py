import streamlit as st
import sqlite3
import json
import random
import time
import os
import google.generativeai as genai

# ================= 1. 資料庫初始化 & 自動升級 =================
DB_FILE = 'quiz_database.db'
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
conn.row_factory = sqlite3.Row 
c = conn.cursor()

c.execute('''CREATE TABLE IF NOT EXISTS questions
             (id INTEGER PRIMARY KEY AUTOINCREMENT, 
              category TEXT, text TEXT, 
              opt1 TEXT, opt2 TEXT, opt3 TEXT, opt4 TEXT, 
              answer TEXT, wrong_count INTEGER DEFAULT 0)''')
c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')

# 自動檢查並平滑補上新欄位
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

def load_next_question(target_folder, target_pdf, only_starred=False):
    query = "SELECT * FROM questions WHERE 1=1"
    params = []
    if target_folder != "全部資料夾":
        query += " AND folder=?"
        params.append(target_folder)
    if target_pdf != "全部考卷":
        query += " AND category=?"
        params.append(target_pdf)
    if only_starred:
        query += " AND is_starred=1"
        
    query += " ORDER BY wrong_count DESC"
    c.execute(query, params)
    all_q = c.fetchall()
    if not all_q: 
        st.session_state.current_q = None
        return
    pool_size = max(1, len(all_q) // 2)
    st.session_state.current_q = random.choice(all_q[:pool_size])
    st.session_state.answered = False
    st.session_state.explanation = ""

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

tab_quiz, tab_review, tab_import, tab_settings = st.tabs(["🎯 開始測驗", "📖 錯題總覽", "📥 匯入題庫", "⚙️ 設定與管理"])

# ---------- 【測驗區】 ----------
with tab_quiz:
    c.execute("SELECT DISTINCT folder FROM questions WHERE folder IS NOT NULL")
    folders = [row['folder'] for row in c.fetchall() if row['folder']]
    
    if not folders:
        st.warning("題庫空空如也，請先到「匯入題庫」上傳題目，或到「設定與管理」還原題庫！")
    else:
        # 星號考卷快篩與星號題目開關
        col_star_opt1, col_star_opt2 = st.columns(2)
        with col_star_opt1:
            quiz_only_starred_pdf = st.checkbox("⭐ 僅顯示星號考卷", value=False, key="chk_starred_pdf")
        with col_star_opt2:
            quiz_only_starred_q = st.checkbox("⭐ 只練習星號收藏題目", value=False, key="chk_starred_q")
        
        col_f, col_p = st.columns(2)
        with col_f:
            selected_folder = st.selectbox("📁 選擇資料夾：", ["全部資料夾"] + folders, key="quiz_folder")
        
        # 取得 PDF 清單及其星號標記狀態
        pdf_query = "SELECT DISTINCT category, pdf_starred FROM questions WHERE 1=1"
        pdf_params = []
        if selected_folder != "全部資料夾":
            pdf_query += " AND folder=?"
            pdf_params.append(selected_folder)
        if quiz_only_starred_pdf:
            pdf_query += " AND pdf_starred=1"
            
        c.execute(pdf_query, pdf_params)
        pdf_rows = c.fetchall()
        pdf_names = [row['category'] for row in pdf_rows if row['category']]
        pdf_star_map = {row['category']: bool(row['pdf_starred']) for row in pdf_rows}
        
        with col_p:
            selected_pdf = st.selectbox(
                "📄 選擇 PDF 考卷：", 
                ["全部考卷"] + pdf_names, 
                key="quiz_pdf",
                format_func=lambda x: f"⭐ {x}" if pdf_star_map.get(x) else x
            )
        
        state_key = f"{selected_folder}_{selected_pdf}_{quiz_only_starred_q}_{quiz_only_starred_pdf}"
        if 'last_selected' not in st.session_state or st.session_state.last_selected != state_key:
            st.session_state.last_selected = state_key
            load_next_question(selected_folder, selected_pdf, quiz_only_starred_q)
            
        if st.session_state.current_q is None: 
            load_next_question(selected_folder, selected_pdf, quiz_only_starred_q)
            
        q = st.session_state.current_q
        if not q:
            if quiz_only_starred_q:
                st.info("此考卷/分類下目前沒有加星號的題目！可取消勾選星號題目進行全量練習。")
            else:
                st.info("查無題目，請切換分類或確認題庫。")
        else:
            # 題目卡片頭部 (含題目星號切換)
            col_info, col_star_btn = st.columns([4, 1.2])
            is_q_starred = bool(q['is_starred'])
            with col_info:
                st.caption(f"📂 {q['folder']} > 📄 {q['category']} | 歷史錯誤：{q['wrong_count']} 次")
            with col_star_btn:
                star_label = "⭐ 已收藏" if is_q_starred else "☆ 收藏"
                if st.button(star_label, key=f"star_toggle_{q['id']}", use_container_width=True):
                    new_star = 0 if is_q_starred else 1
                    c.execute("UPDATE questions SET is_starred=? WHERE id=?", (new_star, q['id']))
                    conn.commit()
                    # 重新拉取目前題目，不打亂當前做題
                    c.execute("SELECT * FROM questions WHERE id=?", (q['id'],))
                    st.session_state.current_q = c.fetchone()
                    st.rerun()
            
            st.subheader(q['text'])
            options = [q['opt1'], q['opt2'], q['opt3'], q['opt4']]
            correct_ans = q['answer']
            q_id = q['id']
            
            if not st.session_state.answered:
                for opt in options:
                    if st.button(opt, use_container_width=True):
                        check_answer(opt, correct_ans, q_id)
                        st.rerun()
            else:
                if st.session_state.is_correct:
                    st.success("✅ 答對了！")
                else:
                    st.error(f"❌ 答錯了！正確答案是：{correct_ans}")
                
                col1, col2 = st.columns(2)
                with col1:
                    if st.button("👉 下一題", use_container_width=True, type="primary"):
                        load_next_question(selected_folder, selected_pdf, quiz_only_starred_q)
                        st.rerun()
                with col2:
                    has_exp = bool(q['explanation'] and q['explanation'].strip() and q['explanation'] != '無提供詳解')
                    btn_label = "📖 查看詳解" if has_exp else "🧠 AI 即時補寫詳解"
                    btn_type = "secondary" if has_exp else "primary"
                    
                    if not st.session_state.is_correct and st.button(btn_label, use_container_width=True, type=btn_type):
                        if has_exp:
                            st.session_state.explanation = q['explanation']
                        else:
                            api_key = get_api_key()
                            if not api_key: st.error("請先到設定頁面輸入 API Key！")
                            else:
                                with st.spinner("AI 正在為這題撰寫詳解..."):
                                    try:
                                        genai.configure(api_key=api_key)
                                        model = genai.GenerativeModel('gemini-3.8-flash')
                                        prompt = f"題目：{q['text']}\n選項：{options}\n正解：{correct_ans}\n請詳細解釋這題觀念，告訴我為什麼錯。"
                                        response = model.generate_content(prompt)
                                        c.execute("UPDATE questions SET explanation=? WHERE id=?", (response.text, q_id))
                                        conn.commit()
                                        st.session_state.explanation = response.text
                                        st.rerun()
                                    except Exception as e:
                                        st.error(f"呼叫 AI 失敗：{e}")
                        
                if st.session_state.explanation:
                    st.info(st.session_state.explanation)

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
                    st.success(f"✅ 成功將 {len(new_questions)} 題匯入至「{target_folder} / {final_name}」！")
                    st.info("💡 提示：匯入完成後，建議到「⚙️ 設定與管理」點擊下載備份檔存至雲端！")
                    st.session_state.current_q = None
                except Exception as e:
                    st.error(f"解析失敗，詳細錯誤：{e}")

# ---------- 【設定與管理區】 ----------
with tab_settings:
    st.subheader("💾 題庫備份與還原 (防重啟遺失)")
    st.caption("匯入新考卷後，點擊下載備份檔儲存；若伺服器重啟題庫清空，隨時上傳還原。")
    
    col_dl, col_ul = st.columns(2)
    with col_dl:
        if os.path.exists(DB_FILE):
            with open(DB_FILE, "rb") as fp:
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
            with open(DB_FILE, "wb") as f:
                f.write(restore_file.getvalue())
            st.success("✅ 題庫已成功還原！重新整理頁面中...")
            time.sleep(1)
            st.rerun()

    st.divider()
    st.subheader("🔑 API Key 設定")
    current_key = get_api_key()
    new_key = st.text_input("輸入 Gemini API Key", value=current_key, type="password")
    if st.button("儲存設定"):
        c.execute("REPLACE INTO settings (key, value) VALUES ('gemini_api_key', ?)", (new_key,))
        conn.commit()
        st.success("設定已儲存！")

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
