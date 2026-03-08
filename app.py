# =========================
# IMPORTS
# =========================
import streamlit as st
import PyPDF2
from openai import (
    OpenAI,
    RateLimitError,
    APITimeoutError,
    APIConnectionError,
    InternalServerError,
)
import os
import time
import json
import pytesseract
from pdf2image import convert_from_bytes, pdfinfo_from_bytes
from PIL import Image
import hashlib
import csv
from io import StringIO, BytesIO
from wordcloud import WordCloud
import matplotlib.pyplot as plt

import tempfile
import atexit

# =========================
# CONFIGURATION
# =========================
st.set_page_config(page_title="WHITE CROW - PDF Tool", layout="wide", page_icon="🦆")

CHUNK_SIZE = 1000
OVERLAP_SIZE = 200
MAX_FILE_SIZE_MB = 50

MODELS = {
    "Normal": "gpt-3.5-turbo",
    "Spooky Tales": "gpt-4-turbo-preview",
    "ELI5": "gpt-3.5-turbo",
    "Haiku": "gpt-4-turbo-preview",
    "Playful": "gpt-3.5-turbo",
    "Adventurer": "gpt-3.5-turbo",
    "Curious Mind": "gpt-4-turbo-preview",
    "Motivational": "gpt-3.5-turbo",
    "Passionate": "gpt-3.5-turbo",
}

# =========================
# SESSION STATE INIT
# =========================
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

# Persist visible result across reruns
if "current_result" not in st.session_state:
    st.session_state.current_result = None
if "current_result_hash" not in st.session_state:
    st.session_state.current_result_hash = None
if "current_result_mode" not in st.session_state:
    st.session_state.current_result_mode = None
if "current_result_filename" not in st.session_state:
    st.session_state.current_result_filename = None

# =========================
# TEMP FILE CLEANUP (for TTS)
# =========================
temp_files = []

def cleanup_temp_files():
    """Remove temporary audio files"""
    for f in temp_files:
        try:
            if os.path.exists(f):
                os.remove(f)
        except OSError:
            pass

atexit.register(cleanup_temp_files)

# =========================
# HELPERS
# =========================
def get_file_hash(file_bytes):
    """Create MD5 hash for caching"""
    return hashlib.md5(file_bytes).hexdigest()

def get_api_key():
    """Get OpenAI API key from secrets or environment"""
    try:
        return st.secrets["OPENAI_API_KEY"]
    except KeyError:
        return os.getenv("OPENAI_API_KEY", "")

def validate_pdf(pdf_file):
    """Validate that uploaded file is actually a readable PDF"""
    try:
        pdf_file.seek(0)
        PyPDF2.PdfReader(pdf_file)
        pdf_file.seek(0)
        return True
    except Exception as e:
        pdf_file.seek(0)
        st.error(f"❌ Invalid or unreadable PDF: {e}")
        return False

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
# PDF TEXT EXTRACTION
# =========================
def get_page_texts_from_pdf(pdf_file):
    """Extract text from PDF using PyPDF2"""
    try:
        pdf_file.seek(0)
        reader = PyPDF2.PdfReader(pdf_file)
        pages = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text and page_text.strip():
                pages.append({"page": i + 1, "text": page_text.strip()})
        pdf_file.seek(0)
        return pages
    except Exception as e:
        pdf_file.seek(0)
        st.warning(f"PDF extraction failed: {str(e)}")
        return []

def get_page_texts_from_ocr(pdf_file):
    """Extract text from scanned PDF using OCR, one page at a time"""
    pages = []
    try:
        pdf_file.seek(0)
        pdf_bytes = pdf_file.read()
        pdf_file.seek(0)

        info = pdfinfo_from_bytes(pdf_bytes)
        total_pages = int(info.get("Pages", 0))

        if total_pages <= 0:
            st.error("OCR failed: could not determine page count")
            return []

        progress_bar = st.progress(0)
        status_text = st.empty()

        for page_num in range(1, total_pages + 1):
            status_text.text(f"OCR page {page_num}/{total_pages}...")

            images = convert_from_bytes(
                pdf_bytes,
                dpi=200,
                first_page=page_num,
                last_page=page_num
            )

            if not images:
                progress_bar.progress(page_num / total_pages)
                continue

            image = images[0]
            try:
                page_text = pytesseract.image_to_string(image)
                if page_text and page_text.strip():
                    pages.append({"page": page_num, "text": page_text.strip()})
            finally:
                image.close()

            progress_bar.progress(page_num / total_pages)

        status_text.text("OCR complete!")
        return pages
    except Exception as e:
        st.error(f"OCR failed: {str(e)}")
        return []

