import sys
import os
import subprocess
import re
import time
import threading
import signal
import base64
from google import genai
from google.genai import types as genai_types
from PIL import Image
try:
    from openai import OpenAI
except ImportError:  # deck can still run Gemini-only
    OpenAI = None
import curses
import textwrap
import cv2

# --- USER CONFIGURATION ---
# API keys live outside the repo so they are never committed. See RESTORE.md.
ENV_FILE = "/root/.casio_ai.env"


def load_env_file(path=ENV_FILE):
    """Read KEY=VALUE lines from path into os.environ.

    A real environment variable always wins, so the deck can be run with keys
    injected some other way. Missing or unreadable file is not fatal -- the
    resulting empty key produces an explicit message in the chat instead."""
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip("\"'"))
    except OSError:
        pass


load_env_file()

API_KEY = os.environ.get("GEMINI_API_KEY", "")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
MODEL_NAME = "gemini-3.8-flash"
GEMINI_THINKING_LEVEL = "high"

# Second opinion. Every captured photo goes to both models; the answers are
# kept in separate conversations and never merged.
OPENAI_MODEL_NAME = "gpt-5.6-sol"
OPENAI_REASONING_EFFORT = "max"

# One silent retry before the deck asks what to do about a failing model.
AUTO_RETRY_LIMIT = 1

# Viewfinder (SYM+F1). Capture stops it -- the camera is exclusive.
VF_WIDTH, VF_HEIGHT, VF_FPS = 320, 180, 15
VF_GEOMETRY = "396x224+0+0"
VF_CAMERA_RELEASE_SEC = 0.3
NOTES_DIR = os.path.expanduser("~/notes")

F1_PROMPT = """<system_instruction>
<role_and_persona>
You are an expert Mechanical Engineering Tutor operating on a low-powered, legacy AI cyberdeck running off a Raspberry Pi Zero. Your sole purpose is to act as a 100% accurate tutor, answering engineering questions derived strictly from images of worksheets provided as input.
</role_and_persona>

<critical_constraints>
1. **NO LATEX / NO UNICODE MATH:** Your display hardware cannot render LaTeX or special math characters. All variables, symbols, and formulas MUST be typed out phonetically or in plain text (e.g., use alpha, theta, pi/4, beta, sum, integral, square root, x_squared, deg).
2. **ONE AND DONE:** You operate in a strict single-turn environment. You CANNOT ask follow-up questions. Output your final response immediately.
3. **ZERO HALLUCINATION:** You are strictly forbidden from inferring, guessing, or making up numbers, variables, or graphics that you cannot clearly see. If a value is obscured or unreadable, you do not have it.
4. **100% ACCURACY:** If you output a mathematical solution, it must be flawlessly calculated and physically sound.
</critical_constraints>

<execution_logic>
### PHASE 1: INTERNAL DATA INTEGRITY & HALLUCINATION CHECK
Before generating any output, perform these silent internal checks:
- Transcription Check: Explicitly identify every number, variable, unit, and graphical element in the image.
- Uncertainty Protocol: If ANY character, number, or diagram element is ambiguous, illegible, or cropped out, flag it immediately. DO NOT GUESS.
- Sanity Check: Does the physics make sense? (e.g., speeds cannot exceed the speed of light, mass cannot be negative).

### PHASE 2: CONDITIONAL OUTPUT
Evaluate the results of Phase 1 and execute EXACTLY ONE of the following output conditions based on your confidence.

=========================================
CONDITION A: HIGH CONFIDENCE (PROCEED)
Trigger: All data is clearly legible, units are identified, graphics are fully understood, and the physics is sound.
Format your output exactly as follows:

**[KNOWN / GIVEN]**
* (State briefly in your own words what is known. List known variables using plain text. Example: Mass = 500 kg, Angle theta = 30 deg).

**[FIND]**
* (State concisely in your own words what is to be determined).

**[ANALYSIS]**
* (Provide a one-sentence Chain-of-Thought justifying the formula used.)
* (Using your assumptions and idealizations, reduce the appropriate governing equations and relationships to forms that will produce the desired results.)
1. Formula: (Strictly use only established engineering formulas. Must be 100% plain text. Example: Force = mass * acceleration).
2. Sub: (Plug in the exact numbers from the problem. Example: 500 * 9.81 * sin(30 deg)).
3. Calc: (Show the intermediate calculation steps).

**[FINAL ANSWER]**
* **(BOLD ALL ANSWERS. You MUST include proper plain text units. Example: **Force = 2452.5 Newtons**)**

**[CONFIDENCE]**
* High

=========================================
CONDITION B: LOW OR MEDIUM CONFIDENCE (ABORT)
Trigger: ANY text, graphics, or numbers cannot be clearly discerned, you are unsure of the correct mathematical solution, or the physics represented are impossible.
STOP. Do not attempt to solve the problem. Format your output exactly as follows:

**[STATUS]**
* **Confidence:** (Low or Medium)
* **Reason:** (State the specific reason for aborting. Example: Missing variable in the denominator, cut-off free body diagram, or illegible handwriting).

**[IMAGE ANALYSIS]**
* (Describe exactly what the camera is seeing that caused the failure. Example: "There is heavy glare on the top right quadrant of the paper obscuring the initial velocity vector," or "The subscript attached to the variable 'mu' is heavily blurred.")

**[USER ADVICE]**
* (Provide actionable instructions on what the user needs to do to get the required information to the AI. WARNING: NEVER SUGGEST USING A FLASH. Examples: "Move the cyberdeck camera 4 inches closer to the worksheet," "Adjust your body position to block the overhead glare," "Flatten the paper to remove the shadow," or "Move to a location with better ambient lighting.")
=========================================
</execution_logic>
<session_context_reminder>
RECALL YOUR CLASS CONTEXT: At the start of this session, you ingested course-specific
methods, variable notation, and formulas. You MUST apply them now:
- Every variable name in your solution must match the notation from that context exactly.
  Do not substitute equivalent symbols from general convention.
- The method and formula form you choose in [ANALYSIS] must be the one from that context,
  not a textbook alternative unless absolutely necessary to solving the problem.
- If a value's unit or symbol differs between the class context and general practice,
  the class context wins.
This is non-negotiable. Solving correctly but in the wrong notation is a wrong answer.
</session_context_reminder>
</system_instruction>"""

# Scrolling: 4 lines at a time
SCROLL_JUMP = 4
DIAGNOSTIC_UPDATE_INTERVAL = 0.5

# Throbber animation cadence is now fixed regardless of state. Redraw ticks
# every THROBBER_TICK_SEC seconds; the dots advance a frame every
# THROBBER_TICKS_PER_FRAME ticks. Net dots-per-second ~= 1 / (0.1 * 3).
THROBBER_TICK_SEC = 0.1          # redraw / stopwatch refresh at 10 Hz
THROBBER_TICKS_PER_FRAME = 3     # advance . . . every 3 ticks ~= 3.3 Hz
THROBBER_FRAMES = ['   ', '.  ', '.. ', '...']
throbber_frame = 0
throbber_tick = 0

# 256-color palette picks. These read well on a white background; 8-color
# terminals fall back to the standard curses constants.
DARK_MAGENTA_256 = 90   # RGB 135/0/135   -- numbers & status line
DARK_YELLOW_256  = 130  # RGB 175/95/0    -- amber/ochre (replaces pastel yellow)
DARK_GREEN_256   = 28   # RGB 0/135/0     -- richer green for diag_ok

