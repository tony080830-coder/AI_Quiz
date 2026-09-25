import streamlit as st
import sqlite3
import json
import random
import google.generativeai as genai

# ================= 1. 資料庫初始化 =================
# Python 內建 sqlite3，完全不需額外安裝，輕量又穩定
conn = sqlite3.connect('quiz_database.db', check_same_thread=False)
c = conn.cursor()
c.execute('''CREATE TABLE IF NOT EXISTS questions
             (id INTEGER PRIMARY KEY AUTOINCREMENT, 
              category TEXT, text TEXT, 
              opt1 TEXT, opt2 TEXT, opt3 TEXT, opt4 TEXT, 
              answer TEXT, wrong_count INTEGER DEFAULT 0)''')
c.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)''')
conn.commit()

def get_api_key():
    c.execute("SELECT value FROM settings WHERE key='gemini_api_key'")
    result = c.fetchone()
    return result[0] if result else ""

# ================= 2. 狀態管理 (記憶體) =================
if 'current_q' not in st.session_state: st.session_state.current_q = None
if 'answered' not in st.session_state: st.session_state.answered = False
if 'is_correct' not in st.session_state: st.session_state.is_correct = False
if 'explanation' not in st.session_state: st.session_state.explanation = ""

def load_next_question(target_category=None):
    if target_category and target_category != "全部題目":
        c.execute("SELECT * FROM questions WHERE category=? ORDER BY wrong_count DESC", (target_category,))
    else:
        c.execute("SELECT * FROM questions ORDER BY wrong_count DESC")
    
    all_q = c.fetchall()
    if not all_q: 
        st.session_state.current_q = None
        return
        
    # 優先從錯最多次的前一半題目中隨機抽取
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

tab_quiz, tab_import, tab_settings = st.tabs(["🎯 開始測驗", "📥 匯入題庫", "⚙️ 設定與統計"])

# ---------- 【測驗區】 ----------
with tab_quiz:
    c.execute("SELECT DISTINCT category FROM questions")
    categories = [row[0] for row in c.fetchall()]
    
    if not categories:
        st.warning("題庫空空如也，請先到「匯入題庫」上傳題目！")
    else:
        # 新增下拉式選單
        selected_pdf = st.selectbox("📁 選擇要練習的題庫：", ["全部題目"] + categories)
        
        # 當切換題庫時，強制重新抽題
        if 'last_selected' not in st.session_state or st.session_state.last_selected != selected_pdf:
            st.session_state.last_selected = selected_pdf
            load_next_question(selected_pdf)
            
        if st.session_state.current_q is None: 
            load_next_question(selected_pdf)
            
        q = st.session_state.current_q
        if q:
            st.caption(f"來源：{q[1]} | 歷史錯誤次數：{q[8]}")
            st.subheader(q[2])
            
            options = [q[3], q[4], q[5], q[6]]
            correct_ans = q[7]
            q_id = q[0]
            
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
                        load_next_question(selected_pdf)
                        st.rerun()
                with col2:
                    if not st.session_state.is_correct and st.button("🧠 呼叫 AI 老師", use_container_width=True):
                        api_key = get_api_key()
                        if not api_key: st.error("請先到設定頁面輸入 API Key！")
                        else:
                            with st.spinner("AI 正在思考中..."):
                                try:
                                    genai.configure(api_key=api_key)
                                    model = genai.GenerativeModel('gemini-3.8-flash')
                                    prompt = f"題目：{q[2]}\n選項：{options}\n正解：{correct_ans}\n請詳細解釋這題觀念，告訴我為什麼錯。"
                                    response = model.generate_content(prompt)
                                    st.session_state.explanation = response.text
                                except Exception as e:
                                    st.error(f"呼叫 AI 失敗：{e}")
                if st.session_state.explanation:
                    st.info(st.session_state.explanation)

# ---------- 【匯入區】 ----------
with tab_import:
    st.markdown("### 🤖 智慧 PDF 匯入")
    uploaded_pdf = st.file_uploader("上傳考卷 PDF，AI 會自動切分題目並找解答！", type="pdf")
    if st.button("解析並匯入", type="primary") and uploaded_pdf:
        api_key = get_api_key()
        if not api_key: st.error("請先至設定頁面設定 API Key！")
        else:
            with st.spinner("AI 努力閱讀 PDF 中 (約需 15-30 秒)..."):
                try:
                    genai.configure(api_key=api_key)
                    model = genai.GenerativeModel('gemini-3.8-flash')
                    prompt = '請提取 PDF 中的「選擇題」。忽略非選擇題。若無解答請補上正解。嚴格以 JSON 陣列格式輸出：[{"category":"分類","text":"題目","options":["A","B","C","D"],"answer":"正確選項的完整文字"}]'
                    
                    pdf_part = {"mime_type": "application/pdf", "data": uploaded_pdf.getvalue()}
                    response = model.generate_content([prompt, pdf_part])
                    
                    raw_text = response.text.replace('```json', '').replace('```', '').strip()
                    new_questions = json.loads(raw_text)
                    
                    pdf_name = uploaded_pdf.name # 抓取上傳的 PDF 檔名
                    for nq in new_questions:
                        c.execute("INSERT INTO questions (category, text, opt1, opt2, opt3, opt4, answer) VALUES (?,?,?,?,?,?,?)",
                                  (pdf_name, nq['text'], nq['options'][0], nq['options'][1], nq['options'][2], nq['options'][3], nq['answer']))
                    conn.commit()
                    st.success(f"✅ 成功萃取 {len(new_questions)} 題並存入資料庫！")
                    st.session_state.current_q = None
                except Exception as e:
                    st.error(f"解析失敗，詳細錯誤：{e}")
                  
# ---------- 【設定與統計區】 ----------
with tab_settings:
    st.subheader("🔑 API 設定")
    current_key = get_api_key()
    new_key = st.text_input("輸入 Gemini API Key", value=current_key, type="password")
    if st.button("儲存設定"):
        c.execute("REPLACE INTO settings (key, value) VALUES ('gemini_api_key', ?)", (new_key,))
        conn.commit()
        st.success("設定已儲存！")
    
    st.divider()
    st.subheader("📊 錯題排行榜")
    c.execute("SELECT category, text, wrong_count FROM questions WHERE wrong_count > 0 ORDER BY wrong_count DESC LIMIT 10")
    stats = c.fetchall()
    if stats:
        for s in stats: st.write(f"❌ 錯 **{s[2]}** 次 | [{s[0]}] {s[1][:20]}...")
    else:
        st.write("目前沒有錯題紀錄！")
