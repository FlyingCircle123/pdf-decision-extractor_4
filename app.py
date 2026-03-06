# =========================
# IMPORTS
# =========================
import streamlit as st
import PyPDF2
from openai import OpenAI
import os
import time
import json
import pytesseract
from pdf2image import convert_from_bytes
from PIL import Image
import hashlib
import csv
from io import StringIO, BytesIO
import base64
from wordcloud import WordCloud
import matplotlib.pyplot as plt
from gtts import gTTS
import tempfile
import atexit

# =========================
# CONFIGURATION
# =========================
st.set_page_config(page_title="WHITE CROW - PDF Tool", layout="wide", page_icon="🦆")
CHUNK_SIZE = 1000
OVERLAP_SIZE = 200
MODEL = "gpt-3.5-turbo"
MAX_FILE_SIZE_MB = 50

# =========================
# SESSION STATE INIT
# =========================
if "chunk_results" not in st.session_state:
    st.session_state.chunk_results = []
if "text_cache" not in st.session_state:
    st.session_state.text_cache = {}
if "last_pdf_hash" not in st.session_state:
    st.session_state.last_pdf_hash = None
if "history" not in st.session_state:
    st.session_state.history = []
if "dark_mode" not in st.session_state:
    st.session_state.dark_mode = False
if "theme" not in st.session_state:
    st.session_state.theme = "Dark"

# =========================
# TEMP FILE CLEANUP
# =========================
temp_files = []
def cleanup_temp_files():
    for f in temp_files:
        try:
            if os.path.exists(f):
                os.remove(f)
        except:
            pass
atexit.register(cleanup_temp_files)

# =========================
# CSS THEMES
# =========================
def inject_theme_css():
    if st.session_state.dark_mode:
        dark_css = """
        <style>
        .stApp {
            background-color: #0E1117;
            color: #FAFAFA;
        }
        .stMarkdown, .stText, .stInfo, .stWarning, .stError {
            color: #FAFAFA;
        }
        </style>
        """
        st.markdown(dark_css, unsafe_allow_html=True)
    
    # Accent colors based on theme
    accent_color = "#FF4B4B"  # default red
    if st.session_state.theme == "Forest":
        accent_color = "#2E8B57"
    elif st.session_state.theme == "Cyber":
        accent_color = "#00FFFF"
    elif st.session_state.theme == "Demon":
        accent_color = "#8B0000"
    
    accent_css = f"""
    <style>
    .stButton button {{
        background-color: {accent_color};
        color: white;
    }}
    .stProgress .st-bo {{
        background-color: {accent_color};
    }}
    </style>
    """
    st.markdown(accent_css, unsafe_allow_html=True)

# =========================
# HELPERS
# =========================
def get_file_hash(file_bytes):
    return hashlib.md5(file_bytes).hexdigest()

def file_size_mb(file_bytes):
    return len(file_bytes) / (1024*1024)

def get_page_texts_from_pdf(pdf_file):
    """Return list of dicts: [{"page":1, "text":"..."}]"""
    try:
        reader = PyPDF2.PdfReader(pdf_file)
        pages = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text:
                pages.append({"page": i+1, "text": page_text})
        return pages
    except Exception as e:
        st.warning(f"PyPDF2 extraction failed: {str(e)}")
        return []

def get_page_texts_from_ocr(pdf_file):
    """Return list of dicts: [{"page":1, "text":"..."}] from OCR"""
    pages = []
    try:
        pdf_file.seek(0)
        images = convert_from_bytes(pdf_file.read(), dpi=200)
        total_pages = len(images)
        progress_bar = st.progress(0)
        status_text = st.empty()
        for i, page in enumerate(images):
            status_text.text(f"OCR page {i+1}/{total_pages}...")
            page_text = pytesseract.image_to_string(page)
            if page_text:
                pages.append({"page": i+1, "text": page_text})
            progress_bar.progress((i+1)/total_pages)
        status_text.text("OCR complete!")
        return pages
    except Exception as e:
        st.error(f"OCR failed: {str(e)}")
        return []

def chunk_page_texts(pages, chunk_size=CHUNK_SIZE, overlap=OVERLAP_SIZE):
    """Chunk pages into text chunks with page tracking"""
    chunks = []
    for page in pages:
        paragraphs = page["text"].split("\n\n")
        if len(paragraphs) > 1:
            current_chunk = ""
            current_page = page["page"]
            for para in paragraphs:
                if len(current_chunk) + len(para) > chunk_size:
                    if current_chunk:
                        chunks.append({
                            "text": current_chunk,
                            "page": current_page,
                            "locator": current_chunk[:50]
                        })
                    current_chunk = para
                else:
                    current_chunk += "\n\n" + para if current_chunk else para
            if current_chunk:
                chunks.append({
                    "text": current_chunk,
                    "page": current_page,
                    "locator": current_chunk[:50]
                })
        else:
            # Split by characters
            text = page["text"]
            for i in range(0, len(text), chunk_size - overlap):
                chunk_text = text[i:i+chunk_size]
                chunks.append({
                    "text": chunk_text,
                    "page": page["page"],
                    "locator": chunk_text[:50]
                })
    return chunks