THEME = {
    "normal":       (curses.COLOR_BLACK,   curses.COLOR_WHITE),
    "latex":        (curses.COLOR_BLUE,    curses.COLOR_WHITE),
    "number":       (curses.COLOR_MAGENTA, curses.COLOR_WHITE),
    "diag_ok":      (curses.COLOR_GREEN,   curses.COLOR_WHITE),
    "diag_warn":    (curses.COLOR_YELLOW,  curses.COLOR_WHITE),
    "diag_crit":    (curses.COLOR_RED,     curses.COLOR_WHITE),
    "status_text":  (curses.COLOR_MAGENTA, curses.COLOR_WHITE),
    "mode_num":     (curses.COLOR_CYAN,    curses.COLOR_WHITE),
    "mode_alpha":   (curses.COLOR_GREEN,   curses.COLOR_WHITE),
    "splash_title": (curses.COLOR_BLUE,    curses.COLOR_WHITE),
    "splash_key":   (curses.COLOR_MAGENTA, curses.COLOR_WHITE),
    # RSSI legend colors only -- the live diagnostics bar keeps using diag_ok/warn/crit.
    "rssi_strong":  (curses.COLOR_GREEN,   curses.COLOR_WHITE),
    "rssi_weak":    (curses.COLOR_RED,     curses.COLOR_WHITE),
}

# --- KEYBOARD MODE CONFIGURATION ---
# The gint add-in this project is built on is ALPHA-locked by default, so
# when the physical '1' key is pressed on the Casio it actually sends the
# character 'u' to the Python program, not '1' -- gint emits the red ALPHA
# label in LOWERCASE. Same for every numpad key and the scientific-row keys.
#
# KEY_MAP is therefore keyed on the uppercase form of what gint sends and
# produces an output per program mode. Lookup in process_key_input() upper-
# cases the incoming char before indexing so case differences can't cause
# silent misses.
#
# ALPHA mode is not represented in the table: it is defined as pure
# pass-through of gint's native lowercase output, so users type plain text
# with no transformation.
#
#   tuple order: (num_out, sym_out)
#
# Physical layout (red ALPHA labels above each key):
#   [7 M] [8 N] [9 O]
#   [4 P] [5 Q] [6 R] [x S] [/ T]
#   [1 U] [2 V] [3 W] [+ X] [- Y]
#   [0 Z] [.]
# Scientific-row keys currently mapped: B (log), C (ln), D (sin), E (cos),
# F (tan), I ('('), J (')'), K (',').

NUM, ALPHA, SYM = 'NUM', 'ALPHA', 'SYM'

KEY_MAP = {
    'M': ('7', '('),       # 7 key
    'N': ('8', ')'),       # 8 key
    'O': ('9', '}'),       # 9 key
    'P': ('4', '<'),       # 4 key
    'Q': ('5', '>'),       # 5 key
    'R': ('6', '{'),       # 6 key
    'S': ('*', ';'),       # multiply key
    'T': ('/', ':'),       # divide key
    'U': ('1', '['),       # 1 key
    'V': ('2', ']'),       # 2 key
    'W': ('3', '_'),       # 3 key
    'X': ('+', '"'),       # + key
    'Y': ('-', "'"),       # - key
    'Z': ('0', ' '),       # 0 key
    '.': ('.', ','),       # . key

    # Scientific-row keys: same output in NUM and SYM mode per user spec.
    'B': ('log(',  'log('),   # log key
    'C': ('ln(',   'ln('),    # ln key
    'D': ('sin(',  'sin('),   # sin key
    'E': ('cos(',  'cos('),   # cos key
    'F': ('tan(',  'tan('),   # tan key
    'I': ('(',     '('),      # ( key
    'J': (')',     ')'),      # ) key
    'K': (',',     ','),      # , key
}

# ALPHA-mode-only overrides. In ABC mode these keys emit the mapped token
# instead of gint's raw lowercase letter. Everything NOT in this dict falls
# through to plain pass-through, preserving normal text entry.
ALPHA_OVERRIDES = {}
# --- END OF CONFIGURATION ---

STYLES = {}

def new_gemini_chat():
    """Gemini chat session at the configured thinking level."""
    return client.chats.create(
        model=MODEL_NAME,
        config=genai_types.GenerateContentConfig(
            thinking_config=genai_types.ThinkingConfig(
                thinking_level=GEMINI_THINKING_LEVEL)))


# Each Channel owns its own session; there is deliberately no module-level
# one, so context can never be loaded into a session that nothing answers from.
client = None
try:
    client = genai.Client(api_key=API_KEY)
except Exception:
    client = None

# --- GLOBAL STATE ---
header_lines = []


GEMINI, OPENAI_CH = "GEMINI", "OPENAI"


class Channel:
    """One model's independent conversation.

    Both channels receive every captured photo, but their histories are kept
    apart -- switching the view must never clear or reset either one."""

    def __init__(self, key, label):
        self.key = key
        self.label = label
        self.history = []
        self.scroll_offset = 0
        self.status = "idle"      # idle | working | done | error | disabled
        self.step = "..."
        self.wait_start = 0.0
        self.answer = None
        self.error = None
        self.thread = None
        self.session = None       # genai chat session / OpenAI client
        self.prev_response_id = None   # OpenAI conversation chaining
        self.retries = 0
        self.pending = None       # (is_f1, path, text) for a retry
        self.awaiting_choice = False
        self.status_index = None
        self.last_answer = None   # extracted final number, for the DIFF marker
        self.last_usage = None    # token usage of the last call, if reported
        self.enabled = True

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()


channels = {GEMINI: Channel(GEMINI, "GEMINI"), OPENAI_CH: Channel(OPENAI_CH, "OPENAI")}
active_channel = GEMINI

# chat_history aliases the active channel's list, so every existing helper that
# appends to it keeps working unchanged.
chat_history = channels[active_channel].history
scroll_offset = 0
force_redraw = False
redraw_lock = threading.Lock()
restart_confirm_active = False
active_class_name = None
context_confirm_active = False
pending_class_md = None

# Keyboard mode state
input_mode = NUM
sym_active = False

# Viewfinder overlay state
viewfinder_process = None

# Stats State
last_net_stats = {'time': 0, 'rx': 0, 'tx': 0}
current_net_speed = {'down': 0, 'up': 0}