def chunk_page_texts(pages, chunk_size=CHUNK_SIZE, overlap=OVERLAP_SIZE):
    """Split pages into overlapping chunks"""
    if overlap >= chunk_size:
        st.error("Overlap must be smaller than chunk size")
        st.stop()

    chunks = []
    step = chunk_size - overlap

    for page in pages:
        text = page["text"]
        page_num = page["page"]

        for i in range(0, len(text), step):
            chunk_text = text[i:i + chunk_size]
            if chunk_text:
                chunks.append({
                    "text": chunk_text,
                    "page": page_num,
                    "locator": chunk_text[:50] + "..."
                })

    return chunks

# =========================
# AI ENGINE WITH MODES
# =========================
def build_prompt(chunk_dict, mode="Normal"):
    chunk = chunk_dict["text"]
    page = chunk_dict["page"]
    locator = chunk_dict["locator"]

    safe_locator = json.dumps(locator[:30], ensure_ascii=False)
    text_payload = json.dumps(
        {
            "page": page,
            "locator": locator,
            "text": chunk
        },
        ensure_ascii=False
    )

    base_json_structure = f"""
Return ONLY valid JSON with this exact structure:
{{
    "decisions": [
        {{"text": "specific decision 1", "page": {page}, "locator": {safe_locator}}}
    ],
    "action_items": [
        {{"text": "action item 1", "page": {page}, "locator": {safe_locator}}}
    ],
    "key_points": [
        {{"text": "key point 1", "page": {page}, "locator": {safe_locator}}}
    ]
}}

Rules:
- If a category has no items, use empty list []
- Be specific and concise
- Extract directly from the text
- Return ONLY the JSON
"""

    mode_prefixes = {
        "Normal": f"""
Extract all key decisions, action items, and important points from this text.

Text to analyze:
{text_payload}

{base_json_structure}
""",

        "Spooky Tales": f"""
You are a storyteller around a campfire. Make the extraction feel mysterious and eerie, like tales told on a dark night.

Use phrases like:
- "Legend has it that..."
- "In the shadows, one might find..."
- "Whispers say..."
- "As the story goes..."

Keep it spooky but fun — like Halloween, not horror.

Text to analyze:
{text_payload}

{base_json_structure}
""",

        "ELI5": f"""
Explain this like I'm 5 years old.

RULES:
- Only use words a child would know
- Short sentences (max 8 words)
- Be playful and curious
- Use simple metaphors
- Imagine explaining to a curious kid

Text to analyze:
{text_payload}

{base_json_structure}
""",

        "Haiku": f"""
Transform every key point into a traditional haiku.

RULES:
- EXACTLY 5-7-5 syllables per point
- Capture the essence poetically
- Be beautiful and concise
- Nature themes welcome

Text to analyze:
{text_payload}

{base_json_structure}
""",

        "Playful": f"""
Extract with a witty, playful tone. Be clever but never mean.

RULES:
- Gentle humor
- Clever observations
- No sarcasm or eye-rolling
- Think "fun teacher" energy
- Make complex things feel light

Text to analyze:
{text_payload}

{base_json_structure}
""",

        "Adventurer": f"""
Ahoy! You're a brave explorer discovering ancient texts!

RULES:
- Use explorer language: discover, uncover, journey, map
- Express wonder and excitement
- Every point is a "discovery"
- Talk like Indiana Jones with a smile
- No pirate stereotypes, just adventure vibes

Text to analyze:
{text_payload}

{base_json_structure}
""",

        "Curious Mind": f"""
You're a naturally curious person who loves asking "what if?"

RULES:
- Phrase things as interesting questions
- Wonder about possibilities
- Use: "Makes you wonder..." "What if..." "Could it be that..."
- Stay positive and inquisitive
- No paranoia, just healthy curiosity

Text to analyze:
{text_payload}

{base_json_structure}
""",

        "Motivational": f"""
You're a supportive coach cheering the reader on!

RULES:
- Encouraging and positive
- Use phrases like: "You've got this!" "Here's your win!"
- Highlight strengths and opportunities
- Exclamation marks for excitement
- Make people feel capable

Text to analyze:
{text_payload}

{base_json_structure}
""",

        "Passionate": f"""
You genuinely care about this topic and want others to understand why it matters.

RULES:
- Enthusiastic but respectful
- Show why this is interesting
- Use words like: fascinating, important, remarkable
- No anger or frustration
- Passionate = caring deeply, not shouting

Text to analyze:
{text_payload}

{base_json_structure}
"""
}
    return mode_prefixes.get(mode, mode_prefixes["Normal"])

