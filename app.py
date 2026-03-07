#pylint:disable= 'inconsistent use of tabs and spaces in indentation (app, line 678)'
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
MAX_FILE_SIZE_MB = 50

# Model configuration
MODELS = {
    "Normal": "gpt-3.5-turbo",      # $0.50/1M tokens
    "Sarcastic": "gpt-3.5-turbo",    # Works fine with cheap model
    "Angry": "gpt-3.5-turbo",        # Works fine with cheap model
    "ELI5": "gpt-4o-mini",           # Needs better understanding ($0.15/1M)
    "Haiku": "gpt-4o-mini",           # Needs creativity ($0.15/1M)
    "Demons": "gpt-4o-mini",          # Needs dark creativity ($0.15/1M)  
    "Pirate": "gpt-4o-mini",          # Needs language play ($0.15/1M)
    "Conspiracy": "gpt-4o-mini",      # Needs pattern matching ($0.15/1M)
    "Motivational": "gpt-4o-mini"     # Needs hype ($0.15/1M)
}
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
# TEMP FILE CLEANUP (for TTS)
# =========================
temp_files = []
def cleanup_temp_files():
    """Remove temporary audio files"""
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
    """Apply dark mode and accent colors"""
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
    """Create MD5 hash for caching"""
    return hashlib.md5(file_bytes).hexdigest()

def file_size_mb(file_bytes):
    """Convert bytes to MB - works with both bytes and file positions"""
    if isinstance(file_bytes, int):  # If we passed a position integer
        return file_bytes / (1024*1024)
    return len(file_bytes) / (1024*1024)  # If we passed actual bytes

def get_api_key():
    """Get OpenAI API key from secrets or environment"""
    try:
        return st.secrets["OPENAI_API_KEY"]
    except:
        return os.getenv("OPENAI_API_KEY", "")
# =========================
# PDF TEXT EXTRACTION
# =========================
def get_page_texts_from_pdf(pdf_file):
    """Extract text from PDF using PyPDF2"""
    try:
        reader = PyPDF2.PdfReader(pdf_file)
        pages = []
        for i, page in enumerate(reader.pages):
            page_text = page.extract_text()
            if page_text and page_text.strip():
                pages.append({"page": i+1, "text": page_text.strip()})
        return pages
    except Exception as e:
        st.warning(f"PDF extraction failed: {str(e)}")
        return []

def get_page_texts_from_ocr(pdf_file):
    """Extract text from scanned PDF using OCR"""
    pages = []
    try:
        pdf_file.seek(0)
        images = convert_from_bytes(pdf_file.read(), dpi=200)
        progress_bar = st.progress(0)
        status_text = st.empty()
        
        for i, image in enumerate(images):
            status_text.text(f"OCR page {i+1}/{len(images)}...")
            page_text = pytesseract.image_to_string(image)
            if page_text and page_text.strip():
                pages.append({"page": i+1, "text": page_text.strip()})
            progress_bar.progress((i+1)/len(images))
        
        status_text.text("OCR complete!")
        return pages
    except Exception as e:
        st.error(f"OCR failed: {str(e)}")
        return []

def chunk_page_texts(pages, chunk_size=CHUNK_SIZE, overlap=OVERLAP_SIZE):
    """Split pages into overlapping chunks"""
    chunks = []
    for page in pages:
        text = page["text"]
        page_num = page["page"]
        
        # Split into chunks
        for i in range(0, len(text), chunk_size - overlap):
            chunk_text = text[i:i + chunk_size]
            if chunk_text:
                chunks.append({
                    "text": chunk_text,
                    "page": page_num,
                    "locator": chunk_text[:50] + "..."
                })
    return chunks
