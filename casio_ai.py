import sys
import os
import subprocess
import re
import time
import threading
from google import genai
from PIL import Image
import curses
import textwrap
import cv2

# --- USER CONFIGURATION ---
API_KEY = "AIzaSyAfXnIPlVQX4WrI2QYQqYCIPxRKEalglqc"
MODEL_NAME = "gemini-3.1-pro-preview"

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
</system_instruction>"""

# Scrolling: 4 lines at a time
SCROLL_JUMP = 4
DIAGNOSTIC_UPDATE_INTERVAL = 0.5

# Throbber animation cadence is now fixed regardless of state. Redraw ticks
# every THROBBER_TICK_SEC seconds; the dots advance a frame every
# THROBBER_TICKS_PER_FRAME ticks. Net dots-per-second ~= 1 / (0.1 * 3).
THROBBER_TICK_SEC = 0.1          # redraw / stopwatch refresh at 10 Hz
THROBBER_TICKS_PER_FRAME = 3     # advance . . . every 3 ticks ~= 3.3 Hz

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
ALPHA_OVERRIDES = {
    'K': 'x',   # , key  -> x  (common math variable)
    'L': 'y',   # -> key -> y  (common math variable)
}
# --- END OF CONFIGURATION ---

STYLES = {}

# Chat session for memory
chat_session = None
try:
    client = genai.Client(api_key=API_KEY)
    chat_session = client.chats.create(model=MODEL_NAME)
except:
    pass

# --- GLOBAL STATE ---
header_lines = []
chat_history = []
scroll_offset = 0
response_holder = []
processing_step = ""
response_wait_start = 0.0
force_redraw = False
redraw_lock = threading.Lock()
restart_confirm_active = False

# Keyboard mode state
input_mode = NUM
sym_active = False

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
    STYLES['user_input'] = STYLES['normal'] | curses.A_UNDERLINE

    # Number Attributes (Dark Magenta + Italic)
    STYLES['number'] = STYLES['number'] | curses.A_ITALIC
    STYLES['bold_number'] = STYLES['number'] | curses.A_BOLD
    STYLES['italic_number'] = STYLES['number']

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
    segments = []
    regex_italic = re.compile(r'(\*[^\*]+\*)')
    parts_italic = [p for p in regex_italic.split(text) if p]
    for p_ital in parts_italic:
        if p_ital.startswith('*') and p_ital.endswith('*'):
            content = p_ital[1:-1]
            segments.extend(parse_inner_numbers(content, STYLES['italic'], is_bold))
        else:
            segments.extend(parse_inner_numbers(p_ital, base_style, is_bold))
    return segments


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
        if gap > 0 and (stripped[:gap].startswith('F') or
                        stripped[:gap] in ('Enter', 'Up/Down', 'Key', 'RSSI')):
            key_part = stripped[:gap]
            rest_part = stripped[gap:]
            stdscr.addstr(y, block_x, " " * leading_ws, STYLES['normal'])
            stdscr.addstr(y, block_x + leading_ws, key_part,
                          STYLES.get('splash_key', STYLES['normal']))
            stdscr.addstr(y, block_x + leading_ws + len(key_part),
                          rest_part[:max(0, w - block_x - leading_ws - len(key_part) - 1)],
                          STYLES['normal'])
            return

        # Inline-marker path: colors [[G]]...[[/G]], [[R]]...[[/R]] segments.
        marker_re = re.compile(r'\[\[(G|R)\]\](.*?)\[\[/\1\]\]')
        cursor_x = block_x
        pos = 0
        for m in marker_re.finditer(line):
            before = line[pos:m.start()]
            if before:
                stdscr.addstr(y, cursor_x, before[:max(0, w - cursor_x - 1)], STYLES['normal'])
                cursor_x += len(before)
            tag, content = m.group(1), m.group(2)
            style = STYLES['rssi_strong'] if tag == 'G' else STYLES['rssi_weak']
            stdscr.addstr(y, cursor_x, content[:max(0, w - cursor_x - 1)], style)
            cursor_x += len(content)
            pos = m.end()
        tail = line[pos:]
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
        "F2      Toggle this splashscreen",
        "F4      Toggle SHIFT  (SYM, 1-press)",
        "F5      Toggle NUM/ALPHA",
        "F6      Restart program  (press 2x)",
        "",
        "Enter   Send text message",
        "Esc     Clear input buffer",
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


# --- STATUS / UPLOAD STATE MACHINE --------------------------------------------

def format_status():
    """Build the status line text based on current processing_step and stopwatch."""
    step = processing_step
    if step == "CAPTURING":
        return "[*] Capturing"
    if step == "UPLOADING":
        return "[*] Uploading"
    if step == "UPLOADED":
        return "[*] Uploaded"
    if step == "WAITING":
        elapsed = max(0.0, time.time() - response_wait_start)
        if elapsed < 60.0:
            return f"[*] Waiting for response ({elapsed:.1f})"
        mins = int(elapsed // 60)
        secs = elapsed - (mins * 60)
        return f"[*] Waiting for response ({mins}min{secs:.1f})"
    if step == "SENT":
        return "[*] Sent"
    return "[*] ..."


def processing_task(is_f1, current_input=None):
    global response_holder, processing_step, chat_session, response_wait_start

    if is_f1:
        path = "/tmp/capture.jpg"

        processing_step = "CAPTURING"
        # OV5647 fixed focus at 7in. No AF motor -- instant capture.
        cmd = ["rpicam-still", "-o", path, "-t", "100",
               "--width", "2592", "--height", "1944",
               "--exposure", "sport", "-q", "95", "-n", "--immediate"]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0 or not os.path.exists(path):
            err_tail = result.stderr.strip().split("\n")[-1][:30] if result.stderr else "unknown"
            response_holder.append(f"Cam Fail: {err_tail}...")
            return

        processing_step = "UPLOADING"
        try:
            if chat_session is None:
                chat_session = client.chats.create(model=MODEL_NAME)

            # Upload separately from the message so state can transition cleanly
            # between UPLOADING and WAITING. Falls back to inline PIL image if
            # the Files API is unavailable for any reason.
            try:
                uploaded_file = client.files.upload(file=path)
                message_parts = [F1_PROMPT, uploaded_file]
            except Exception:
                uploaded_file = Image.open(path)
                message_parts = [F1_PROMPT, uploaded_file]

            processing_step = "UPLOADED"
            time.sleep(0.5)  # hold "Uploaded" for exactly 0.5s per spec

            response_wait_start = time.time()
            processing_step = "WAITING"
            response = chat_session.send_message(message_parts)

            response_holder.append(response.text)

        except Exception as e:
            response_holder.append(f"Net Error: {e}")

    else:
        try:
            if chat_session is None:
                chat_session = client.chats.create(model=MODEL_NAME)

            response_wait_start = time.time()
            processing_step = "WAITING"
            response = chat_session.send_message(current_input)

            processing_step = "SENT"
            time.sleep(0.5)

            response_holder.append(response.text)
        except Exception as e:
            response_holder.append(f"Net Error: {e}")


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
    global scroll_offset, response_holder, force_redraw, chat_session, processing_step, restart_confirm_active
    global input_mode, sym_active, response_wait_start

    initialize_theme(stdscr)
    stdscr.keypad(True)
    wait_for_network(stdscr)

    # Startup splash: FUNCTIONS  (F4 dismiss / F5 -> keyboard map)
    show_splash_system(stdscr)

    stdscr.timeout(50)
    height, width = stdscr.getmaxyx()
    current_input = ""
    needs_redraw = True

    # Initialize Chat Session (fallback if module-level init failed)
    try:
        if not chat_session:
            client = genai.Client(api_key=API_KEY)
            chat_session = client.chats.create(model=MODEL_NAME)
    except Exception:
        pass

    header_lines.append(get_diagnostics_styled())
    header_lines.append([("", STYLES['normal'])])

    # Scrollable hint line printed into chat history after dismissing splash.
    add_splash_hint_line()

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
            continue
        needs_redraw = True

        # --- KEY HANDLING ---
        # Cancel restart confirmation on any non-F6 key
        if key != curses.KEY_F6 and restart_confirm_active:
            restart_confirm_active = False
            chat_history.pop()
            needs_redraw = True

        if key == curses.KEY_UP:
            scroll_offset -= SCROLL_JUMP
        elif key == curses.KEY_DOWN:
            scroll_offset += SCROLL_JUMP
        elif key == curses.KEY_BACKSPACE or key == 127:
            current_input = current_input[:-1]
        elif key == 27:  # Esc (kept for SSH convenience; calculator has no Esc)
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
                os.execl(sys.executable, sys.executable, *sys.argv)
            else:
                restart_confirm_active = True
                chat_history.append([("[!] Press F6 again to RESTART", STYLES['diag_crit'])])
                chat_area_height = height - len(header_lines) - 1
                scroll_offset = max(0, len(chat_history) - chat_area_height)

        # --- EXECUTE (F1 or Enter) ---
        elif key == curses.KEY_F1 or (key == curses.KEY_ENTER or key == 10):
            is_f1_press = (key == curses.KEY_F1)
            if not is_f1_press and not current_input:
                continue

            if chat_history:
                parse_and_add_history(" ", width)

            if not is_f1_press:
                parse_and_add_history(f"> {current_input}", width, force_style=STYLES['user_input'])

            parse_and_add_history("[*] ...", width)
            chat_area_height = height - len(header_lines) - 1
            scroll_offset = max(0, len(chat_history) - chat_area_height)
            draw_screen(stdscr, current_input)

            response_holder.clear()
            processing_step = "..."
            response_wait_start = 0.0  # reset stopwatch per spec

            worker = threading.Thread(target=processing_task, args=(is_f1_press, current_input))
            worker.start()

            # Decoupled throbber: redraw every tick (so the stopwatch updates
            # smoothly during WAITING), but advance the . . . frame only every
            # THROBBER_TICKS_PER_FRAME ticks -- giving a constant dot cadence
            # regardless of which state we're in.
            animation_frames = ['   ', '.  ', '.. ', '...']
            tick_count = 0
            frame_index = 0
            while worker.is_alive():
                if tick_count % THROBBER_TICKS_PER_FRAME == 0:
                    frame_index = (frame_index + 1) % len(animation_frames)
                status_text = format_status()
                chat_history[-1] = [(status_text + animation_frames[frame_index], STYLES['status_text'])]
                draw_screen(stdscr, current_input)
                time.sleep(THROBBER_TICK_SEC)
                tick_count += 1

            chat_history.pop()

            if response_holder:
                parse_and_add_history(f"Model: {response_holder[0]}", width)
            else:
                parse_and_add_history("Error: No response from thread.", width)

            current_input = ""
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