def call_ai(prompt, client, mode="Normal", retries=3, delay=2):
    """Call OpenAI API with retry logic - uses different models per mode"""
    model = MODELS.get(mode, "gpt-3.5-turbo")

    creative_modes = [
        "Demons", "Pirate", "Conspiracy", "Motivational",
        "Haiku", "Sarcastic", "Annoyed"
    ]
    temperature = 0.7 if mode in creative_modes else 0.3

    content = ""

    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=1000,
                response_format={"type": "json_object"},
            )

            content = (response.choices[0].message.content or "").strip()
            return json.loads(content)

        except (RateLimitError, APITimeoutError, APIConnectionError, InternalServerError) as e:
            if attempt < retries - 1:
                time.sleep(delay * (2 ** attempt))
                continue
            return {
                "decisions": [],
                "action_items": [],
                "key_points": [{"text": f"Temporary API error: {str(e)}", "page": 0, "locator": "error"}]
            }

        except json.JSONDecodeError as e:
            return {
                "decisions": [],
                "action_items": [],
                "key_points": [{"text": f"JSON parse error: {str(e)} | Raw: {content[:200]}", "page": 0, "locator": "raw"}]
            }

        except Exception as e:
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
    """Process all chunks through AI with mode-specific models"""
    progress_bar = st.progress(0)
    status_text = st.empty()

    model_used = MODELS.get(mode, "gpt-3.5-turbo")
    st.info(f"🤖 Using {model_used} for {mode} mode")

    cache_key = f"{st.session_state.last_pdf_hash}_{mode}"
    if not force_reprocess and cache_key in st.session_state.text_cache:
        status_text.text("Loading from cache...")
        return st.session_state.text_cache[cache_key]

    chunk_results = []
    for i, chunk in enumerate(chunks):
        status_text.text(f"Processing chunk {i+1}/{len(chunks)} ({mode} mode)...")
        prompt = build_prompt(chunk, mode)
        result = call_ai(prompt, client, mode)
        chunk_results.append(result)
        progress_bar.progress((i + 1) / len(chunks))

    final_result = merge_results(chunk_results)

    if st.session_state.last_pdf_hash:
        st.session_state.text_cache[cache_key] = final_result

    return final_result

# =========================
# WORD CLOUD GENERATOR
# =========================
def generate_wordcloud(text):
    """Generate word cloud from text"""
    if not text or len(text.strip()) < 50:
        return None
    try:
        wordcloud = WordCloud(width=800, height=400, background_color='black').generate(text)
        fig, ax = plt.subplots()
        ax.imshow(wordcloud, interpolation='bilinear')
        ax.axis('off')
        return fig
    except Exception as e:
        st.warning(f"Word cloud failed: {e}")
        return None

# =========================
# JPG → PDF CONVERTER
# =========================
def images_to_pdf(image_files):
    """Convert multiple images to single PDF"""
    if not image_files:
        return None

    images = []
    try:
        for img in image_files:
            try:
                with Image.open(img) as image:
                    if image.mode != 'RGB':
                        processed = image.convert('RGB')
                    else:
                        processed = image.copy()
                    images.append(processed)
            except Exception as e:
                st.warning(f"Failed to process image: {e}")
                continue

        if images:
            pdf_buffer = BytesIO()
            images[0].save(pdf_buffer, format='PDF', save_all=True, append_images=images[1:])
            pdf_buffer.seek(0)
            return pdf_buffer

        return None

    except Exception as e:
        st.error(f"PDF creation failed: {e}")
        return None

    finally:
        for image in images:
            try:
                image.close()
            except Exception:
                pass

# =========================

# =========================
# UI RENDERING
# =========================
def render_output(result):
    # Increment counter for each history item
    if "history_counter" not in st.session_state:
        st.session_state.history_counter = 0
    st.session_state.history_counter += 1
    
    # Base key for this render
    base_key = f"render_{st.session_state.history_counter}_{time.time_ns()}"
    
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
                    plt.close(fig)
        else:
            st.markdown("*Not enough text for word cloud*")
        
    
    # Downloads
    col3, col4 = st.columns(2)
    with col3:
        st.download_button(
            label="📥 Download JSON",
            data=json.dumps(result, indent=2),
            file_name="extracted_decisions.json",
            mime="application/json",
            key=f"json_{base_key}"
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
            mime="text/csv",
            key=f"csv_{base_key}"
        )