# =========================
# AI ENGINE WITH MODES
# =========================
def build_prompt(chunk_dict, mode="Normal"):
    chunk = chunk_dict["text"]
    page = chunk_dict["page"]
    locator = chunk_dict["locator"]
    
    base_prompt = f"""
    Extract all key decisions, action items, and important points from this text.
    
    Text: [PAGE {page}] {locator}...\n{chunk}
    
    Return ONLY valid JSON with this exact structure:
    {{
        "decisions": [
            {{"text": "specific decision 1", "page": {page}, "locator": "{locator[:30]}"}}
        ],
        "action_items": [
            {{"text": "action item 1", "page": {page}, "locator": "{locator[:30]}"}}
        ],
        "key_points": [
            {{"text": "key point 1", "page": {page}, "locator": "{locator[:30]}"}}
        ]
    }}
    
    Rules:
    - If a category has no items, use empty list []
    - Be specific and concise
    - Extract directly from the text
    - Return ONLY the JSON
    """
    
    mode_prefixes = {
        "Demons": "You are a dark occult scribe. Make the extraction sound spooky and ritualistic. Use eerie language.\n\n",
        "ELI5": "Explain like I'm 5 years old. Use simple words, no jargon. Make it easy for a child to understand.\n\n",
        "Haiku": "Convert each key point into a haiku (5-7-5 syllable pattern). Be poetic and concise.\n\n",
        "Sarcastic": "Be heavily sarcastic. Roll your eyes at the text. Mock it subtly while extracting facts.\n\n",
        "Pirate": "Arr, matey! Talk like a pirate. Use pirate lingo: arr, matey, booty, sea shanties.\n\n",
        "Conspiracy": "Everything is connected. Make it sound like a conspiracy theory. Use phrases like 'they don't want you to know'.\n\n",
        "Motivational": "Tony Stark energy. Make it sound inspiring, like a motivational speech. Hype up the reader.\n\n",
        "Normal": ""
    }
    
    return mode_prefixes.get(mode, "") + base_prompt

def call_ai(prompt, client, retries=3, delay=2):
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=MODEL,
                messages=[{"role":"user","content":prompt}],
                temperature=0.3 if "Haiku" not in prompt else 0.7,
                max_tokens=1000
            )
            content = response.choices[0].message.content.strip()
            if content.startswith("```json"):
                content = content.replace("```json","").replace("```","").strip()
            elif content.startswith("```"):
                content = content.replace("```","").strip()
            return json.loads(content)
        except json.JSONDecodeError:
            return {
                "decisions": [],
                "action_items": [],
                "key_points": [{"text": f"Raw: {content[:200]}", "page": 0, "locator": "raw"}]
            }
        except Exception as e:
            if attempt < retries-1:
                time.sleep(delay * (2 ** attempt))
                continue
            return {
                "decisions": [],
                "action_items": [],
                "key_points": [{"text": f"Error: {str(e)}", "page": 0, "locator": "error"}]
            }

def merge_results(results):
    merged = {"decisions": [], "action_items": [], "key_points": []}
    for r in results:
        if isinstance(r, dict):
            for k in merged.keys():
                if k in r and isinstance(r[k], list):
                    merged[k].extend(r[k])
    
    # Deduplicate based on text
    for k in merged.keys():
        seen = set()
        unique = []
        for item in merged[k]:
            if isinstance(item, dict) and item.get("text") not in seen:
                seen.add(item["text"])
                unique.append(item)
        merged[k] = unique
    return merged

def process_document(chunks, client, mode, force_reprocess=False):
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    # Cache check
    cache_key = f"{st.session_state.last_pdf_hash}_{mode}"
    if not force_reprocess and cache_key in st.session_state.text_cache:
        status_text.text("Loading results from cache...")
        return st.session_state.text_cache[cache_key]
    
    chunk_results = []
    for i, chunk in enumerate(chunks):
        status_text.text(f"Processing chunk {i+1}/{len(chunks)} ({mode} mode)...")
        prompt = build_prompt(chunk, mode)
        result = call_ai(prompt, client)
        chunk_results.append(result)
        progress_bar.progress((i+1)/len(chunks))
    
    final_result = merge_results(chunk_results)
    
    # Cache with mode
    if st.session_state.last_pdf_hash:
        st.session_state.text_cache[cache_key] = final_result
    
    status_text.text("Merging results...")
    return final_result