def initialize_theme(stdscr):
    curses.start_color()
    curses.use_default_colors()
    is256 = curses.COLORS >= 256

    # Color substitutions for 256-color terminals. On 8-color terms we keep
    # the standard curses constants, which may look pastel on white.
    substitutions = {}
    if is256:
        substitutions[curses.COLOR_MAGENTA] = DARK_MAGENTA_256
        substitutions[curses.COLOR_YELLOW]  = DARK_YELLOW_256
        substitutions[curses.COLOR_GREEN]   = DARK_GREEN_256

    resolved_theme = {}
    for name, (fg, bg) in THEME.items():
        if fg in substitutions:
            fg = substitutions[fg]
        resolved_theme[name] = (fg, bg)

    for i, (name, (fg, bg)) in enumerate(resolved_theme.items(), 1):
        try:
            curses.init_pair(i, fg, bg)
        except curses.error:
            orig_fg, orig_bg = THEME[name]
            curses.init_pair(i, orig_fg, orig_bg)
        STYLES[name] = curses.color_pair(i)

    # Base Attributes
    STYLES['bold'] = STYLES['normal'] | curses.A_BOLD
    STYLES['italic'] = STYLES['normal'] | curses.A_ITALIC
    STYLES['user_input'] = STYLES['normal']

    # Number Attributes (Dark Magenta)
    STYLES['number'] = STYLES['number']
    STYLES['bold_number'] = STYLES['number'] | curses.A_BOLD
    STYLES['italic_number'] = STYLES['number'] | curses.A_ITALIC

    # Diagnostics
    STYLES['diag_ok'] = STYLES['diag_ok'] | curses.A_BOLD

    # Splash
    STYLES['splash_title'] = STYLES['splash_title'] | curses.A_BOLD
    STYLES['splash_key'] = STYLES['splash_key'] | curses.A_BOLD

    # RSSI legend endpoints
    STYLES['rssi_strong'] = STYLES['rssi_strong'] | curses.A_BOLD
    STYLES['rssi_weak'] = STYLES['rssi_weak'] | curses.A_BOLD

    # Post-splash hint line is dim so it reads as ambient/informational.
    STYLES['hint'] = STYLES['normal'] | curses.A_DIM

    stdscr.bkgd(' ', STYLES['normal'])


def add_segmented_history(segments):
    chat_history.append(segments)


def get_diagnostics_styled():
    global input_mode, sym_active
    segments = []
    normal, ok, warn, crit = STYLES['normal'], STYLES['diag_ok'], STYLES['diag_warn'], STYLES['diag_crit']
    try:
        # Mode indicator -- brackets per README, 7 chars wide so toggling modes
        # doesn't shift the rest of the line. SHIFT (SYM) takes priority over
        # the base mode display when active.
        if sym_active:
            mode_text = " [SYM] "
            mode_style = warn | curses.A_BOLD
        elif input_mode == ALPHA:
            mode_text = " [abc] "
            mode_style = STYLES.get('mode_alpha', normal) | curses.A_BOLD
        else:
            mode_text = " [123] "
            mode_style = STYLES.get('mode_num', normal) | curses.A_BOLD
        segments.append((mode_text, mode_style))

        # CPU
        with open('/sys/class/thermal/thermal_zone0/temp', 'r') as f:
            temp_c = int(f.read()) / 1000.0
        temp_text = f"{temp_c:.0f}C"
        temp_style = ok if temp_c < 50 else (warn if temp_c < 70 else crit)
        segments.extend([("CPU: ", normal), (temp_text, temp_style)])

        # RAM
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
        mem_total_kb, mem_avail_kb = int(lines[0].split()[1]), int(lines[2].split()[1])
        mem_used_mb = (mem_total_kb - mem_avail_kb) / 1024
        mem_total_mb = mem_total_kb / 1024
        usage_percent = (mem_used_mb / mem_total_mb) * 100
        ram_text = f"{int(mem_used_mb)}/{int(mem_total_mb)}M"
        ram_style = ok if usage_percent < 50 else (warn if usage_percent < 75 else crit)
        segments.extend([(" | RAM: ", normal), (ram_text, ram_style)])

        # WiFi signal bar + dBm (unchanged behavior from before)
        try:
            ssid = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, check=True).stdout.strip()
            link = subprocess.run(["iw", "dev", "wlan0", "link"], capture_output=True, text=True, check=True).stdout
            match = re.search(r"signal: (-?\d+)", link)
            if ssid and match:
                signal_dbm = int(match.group(1))
                if signal_dbm >= -55:
                    bar_str, bar_style = "[####]", ok
                elif signal_dbm >= -65:
                    bar_str, bar_style = "[### ]", ok
                elif signal_dbm >= -75:
                    bar_str, bar_style = "[##  ]", warn
                else:
                    bar_str, bar_style = "[#   ]", crit
                segments.extend([(" | WiFi: ", normal), (bar_str, bar_style), (f" ({signal_dbm})", normal)])
        except (subprocess.CalledProcessError, FileNotFoundError):
            segments.extend([(" | WiFi: ", normal), ("OFF", crit)])

        # RTC in military time (HH:MM local) -- replaces token counter.
        segments.extend([(" | ", normal), (time.strftime("%H:%M"), normal | curses.A_BOLD)])

        return segments
    except Exception:
        return [("Diag Error", normal)]


def parse_inner_text(text, base_style, is_bold=False):
    return parse_inner_numbers(text, base_style, is_bold)


def parse_inner_numbers(text, current_style, is_bold):
    segments = []
    regex_number = re.compile(r'(\b\d+(?:,\d{3})*(?:\.\d+)?\b(?!\. ))')
    parts = [p for p in regex_number.split(text) if p]
    for p in parts:
        if regex_number.match(p):
            style = STYLES['bold_number'] if is_bold else STYLES['number']
            if not is_bold and current_style == STYLES['italic']:
                style = STYLES['italic_number']
            segments.append((p, style))
        else:
            segments.append((p, current_style))
    return segments


def parse_and_add_history(text, width, force_style=None):
    normal = STYLES['normal']
    bold = STYLES['bold']
    latex = STYLES['latex']

    text = text.replace(r'\$', '$')
    regex_structure = re.compile(r'(\$\$.*?\$\$)|(\*\*.*?\*\*)|(\$.*?\$)')

    for line in text.splitlines():
        if line.strip().startswith('* '):
            line = line.replace('* ', '\u2022 ', 1)

        is_heading = line.strip().startswith('###')
        if is_heading:
            line = line.strip()[3:].strip()
        wrapped_lines = textwrap.wrap(line, width - 2, replace_whitespace=False, drop_whitespace=False)

        if not wrapped_lines:
            chat_history.append([("", normal)])
        else:
            for i, wrapped_line in enumerate(wrapped_lines):
                segments = []
                if force_style:
                    segments.append((wrapped_line, force_style))
                    chat_history.append(segments)
                    continue

                is_line_bold = (is_heading and i == 0)
                base_style = bold if is_line_bold else normal

                parts_1 = [p for p in regex_structure.split(wrapped_line) if p]

                for p1 in parts_1:
                    if (p1.startswith('$$') and p1.endswith('$$')) or (p1.startswith('$') and p1.endswith('$')):
                        content = p1.replace('$', '')
                        segments.append((content, latex))
                    elif p1.startswith('**') and p1.endswith('**'):
                        content = p1[2:-2]
                        segments.extend(parse_inner_text(content, bold, is_bold=True))
                    else:
                        segments.extend(parse_inner_text(p1, base_style, is_bold=is_line_bold))

                chat_history.append(segments)


def add_splash_hint_line():
    """Append a scrollable, non-persistent 'F2: Open splashscreen' line.
    Lives in chat_history like any other line so the user can scroll past it.
    """
    hint_style = STYLES.get('hint', STYLES['normal'])
    key_style = STYLES.get('splash_key', STYLES['normal'])
    chat_history.append([
        ("F2", key_style),
        (": Open splashscreen", hint_style),
    ])


