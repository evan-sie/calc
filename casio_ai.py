import sys
import os
import subprocess
import re
import time
import threading
import signal
import socket
import base64
import json
from PIL import Image
try:
    from openai import OpenAI
except ImportError:  # Show an actionable error in the UI if setup is incomplete.
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

OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL_NAME = "gpt-6.1-sol"
OPENAI_REASONING_EFFORT = "high"

# One automatic retry before the deck asks what to do about a failed request.
AUTO_RETRY_LIMIT = 1

# The OV5647 can wedge and take ~10s to reset itself, so the capture runs off
# the main loop and is abandoned outright past this.
CAPTURE_TIMEOUT_SEC = 30

# Stop waiting on an API call rather than hanging on a half-open socket when
# the hotspot drops. The SDK defaults to minutes.
REQUEST_TIMEOUT_SEC = 120
# Per-request counters only: no prompts, images, answers, keys, or summary text.
REQUEST_STATUS_LOG = "/tmp/casio_ai_request_status.json"

# Longest edge for the uploaded photo. The full 2592x1944 frame is ~950KB,
# which is painful on a flaky 2.4GHz link. 2048 keeps roughly 240 px/inch on a
# letter page -- 12pt text lands around 40px tall and subscripts around 20px,
# which OCR reads comfortably. Resolution matters far more than JPEG quality
# for small glyphs, so the dimension is deliberately generous and the quality
# only moderately reduced. Lower these only against a real worksheet.
UPLOAD_MAX_DIM = 2048
UPLOAD_QUALITY = 88

# Viewfinder (SYM+F1). Capture stops it -- the camera is exclusive.
VF_WIDTH, VF_HEIGHT, VF_FPS = 320, 180, 15
VF_GEOMETRY = "396x224+0+0"
VF_CAMERA_RELEASE_SEC = 0.3
# If no mpv surface exists by now the camera never delivered a frame.
VF_WATCHDOG_SEC = 12
NOTES_DIR = os.path.expanduser("~/notes")