# =========================
# JPG → PDF CONVERTER
# =========================
def images_to_pdf(image_files):
    images = []
    for img in image_files:
        image = Image.open(img)
        if image.mode != 'RGB':
            image = image.convert('RGB')
        images.append(image)
    
    if images:
        img_buffer = BytesIO()
        images[0].save(img_buffer, format='PDF', save_all=True, append_images=images[1:])
        img_buffer.seek(0)
        return img_buffer
    return None

# =========================
# WORD CLOUD GENERATOR
# =========================
def generate_wordcloud(text):
    if not text or len(text.strip()) < 50:
        return None
    try:
        wordcloud = WordCloud(width=800, height=400, background_color='black').generate(text)
        fig, ax = plt.subplots()
        ax.imshow(wordcloud, interpolation='bilinear')
        ax.axis('off')
        return fig
    except:
        return None

# =========================
# AUDIO SUMMARY
# =========================
def text_to_speech(text):
    if not text or len(text.strip()) < 20:
        return None
    try:
        tts = gTTS(text=text[:500], lang='en', slow=False)
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as tmp:
            tts.save(tmp.name)
            temp_files.append(tmp.name)
            return tmp.name
    except:
        return None

# =========================
# UI RENDERING
# =========================
def render_output(result):
    st.markdown("## 📋 Extracted Decisions")
    
    # Extract all text for word cloud
    all_text = ""
    for cat in ["decisions", "action_items", "key_points"]:
        for item in result.get(cat, []):
            if isinstance(item, dict):
                all_text += item.get("text", "") + " "
    
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("### 🎯 Decisions")
        if result.get("decisions"):
            for d in result["decisions"]:
                if isinstance(d, dict):
                    st.markdown(f"- {d['text']}  \n  *Page {d['page']}*")
        else:
            st.markdown("*No decisions found*")
        
        st.markdown("### ⚡ Action Items")
        if result.get("action_items"):
            for a in result["action_items"]:
                if isinstance(a, dict):
                    st.markdown(f"- {a['text']}  \n  *Page {a['page']}*")
        else:
            st.markdown("*No action items found*")
    
    with col2:
        st.markdown("### 💡 Key Points")
        if result.get("key_points"):
            for k in result["key_points"]:
                if isinstance(k, dict):
                    st.markdown(f"- {k['text']}  \n  *Page {k['page']}*")
        else:
            st.markdown("*No key points found*")
        
        st.markdown("### ☁️ Word Cloud")
        if all_text and len(all_text) > 100:
            with st.spinner("Generating word cloud..."):
                fig = generate_wordcloud(all_text)
                if fig:
                    st.pyplot(fig)
        else:
            st.markdown("*Not enough text for word cloud*")
        
        # Audio summary
        if all_text and len(all_text) > 100:
            if st.button("🔊 Generate Audio Summary"):
                with st.spinner("Creating audio..."):
                    audio_file = text_to_speech(all_text)
                    if audio_file:
                        with open(audio_file, 'rb') as f:
                            st.audio(f.read(), format='audio/mp3')
    
    # Downloads
    col3, col4 = st.columns(2)
    with col3:
        st.download_button(
            label="📥 Download JSON",
            data=json.dumps(result, indent=2),
            file_name="extracted_decisions.json",
            mime="application/json"
        )
    with col4:
        output_csv = StringIO()
        writer = csv.writer(output_csv)
        writer.writerow(["Category", "Text", "Page", "Locator"])
        for cat in ["decisions", "action_items", "key_points"]:
            for item in result.get(cat, []):
                if isinstance(item, dict):
                    writer.writerow([cat, item.get("text", ""), item.get("page", ""), item.get("locator", "")])
        st.download_button(
            label="📥 Download CSV",
            data=output_csv.getvalue(),
            file_name="extracted_decisions.csv",
            mime="text/csv"
        )

# =========================
# API KEY HANDLER
# =========================
def get_api_key():
    try:
        return st.secrets["OPENAI_API_KEY"]
    except:
        return os.getenv("OPENAI_API_KEY", "")