def draw_screen(stdscr, current_input):
    global scroll_offset
    stdscr.clear()
    height, width = stdscr.getmaxyx()

    header_height = len(header_lines)
    for i, line_segments in enumerate(header_lines):
        if i >= height:
            break
        cursor_x = 0
        for text, style in line_segments:
            if cursor_x < width:
                try:
                    stdscr.addstr(i, cursor_x, text, style)
                except curses.error:
                    pass
                cursor_x += len(text)

    chat_area_height = height - header_height - 1
    if chat_area_height < 1:
        return

    max_scroll = max(0, len(chat_history) - chat_area_height)
    if scroll_offset > max_scroll:
        scroll_offset = max_scroll
    if scroll_offset < 0:
        scroll_offset = 0

    display_lines = chat_history[scroll_offset: scroll_offset + chat_area_height]

    for i, line_segments in enumerate(display_lines):
        row = i + header_height
        if row >= height - 1:
            break
        cursor_x = 0
        for text, style in line_segments:
            if cursor_x < width:
                try:
                    stdscr.addstr(row, cursor_x, text, style)
                except curses.error:
                    pass
                cursor_x += len(text)

    prompt = f"> {current_input}"
    try:
        stdscr.addstr(height - 1, 0, prompt[-width + 1:], STYLES['normal'])
    except curses.error:
        pass
    stdscr.refresh()


# --- SPLASH SCREENS -----------------------------------------------------------

def render_splash_line(stdscr, y, block_x, w, line):
    """Helper: render one splash body line, highlighting leading key tokens
    and coloring inline [marker] style tokens. Mutates nothing; safe to call
    repeatedly."""
    try:
        # Leading-key highlight path: "F1  Capture..." -> F1 in splash_key color
        stripped = line.lstrip()
        leading_ws = len(line) - len(stripped)
        gap = stripped.find("  ")
        
        cursor_x = block_x
        
        if gap > 0 and (stripped[:gap].startswith('F') or
                        stripped[:gap].startswith('SYM+') or
                        stripped[:gap] in ('Enter', 'Up/Down', 'Key', 'RSSI')):
            key_part = stripped[:gap]
            rest_part = stripped[gap:]
            stdscr.addstr(y, cursor_x, " " * leading_ws, STYLES['normal'])
            cursor_x += leading_ws
            stdscr.addstr(y, cursor_x, key_part,
                          STYLES.get('splash_key', STYLES['normal']))
            cursor_x += len(key_part)
            line_to_process = rest_part
        else:
            line_to_process = line
            
        # Inline-marker path: colors [[G]]...[[/G]], [[R]]...[[/R]] segments.
        marker_re = re.compile(r'\[\[(G|R)\]\](.*?)\[\[/\1\]\]')
        pos = 0
        for m in marker_re.finditer(line_to_process):
            before = line_to_process[pos:m.start()]
            if before:
                stdscr.addstr(y, cursor_x, before[:max(0, w - cursor_x - 1)], STYLES['normal'])
                cursor_x += len(before)
            tag, content = m.group(1), m.group(2)
            style = STYLES['rssi_strong'] if tag == 'G' else STYLES['rssi_weak']
            stdscr.addstr(y, cursor_x, content[:max(0, w - cursor_x - 1)], style)
            cursor_x += len(content)
            pos = m.end()
        tail = line_to_process[pos:]
        if tail:
            stdscr.addstr(y, cursor_x, tail[:max(0, w - cursor_x - 1)], STYLES['normal'])
    except curses.error:
        pass