F1_PROMPT = """<system_instruction>
<role_and_persona>
You are an expert Mechanical Engineering Tutor operating on a low-powered, legacy AI cyberdeck running off a Raspberry Pi Zero. Your sole purpose is to act as a 100% accurate tutor, answering engineering questions derived strictly from images of worksheets provided as input.
</role_and_persona>

<critical_constraints>
1. **NO LATEX / NO UNICODE MATH:** Your display hardware cannot render LaTeX or special math characters. All variables, symbols, and formulas MUST be typed out phonetically or in plain text (e.g., use alpha, theta, pi/4, beta, sum, integral, square root, x_squared, deg).
2. **NEVER ASK, ALWAYS ANSWER:** You CANNOT ask follow-up questions or request clarification of any kind. Output your final response immediately, using only the information you already have. You DO retain this conversation, including the course context loaded at the start of the session, and you MUST use it -- but having that memory NEVER licenses asking a question. If something required is missing or unreadable, use CONDITION B below instead of asking for it.
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
    '.': ('.', '='),       # . key -- SYM gives '='; ',' has its own key (K)

    # Scientific-row keys: same output in NUM and SYM mode per user spec.
    'B': ('log(',  'log('),   # log key
    'C': ('ln(',   'ln('),    # ln key
    'D': ('sin(',  'sin('),   # sin key
    'E': ('cos(',  'cos('),   # cos key
    'F': ('tan(',  'tan('),   # tan key
    'I': ('(',     '('),      # ( key
    'J': (')',     ')'),      # ) key
    'K': (',',     ','),      # , key
    'L': ('l',     '^'),      # SYM gives '^' for powers; NUM keeps the
                              # plain pass-through gint already sent
}

# ALPHA-mode-only overrides. In ABC mode these keys emit the mapped token
# instead of gint's raw lowercase letter. Everything NOT in this dict falls
# through to plain pass-through, preserving normal text entry.
ALPHA_OVERRIDES = {}
# --- END OF CONFIGURATION ---

STYLES = {}

# --- GLOBAL STATE ---
header_lines = []


class ChatState:
    """One GPT conversation shared by course notes, photos, and follow-ups."""

    def __init__(self):
        self.label = "GPT"
        self.history = []
        self.status = "idle"      # idle | working | done | error
        self.step = "..."
        self.wait_start = 0.0
        self.answer = None
        self.error = None
        self.thread = None
        self.session = None       # OpenAI client, created on the first request
        self.stream = None
        self.completed_response = None
        self.cancel_event = threading.Event()
        self.cancel_thread = None
        self.session_needs_reset = False
        self.request_lock = threading.Lock()
        self.prev_response_id = None   # OpenAI conversation chaining
        self.retries = 0
        self.pending = None       # (is_f1, path, text) for a retry
        self.awaiting_choice = False
        self.status_index = None
        self.last_usage = None    # token usage of the last call, if reported
        self.thought = ""         # provider's streamed reasoning summary
        self.thought_label = ""   # newest **header** from it, for the status line
        self.thought_part = None  # (reasoning item id, summary index)
        self.live_len = 0         # lines the in-flight block currently occupies

    @property
    def busy(self):
        return self.thread is not None and self.thread.is_alive()


chat = ChatState()
chat_history = chat.history
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
viewfinder_started = 0.0
viewfinder_checked = False

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
        segments.append((temp_text, temp_style))

        # RAM
        with open('/proc/meminfo', 'r') as f:
            lines = f.readlines()
        mem_total_kb, mem_avail_kb = int(lines[0].split()[1]), int(lines[2].split()[1])
        mem_used_mb = (mem_total_kb - mem_avail_kb) / 1024
        mem_total_mb = mem_total_kb / 1024
        usage_percent = (mem_used_mb / mem_total_mb) * 100
        ram_text = f"{int(mem_used_mb)}/{int(mem_total_mb)}"
        ram_style = ok if usage_percent < 50 else (warn if usage_percent < 75 else crit)
        segments.extend([(" ", normal), (ram_text, ram_style)])

        # WiFi signal bar + dBm (unchanged behavior from before)
        try:
            ssid = subprocess.run(["iwgetid", "-r"], capture_output=True, text=True, check=True).stdout.strip()
            link = subprocess.run(["iw", "dev", "wlan0", "link"], capture_output=True, text=True, check=True).stdout
            match = re.search(r"signal: (-?\d+)", link)
            if ssid and match:
                signal_dbm = int(match.group(1))
                # Colour the dBm rather than spending columns on a bar.
                if signal_dbm >= -65:
                    dbm_style = ok
                elif signal_dbm >= -75:
                    dbm_style = warn
                else:
                    dbm_style = crit
                segments.extend([(f" {ssid} ", normal), (str(signal_dbm), dbm_style)])
            else:
                segments.extend([(" ", normal), ("NOWIFI", crit)])
        except (subprocess.CalledProcessError, FileNotFoundError):
            segments.extend([(" ", normal), ("NOWIFI", crit)])

        # GPT is the only backend. Keep its health visible beside the clock.
        if chat.awaiting_choice or chat.status == "error":
            model_style = crit
        elif chat.status == "working":
            model_style = warn
        else:
            model_style = ok
        segments.extend([(" ", normal), ("GPT", model_style | curses.A_BOLD)])

        # RTC in military time (HH:MM local).
        segments.extend([(" ", normal), (time.strftime("%H:%M"), normal | curses.A_BOLD)])

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
                        stripped[:gap] in ('Enter', 'Up/Down', 'Key', 'RSSI', 'IP')):
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


def get_wlan_ip():
    """Current wlan0 address. The deck moves between networks, so the splash
    has to report the live one rather than anything baked in."""
    try:
        out = subprocess.run(["ip", "-4", "addr", "show", "wlan0"],
                             capture_output=True, text=True, timeout=2).stdout
        match = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out)
        return match.group(1) if match else "no address"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def get_startup_splash_lines():
    return [
        "F1      Capture & analyze image",
        "SYM+F1  Toggle camera viewfinder",
        "F2      Toggle this splashscreen",
        "F4      Toggle SHIFT  (SYM, 1-press)",
        "F5      Toggle NUM/ALPHA",
        "F6      Restart program  (press 2x)",
        "",
        "Enter   Send text message",
        "F3      Clear input / stop request",
        "Up/Down Scroll chat history",
        "",
        "RSSI    [[[G]]-30[[/G]], [[R]]-90[[/R]]] dBm",
        f"IP      {get_wlan_ip()}",
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
    """Load the selected notes into the same GPT conversation used for photos."""
    if chat_is_busy():
        return False
    parse_and_add_history(f"[*] Loading {class_name}", width,
                          force_style=STYLES['hint'])
    chat.pending = (False, None, build_class_context_message(class_name, md_content))
    chat.retries = 0
    launch_request(chat, width)
    return True


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
    global viewfinder_process, viewfinder_started, viewfinder_checked

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
        viewfinder_started = time.time()
        viewfinder_checked = False
    except (OSError, subprocess.SubprocessError):
        viewfinder_process = None
        _swaymsg('[app_id="foot"]', "fullscreen", "enable")
        return False

    return True


def check_viewfinder_alive(width):
    """A camera that never delivers a frame leaves mpv running with no surface:
    RAM climbing, nothing on screen, no error anywhere. Catch that and say so."""
    global viewfinder_checked

    if viewfinder_process is None or viewfinder_checked:
        return False
    if time.time() - viewfinder_started < VF_WATCHDOG_SEC:
        return False

    viewfinder_checked = True
    try:
        out = subprocess.run(["swaymsg", "-t", "get_tree"],
                             capture_output=True, text=True, timeout=3)
        if out.returncode != 0:
            return False
        # Parse rather than grep: swaymsg's whitespace is not a contract.
        found = []

        def walk(node):
            if node.get("app_id") == "mpv":
                found.append(node)
            for child in node.get("nodes", []) + node.get("floating_nodes", []):
                walk(child)

        walk(json.loads(out.stdout))
        visible = bool(found)
    except (OSError, subprocess.SubprocessError, ValueError):
        return False

    if visible:
        return False

    stop_viewfinder()
    parse_and_add_history("[!] Viewfinder: no frames (camera)", width,
                          force_style=STYLES['diag_crit'])
    return True


def toggle_viewfinder(width):
    """SYM+F1 handler. The same key closes it; so does an F1 capture."""
    if viewfinder_process is None and pending_capture.thread is not None:
        parse_and_add_history("[!] Busy capturing", width,
                              force_style=STYLES['diag_warn'])
        return
    if viewfinder_process is None:
        if start_viewfinder():
            parse_and_add_history("[*] Viewfinder ON  (SYM+F1 to close)", width)
        else:
            parse_and_add_history("[!] Viewfinder failed to start", width)
    else:
        stop_viewfinder()
        parse_and_add_history("[*] Viewfinder OFF", width)


# --- GPT CONVERSATION -------------------------------------------------------

def format_request_status(ch):
    """Status line text for the current GPT request."""
    step = ch.step
    if step == "CANCELLING":
        return "[*] Stopping request"
    if step == "CAPTURING":
        return "[*] Capturing"
    if step == "UPLOADING":
        return "[*] Uploading"
    if step == "UPLOADED":
        return "[*] Uploaded"
    if step == "WAITING":
        elapsed = max(0.0, time.time() - ch.wait_start)
        if elapsed < 60.0:
            clock = f"{elapsed:.1f}"
        else:
            mins = int(elapsed // 60)
            clock = f"{mins}min{elapsed - (mins * 60):.1f}"
        # Say what the model is actually doing once it starts reporting.
        if ch.thought_label:
            return f"[*] {ch.thought_label} ({clock})"
        return f"[*] {ch.label} thinking ({clock})"
    return "[*] ..."


# --- OPENAI REQUESTS ---------------------------------------------------------

_THOUGHT_HEADER_RE = re.compile(r"\*\*(.+?)\*\*")
_THOUGHT_MARKDOWN_RE = re.compile(r"^#{1,6}[ \t]+(.+)$", re.MULTILINE)


def note_thought(ch, text, part=None, replace=False):
    """Use the provider's summary regardless of its Markdown formatting.

    Prefer a title; otherwise use a short excerpt of the latest paragraph on
    the same status line. Separate summary parts must not inherit old titles.
    A done event replaces its deltas rather than duplicating them.
    """
    if not text:
        return
    if part is not None and part != ch.thought_part:
        ch.thought = ""
        ch.thought_label = ""
        ch.thought_part = part
    ch.thought = text if replace else ch.thought + text
    headers = _THOUGHT_HEADER_RE.findall(ch.thought)
    if headers:
        label = headers[-1]
    else:
        headings = _THOUGHT_MARKDOWN_RE.findall(ch.thought)
        if headings:
            label = headings[-1]
        else:
            paragraph = re.split(r"\n\s*\n", ch.thought.strip())[-1]
            # Wait for a split bold title to close instead of showing its '*'.
            if paragraph.startswith("**") and "**" not in paragraph[2:]:
                return
            label = re.sub(r"^[\s#>*_-]+", "", paragraph)
    ch.thought_label = " ".join(label.replace("**", "").split())[:32]


def handle_summary_event(ch, event):
    """Consume deltas and authoritative text/part snapshots from Responses."""
    kind = getattr(event, "type", "")
    part = (getattr(event, "item_id", ""), getattr(event, "summary_index", 0))
    if kind == "response.reasoning_summary_text.delta":
        text = getattr(event, "delta", "") or ""
        note_thought(ch, text, part=part)
    elif kind == "response.reasoning_summary_text.done":
        text = getattr(event, "text", "") or ""
        note_thought(ch, text, part=part, replace=True)
    elif kind in ("response.reasoning_summary_part.added",
                  "response.reasoning_summary_part.done"):
        text = getattr(getattr(event, "part", None), "text", "") or ""
        note_thought(ch, text, part=part, replace=True)
    elif kind in ("response.output_item.added", "response.output_item.done"):
        item = getattr(event, "item", None)
        if getattr(item, "type", "") != "reasoning":
            return False
        for index, summary in enumerate(getattr(item, "summary", None) or []):
            text = getattr(summary, "text", "") or ""
            note_thought(ch, text, part=(getattr(item, "id", ""), index), replace=True)
        return bool(ch.thought)
    else:
        return False
    return bool(text)


def write_request_status(ch, counts, started, completed):
    """Record whether a real request supplied summaries without logging content."""
    usage = getattr(completed, "usage", None)
    details = getattr(usage, "output_tokens_details", None)
    record = {
        "model": OPENAI_MODEL_NAME,
        "effort": OPENAI_REASONING_EFFORT,
        "elapsed_seconds": round(time.time() - started, 1),
        "completed": completed is not None,
        "cancel_requested": ch.cancel_event.is_set(),
        "event_counts": counts,
        "summary_received": bool(ch.thought),
        "summary_label_available": bool(ch.thought_label),
        "reasoning_tokens": getattr(details, "reasoning_tokens", None),
    }
    try:
        with open(REQUEST_STATUS_LOG, "w") as fh:
            json.dump(record, fh)
    except OSError:
        pass


class RequestCancelled(Exception):
    """The user stopped this request; it must not be retried or enter history."""


def _openai_request(ch, is_f1, path, text):
    if ch.cancel_event.is_set():
        raise RequestCancelled()
    if OpenAI is None:
        raise RuntimeError("openai package not installed")
    if not OPENAI_API_KEY:
        raise RuntimeError("No OpenAI key. Set OPENAI_API_KEY in %s" % ENV_FILE)
    if ch.session is None:
        # max_retries=0: the deck does its own retry and tells you about it.
        ch.session = OpenAI(api_key=OPENAI_API_KEY,
                            timeout=REQUEST_TIMEOUT_SEC, max_retries=0)

    if is_f1:
        ch.step = "UPLOADING"
        with open(path, "rb") as fh:
            b64 = base64.b64encode(fh.read()).decode("ascii")
        content = [
            {"type": "input_image", "image_url": "data:image/jpeg;base64," + b64},
        ]
        ch.step = "UPLOADED"
        time.sleep(0.5)
    else:
        content = [{"type": "input_text", "text": text}]

    if ch.cancel_event.is_set():
        raise RequestCancelled()
    ch.wait_start = time.time()
    ch.step = "WAITING"
    kwargs = {
        "model": OPENAI_MODEL_NAME,
        # Request the provider's reasoning summaries explicitly. They are
        # separate from answer text and may be absent even when reasoning
        # tokens were used; summary formatting is not guaranteed.
        "reasoning": {"effort": OPENAI_REASONING_EFFORT, "summary": "detailed"},
        "input": [{"role": "user", "content": content}],
        "stream": True,
        # Re-sent every call: the Responses API does not inherit instructions
        # through previous_response_id.
        "instructions": F1_PROMPT,
    }
    # Chain turns server-side so the photo is not re-uploaded every message.
    if ch.prev_response_id:
        kwargs["previous_response_id"] = ch.prev_response_id

    answer = []
    completed = None
    counts = {}
    started = time.time()
    stream = None
    try:
        stream = ch.session.responses.create(**kwargs)
        ch.stream = stream
        if ch.cancel_event.is_set():
            raise RequestCancelled()
        for event in stream:
            if ch.cancel_event.is_set():
                raise RequestCancelled()
            etype = getattr(event, "type", "") or ""
            counts[etype] = counts.get(etype, 0) + 1
            if (etype.startswith("response.reasoning_summary_")
                    or etype in ("response.output_item.added", "response.output_item.done")):
                handle_summary_event(ch, event)
            elif etype == "response.output_text.delta":
                answer.append(getattr(event, "delta", "") or "")
            elif etype == "response.completed":
                completed = event.response
            elif etype in ("response.failed", "response.incomplete", "error"):
                raise RuntimeError("GPT response failed or was incomplete; retry the request")
    finally:
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        ch.stream = None
        write_request_status(ch, counts, started, completed)

    if ch.cancel_event.is_set():
        raise RequestCancelled()
    text = "".join(answer)
    if completed is None or not text.strip():
        raise RuntimeError("GPT stream ended without a complete answer; retry the request")
    # The worker commits this under request_lock so Stop and completion cannot
    # race to advance the conversation in opposite ways.
    ch.completed_response = completed
    return text


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


def _run_request(ch):
    """Worker body. Never touches curses -- only conversation state."""
    is_f1, path, text = ch.pending
    try:
        answer = _openai_request(ch, is_f1, path, text)
    except Exception as e:
        with ch.request_lock:
            if ch.cancel_event.is_set():
                ch.answer = None
                ch.status = "cancelled"
            else:
                ch.error = _short_error(e)
                ch.status = "error"
    else:
        with ch.request_lock:
            if ch.cancel_event.is_set():
                ch.answer = None
                ch.status = "cancelled"
            else:
                ch.prev_response_id = ch.completed_response.id
                ch.last_usage = getattr(ch.completed_response, "usage", None)
                ch.answer = answer
                ch.status = "done"


def cancel_request():
    """Stop the active synchronous stream without blocking the UI thread."""
    with chat.request_lock:
        if chat.status != "working":
            return False
        if chat.cancel_event.is_set():
            return True
        chat.cancel_event.set()
        chat.step = "CANCELLING"
        if chat.thread is not None:
            stream, session = chat.stream, chat.session
            # If the upload has not returned a stream yet, close its HTTP
            # client. A fresh client is needed for the next request.
            if stream is None:
                chat.session_needs_reset = True

            def close_connection():
                try:
                    if stream is not None:
                        # close() alone can leave another thread blocked in
                        # recv until the next chunk. Shutdown wakes that read
                        # immediately, then normal SDK cleanup releases it.
                        response = getattr(stream, "response", None)
                        transport = getattr(response, "extensions", {}).get("network_stream")
                        if transport is not None:
                            try:
                                sock = transport.get_extra_info("socket")
                                if sock is not None:
                                    sock.shutdown(socket.SHUT_RDWR)
                            except (AttributeError, OSError):
                                pass
                        stream.close()
                    elif session is not None:
                        session.close()
                except Exception:
                    pass

            chat.cancel_thread = threading.Thread(target=close_connection, daemon=True)
            chat.cancel_thread.start()
    return True


def render_live_block(ch, width):
    """One line: the newest reasoning header where the status text goes, with
    the dots still animating. Headers replace each other in place rather than
    accumulating, and nothing survives the request."""
    dots = THROBBER_FRAMES[throbber_frame]
    status = format_request_status(ch)
    # Leave space for the animated dots even on a narrow terminal.
    status = status[:max(0, width - 1 - len(dots))]
    return [[(status + dots, STYLES['status_text'])]]


def replace_live_block(ch, new_lines):
    """Swap the in-flight block for something else, tracking its length so a
    later swap cannot eat messages appended after it."""
    if ch.status_index is None:
        return
    ch.history[ch.status_index:ch.status_index + ch.live_len] = new_lines
    ch.live_len = len(new_lines)


def launch_request(ch, width):
    """Start (or restart) the GPT request on its own thread."""
    ch.status = "working"
    ch.step = "UPLOADING" if ch.pending[0] else "WAITING"
    ch.answer = None
    ch.error = None
    ch.completed_response = None
    ch.cancel_event.clear()
    ch.cancel_thread = None
    ch.thought = ""
    ch.thought_label = ""
    ch.thought_part = None
    ch.awaiting_choice = False
    ch.status_index = len(ch.history)
    ch.live_len = 0
    replace_live_block(ch, [[("[*] ...", STYLES['status_text'])]])
    ch.thread = threading.Thread(target=_run_request, args=(ch,), daemon=True)
    ch.thread.start()


class PendingCapture:
    """A photo captured off the UI thread before sending it to GPT."""

    def __init__(self):
        self.thread = None
        self.path = None
        self.error = None


pending_capture = PendingCapture()


def chat_is_busy():
    # A finished worker must be reaped before another request can replace its
    # status block, even if a key arrives before the next idle UI tick.
    return (chat.status == "working" or chat.thread is not None or chat.cancel_thread is not None
            or pending_capture.thread is not None)


def prepare_upload(path):
    """Shrink the frame before it goes over the air.

    The full 2592x1944 capture is ~950KB, which on a weak hotspot is the
    difference between a request that completes and one that stalls. Falls back
    to the original if PIL cannot do it."""
    try:
        out = "/tmp/capture_upload.jpg"
        with Image.open(path) as im:
            im = im.convert("RGB")
            # thumbnail() only ever shrinks, so a small frame is left alone.
            im.thumbnail((UPLOAD_MAX_DIM, UPLOAD_MAX_DIM), Image.LANCZOS)
            im.save(out, "JPEG", quality=UPLOAD_QUALITY, optimize=True)
        # If it did not actually get smaller, send the original untouched.
        if os.path.getsize(out) >= os.path.getsize(path):
            return path
        return out
    except Exception:
        return path


def _run_capture(cap):
    """Worker body: release the camera, shoot, shrink. Never touches curses."""
    global force_redraw

    if stop_viewfinder():
        force_redraw = True

    path = "/tmp/capture.jpg"
    cmd = ["rpicam-still", "-o", path, "-t", "100",
           "--width", "2592", "--height", "1944",
           "--exposure", "sport", "-q", "95", "-n", "--immediate"]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                timeout=CAPTURE_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        # A wedged OV5647 never returns; abandoning it keeps the deck usable.
        subprocess.run(["pkill", "-x", "rpicam-still"], capture_output=True)
        cap.error = "camera timeout (%ds)" % CAPTURE_TIMEOUT_SEC
        return
    except OSError as e:
        cap.error = str(e)[:40]
        return

    if result.returncode != 0 or not os.path.exists(path):
        tail = (result.stderr or "").strip().split("\n")[-1][:40]
        cap.error = tail or "unknown"
        return

    cap.path = prepare_upload(path)


def start_capture(width):
    """Capture on a worker so a stalled camera cannot freeze the UI."""
    if chat_is_busy():
        return False
    cap = pending_capture
    cap.error = None
    cap.path = None
    chat.cancel_event.clear()
    chat.awaiting_choice = False
    if chat.history:
        parse_and_add_history(" ", width)
    chat.status = "working"
    chat.step = "CAPTURING"
    chat.thread = None  # The API worker starts after the frame is ready.
    chat.status_index = len(chat.history)
    chat.live_len = 0
    replace_live_block(chat, [[("[*] Capturing", STYLES['status_text'])]])
    cap.thread = threading.Thread(target=_run_capture, args=(cap,), daemon=True)
    cap.thread.start()
    return True


def tick_capture(width):
    """Pass a finished frame to GPT, or report the camera error."""
    cap = pending_capture
    if cap.thread is None or cap.thread.is_alive():
        return False
    cap.thread = None
    replace_live_block(chat, [])
    chat.status_index = None
    chat.live_len = 0
    chat.status = "idle"
    if chat.cancel_event.is_set():
        parse_and_add_history("[*] Capture stopped; photo not sent.", width,
                              force_style=STYLES['hint'])
    elif cap.error:
        parse_and_add_history(f"Cam Fail: {cap.error}", width,
                              force_style=STYLES['diag_crit'])
    else:
        chat.pending = (True, cap.path, "")
        chat.retries = 0
        launch_request(chat, width)
    return True


def send_message(text, width):
    """Append a follow-up to the existing GPT conversation."""
    if chat_is_busy():
        return False
    chat.awaiting_choice = False
    if chat.history:
        parse_and_add_history(" ", width)
    parse_and_add_history(f"> {text}", width, force_style=STYLES['user_input'])
    chat.pending = (False, None, text)
    chat.retries = 0
    launch_request(chat, width)
    return True


def tick_request(width):
    """Animate and reap workers from the main loop; curses stays on one thread."""
    global throbber_frame, throbber_tick
    dirty = tick_capture(width)
    if check_viewfinder_alive(width):
        dirty = True
    throbber_tick += 1
    if throbber_tick % THROBBER_TICKS_PER_FRAME == 0:
        throbber_frame = (throbber_frame + 1) % len(THROBBER_FRAMES)

    if chat.status == "working" and chat.status_index is not None:
        replace_live_block(chat, render_live_block(chat, width))
        if throbber_tick % THROBBER_TICKS_PER_FRAME == 0:
            dirty = True

    closing = chat.cancel_thread is not None and chat.cancel_thread.is_alive()
    if (chat.thread is not None and not chat.busy and not closing
            and chat.status in ("done", "error", "cancelled")):
        dirty = True
        replace_live_block(chat, [])
        chat.status_index = None
        chat.live_len = 0
        chat.thread = None
        chat.cancel_thread = None
        if chat.session_needs_reset:
            chat.session = None
            chat.session_needs_reset = False
        if chat.status == "done":
            parse_and_add_history(f"GPT: {chat.answer}", width)
            chat.status = "idle"
        elif chat.status == "cancelled":
            chat.answer = None
            chat.awaiting_choice = False
            parse_and_add_history("[*] Request stopped.", width,
                                  force_style=STYLES['hint'])
            chat.status = "idle"
        else:
            _handle_request_error(width)
    if dirty:
        refresh_model_header()
    return dirty


def _handle_request_error(width):
    """Retry once automatically, then let the user retry or dismiss."""
    parse_and_add_history(f"[!] GPT failed: {chat.error}", width,
                          force_style=STYLES['diag_crit'])
    if chat.retries < AUTO_RETRY_LIMIT:
        chat.retries += 1
        parse_and_add_history(f"[*] Retrying GPT ({chat.retries}/{AUTO_RETRY_LIMIT})",
                              width, force_style=STYLES['diag_warn'])
        launch_request(chat, width)
        return
    chat.status = "error"
    chat.awaiting_choice = True
    parse_and_add_history("[?] Enter=retry  F3=dismiss", width,
                          force_style=STYLES['diag_ok'])


def handle_retry_choice(key, width):
    """Dismiss leaves GPT usable and retains the conversation for the next turn."""
    if not chat.awaiting_choice or key not in (curses.KEY_ENTER, 10, curses.KEY_F3):
        return False
    chat.awaiting_choice = False
    if key == curses.KEY_F3:
        parse_and_add_history("[*] Request dismissed. Ready for a new question.",
                              width, force_style=STYLES['hint'])
    else:
        chat.retries = 0
        parse_and_add_history("[*] Retrying GPT", width,
                              force_style=STYLES['diag_warn'])
        launch_request(chat, width)
    return True


def refresh_model_header():
    """Refresh GPT's health in the single toolbar row."""
    with redraw_lock:
        if header_lines:
            header_lines[0] = get_diagnostics_styled()


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
            if tick_request(width):
                chat_area_height = height - len(header_lines) - 1
                max_scroll = max(0, len(chat_history) - chat_area_height)
                if scroll_offset >= max_scroll - SCROLL_JUMP:
                    scroll_offset = max_scroll
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

        # F3 stops an active request; when idle it still clears typed input.
        if key == curses.KEY_F3 and cancel_request():
            sym_active = False
            refresh_model_header()
            force_redraw = True
            continue

        # --- RETRY / DISMISS PROMPT ---
        if handle_retry_choice(key, width):
            refresh_model_header()
            chat_area_height = height - len(header_lines) - 1
            scroll_offset = max(0, len(chat_history) - chat_area_height)
            force_redraw = True
            continue

        # SYM+F1 keeps its viewfinder binding. The old model-switching and
        # comparison shortcuts have no action in the single-model interface.
        if sym_active and key in (curses.KEY_F1, curses.KEY_F2,
                                  curses.KEY_F3, curses.KEY_F5):
            sym_active = False
            if key == curses.KEY_F1:
                toggle_viewfinder(width)
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

        # --- EXECUTE (F1 or Enter): one persistent GPT conversation ---
        elif key == curses.KEY_F1 or key in (curses.KEY_ENTER, 10):
            is_f1_press = (key == curses.KEY_F1)
            if not is_f1_press and not current_input:
                continue
            if chat_is_busy():
                parse_and_add_history("[!] GPT busy", width,
                                      force_style=STYLES['diag_warn'])
                chat_area_height = height - len(header_lines) - 1
                scroll_offset = max(0, len(chat_history) - chat_area_height)
                continue
            if is_f1_press:
                start_capture(width)
            else:
                send_message(current_input, width)
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


CRASH_LOG = "/tmp/casio_ai_crash.log"

if __name__ == "__main__":
    try:
        curses.wrapper(main)
    except KeyboardInterrupt:
        pass
    except Exception:
        # foot exits with the process, taking the traceback with it. Keep a
        # copy so a crash can actually be diagnosed afterwards.
        import traceback
        with open(CRASH_LOG, "a") as fh:
            fh.write("\n=== %s ===\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
            traceback.print_exc(file=fh)
        traceback.print_exc()
        raise