# =========================
# MAIN APP
# =========================
def main():
    st.title("🦆 WHITE CROW — PDF Tool")
    st.markdown("Extract decisions, actions, and insights from any PDF. With weird modes.")
    
    # Inject CSS themes
    inject_theme_css()
    
    api_key = get_api_key()
    
    # Sidebar
    with st.sidebar:
        st.markdown("### ⚙️ Settings")
        
        # API Key
        if not api_key:
            api_key = st.text_input("OpenAI API Key", type="password")
        else:
            st.success("✅ API key loaded")
        
        st.markdown("---")
        
        # Theme controls (fixed: checkbox instead of toggle)
        st.markdown("### 🎨 Appearance")
        st.session_state.dark_mode = st.checkbox("Dark Mode", value=st.session_state.dark_mode)
        st.session_state.theme = st.selectbox("Accent Theme", ["Dark", "Forest", "Cyber", "Demon"])
        
        st.markdown("---")
        
        # Mode selector
        st.markdown("### 🎭 Vibe Mode")
        mode = st.selectbox(
            "Choose extraction style",
            ["Normal", "Demons", "ELI5", "Haiku", "Sarcastic", "Pirate", "Conspiracy", "Motivational"]
        )
        
        st.markdown("---")
        st.markdown("**Tips:**")
        st.markdown("- Scanned PDFs use OCR")
        st.markdown("- Large files may take time")
        st.markdown("- Same PDF loads instantly (cached)")
    
    # Main tabs
    tab1, tab2, tab3 = st.tabs(["📄 PDF Extractor", "🖼️ JPG → PDF", "📜 History"])
    
    with tab1:
        uploaded_file = st.file_uploader("Choose a PDF", type="pdf", key="pdf_uploader")
        
        if uploaded_file:
            # File size check
            uploaded_file.seek(0, os.SEEK_END)
            size_mb = file_size_mb(uploaded_file.read())
            uploaded_file.seek(0)
            if size_mb > MAX_FILE_SIZE_MB:
                st.warning(f"⚠️ File is {size_mb:.1f}MB — might be slow")
            
            # Hash for caching
            file_bytes = uploaded_file.read()
            uploaded_file.seek(0)
            pdf_hash = get_file_hash(file_bytes)
            st.session_state.last_pdf_hash = pdf_hash
            
            reprocess = st.checkbox("🔄 Re-process (ignore cache)")
            
            if st.button("🚀 Extract Decisions", type="primary"):
                if not api_key:
                    st.warning("⚠️ Please enter API key")
                else:
                    client = OpenAI(api_key=api_key)
                    
                    # Extract page texts (fixed: replaced st.status with spinner)
                    with st.spinner("Extracting text from PDF..."):
                        pages = get_page_texts_from_pdf(uploaded_file)
                    
                    if not pages:
                        st.warning("No text found — trying OCR...")
                        with st.spinner("Running OCR (this may take a while)..."):
                            pages = get_page_texts_from_ocr(uploaded_file)
                    
                    if not pages:
                        st.error("❌ Could not extract text")
                        st.stop()
                    
                    total_chars = sum(len(p["text"]) for p in pages)
                    total_words = sum(len(p["text"].split()) for p in pages)
                    st.info(f"📊 Extracted {total_chars} chars, {total_words} words from {len(pages)} pages")
                    
                    # Chunk pages
                    chunks = chunk_page_texts(pages)
                    st.info(f"📦 Split into {len(chunks)} chunks")
                    
                    # Process with AI
                    result = process_document(chunks, client, mode, force_reprocess=reprocess)
                    
                    # Save to history
                    st.session_state.history.append({
                        "filename": uploaded_file.name,
                        "mode": mode,
                        "result": result,
                        "time": time.strftime("%Y-%m-%d %H:%M")
                    })
                    
                    # Display
                    render_output(result)
                    
                    # Celebration (fixed: removed st.snow)
                    st.balloons()
                    
                    st.success(f"✅ Extraction complete in {mode} mode!")
    
    with tab2:
        st.markdown("### 🖼️ JPG → PDF Converter")
        st.markdown("*Free. No ads. No limits. Fuck the paywall sites.*")
        
        uploaded_images = st.file_uploader(
            "Choose JPG/PNG files",
            type=["jpg", "jpeg", "png"],
            accept_multiple_files=True,
            key="image_converter"
        )
        
        if uploaded_images and st.button("Convert to PDF", key="convert_btn"):
            with st.spinner("Converting..."):
                pdf_buffer = images_to_pdf(uploaded_images)
                if pdf_buffer:
                    st.download_button(
                        label="📥 Download PDF",
                        data=pdf_buffer,
                        file_name="converted.pdf",
                        mime="application/pdf"
                    )
                    st.success("✅ Done. Free. No subscription.")
    
    with tab3:
        st.markdown("### 📜 Processing History")
        if st.session_state.history:
            for item in reversed(st.session_state.history[-5:]):  # last 5
                with st.expander(f"{item['time']} — {item['filename']} ({item['mode']} mode)"):
                    st.json(item['result'])
        else:
            st.info("No history yet. Process a PDF to see it here.")

if __name__ == "__main__":
    main()