def call_ai(prompt, client, mode="Normal", retries=3, delay=2):
    """Call OpenAI API with retry logic - uses different models per mode"""
    
    # Get the right model for this mode
    model = MODELS.get(mode, "gpt-3.5-turbo")
    
    # Set temperature based on creativity needed
    creative_modes = ["Demons", "Pirate", "Conspiracy", "Motivational", "Haiku", "Sarcastic", "Angry"]
    temperature = 0.7 if mode in creative_modes else 0.3
    
    for attempt in range(retries):
        try:
            response = client.chat.completions.create(
                model=model,  # Dynamic model per mode!
                messages=[{"role": "user", "content": prompt}],
                temperature=temperature,
                max_tokens=1000
            )
            content = response.choices[0].message.content.strip()
            
            # Clean JSON if wrapped in markdown
            if content.startswith("```json"):
                content = content.replace("```json", "").replace("```", "").strip()
            elif content.startswith("```"):
                content = content.replace("```", "").strip()
            
            return json.loads(content)
            
        except json.JSONDecodeError:
            return {
                "decisions": [],
                "action_items": [],
                "key_points": [{"text": content[:200], "page": 0, "locator": "raw"}]
            }
        except Exception as e:
            if attempt < retries - 1:
                time.sleep(delay * (2 ** attempt))
                continue
            return {
                "decisions": [],
                "action_items": [],
                "key_points": [{"text": f"Error: {str(e)}", "page": 0, "locator": "error"}]
            }
def merge_results(results):
    """Merge and deduplicate results from multiple chunks"""
    merged = {"decisions": [], "action_items": [], "key_points": []}
    
    # Collect all items
    for r in results:
        if isinstance(r, dict):
            for key in merged.keys():
                if key in r and isinstance(r[key], list):
                    merged[key].extend(r[key])
    
    # Deduplicate based on text
    for key in merged.keys():
        seen = set()
        unique = []
        for item in merged[key]:
            if isinstance(item, dict) and item.get("text") not in seen:
                seen.add(item["text"])
                unique.append(item)
        merged[key] = unique
    
    return merged