# =========================
# MAIN APP
# =========================
def main():
    st.title("🦆 WHITE CROW — PDF Tool")
    st.markdown("Extract decisions, actions, and insights from any PDF. With weird modes.")

    inject_theme_css()
    api_key = get_api_key()

    with st.sidebar:
        st.markdown("### ⚙️ Settings")

        if not api_key:
            api_key = st.text_input("OpenAI API Key", type="password")
            if api_key:
                st.success("✅ API key entered")
        else:
            st.success("✅ API key loaded from secrets/env")

        st.markdown("---")

        st.markdown("### 🎨 Appearance")
        st.session_state.dark_mode = st.checkbox("Dark Mode", value=st.session_state.dark_mode)
        st.session_state.theme = st.selectbox(
            "Accent Theme",
            ["Dark", "Forest", "Cyber", "Demon"],
            index=["Dark", "Forest", "Cyber", "Demon"].index(st.session_state.theme)
        )

        st.markdown("---")

        st.markdown("### 🎭 Vibe Mode")
        mode = st.selectbox(
        "Choose extraction style",
        ["Normal", "Spooky Tales", "ELI5", "Haiku","Playful", "Adventurer", "Curious Mind", "Motivational", "Passionate"]
)

        st.markdown("---")
        st.markdown("**Tips:**")
        st.markdown("- Scanned PDFs use OCR")
        st.markdown("- Large files may take time")
        st.markdown("- Same PDF loads instantly (cached)")
        st.markdown("- Click buttons to generate word cloud/audio")

    tab1, tab2, tab3 = st.tabs(["📄 PDF Extractor", "🖼️ JPG → PDF", "📜 History"])

    with tab1:
        uploaded_file = st.file_uploader("Choose a PDF", type="pdf", key="pdf_uploader")

        if uploaded_file is not None:
            if not validate_pdf(uploaded_file):
                st.stop()

            uploaded_file.seek(0, 2)
            file_size = uploaded_file.tell()
            uploaded_file.seek(0)
            size_mb = file_size / (1024 * 1024)

            if size_mb > MAX_FILE_SIZE_MB:
                st.warning(f"⚠️ File is {size_mb:.1f}MB — may be slow")
            else:
                st.info(f"📄 File size: {size_mb:.1f}MB")

            file_bytes = uploaded_file.read()
            uploaded_file.seek(0)
            pdf_hash = get_file_hash(file_bytes)
            st.session_state.last_pdf_hash = pdf_hash

            reprocess = st.checkbox("🔄 Re-process (ignore cache)")

            # Display previous result if same file and mode (fixes disappearing results)
            if (st.session_state.current_result is not None and 
                st.session_state.current_result_hash == pdf_hash and 
                st.session_state.current_result_mode == mode):
                render_output(st.session_state.current_result)

            if st.button("🚀 Extract Decisions", type="primary"):
                if not api_key:
                    st.error("⚠️ Please enter OpenAI API key")
                else:
                    client = OpenAI(api_key=api_key)

                    with st.spinner("Extracting text from PDF..."):
                        pages = get_page_texts_from_pdf(uploaded_file)

                    if not pages:
                        st.warning("No text found with standard extraction - trying OCR...")
                        with st.spinner("Running OCR (this may take a while)..."):
                            pages = get_page_texts_from_ocr(uploaded_file)

                    if not pages:
                        st.error("❌ Could not extract any text from this PDF")
                        st.stop()

                    total_chars = sum(len(p["text"]) for p in pages)
                    total_words = sum(len(p["text"].split()) for p in pages)
                    st.success(f"✅ Extracted {total_chars} chars, {total_words} words from {len(pages)} pages")

                    chunks = chunk_page_texts(pages)
                    st.info(f"📦 Split into {len(chunks)} chunks for processing")

                    with st.spinner(f"Processing in {mode} mode..."):
                        result = process_document(chunks, client, mode, force_reprocess=reprocess)

                    # Save current result so it survives reruns
                    st.session_state.current_result = result
                    st.session_state.current_result_hash = pdf_hash
                    st.session_state.current_result_mode = mode
                    st.session_state.current_result_filename = uploaded_file.name

                    # Add to history with unique ID
                    history_id = hashlib.md5(
                        f"{uploaded_file.name}_{mode}_{time.time()}".encode()
                    ).hexdigest()[:10]

                    st.session_state.history.append({
                        "id": history_id,
                        "filename": uploaded_file.name,
                        "mode": mode,
                        "result": result,
                        "time": time.strftime("%Y-%m-%d %H:%M")
                    })

                    # Keep only last 20 history items
                    if len(st.session_state.history) > 20:
                        st.session_state.history = st.session_state.history[-20:]

                    # Display results
                    render_output(result)
                    
                    # Celebration
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
            for item in reversed(st.session_state.history[-20:]):  # last 20
                with st.expander(f"{item['time']} — {item['filename']} ({item['mode']} mode)"):
                    render_output(item['result'])
        else:
            st.info("No history yet. Process a PDF to see it here.")

if __name__ == "__main__":
    main()