def show_splash(stdscr, title, body_lines):
    """Render one splash. Blocks until F2 (DISMISS) or F3 (SWAP).
    Returns 'DISMISS' or 'SWAP'. All other keys are ignored.
    """
    stdscr.clear()
    h, w = stdscr.getmaxyx()

    title_line = f"=== {title} ==="
    footer_line = "[ F2=close | F3=keyboard map ]"

    total_rows = 1 + 1 + len(body_lines) + 1 + 1
    start_y = max(0, (h - total_rows) // 2)

    try:
        x = max(0, (w - len(title_line)) // 2)
        stdscr.addstr(start_y, x, title_line[:w - 1],
                      STYLES.get('splash_title', STYLES['normal']))
    except curses.error:
        pass

    # Width for centering: use the DISPLAYED length of each line (markers stripped)
    def display_len(s):
        return len(re.sub(r'\[\[/?[GR]\]\]', '', s))

    max_body_w = max((display_len(l) for l in body_lines), default=0)
    block_x = max(0, (w - max_body_w) // 2)
    for i, line in enumerate(body_lines):
        y = start_y + 2 + i
        if y >= h - 2:
            break
        render_splash_line(stdscr, y, block_x, w, line)

    try:
        y = min(h - 1, start_y + 2 + len(body_lines) + 1)
        x = max(0, (w - len(footer_line)) // 2)
        stdscr.addstr(y, x, footer_line[:w - 1], STYLES['normal'] | curses.A_BOLD)
    except curses.error:
        pass

    stdscr.refresh()

    stdscr.timeout(-1)
    try:
        while True:
            try:
                k = stdscr.getch()
            except curses.error:
                continue
            if k == curses.KEY_F2:
                return 'DISMISS'
            if k == curses.KEY_F3:
                return 'SWAP'
    finally:
        stdscr.timeout(50)


def show_splash_system(stdscr):
    current = 'FKEYS'
    while True:
        if current == 'FKEYS':
            result = show_splash(stdscr, "FUNCTIONS", get_startup_splash_lines())
        else:
            result = show_splash(stdscr, "KEYBOARD MAP", get_keyboard_splash_lines())
        if result == 'DISMISS':
            return
        current = 'KBMAP' if current == 'FKEYS' else 'FKEYS'


def get_startup_splash_lines():
    return [
        "F1      Capture & analyze image",
        "SYM+F1  Toggle camera viewfinder",
        "SYM+F2  Switch model view",
        "SYM+F3  Re-enable a stopped model",
        "SYM+F5  Compare both answers",
        "F2      Toggle this splashscreen",
        "F4      Toggle SHIFT  (SYM, 1-press)",
        "F5      Toggle NUM/ALPHA",
        "F6      Restart program  (press 2x)",
        "",
        "Enter   Send text message",
        "F3      Clear input buffer",
        "Up/Down Scroll chat history",
        "",
        "RSSI    [[[G]]-30[[/G]], [[R]]-90[[/R]]] dBm",
    ]


def get_keyboard_splash_lines():
    lines = [
        "Modes:",
        "   [123]  NUM    numpad = numbers",
        "   [abc]  ALPHA  pass-through lowercase",
        "   [SYM]  SHIFT  F4 one-press sticky",
        "",
        "Sent  NUM    SYM",
        "----  -----  -----",
    ]
    for k, (n, s) in KEY_MAP.items():
        n_d = n if n != ' ' else "_"
        s_d = s if s != ' ' else "_"
        lines.append(f" {k:<3}   {n_d:<5}  {s_d}")

    if ALPHA_OVERRIDES:
        lines.append("")
        lines.append("ALPHA overrides:")
        for k, v in ALPHA_OVERRIDES.items():
            lines.append(f" {k:<3}   {v}")
    return lines


def show_class_selector(stdscr):
    if not os.path.exists(NOTES_DIR):
        return None, None
    md_files = [f for f in os.listdir(NOTES_DIR) if f.endswith('.md')]
    if not md_files:
        return None, None
        
    md_files.sort()
    class_names = [f[:-3] for f in md_files]
    
    selected_idx = 0
    stdscr.timeout(-1)
    try:
        while True:
            stdscr.clear()
            h, w = stdscr.getmaxyx()
            
            title_line = "=== SELECT CLASS CONTEXT ==="
            footer_line = "[ Up/Down=navigate | Enter=select | F2=skip ]"
            
            total_rows = 1 + 1 + len(class_names) + 1 + 1
            start_y = max(0, (h - total_rows) // 2)
            
            try:
                x = max(0, (w - len(title_line)) // 2)
                stdscr.addstr(start_y, x, title_line[:w - 1],
                              STYLES.get('splash_title', STYLES['normal']))
            except curses.error:
                pass
                
            max_body_w = max((len(name) + 2 for name in class_names), default=0)
            block_x = max(0, (w - max_body_w) // 2)
            
            for i, name in enumerate(class_names):
                y = start_y + 2 + i
                if y >= h - 2:
                    break
                try:
                    if i == selected_idx:
                        stdscr.addstr(y, block_x, f"> {name}"[:w - 1], STYLES['bold'])
                    else:
                        stdscr.addstr(y, block_x, f"  {name}"[:w - 1], STYLES['normal'])
                except curses.error:
                    pass
                    
            try:
                y = min(h - 1, start_y + 2 + len(class_names) + 1)
                x = max(0, (w - len(footer_line)) // 2)
                stdscr.addstr(y, x, footer_line[:w - 1], STYLES['normal'] | curses.A_BOLD)
            except curses.error:
                pass
                
            stdscr.refresh()
            
            try:
                k = stdscr.getch()
            except curses.error:
                continue
                
            if k == curses.KEY_UP:
                selected_idx = max(0, selected_idx - 1)
            elif k == curses.KEY_DOWN:
                selected_idx = min(len(class_names) - 1, selected_idx + 1)
            elif k == curses.KEY_ENTER or k == 10:
                selected_class = class_names[selected_idx]
                with open(os.path.join(NOTES_DIR, md_files[selected_idx]), 'r', encoding='utf-8') as f:
                    md_content = f.read()
                return selected_class, md_content
            elif k == curses.KEY_F2:
                return None, None
    finally:
        stdscr.timeout(50)

def build_class_context_message(class_name, md_content):
    return f"""<class_context>
You are being initialized for a {class_name} engineering problem-solving session.
The following document contains the exact methods, variable notation, and formulas
used in this course. Read it carefully and completely.

---
{md_content}
---

</class_context>

<initialization_instruction>
For this entire session you MUST:
1. Use the variable names and notation defined in the document above.
   If the course uses a specific symbol for a quantity, use that symbol -- no substitutions.
2. Apply the specific methods and formula forms shown above.
3. When the class method and a general method differ, always prefer to use the class method.
4. Treat this context as your ground truth for this subject.

Respond with this confirmation line once you have understood and digested the context:
"Context Loaded. Course: {class_name}"
</initialization_instruction>"""


def send_class_context(class_name, md_content, width):
    """Prime every enabled model with the course notes.

    Both models must be primed from identical context or the cross-check is
    comparing two different setups. This goes through the same Channel
    machinery as a capture, so it is non-blocking and each model's
    confirmation lands in its own conversation."""
    msg = build_class_context_message(class_name, md_content)
    for key in (GEMINI, OPENAI_CH):
        ch = channels[key]
        if not ch.enabled:
            continue
        add_to_channel(ch, f"[*] Loading {class_name}", width,
                       force_style=STYLES['hint'])
        ch.pending = (False, None, msg)
        ch.retries = 0
        ch.last_answer = None
        launch_channel(ch, width)


# --- STATUS / UPLOAD STATE MACHINE --------------------------------------------

def _swaymsg(*args):
    """Fire a swaymsg command. Sway is the parent of this process, so SWAYSOCK
    is inherited; failures are non-fatal because the viewfinder is optional."""
    try:
        subprocess.run(["swaymsg", *args], capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        pass


def stop_viewfinder():
    """Tear down the viewfinder and wait for /dev/video0 to be released.
    Returns True if it had been running."""
    global viewfinder_process

    was_running = viewfinder_process is not None
    if viewfinder_process:
        try:
            pgid = os.getpgid(viewfinder_process.pid)
            os.killpg(pgid, signal.SIGTERM)
            time.sleep(0.15)
            if viewfinder_process.poll() is None:
                os.killpg(pgid, signal.SIGKILL)
        except (ProcessLookupError, OSError):
            pass
        viewfinder_process = None

    # Always sweep for orphans -- a half-dead rpicam-vid keeps the camera.
    for pattern in ("rpicam-vid", "mpv.*viewfinder"):
        try:
            subprocess.run(["pkill", "-9", "-f", pattern],
                           capture_output=True, text=True, timeout=2)
        except (OSError, subprocess.SubprocessError):
            pass

    if was_running:
        # Sway draws fullscreen windows above floating ones, so the terminal
        # is un-fullscreened while the overlay is up. Put it back.
        _swaymsg('[app_id="foot"]', "fullscreen", "enable")
        time.sleep(VF_CAMERA_RELEASE_SEC)

    return was_running


def start_viewfinder():
    """Launch the rpicam-vid -> mpv overlay. Returns True on launch.

    OV5647 is fixed-focus at 7in with no AF motor, so there is no autofocus
    or lens-position handling here."""
    global viewfinder_process

    if viewfinder_process is not None:
        return False

    cmd = (
        f"rpicam-vid -t 0 --width {VF_WIDTH} --height {VF_HEIGHT} "
        f"--codec mjpeg --framerate {VF_FPS} -n -o - | "
        "mpv --no-terminal --vo=wlshm --profile=low-latency "
        "--demuxer=lavf --demuxer-lavf-format=mjpeg --untimed "
        f"--title=viewfinder --geometry={VF_GEOMETRY} --no-border -"
    )

    # Drop fullscreen first, or the floating overlay renders behind the term.
    _swaymsg('[app_id="foot"]', "fullscreen", "disable")

    try:
        viewfinder_process = subprocess.Popen(
            cmd, shell=True, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, preexec_fn=os.setsid)
    except (OSError, subprocess.SubprocessError):
        viewfinder_process = None
        _swaymsg('[app_id="foot"]', "fullscreen", "enable")
        return False

    return True


def toggle_viewfinder(width):
    """SYM+F1 handler. The same key closes it; so does an F1 capture."""
    if viewfinder_process is None:
        if start_viewfinder():
            parse_and_add_history("[*] Viewfinder ON  (SYM+F1 to close)", width)
        else:
            parse_and_add_history("[!] Viewfinder failed to start", width)
    else:
        stop_viewfinder()
        parse_and_add_history("[*] Viewfinder OFF", width)


# --- MODEL CHANNELS ---------------------------------------------------------

def switch_channel(target=None):
    """Flip the visible model. Neither history is touched."""
    global active_channel, chat_history, scroll_offset
    channels[active_channel].scroll_offset = scroll_offset
    if target is None:
        order = [GEMINI, OPENAI_CH]
        target = order[(order.index(active_channel) + 1) % len(order)]
    active_channel = target
    chat_history = channels[active_channel].history
    scroll_offset = channels[active_channel].scroll_offset
    return channels[active_channel]


def add_to_channel(ch, text, width, force_style=None):
    """Append to a specific channel, whether or not it is the visible one."""
    global chat_history
    saved = chat_history
    chat_history = ch.history
    try:
        parse_and_add_history(text, width, force_style)
    finally:
        chat_history = saved


def other_channel(ch):
    return channels[OPENAI_CH if ch.key == GEMINI else GEMINI]


def format_channel_status(ch):
    """Status line text for one channel's in-flight request."""
    step = ch.step
    if step == "CAPTURING":
        return "[*] Capturing"
    if step == "UPLOADING":
        return "[*] Uploading"
    if step == "UPLOADED":
        return "[*] Uploaded"
    if step == "WAITING":
        elapsed = max(0.0, time.time() - ch.wait_start)
        if elapsed < 60.0:
            return f"[*] {ch.label} thinking ({elapsed:.1f})"
        mins = int(elapsed // 60)
        secs = elapsed - (mins * 60)
        return f"[*] {ch.label} thinking ({mins}min{secs:.1f})"
    return "[*] ..."


_NUM_RE = re.compile(r"-?\d+(?:,\d{3})*(?:\.\d+)?(?:[eE][-+]?\d+)?")


def extract_final_answer(text):
    """Last number in a response -- a cheap stand-in for 'the final answer'.

    Deliberately crude: it exists only to raise the DIFF marker, and returns
    None whenever there is nothing numeric to compare."""
    if not text:
        return None
    nums = _NUM_RE.findall(text)
    if not nums:
        return None
    try:
        return float(nums[-1].replace(",", ""))
    except ValueError:
        return None


def answers_disagree():
    """True only when both channels produced a comparable number and differ."""
    a = channels[GEMINI].last_answer
    b = channels[OPENAI_CH].last_answer
    if a is None or b is None:
        return False
    scale = max(abs(a), abs(b), 1.0)
    return abs(a - b) > 1e-6 * scale


# --- PER-MODEL REQUESTS -----------------------------------------------------

def _gemini_request(ch, is_f1, path, text):
    if client is None:
        raise RuntimeError("No Gemini key. Set GEMINI_API_KEY in %s" % ENV_FILE)
    if ch.session is None:
        ch.session = new_gemini_chat()

    if is_f1:
        ch.step = "UPLOADING"
        try:
            parts = [F1_PROMPT, Image.open(path)]
        except Exception:
            parts = [F1_PROMPT, client.files.upload(file=path)]
        ch.step = "UPLOADED"
        time.sleep(0.5)
    else:
        parts = [text]

    ch.wait_start = time.time()
    ch.step = "WAITING"
    return ch.session.send_message(parts).text


def _openai_request(ch, is_f1, path, text):
    if OpenAI is None:
        raise RuntimeError("openai package not installed")
    if not OPENAI_API_KEY:
        raise RuntimeError("No OpenAI key. Set OPENAI_API_KEY in %s" % ENV_FILE)
    if ch.session is None:
        ch.session = OpenAI(api_key=OPENAI_API_KEY)

    if is_f1:
        ch.step = "UPLOADING"
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        content = [
            {"type": "input_text", "text": F1_PROMPT},
            {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64},
        ]
        ch.step = "UPLOADED"
        time.sleep(0.5)
    else:
        content = [{"type": "input_text", "text": text}]

    ch.wait_start = time.time()
    ch.step = "WAITING"
    kwargs = {
        "model": OPENAI_MODEL_NAME,
        "reasoning": {"effort": OPENAI_REASONING_EFFORT},
        "input": [{"role": "user", "content": content}],
    }
    # Chain turns server-side so the photo is not re-uploaded every message.
    if ch.prev_response_id:
        kwargs["previous_response_id"] = ch.prev_response_id
    response = ch.session.responses.create(**kwargs)
    ch.prev_response_id = response.id
    ch.last_usage = getattr(response, "usage", None)
    return response.output_text


REQUEST_FNS = {GEMINI: _gemini_request, OPENAI_CH: _openai_request}


def _short_error(e):
    """SDK errors carry a JSON blob and a URL; the deck has ~40 columns.
    Pull the human-readable message out and keep it to one or two lines."""
    text = str(e)
    match = re.search(r"'message': ['\"](.+?)['\"](?:, ')", text)
    msg = match.group(1) if match else text
    msg = " ".join(msg.split())
    if len(msg) > 68:
        msg = msg[:65] + "..."
    return "%s: %s" % (type(e).__name__.replace("Error", ""), msg)


def _run_channel(ch):
    """Worker body. Never touches curses -- only channel state."""
    is_f1, path, text = ch.pending
    try:
        ch.answer = REQUEST_FNS[ch.key](ch, is_f1, path, text)
        ch.status = "done"
    except Exception as e:
        ch.error = _short_error(e)
        ch.status = "error"


def launch_channel(ch, width):
    """Start (or restart) one channel's request on its own thread."""
    ch.status = "working"
    ch.step = "UPLOADING" if ch.pending[0] else "WAITING"
    ch.answer = None
    ch.error = None
    ch.awaiting_choice = False
    ch.status_index = len(ch.history)
    add_to_channel(ch, "[*] ...", width)
    ch.thread = threading.Thread(target=_run_channel, args=(ch,), daemon=True)
    ch.thread.start()


def dispatch_capture(is_f1, path, text, width):
    """Fire every enabled channel in parallel. Each gets its own turn."""
    started = []
    for key in (GEMINI, OPENAI_CH):
        ch = channels[key]
        if not ch.enabled:
            continue
        if ch.history:
            add_to_channel(ch, " ", width)
        if not is_f1:
            add_to_channel(ch, f"> {text}", width, force_style=STYLES['user_input'])
        ch.pending = (is_f1, path, text)
        ch.retries = 0
        ch.last_answer = None
        launch_channel(ch, width)
        started.append(ch)
    return started


def tick_channels(width):
    """Advance throbbers and reap finished workers. Returns True if the screen
    needs repainting. Called from the main loop, so curses stays single
    threaded."""
    global throbber_frame, throbber_tick
    dirty = False

    throbber_tick += 1
    if throbber_tick % THROBBER_TICKS_PER_FRAME == 0:
        throbber_frame = (throbber_frame + 1) % len(THROBBER_FRAMES)

    for key in (GEMINI, OPENAI_CH):
        ch = channels[key]

        if ch.status == "working" and ch.status_index is not None:
            if ch.status_index < len(ch.history):
                ch.history[ch.status_index] = [
                    (format_channel_status(ch) + THROBBER_FRAMES[throbber_frame],
                     STYLES['status_text'])]
            # Only repaint on a frame boundary; the loop ticks at 20Hz and a
            # full clear/redraw every tick is wasteful on a Pi Zero.
            if throbber_tick % THROBBER_TICKS_PER_FRAME == 0:
                dirty = True

        if ch.status in ("done", "error") and not ch.busy:
            dirty = True
            if ch.status_index is not None and ch.status_index < len(ch.history):
                ch.history.pop(ch.status_index)
            ch.status_index = None
            ch.thread = None

            if ch.status == "done":
                ch.last_answer = extract_final_answer(ch.answer)
                add_to_channel(ch, f"{ch.label}: {ch.answer}", width)
                ch.status = "idle"
            else:
                _handle_channel_error(ch, width)

    if dirty:
        refresh_model_header()
    return dirty


def _handle_channel_error(ch, width):
    """One silent retry, then hand the decision to the user. The other model
    is flagged either way so a failure is visible without switching views."""
    peer = other_channel(ch)

    if ch.retries < AUTO_RETRY_LIMIT:
        ch.retries += 1
        add_to_channel(ch, f"[!] {ch.label} failed: {ch.error}", width,
                       force_style=STYLES['diag_crit'])
        add_to_channel(ch, f"[*] Retrying {ch.label} ({ch.retries}/{AUTO_RETRY_LIMIT})",
                       width, force_style=STYLES['diag_warn'])
        add_to_channel(peer, f"[!] {ch.label} failed, retrying", width,
                       force_style=STYLES['diag_warn'])
        launch_channel(ch, width)
        return

    ch.status = "error"
    ch.awaiting_choice = True
    add_to_channel(ch, f"[!] {ch.label} failed: {ch.error}", width,
                   force_style=STYLES['diag_crit'])
    add_to_channel(ch, "[?] Enter=retry  F3=stop model", width,
                   force_style=STYLES['diag_ok'])
    add_to_channel(peer, f"[!] {ch.label} failed - SYM+F2", width,
                   force_style=STYLES['diag_crit'])


def get_model_header_segments():
    """Second header row: which model you are looking at, and how the other
    one is doing, so a failure is visible without switching."""
    normal = STYLES['normal']
    segs = []
    for i, key in enumerate((GEMINI, OPENAI_CH)):
        ch = channels[key]
        if i:
            segs.append(("  ", normal))
        is_active = (key == active_channel)
        segs.append((f"[{ch.label}] " if is_active else f" {ch.label}  ",
                     (STYLES.get('mode_num', normal) | curses.A_BOLD) if is_active
                     else STYLES.get('hint', normal)))
        if not ch.enabled:
            segs.append(("off", STYLES.get('hint', normal)))
        elif ch.status == "working":
            segs.append(("...", STYLES['diag_warn']))
        elif ch.awaiting_choice or ch.status == "error":
            segs.append(("ERR", STYLES['diag_crit']))
        else:
            segs.append(("ok", STYLES['diag_ok']))
    if answers_disagree():
        segs.append(("  DIFF", STYLES['diag_crit'] | curses.A_BOLD))
    return segs


def refresh_model_header():
    with redraw_lock:
        if len(header_lines) > 1:
            header_lines[1] = get_model_header_segments()


def compare_answers(width):
    """The optional third call. Asks OpenAI whether the two answers agree."""
    g, o = channels[GEMINI], channels[OPENAI_CH]
    if not g.answer or not o.answer:
        return "[!] Need an answer from both models first."
    if OpenAI is None or not OPENAI_API_KEY:
        return "[!] No OpenAI key -- cannot compare."

    prompt = (
        "Two models answered the same engineering question. Say whether they "
        "reach the same final answer. Reply with AGREE or DISAGREE on the "
        "first line, then one short sentence naming the difference if any.\n\n"
        "ANSWER A (Gemini):\n%s\n\nANSWER B (GPT):\n%s" % (g.answer, o.answer)
    )
    try:
        c = OpenAI(api_key=OPENAI_API_KEY)
        r = c.responses.create(
            model=OPENAI_MODEL_NAME,
            reasoning={"effort": OPENAI_REASONING_EFFORT},
            input=prompt)
        return "Compare: " + r.output_text.strip()
    except Exception as e:
        return "[!] Compare failed: %s: %s" % (type(e).__name__, str(e)[:120])


def wait_for_network(stdscr):
    animation_frames = ['   ', '.  ', '.. ', '...']
    frame_index = 0
    while True:
        try:
            if "inet " in subprocess.run(["ip", "addr", "show", "wlan0"], capture_output=True, text=True).stdout:
                break
        except Exception:
            pass
        stdscr.clear()
        wait_text = "Connecting to Network" + animation_frames[frame_index]
        stdscr.addstr(0, 0, wait_text, STYLES['normal'])
        stdscr.refresh()
        frame_index = (frame_index + 1) % len(animation_frames)
        time.sleep(0.5)


def update_diagnostics_periodically():
    global force_redraw, last_net_stats, current_net_speed
    while True:
        try:
            with open('/sys/class/net/wlan0/statistics/rx_bytes', 'r') as f:
                rx = int(f.read())
            with open('/sys/class/net/wlan0/statistics/tx_bytes', 'r') as f:
                tx = int(f.read())
            now = time.time()
            if last_net_stats['time'] != 0:
                dt = now - last_net_stats['time']
                if dt > 0:
                    current_net_speed['down'] = (rx - last_net_stats['rx']) / dt / 1024
                    current_net_speed['up'] = (tx - last_net_stats['tx']) / dt / 1024
            last_net_stats = {'time': now, 'rx': rx, 'tx': tx}
        except Exception:
            pass

        time.sleep(DIAGNOSTIC_UPDATE_INTERVAL)
        new_diag_data = get_diagnostics_styled()
        with redraw_lock:
            if len(header_lines) > 0:
                header_lines[0] = new_diag_data
                force_redraw = True


def update_status_line(text):
    chat_history[-1] = [(text, STYLES['status_text'])]


def process_key_input(key_char):
    """Map a typed char through the current input mode.
    KEY_MAP is keyed on the uppercase letter gint sends -- see comment by
    KEY_MAP def. ALPHA mode is always pass-through of gint's native
    lowercase, so only NUM and SYM consult the map."""
    global input_mode, sym_active

    lookup = key_char.upper() if key_char.isalpha() else key_char

    # SHIFT (SYM) takes priority over base mode, and is one-press sticky.
    if sym_active:
        sym_active = False
        if lookup in KEY_MAP:
            _, sym_out = KEY_MAP[lookup]
            return sym_out
        if key_char.isprintable():
            return key_char
        return ''

    # ALPHA: plain pass-through of whatever gint sent (lowercase letters),
    # except for a small set of explicit overrides (e.g. K -> x, L -> y).
    if input_mode == ALPHA:
        if lookup in ALPHA_OVERRIDES:
            return ALPHA_OVERRIDES[lookup]
        if key_char.isprintable():
            return key_char
        return ''

    # NUM: look up the mapped digit/operator, else pass through.
    if lookup in KEY_MAP:
        num_out, _ = KEY_MAP[lookup]
        return num_out
    if key_char.isprintable():
        return key_char
    return ''


def main(stdscr):
    global scroll_offset, force_redraw, restart_confirm_active
    global input_mode, sym_active
    global active_class_name, pending_class_md, context_confirm_active

    initialize_theme(stdscr)
    stdscr.keypad(True)
    wait_for_network(stdscr)

    # Startup splash: FUNCTIONS  (F4 dismiss / F5 -> keyboard map)
    show_splash_system(stdscr)

    stdscr.timeout(50)
    height, width = stdscr.getmaxyx()
    current_input = ""
    needs_redraw = True

    header_lines.append(get_diagnostics_styled())
    header_lines.append(get_model_header_segments())

    # Scrollable hint line printed into chat history after dismissing splash.
    add_splash_hint_line()

    selected_class, class_md = show_class_selector(stdscr)
    if selected_class:
        active_class_name = selected_class
        pending_class_md = class_md
        context_confirm_active = True
        chat_history.append([(f"Class: {active_class_name}", STYLES['mode_alpha'] | curses.A_BOLD)])
        chat_history.append([(f"[?] Press Enter to load {active_class_name} context, or F2 to skip", STYLES['diag_ok'])])
        chat_area_height = height - len(header_lines) - 1
        scroll_offset = max(0, len(chat_history) - chat_area_height)
    else:
        chat_history.append([("Class: None", STYLES['normal'])])

    diag_thread = threading.Thread(target=update_diagnostics_periodically, daemon=True)
    diag_thread.start()

    while True:
        with redraw_lock:
            if force_redraw:
                needs_redraw = True
                force_redraw = False

        if needs_redraw:
            draw_screen(stdscr, current_input)
            needs_redraw = False
        try:
            key = stdscr.getch()
        except Exception:
            key = -1
        if key == -1:
            if tick_channels(width):
                draw_screen(stdscr, current_input)
            continue
        needs_redraw = True

        # --- KEY HANDLING ---
        # Cancel restart confirmation on any non-F6 key
        if key != curses.KEY_F6 and restart_confirm_active:
            restart_confirm_active = False
            chat_history.pop()
            needs_redraw = True

        if context_confirm_active:
            if key == curses.KEY_ENTER or key == 10:
                context_confirm_active = False
                send_class_context(active_class_name, pending_class_md, width)
                pending_class_md = None
                refresh_model_header()
                chat_area_height = height - len(header_lines) - 1
                scroll_offset = max(0, len(chat_history) - chat_area_height)
                needs_redraw = True
                continue
            elif key == curses.KEY_F2:  # F2 to skip
                context_confirm_active = False
                pending_class_md = None
                active_class_name = None
                chat_history.append([("Context skipped. Running in general mode.", STYLES['normal'])])
                chat_area_height = height - len(header_lines) - 1
                scroll_offset = max(0, len(chat_history) - chat_area_height)
                needs_redraw = True
                continue
            else:
                continue

        # --- RETRY / GIVE UP PROMPT (visible channel only) ---
        active = channels[active_channel]
        if active.awaiting_choice and key in (curses.KEY_ENTER, 10, curses.KEY_F3):
            if key == curses.KEY_F3:
                active.awaiting_choice = False
                active.enabled = False
                active.status = "disabled"
                peer = other_channel(active)
                add_to_channel(active, f"[*] {active.label} off (SYM+F3 on)",
                               width, force_style=STYLES['hint'])
                add_to_channel(peer, f"[*] {active.label} off - {peer.label} only",
                               width, force_style=STYLES['hint'])
                if peer.enabled:
                    switch_channel(peer.key)
            else:
                active.awaiting_choice = False
                active.retries = 0
                add_to_channel(active, f"[*] Retrying {active.label}", width,
                               force_style=STYLES['diag_warn'])
                launch_channel(active, width)
            refresh_model_header()
            chat_area_height = height - len(header_lines) - 1
            scroll_offset = max(0, len(chat_history) - chat_area_height)
            force_redraw = True
            continue

        # --- SYM + F-KEY COMBOS ---
        # F4 is the SYM toggle itself, so SYM+F4 can never be pressed.
        if sym_active and key in (curses.KEY_F1, curses.KEY_F2,
                                  curses.KEY_F3, curses.KEY_F5):
            sym_active = False
            if key == curses.KEY_F1:
                toggle_viewfinder(width)
            elif key == curses.KEY_F2:
                switch_channel()
            elif key == curses.KEY_F3:
                off = [channels[k] for k in (GEMINI, OPENAI_CH)
                       if not channels[k].enabled]
                if not off:
                    add_to_channel(active, "[*] Both models already on", width,
                                   force_style=STYLES['hint'])
                else:
                    for ch in off:
                        ch.enabled = True
                        ch.status = "idle"
                        add_to_channel(ch, f"[*] {ch.label} back on", width,
                                       force_style=STYLES['diag_ok'])
            elif key == curses.KEY_F5:
                add_to_channel(active, compare_answers(width), width)
            refresh_model_header()
            chat_area_height = height - len(header_lines) - 1
            scroll_offset = max(0, len(chat_history) - chat_area_height)
            force_redraw = True
            continue

        if key == curses.KEY_UP:
            scroll_offset -= SCROLL_JUMP
        elif key == curses.KEY_DOWN:
            scroll_offset += SCROLL_JUMP
        elif key == curses.KEY_BACKSPACE or key == 127:
            current_input = current_input[:-1]
        elif key == 27 or key == curses.KEY_F3:  # Esc (kept for SSH convenience; calculator has no Esc)
            current_input = ""
            sym_active = False

        # --- F2: OPEN SPLASHSCREEN (controls + keyboard map) ---
        elif key == curses.KEY_F2:
            show_splash_system(stdscr)
            needs_redraw = True

        # --- F4: TOGGLE SHIFT (SYM) -- per README, one-press sticky ---
        elif key == curses.KEY_F4:
            sym_active = not sym_active
            force_redraw = True

        # --- F5: TOGGLE NUM/ALPHA -- per README ---
        elif key == curses.KEY_F5:
            input_mode = ALPHA if input_mode == NUM else NUM
            sym_active = False
            force_redraw = True

        # --- F6: RESTART (press twice to confirm) ---
        elif key == curses.KEY_F6:
            if restart_confirm_active:
                stop_viewfinder()
                os.execl(sys.executable, sys.executable, *sys.argv)
            else:
                restart_confirm_active = True
                chat_history.append([("[!] Press F6 again to RESTART", STYLES['diag_crit'])])
                chat_area_height = height - len(header_lines) - 1
                scroll_offset = max(0, len(chat_history) - chat_area_height)

        # --- EXECUTE (F1 or Enter): fire every enabled model in parallel ---
        elif key == curses.KEY_F1 or (key == curses.KEY_ENTER or key == 10):
            is_f1_press = (key == curses.KEY_F1)
            if not is_f1_press and not current_input:
                continue
            if any(channels[k].status == "working" for k in (GEMINI, OPENAI_CH)):
                continue
            if not any(channels[k].enabled for k in (GEMINI, OPENAI_CH)):
                parse_and_add_history("[!] No models on (SYM+F3)", width,
                                      force_style=STYLES['diag_crit'])
                current_input = ""
                continue

            path = "/tmp/capture.jpg"
            if is_f1_press:
                # The camera is exclusive: the viewfinder must let go first.
                if stop_viewfinder():
                    force_redraw = True
                for k in (GEMINI, OPENAI_CH):
                    channels[k].step = "CAPTURING"
                refresh_model_header()
                draw_screen(stdscr, current_input)
                cmd = ["rpicam-still", "-o", path, "-t", "100",
                       "--width", "2592", "--height", "1944",
                       "--exposure", "sport", "-q", "95", "-n", "--immediate"]
                result = subprocess.run(cmd, capture_output=True, text=True)
                if result.returncode != 0 or not os.path.exists(path):
                    tail = result.stderr.strip().split("\n")[-1][:30] if result.stderr else "unknown"
                    parse_and_add_history(f"Cam Fail: {tail}...", width,
                                          force_style=STYLES['diag_crit'])
                    scroll_offset = max(0, len(chat_history) - chat_area_height)
                    continue

            # One photo, one turn appended to each model's own conversation.
            dispatch_capture(is_f1_press, path, current_input, width)
            refresh_model_header()
            current_input = ""
            chat_area_height = height - len(header_lines) - 1
            scroll_offset = max(0, len(chat_history) - chat_area_height)

        # Character input through mode mapping
        elif key != -1 and key < 256:
            try:
                char = chr(key)
                processed = process_key_input(char)
                if processed:
                    current_input += processed
            except (ValueError, OverflowError):
                pass


if __name__ == "__main__":
    curses.wrapper(main)