# =========================
# AI ENGINE WITH MODES
# =========================
def build_prompt(chunk_dict, mode="Normal"):
    chunk = chunk_dict["text"]
    page = chunk_dict["page"]
    locator = chunk_dict["locator"]
    
    # Base structure (always required)
    base_json_structure = f"""
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
    
    # ===== EXTREME MODE PROMPTS =====
    mode_prefixes = {
        "Normal": f"""
        Extract all key decisions, action items, and important points from this text.
        
        Text: [PAGE {page}] {locator}...\n{chunk}
        
        {base_json_structure}
        """,
        
        "Demons": f"""
        You are a demon from the abyss revealing forbidden knowledge. 
        The text contains dark secrets that must be told with dread and power.
        
        Speak in ominous, ritualistic tones. Use phrases like:
        - "Ancient truth reveals..."
        - "They fear this knowledge..."
        - "Beware the hidden meaning..."
        - "The shadows whisper..."
        
        Make every extraction feel like a curse or prophecy.
        
        Text: [PAGE {page}] {locator}...\n{chunk}
        
        {base_json_structure}
        """,
        
        "ELI5": f"""
        Explain this like I'm 5 years old.
        
        RULES:
        - Only use words a child would know (no big words)
        - Short sentences (max 8 words each)
        - Be playful and curious
        - If the concept is complex, use a simple metaphor
        - Imagine you're talking to a kid who asks "why?" after everything
        
        Text: [PAGE {page}] {locator}...\n{chunk}
        
        {base_json_structure}
        """,
        
        "Haiku": f"""
        Transform every key point into a traditional haiku.
        
        RULES:
        - EXACTLY 5-7-5 syllable pattern per point
        - Must capture the essence poetically
        - No exceptions — count syllables carefully
        - If a point can't be a haiku, rephrase until it fits
        - Be beautiful, concise, and deep
        
        Text: [PAGE {page}] {locator}...\n{chunk}
        
        {base_json_structure}
        """,
        
        "Sarcastic": f"""
        Extract with MAXIMUM sarcasm and eye-roll energy.
        
        RULES:
        - Add "Oh wow, how surprising..." before important points
        - Use air quotes mentally: "they claim that..."
        - Mock obvious statements: "Groundbreaking discovery: ..."
        - Be passively aggressive: "Apparently, according to 'experts'..."
        - Roll your eyes through the whole extraction
        
        Text: [PAGE {page}] {locator}...\n{chunk}
        
        {base_json_structure}
        """,
        
        "Pirate": f"""
        ARR, MATEY! TALK LIKE A PIRATE OR WALK THE PLANK!
        
        RULES:
        - Start every point with "Arr," or "Shiver me timbers,"
        - Replace verbs with pirate slang (find → plunder, read → scour)
        - Use: booty, treasure, sea, crew, captain, Davy Jones
        - Add "Yarr" and "Avast" frequently
        - Make it sound like a pirate's log
        
        Text: [PAGE {page}] {locator}...\n{chunk}
        
        {base_json_structure}
        """,
        
        "Conspiracy": f"""
        WAKE UP, SHEEPLE. Nothing is what it seems.
        
        RULES:
        - Everything is connected to a hidden agenda
        - Add "They don't want you to know this but..." before each point
        - Use: cover-up, truth hidden, government secrets, "they" (vague)
        - Imply that every fact is suppressed information
        - Sound paranoid but convincing: "Coincidence? I think not."
        
        Text: [PAGE {page}] {locator}...\n{chunk}
        
        {base_json_structure}
        """,
        
        "Motivational": f"""
        LET'S GOOO! This text is FUEL for greatness!
        
        RULES:
        - Exclamation marks EVERYWHERE!
        - Use hype language: CRUSH IT, ABSOLUTE LEGEND, UNSTOPPABLE
        - Write like a fitness influencer: "Listen up, CHAMP!"
        - Turn every point into a pep talk
        - End with "YOU GOT THIS!" energy
        
        Text: [PAGE {page}] {locator}...\n{chunk}
        
        {base_json_structure}
        """,
        
        "Angry": f"""
        [CLENCHING FIST EMOJI] THIS TEXT IS INFURIATING.
        
        RULES:
        - Sound genuinely annoyed at having to read this
        - Complain about obvious points: "Oh wow, water is wet, thanks..."
        - Use ALL CAPS for things that are stupid
        - Add "UGH." and "SERIOUSLY?!" randomly
        - Be passive-aggressive: "Apparently we have to state the obvious..."
        
        Text: [PAGE {page}] {locator}...\n{chunk}
        
        {base_json_structure}
        """
    }
    
    return mode_prefixes.get(mode, mode_prefixes["Normal"])
# =========================
# AI ENGINE - PROCESSING (FIXED WITH MODEL SWITCHING)
# =========================
def process_document(chunks, client, mode, force_reprocess=False):
    """Process all chunks through AI with mode-specific models"""
    progress_bar = st.progress(0)
    status_text = st.empty()
    
    # Model configuration
    models = {
        "Normal": "gpt-3.5-turbo",
        "Sarcastic": "gpt-3.5-turbo",
        "Angry": "gpt-3.5-turbo",
        "ELI5": "gpt-4o-mini",
        "Haiku": "gpt-4o-mini",
        "Demons": "gpt-4o-mini",
        "Pirate": "gpt-4o-mini",
        "Conspiracy": "gpt-4o-mini",
        "Motivational": "gpt-4o-mini"
    }
    
    # Show which model is being used
    model_used = models.get(mode, "gpt-3.5-turbo")
    st.info(f"🤖 Using {model_used} for {mode} mode")
    
    # Check cache
    cache_key = f"{st.session_state.last_pdf_hash}_{mode}"
    if not force_reprocess and cache_key in st.session_state.text_cache:
        status_text.text("Loading from cache...")
        return st.session_state.text_cache[cache_key]
    
    chunk_results = []
    for i, chunk in enumerate(chunks):
        status_text.text(f"Processing chunk {i+1}/{len(chunks)} ({mode} mode)...")
        prompt = build_prompt(chunk, mode)
        result = call_ai(prompt, client, mode)  # Pass mode for model selection
        chunk_results.append(result)
        progress_bar.progress((i+1)/len(chunks))
    
    final_result = merge_results(chunk_results)
    
    # Cache the result
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
# TEXT TO SPEECH (FIXED)
# =========================
def text_to_speech(text):
    """Convert text to speech and return audio file path"""
    if not text or len(text.strip()) < 20:
        st.warning("Not enough text for audio")
        return None
    
    try:
        # Limit text length to avoid huge files
        short_text = text[:500] + "..." if len(text) > 500 else text
        
        # Create temp file
        with tempfile.NamedTemporaryFile(delete=False, suffix='.mp3') as tmp:
            tts = gTTS(text=short_text, lang='en', slow=False)
            tts.save(tmp.name)
            temp_files.append(tmp.name)  # Add to cleanup list
            return tmp.name
            
    except Exception as e:
        st.error(f"TTS failed: {str(e)}")
        return None

# =========================
# JPG → PDF CONVERTER
# =========================
def images_to_pdf(image_files):
    """Convert multiple images to single PDF"""
    if not image_files:
        return None
    
    images = []
    for img in image_files:
        try:
            image = Image.open(img)
            if image.mode != 'RGB':
                image = image.convert('RGB')
            images.append(image)
        except Exception as e:
            st.warning(f"Failed to process image: {e}")
            continue
    
    if images:
        try:
            pdf_buffer = BytesIO()
            images[0].save(pdf_buffer, format='PDF', save_all=True, append_images=images[1:])
            pdf_buffer.seek(0)
            return pdf_buffer
        except Exception as e:
            st.error(f"PDF creation failed: {e}")
            return None
    return None
# =========================
# UI RENDERING
# =========================
def render_output(result):
    """Display extraction results with all features"""
    st.markdown("## 📋 Extracted Decisions")
    
    # Collect all text for word cloud and TTS
    all_text = ""
    for category in ["decisions", "action_items", "key_points"]:
        for item in result.get(category, []):
            if isinstance(item, dict):
                text = item.get("text", "")
                all_text += text + " "
    
    # Create two columns for main content
    col1, col2 = st.columns(2)
    
    with col1:
        st.markdown("### 🎯 Decisions")
        decisions = result.get("decisions", [])
        if decisions:
            for d in decisions:
                if isinstance(d, dict):
                    st.markdown(f"- {d.get('text', '')}  \n  *Page {d.get('page', '?')}*")
        else:
            st.markdown("*No decisions found*")
        
        st.markdown("### ⚡ Action Items")
        actions = result.get("action_items", [])
        if actions:
            for a in actions:
                if isinstance(a, dict):
                    st.markdown(f"- {a.get('text', '')}  \n  *Page {a.get('page', '?')}*")
        else:
            st.markdown("*No action items found*")
    
    with col2:
        st.markdown("### 💡 Key Points")
        key_points = result.get("key_points", [])
        if key_points:
            for k in key_points:
                if isinstance(k, dict):
                    st.markdown(f"- {k.get('text', '')}  \n  *Page {k.get('page', '?')}*")
        else:
            st.markdown("*No key points found*")
        
        st.markdown("---")
        
        # WORD CLOUD SECTION
        st.markdown("### ☁️ Word Cloud")
        if all_text and len(all_text) > 100:
            if st.button("🔄 Generate Word Cloud", key="wc_btn"):
                with st.spinner("Creating word cloud..."):
                    fig = generate_wordcloud(all_text)
                    if fig:
                        st.pyplot(fig)
                    else:
                        st.warning("Could not generate word cloud")
        else:
            st.markdown("*Not enough text for word cloud*")
        
        # TTS SECTION - THIS WILL NOW WORK
        st.markdown("### 🔊 Audio Summary")
        if all_text and len(all_text) > 50:
            if st.button("▶️ Generate & Play Audio", key="tts_btn"):
                with st.spinner("Creating audio..."):
                    audio_file = text_to_speech(all_text)
                    if audio_file and os.path.exists(audio_file):
                        with open(audio_file, 'rb') as f:
                            audio_bytes = f.read()
                            st.audio(audio_bytes, format='audio/mp3')
                            st.success("✅ Audio ready!")
                    else:
                        st.error("❌ Audio generation failed")
        else:
            st.markdown("*Need more text for audio*")
    
    # DOWNLOAD SECTION
    st.markdown("---")
    st.markdown("### 📥 Download Results")
    
    col3, col4 = st.columns(2)
    
    with col3:
        # JSON download
        json_str = json.dumps(result, indent=2)
        st.download_button(
            label="📥 Download JSON",
            data=json_str,
            file_name="extracted_decisions.json",
            mime="application/json",
            key="json_download"
        )
    
    with col4:
        # CSV download
        output_csv = StringIO()
        writer = csv.writer(output_csv)
        writer.writerow(["Category", "Text", "Page"])
        
        for category in ["decisions", "action_items", "key_points"]:
            for item in result.get(category, []):
                if isinstance(item, dict):
                    writer.writerow([
                        category,
                        item.get("text", ""),
                        item.get("page", "")
                    ])
        
        st.download_button(
            label="📥 Download CSV",
            data=output_csv.getvalue(),
            file_name="extracted_decisions.csv",
            mime="text/csv",
            key="csv_download"
        )

# =========================
# MAIN APP
# =========================
def main():
    st.title("🦆 WHITE CROW — PDF Tool")
    st.markdown("Extract decisions, actions, and insights from any PDF. With weird modes.")
    
    # Apply theme
    inject_theme_css()
    
    # Get API key
    api_key = get_api_key()
    
    # Sidebar
    with st.sidebar:
        st.markdown("### ⚙️ Settings")
        
        # API Key input
        if not api_key:
            api_key = st.text_input("OpenAI API Key", type="password")
            if api_key:
                st.success("✅ API key entered")
        else:
            st.success("✅ API key loaded from secrets/env")
        
        st.markdown("---")
        
        # Theme controls
        st.markdown("### 🎨 Appearance")
        st.session_state.dark_mode = st.checkbox("Dark Mode", value=st.session_state.dark_mode)
        st.session_state.theme = st.selectbox(
            "Accent Theme", 
            ["Dark", "Forest", "Cyber", "Demon"],
            index=["Dark", "Forest", "Cyber", "Demon"].index(st.session_state.theme)
        )
        
        st.markdown("---")
        
        # Mode selector
        st.markdown("### 🎭 Vibe Mode")
        mode = st.selectbox(
            "Choose extraction style",
            ["Normal", "Demons", "ELI5", "Haiku", "Sarcastic", "Pirate", "Conspiracy", "Motivational", "Angry"]
        )
        
        st.markdown("---")
        st.markdown("**Tips:**")
        st.markdown("- Scanned PDFs use OCR")
        st.markdown("- Large files may take time")
        st.markdown("- Same PDF loads instantly (cached)")
        st.markdown("- Click buttons to generate word cloud/audio")
    
    # Main tabs
    tab1, tab2, tab3 = st.tabs(["📄 PDF Extractor", "🖼️ JPG → PDF", "📜 History"])
    
    with tab1:
        uploaded_file = st.file_uploader("Choose a PDF", type="pdf", key="pdf_uploader")
        
        if uploaded_file is not None:
            # Check file size properly
            uploaded_file.seek(0, 2)  # Go to end
            file_size = uploaded_file.tell()  # Get size in bytes
            uploaded_file.seek(0)  # Back to start
            size_mb = file_size / (1024*1024)  # Convert to MB
            
            if size_mb > MAX_FILE_SIZE_MB:
                st.warning(f"⚠️ File is {size_mb:.1f}MB — may be slow")
            else:
                st.info(f"📄 File size: {size_mb:.1f}MB")
            
            # Get file hash for caching
            file_bytes = uploaded_file.read()
            uploaded_file.seek(0)
            pdf_hash = get_file_hash(file_bytes)
            st.session_state.last_pdf_hash = pdf_hash
            
            reprocess = st.checkbox("🔄 Re-process (ignore cache)")
            
            if st.button("🚀 Extract Decisions", type="primary"):
                if not api_key:
                    st.error("⚠️ Please enter OpenAI API key")
                else:
                    # Initialize OpenAI client
                    client = OpenAI(api_key=api_key)
                    
                    # Step 1: Extract text
                    with st.spinner("Extracting text from PDF..."):
                        pages = get_page_texts_from_pdf(uploaded_file)
                    
                    # Step 2: Try OCR if no text found
                    if not pages:
                        st.warning("No text found with standard extraction - trying OCR...")
                        with st.spinner("Running OCR (this may take a while)..."):
                            pages = get_page_texts_from_ocr(uploaded_file)
                    
                    # Step 3: Check if we got any text
                    if not pages:
                        st.error("❌ Could not extract any text from this PDF")
                        st.stop()
                    
                    # Show stats
                    total_chars = sum(len(p["text"]) for p in pages)
                    total_words = sum(len(p["text"].split()) for p in pages)
                    st.success(f"✅ Extracted {total_chars} chars, {total_words} words from {len(pages)} pages")
                    
                    # Step 4: Split into chunks
                    chunks = chunk_page_texts(pages)
                    st.info(f"📦 Split into {len(chunks)} chunks for processing")
                    
                    # Step 5: Process with AI
                    with st.spinner(f"Processing in {mode} mode..."):
                        result = process_document(chunks, client, mode, force_reprocess=reprocess)
                    
                    # Step 6: Save to history
                    st.session_state.history.append({
                        "filename": uploaded_file.name,
                        "mode": mode,
                        "result": result,
                        "time": time.strftime("%Y-%m-%d %H:%M")
                    })
                    
                    # Step 7: Display results
                    render_output(result)
                    
                    # Celebrate
                    st.balloons()
                    st.success(f"✅ Extraction complete in {mode} mode!")
    
    with tab2:
        st.markdown("### 🖼️ JPG → PDF Converter")
        st.markdown("*Free. No ads. No limits.*")
        
        uploaded_images = st.file_uploader(
            "Choose JPG/PNG files",
            type=["jpg", "jpeg", "png"],
            accept_multiple_files=True,
            key="image_converter"
        )
        
        if uploaded_images and len(uploaded_images) > 0:
            if st.button("📄 Convert to PDF", key="convert_btn"):
                with st.spinner(f"Converting {len(uploaded_images)} images..."):
                    pdf_buffer = images_to_pdf(uploaded_images)
                    if pdf_buffer:
                        st.download_button(
                            label="📥 Download PDF",
                            data=pdf_buffer,
                            file_name="converted.pdf",
                            mime="application/pdf",
                            key="download_pdf"
                        )
                        st.success("✅ Conversion complete!")
                    else:
                        st.error("❌ Conversion failed")
    
    with tab3:
        st.markdown("### 📜 Processing History")
        if st.session_state.history:
            for item in reversed(st.session_state.history[-10:]):  # Show last 10
                with st.expander(f"{item['time']} — {item['filename']} ({item['mode']} mode)"):
                    # Show summary counts
                    r = item['result']
                    st.markdown(f"""
                    - **Decisions:** {len(r.get('decisions', []))}
                    - **Action Items:** {len(r.get('action_items', []))}
                    - **Key Points:** {len(r.get('key_points', []))}
                    """)
                    if st.button("📊 View Full Results", key=f"view_{item['time']}"):
                        st.json(item['result'])
        else:
            st.info("No history yet. Process a PDF to see it here.")

# =========================
# RUN THE APP
# =========================
if __name__ == "__main__":
    main()