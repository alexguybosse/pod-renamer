#!/usr/bin/env python3
"""
POD Renamer
-----------
Goes through the scanned Proof of Delivery (POD) files in a folder, reads each
page, finds the invoice number and renames the file to it.

Each file is shown on screen with the number it found highlighted, so the
person can check it and press Enter. If the guess is wrong they click the right
number on the page (or type it).

The tool learns what invoice numbers look like (e.g. "IN" + 6 digits) from the
ones that get confirmed, so its guesses get better over time.

Needs: Python 3.9+, pymupdf, pillow, and for scanned images either
  - winocr (uses the OCR built into Windows 10/11), or
  - pytesseract + Tesseract-OCR installed.
"""

import csv
import datetime
import io
import itertools
import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import traceback

APP = "POD Renamer"
MADE_FOR = "ADE Agencies"  # shown in the window title, after the version
VERSION = "1.1.3"         # raise this (and version.txt on GitHub) to send an update to every PC
END_MARK = "# ---- end of POD Renamer ----"   # must stay the very last line of this file
UPDATE_BASE = "https://raw.githubusercontent.com/alexguybosse/pod-renamer/main/"
REQUIRES = ["pymupdf", "PIL"]  # an update that needs more than this waits until SETUP is run again
EXTS = {".pdf", ".jpg", ".jpeg", ".png", ".tif", ".tiff", ".bmp"}
RENDER_DPI = 200          # page render resolution used for OCR and preview
MIN_TEXT_CHARS = 25       # below this, a PDF's own text layer is treated as empty


# --------------------------------------------------------------------------
# Settings and log
# --------------------------------------------------------------------------

def config_dir():
    base = os.environ.get("APPDATA") or os.path.join(os.path.expanduser("~"), ".config")
    d = os.path.join(base, APP)
    os.makedirs(d, exist_ok=True)
    return d


def folder_key(folder):
    return os.path.normcase(os.path.abspath(folder))


class Settings:
    DEFAULTS = {
        "folder": "",
        "prefix": "",        # text added before the invoice number in the filename
        "suffix": "",        # text added after it, e.g. " POD"
        "shapes": {},        # learned invoice-number patterns -> times confirmed
        "labels": {},        # learned labels printed next to the number, e.g. "our ref"
        "handled": {},       # folder -> list of lowercase filenames already done
        "ignore_before": {}, # folder -> timestamp; older files are ignored
        "fingerprints": {},  # folder -> ["size:mtime", ...] of files already done (survives renames)
        "tidy_declined": [], # "(2)"-style duplicates she said not to combine
    }

    def __init__(self, path=None):
        self.path = path or os.path.join(config_dir(), "settings.json")
        self.data = json.loads(json.dumps(self.DEFAULTS))
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.data.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception:
            traceback.print_exc()

    def save(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.data, f, indent=2)
        os.replace(tmp, self.path)

    # handled-file bookkeeping
    def handled(self, folder):
        return set(self.data["handled"].get(folder_key(folder), []))

    def fingerprints(self, folder):
        return set(self.data.setdefault("fingerprints", {}).get(folder_key(folder), []))

    def mark_handled(self, folder, name):
        lst = self.data["handled"].setdefault(folder_key(folder), [])
        if name.lower() not in lst:
            lst.append(name.lower())
        fp = fingerprint(os.path.join(folder, name))
        if fp:
            fps = self.data.setdefault("fingerprints", {}).setdefault(folder_key(folder), [])
            if fp not in fps:
                fps.append(fp)
                del fps[:-5000]

    def unmark_handled(self, folder, name):
        lst = self.data["handled"].setdefault(folder_key(folder), [])
        if name.lower() in lst:
            lst.remove(name.lower())
        fp = fingerprint(os.path.join(folder, name))
        fps = self.data.setdefault("fingerprints", {}).setdefault(folder_key(folder), [])
        if fp and fp in fps:
            fps.remove(fp)

    def learn(self, invoice):
        s = shape_of(invoice)
        if s:
            self.data["shapes"][s] = self.data["shapes"].get(s, 0) + 1

    def unlearn(self, invoice):
        s = shape_of(invoice)
        if s in self.data["shapes"]:
            self.data["shapes"][s] -= 1
            if self.data["shapes"][s] <= 0:
                del self.data["shapes"][s]

    def learn_label(self, phrase):
        if phrase:
            lab = self.data.setdefault("labels", {})
            lab[phrase] = lab.get(phrase, 0) + 1

    def unlearn_label(self, phrase):
        lab = self.data.setdefault("labels", {})
        if phrase in lab:
            lab[phrase] -= 1
            if lab[phrase] <= 0:
                del lab[phrase]


def fingerprint(path):
    """Size + modified time: survives renames and OneDrive syncing, and never downloads the file."""
    try:
        st = os.stat(path)
        return f"{st.st_size}:{int(st.st_mtime)}"
    except OSError:
        return None


# --------------------------------------------------------------------------
# Auto-update from GitHub (checked each time the app opens, before the window)
# --------------------------------------------------------------------------

def _vtuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")[:4])


def _update_state_path():
    return os.path.join(config_dir(), "update-state.json")


def _load_update_state():
    try:
        with open(_update_state_path(), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_update_state(st):
    try:
        tmp = _update_state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=1)
        os.replace(tmp, _update_state_path())
    except Exception:
        traceback.print_exc()


def update_log(msg):
    try:
        with open(os.path.join(config_dir(), "update-log.txt"), "a", encoding="utf-8") as f:
            f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  v{VERSION}  {msg}\n")
    except Exception:
        pass


def _restart():
    subprocess.Popen([sys.executable, os.path.abspath(__file__)] + sys.argv[1:])
    sys.exit(0)


def roll_back_update(reason):
    """The update just installed doesn't work: put the previous program back. Returns True if it did."""
    me = os.path.abspath(__file__)
    st = _load_update_state()
    pend = st.get("pending")
    backup = me + ".previous"
    if not pend or pend.get("version") != VERSION or not os.path.exists(backup):
        return False
    try:
        os.replace(backup, me)
    except Exception:
        return False
    st.setdefault("bad", []).append(VERSION)
    st["pending"] = None
    _save_update_state(st)
    update_log(f"ROLLED BACK to the previous version ({reason})")
    return True


def update_started_ok():
    """Called once the window has been up for a few seconds: this version works, keep it."""
    st = _load_update_state()
    if st.get("pending") and st["pending"].get("version") == VERSION:
        st["pending"] = None
        _save_update_state(st)
        update_log("new version started fine")


def check_for_update():
    """Download and install a newer version from GitHub, then restart into it. Any problem at all
    (no internet, GitHub blocked, a broken file, a missing helper package) just means the version
    already on this PC runs, as before."""
    try:
        me = os.path.abspath(__file__)
        st = _load_update_state()
        pend = st.get("pending")
        if pend and pend.get("version") == VERSION:
            pend["tries"] = pend.get("tries", 0) + 1
            _save_update_state(st)
            if pend["tries"] >= 3 and roll_back_update("it didn't start properly 3 times"):
                _restart()
            return
        try:
            with open(os.path.join(config_dir(), "settings.json"), encoding="utf-8") as f:
                if json.load(f).get("auto_update") is False:
                    return
        except Exception:
            pass
        import urllib.request
        base = os.environ.get("POD_UPDATE_URL", UPDATE_BASE)
        nocache = f"?t={int(time.time())}"
        with urllib.request.urlopen(base + "version.txt" + nocache, timeout=4) as r:
            remote = r.read(100).decode("utf-8", "replace").strip()
        if not _vtuple(remote) or _vtuple(remote) <= _vtuple(VERSION) or remote in st.get("bad", []):
            return
        with urllib.request.urlopen(base + "pod_renamer.py" + nocache, timeout=30) as r:
            data = r.read()
        src = data.decode("utf-8")
        if not src.rstrip().endswith(END_MARK):                     # download cut off
            update_log(f"skipped {remote}: the download was incomplete")
            return
        compile(src, "pod_renamer.py", "exec")                      # not broken
        m = re.search(r'^VERSION = "([^"]+)"', src, re.M)
        if not m or m.group(1) != remote:
            update_log(f"skipped {remote}: the program on GitHub says version {m.group(1) if m else '?'}")
            return
        m = re.search(r"^REQUIRES = \[(.*?)\]", src, re.M)
        import importlib.util
        missing = [x for x in re.findall(r'"([^"]+)"', m.group(1) if m else "")
                   if importlib.util.find_spec(x) is None]
        if missing:
            if st.get("needs_setup") != remote:
                update_log(f"version {remote} needs {', '.join(missing)}: run SETUP again on this PC")
            st["needs_setup"] = remote
            _save_update_state(st)
            return
        import shutil
        shutil.copy2(me, me + ".previous")
        with open(me + ".new", "wb") as f:
            f.write(data)
        os.replace(me + ".new", me)
        st.update(pending={"version": remote, "from": VERSION, "at": time.time(), "tries": 0}, needs_setup=None)
        _save_update_state(st)
        update_log(f"updated to {remote}")
    except SystemExit:
        raise
    except Exception as e:
        update_log(f"no update ({e.__class__.__name__}: {str(e)[:120]})")
        return
    _restart()


def acquire_single_instance():
    """Only one POD Renamer window at a time (two would overwrite each other's memory)."""
    f = open(os.path.join(config_dir(), "app.lock"), "a+")
    try:
        if sys.platform == "win32":
            import msvcrt
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def write_log(folder, old, new, action="renamed"):
    path = os.path.join(config_dir(), "rename-log.csv")
    new_file = not os.path.exists(path)
    try:
        with open(path, "a", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            if new_file:
                w.writerow(["when", "action", "folder", "old name", "new name"])
            w.writerow([datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"), action, folder, old, new])
    except Exception:
        traceback.print_exc()


# --------------------------------------------------------------------------
# Reading pages: PDF text layer first, OCR when the page is just a picture
# --------------------------------------------------------------------------

class Word:
    __slots__ = ("text", "box")

    def __init__(self, text, box):
        self.text = text
        self.box = box  # (x0, y0, x1, y1) in preview-image pixels

    def __repr__(self):
        return f"Word({self.text!r})"


def _attr(o, k):
    if isinstance(o, dict):
        return o.get(k)
    return getattr(o, k, None)


_ocr_lock = threading.Lock()
_ocr_error = None


def _ocr_windows(img):
    import asyncio
    import winocr
    last = None
    for lang in ("en", "en-US", "en-GB"):
        try:
            res = asyncio.run(winocr.to_coroutine(winocr.recognize_pil(img, lang)))
            break
        except Exception as e:  # language pack missing etc.
            last = e
            res = None
    if res is None:
        raise RuntimeError(f"Windows OCR not available: {last}")
    lines_out, words = [], []
    for line in list(_attr(res, "lines") or []):
        lt = _attr(line, "text") or ""
        lines_out.append(lt)
        for w in list(_attr(line, "words") or []):
            r = _attr(w, "bounding_rect")
            x, y = float(_attr(r, "x") or 0), float(_attr(r, "y") or 0)
            ww, hh = float(_attr(r, "width") or 0), float(_attr(r, "height") or 0)
            words.append(Word(_attr(w, "text") or "", (x, y, x + ww, y + hh)))
    return "\n".join(lines_out), words


def _ocr_tesseract(img):
    import pytesseract
    if sys.platform == "win32":
        for p in (r"C:\Program Files\Tesseract-OCR\tesseract.exe",
                  r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"):
            if os.path.exists(p):
                pytesseract.pytesseract.tesseract_cmd = p
    d = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT, config="--psm 3")
    words, lines, order = [], {}, []
    for i, t in enumerate(d["text"]):
        t = (t or "").strip()
        if not t:
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        if key not in lines:
            lines[key] = []
            order.append(key)
        lines[key].append(t)
        x, y, w, h = d["left"][i], d["top"][i], d["width"][i], d["height"][i]
        words.append(Word(t, (x, y, x + w, y + h)))
    return "\n".join(" ".join(lines[k]) for k in order), words


def ocr(img):
    """Return (text, words) for a PIL image using whichever OCR engine exists."""
    global _ocr_error
    engines = []
    if sys.platform == "win32":
        engines.append(_ocr_windows)
    engines.append(_ocr_tesseract)
    errors = []
    with _ocr_lock:
        for eng in engines:
            try:
                return eng(img)
            except Exception as e:
                errors.append(f"{eng.__name__}: {e}")
    _ocr_error = "; ".join(errors)
    return "", []


class PageInfo:
    def __init__(self):
        self.image = None      # PIL image of page 1
        self.text = ""
        self.words = []
        self.pages = 1
        self.source = ""       # "pdf text" / "ocr" / "ocr (rotated 180)"
        self.angle = 0         # degrees the page image was turned (PIL, anticlockwise) to read it
        self.error = ""
        self.candidates = []   # list of Candidate, best first


def _read_bytes(path):
    with open(path, "rb") as f:  # read fully so the file is never left locked
        return f.read()


def pdf_page_count(path):
    if os.path.splitext(path)[1].lower() != ".pdf":
        return 1
    import pymupdf
    try:
        doc = pymupdf.open(stream=_read_bytes(path), filetype="pdf")
        try:
            return len(doc)
        finally:
            doc.close()
    except Exception:
        return 1


def load_page(path, page_no=None):
    """Render one page and get its words, using the PDF's text when it has any.

    page_no=None: a single-document file (page 1 shown, text of the first pages used).
    page_no=N:    one page of a merged PDF, read on its own.
    """
    from PIL import Image, ImageOps
    info = PageInfo()
    data = _read_bytes(path)
    ext = os.path.splitext(path)[1].lower()
    need_ocr = True
    if ext == ".pdf":
        import pymupdf
        doc = pymupdf.open(stream=data, filetype="pdf")
        try:
            info.pages = len(doc)
            page = doc[page_no or 0]
            zoom = RENDER_DPI / 72.0
            pix = page.get_pixmap(matrix=pymupdf.Matrix(zoom, zoom), alpha=False)
            info.image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
            if page_no is None:
                texts = [doc[i].get_text("text") for i in range(min(len(doc), 3))]
            else:
                texts = [page.get_text("text")]
            text = "\n".join(texts)
            if len(re.sub(r"\s", "", text)) >= MIN_TEXT_CHARS:
                need_ocr = False
                info.text = text
                info.source = "pdf text"
                rot = page.rotation_matrix
                for w in page.get_text("words"):
                    r = pymupdf.Rect(w[:4]) * rot
                    info.words.append(Word(w[4], (r.x0 * zoom, r.y0 * zoom, r.x1 * zoom, r.y1 * zoom)))
        finally:
            doc.close()
    else:
        img = Image.open(io.BytesIO(data))
        img.seek(0)
        img = ImageOps.exif_transpose(img).convert("RGB")
        info.image = img

    if need_ocr:
        img = info.image
        # keep OCR images a sensible size
        if max(img.size) > 3600:
            s = 3600 / max(img.size)
            img = img.resize((int(img.width * s), int(img.height * s)))
            info.image = img
        text, words = ocr(img)
        info.source = "ocr"
        # Upside-down / sideways scan? Try other orientations if little was read
        if _readable_score(text) < 6:
            best = (_readable_score(text), 0, text, words, img)
            for angle in (180, 90, 270):
                rimg = img.rotate(angle, expand=True)
                t2, w2 = ocr(rimg)
                sc = _readable_score(t2)
                if sc > best[0] * 1.5 + 2:
                    best = (sc, angle, t2, w2, rimg)
                if best[0] >= 12:
                    break
            _, angle, text, words, img = best
            if angle:
                info.image = img
                info.angle = angle
                info.source = f"ocr (page turned {angle}°)"
        info.text, info.words = text, words
        if not text.strip() and _ocr_error:
            info.error = "Could not read the text on this page. " + _ocr_error
    return info


COMMON_WORDS = set("""
the and of for to by no nr date invoice inv delivery deliver delivered order received receive signature
sign name tel fax vat reg road street description qty quantity amount code account goods proof number
customer total price unit ltd pty cape town johannesburg durban pretoria head office supplier note document
ref reference page from with in on at good condition company address email phone item items stock product
products box boxes case cases cartons pallet pallets driver time branch store shop tax subtotal excl incl
""".split())


def _readable_score(text):
    """How many real, right-way-up words were read; picks the page orientation."""
    return sum(1 for w in re.findall(r"[A-Za-z]{2,}", text or "") if w.lower() in COMMON_WORDS)


# --------------------------------------------------------------------------
# Finding the invoice number
# --------------------------------------------------------------------------

class Candidate:
    def __init__(self, value, score, why):
        self.value = value
        self.score = score
        self.why = why
        self.box = None

    def __repr__(self):
        return f"Candidate({self.value!r}, {self.score}, {self.why!r})"


SEP = r"[ \t]*[.:#\-–=]*[ \t]*"
NUMWORD = r"(?:no|nr|num(?:ber)?|#|ref(?:erence)?)\.?"
VALUE = r"([A-Z]{0,5}[\-/]?[ \t]?\d[0-9A-Z\-/]{2,19})"

LABELS = [
    # (label regex, score, description)
    (rf"\b(?:tax[ \t]*)?inv(?:oice)?\b\.?(?:[ \t]*{NUMWORD})?", 100, "next to 'Invoice'"),
    (rf"\b(?:document|doc)\b\.?[ \t]*{NUMWORD}", 40, "next to 'Document no.'"),
    (rf"\b(?:delivery[ \t]*note|waybill|d/?n)\b\.?(?:[ \t]*{NUMWORD})?", 30, "next to 'Delivery note'"),
    (rf"\b(?:order|p\.?o\.?|purchase[ \t]*order)\b\.?[ \t]*{NUMWORD}", 10, "next to 'Order no.'"),
]

DATE_RE = re.compile(r"^\d{1,4}[/\-.]\d{1,2}[/\-.]\d{2,4}$")


def shape_of(value):
    """'IN004512' -> 'IN\\d{6}'  (letters kept, digit runs become counts)."""
    out = []
    for m in re.finditer(r"\d+|[A-Za-z]+|.", value.strip()):
        s = m.group()
        if s.isdigit():
            out.append(r"\d{%d}" % len(s))
        elif s.isalpha():
            out.append(re.escape(s.upper()))
        elif s.strip():
            out.append(re.escape(s))
    return "".join(out)


def _clean_value(v):
    v = v.strip().strip("-/.:,;()[] ")
    v = re.sub(r"^\$(?=\d)", "S", v)          # OCR reads a leading S as $
    v = re.sub(r"[ \t]+", "", v)
    # OCR often reads 0 as O inside numbers: 12O45 -> 12045
    v = re.sub(r"(?<=\d)[Oo](?=\d|$)", "0", v)
    v = re.sub(r"(?<=\d)[Il|](?=\d)", "1", v)
    # "INO090002" -> "IN0090002": an O between a 2+ letter prefix and the digits is a misread zero
    # (one-letter prefixes such as SO / PO are real words, so they're left alone)
    m = re.match(r"^([A-Za-z]{2,}?)([Oo]+)(\d.*)$", v)
    if m:
        v = m.group(1) + "0" * len(m.group(2)) + m.group(3)
    return v


def _plausible(v):
    if len(re.findall(r"\d", v)) < 3:
        return False
    if DATE_RE.match(v):
        return False
    if len(v) > 24 or not re.match(r"^[0-9A-Za-z][0-9A-Za-z\-/]*$", v):
        return False
    return True


def _looks_like_junk_number(v):
    digits = re.sub(r"\D", "", v)
    if v.isdigit():
        if len(v) == 10 and v.startswith("0"):
            return True                       # SA phone number
        if len(v) == 10 and v.startswith("4"):
            return True                       # SA VAT number
        if len(v) == 8 and re.match(r"(19|20)\d\d(0[1-9]|1[0-2])(0[1-9]|[12]\d|3[01])$", v):
            return True                       # date like 20261002
    if len(digits) >= 13:
        return True                           # ID / account numbers
    return False


# ---- working with word positions on the page ----

def _h(w):
    return max(w.box[3] - w.box[1], 1)


def _union(ws):
    return (min(w.box[0] for w in ws), min(w.box[1] for w in ws),
            max(w.box[2] for w in ws), max(w.box[3] for w in ws))


def _right_of(box, words, h, exclude=()):
    """Nearest word to the right on the same row."""
    yc = (box[1] + box[3]) / 2
    best, bd = None, 1e9
    for w in words:
        if w in exclude:
            continue
        wy = (w.box[1] + w.box[3]) / 2
        gap = w.box[0] - box[2]
        if abs(wy - yc) < 0.7 * h and -0.3 * h < gap < 10 * h and gap < bd:
            best, bd = w, gap
    return best


def _left_of(box, words, h, exclude=()):
    yc = (box[1] + box[3]) / 2
    best, bd = None, 1e9
    for w in words:
        if w in exclude:
            continue
        wy = (w.box[1] + w.box[3]) / 2
        gap = box[0] - w.box[2]
        if abs(wy - yc) < 0.7 * h and -0.3 * h < gap < 10 * h and gap < bd:
            best, bd = w, gap
    return best


def _below(box, words, h):
    """Nearest word underneath, in the same column (for table layouts)."""
    best, bd = None, 1e9
    for w in words:
        gap = w.box[1] - box[3]
        overlap = min(w.box[2], box[2] + h) - max(w.box[0], box[0] - h)
        if -0.2 * h < gap < 3.5 * h and overlap > 0:
            d = gap + abs(w.box[0] - box[0]) * 0.05
            if d < bd:
                best, bd = w, d
    return best


def _above(box, words, h):
    """Words in the row directly above, overlapping this column."""
    rows = []
    for w in words:
        gap = box[1] - w.box[3]
        overlap = min(w.box[2], box[2] + h) - max(w.box[0], box[0] - h)
        if -0.2 * h < gap < 3.5 * h and overlap > 0:
            rows.append((gap, w))
    if not rows:
        return []
    g0 = min(g for g, _ in rows)
    return sorted([w for g, w in rows if g - g0 < 0.6 * h], key=lambda w: w.box[0])


def _value_from(word, words, h):
    """Turn the word after a label into a value (joins 'INV' + '12345')."""
    if word is None:
        return None, None
    t = word.text.strip()
    if re.fullmatch(r"[A-Za-z]{1,5}[\-/]?", t):
        nxt = _right_of(word.box, words, h, exclude=(word,))
        if nxt is not None and re.match(r"\d", nxt.text) and nxt.box[0] - word.box[2] < 1.2 * h:
            return t + nxt.text, _union([word, nxt])
    return t, word.box


def _spatial_labels(words, label_rx):
    """Find label phrases in the word sequence: yields (words_of_label)."""
    if not words:
        return
    starts, pos = [], 0
    for w in words:
        starts.append(pos)
        pos += len(w.text) + 1
    joined = " ".join(w.text for w in words)
    import bisect
    for m in re.finditer(label_rx, joined, re.I):
        i0 = bisect.bisect_right(starts, m.start()) - 1
        i1 = bisect.bisect_right(starts, max(m.end() - 1, m.start())) - 1
        lw = words[i0:i1 + 1]
        # swallow trailing punctuation-only words like ":" or "#"
        j = i1 + 1
        while j < len(words) and re.fullmatch(r"[.:#\-–=]+", words[j].text):
            lw.append(words[j])
            j += 1
        yield lw


def label_for(box, words):
    """The label printed next to (or above) a value: used to learn per-supplier labels."""
    if not words or not box:
        return ""
    h = sorted(_h(w) for w in words)[len(words) // 2]
    left, cur, used = [], box, set()
    for _ in range(3):
        w = _left_of(cur, words, h, exclude=used)
        if w is None:
            break
        t = re.sub(r"[^A-Za-z#/ ]", "", w.text).strip()
        if not t or re.search(r"\d", w.text):
            break
        left.insert(0, t)
        used.add(w)
        cur = w.box
        if len(left) == 2:
            break
    if not left:
        above = _above(box, words, h)
        left = [re.sub(r"[^A-Za-z#/ ]", "", w.text).strip() for w in above if not re.search(r"\d", w.text)]
        left = [t for t in left if t][:3]
    phrase = " ".join(left).lower().strip(" #/")
    phrase = re.sub(r"\s+", " ", phrase)
    return phrase if 2 <= len(phrase) <= 40 else ""


def _label_regex(phrase):
    toks = phrase.split()
    return r"(?<![A-Za-z])" + r"[ \t]*".join(re.escape(t) for t in toks) + r"(?![A-Za-z])\.?"


# characters a scan reader easily mixes up
LOOKALIKE = {"1": "I", "I": "1", "L": "1", "0": "O", "O": "0", "5": "S", "S": "5",
             "8": "B", "B": "8", "2": "Z", "Z": "2", "6": "G", "G": "6"}


def _snap_to_shapes(value, shapes):
    """Read off a picture, 'SI625126' can come out as 'S1625126'. If the value doesn't fit any
    invoice-number format she has confirmed before, but swapping one or two look-alike characters
    makes it fit one, return (fixed value, how often that format was confirmed)."""
    if not shapes or shape_of(value) in shapes:
        return None
    v = value.upper()
    idx = [i for i, ch in enumerate(v) if ch in LOOKALIKE]
    best = None
    for k in (1, 2):
        for pos in itertools.combinations(idx, k):
            cand = list(v)
            for i in pos:
                cand[i] = LOOKALIKE[cand[i]]
            c = "".join(cand)
            n = shapes.get(shape_of(c), 0)
            if n and (best is None or n > best[1]):
                best = (c, n)
        if best:
            return best
    return None


def _lookalike_risk(value):
    """True if a 1/I or 0/O sits where letters meet digits ('S1625126' could be 'SI625126')."""
    v = value.upper()
    for i, ch in enumerate(v):
        if ch in "10IO":
            prev, nxt = v[i - 1] if i else "", v[i + 1] if i + 1 < len(v) else ""
            if (ch in "10" and prev.isalpha()) or (ch in "IO" and nxt.isdigit()):
                return True
    return False


def find_candidates(text, words=None, shapes=None, labels=None, ocr=False):
    shapes = shapes or {}
    labels = labels or {}
    words = words or []
    found = {}
    weak = set()     # fixed up to a format confirmed only once or twice: never "confident"

    def add(value, score, why, box=None):
        if not value:
            return
        value = _clean_value(value)
        if not _plausible(value):
            return
        if ocr:
            snap = _snap_to_shapes(value, shapes)
            if snap:
                why += f" (the scan showed {value})"
                value = snap[0]
                if snap[1] < 3:                # format seen only once or twice: let her check it
                    weak.add(value.upper())
        key = value.upper()
        c = found.get(key)
        if c is None or score > c.score:
            nc = Candidate(value, score, why)
            nc.box = box or (c.box if c else None)
            found[key] = nc
        elif box and c.box is None:
            c.box = box

    rules = list(LABELS)
    for phrase, count in labels.items():
        rules.append((_label_regex(phrase), 72 + min(count, 10) * 2, f"next to '{phrase.title()}', like before"))

    upper = text or ""
    # 1. numbers sitting next to a label like "Invoice No:"
    for label, score, why in rules:
        for m in re.finditer(label + SEP + VALUE, upper, re.I):
            add(m.group(1), score, why)
        # label on one line, number on the next
        for m in re.finditer(label + r"[ \t]*[.:#]*[ \t]*\n[ \t]*" + VALUE, upper, re.I):
            add(m.group(1), score - 35, why + " (line below)")

    # 1b. same, using where words sit on the page (handles tables and columns)
    if words:
        h = sorted(_h(w) for w in words)[len(words) // 2]
        for label, score, why in rules:
            for lw in _spatial_labels(words, label):
                lbox = _union(lw)
                v, b = _value_from(_right_of(lbox, words, h, exclude=lw), words, h)
                if v and _plausible(_clean_value(v)):
                    add(v, score, why, b)
                    continue
                v, b = _value_from(_below(lbox, words, h), words, h)
                if v:
                    add(v, score - 8, why + " (in the column below)", b)

    # 2. tokens like INV12345 / INV-12345 on their own
    for m in re.finditer(r"(?<![0-9A-Z])(INV[\-/ ]?\d{3,}[0-9A-Z]*)", upper, re.I):
        add(m.group(1), 101, "starts with 'INV'")

    # 3. anything that looks like invoice numbers confirmed before
    for shp, count in shapes.items():
        has_letters = bool(re.search(r"[A-Z]", shp.replace(r"\d", "")))
        base = 70 if has_letters else 30
        bonus = min(count, 10) * 2
        try:
            pat = re.compile(r"(?<![0-9A-Za-z/])" + shp + r"(?![0-9A-Za-z/])", re.I)
        except re.error:
            continue
        for m in pat.finditer(upper):
            add(m.group(0), base + bonus, "looks like earlier invoice numbers")
    # boost labelled finds that also match a learned format
    for c in found.values():
        if shape_of(c.value) in shapes and "earlier" not in c.why:
            c.score += 25
            c.why += ", and looks like earlier invoice numbers"

    # 4. any other longish number, as a fallback option
    for m in re.finditer(r"(?<![0-9A-Za-z/])([A-Z]{0,4}\d{5,12})(?![0-9A-Za-z/])", upper, re.I):
        v = m.group(1)
        if not _looks_like_junk_number(v):
            add(v, 5, "just a number on the page")

    # read off a picture, a 1/I or 0/O mix-up is easy: until this number format has been confirmed
    # before, don't call it "confident" (it shows orange: please check)
    if ocr:
        for k, c in found.items():
            if k in weak:
                c.score = min(c.score, 85)
            elif c.score >= 90 and shape_of(c.value) not in shapes and _lookalike_risk(c.value) \
                    and not c.value.upper().startswith("INV"):
                c.score = 85
                c.why += " (on scans 1 and I, or 0 and O, can look alike)"

    cands = sorted(found.values(), key=lambda c: -c.score)
    cands = [c for c in cands if not (_looks_like_junk_number(c.value) and c.score < 90)]
    if words:
        for c in cands:
            if c.box is None:
                c.box = locate(c.value, words)
    return cands[:8]


def _norm(s):
    return re.sub(r"[^0-9A-Z]", "", s.upper().replace("O", "0"))


def locate(value, words):
    """Find the box of the word(s) on the page that hold this value."""
    target = _norm(value)
    if not target:
        return None
    best = None
    for i, w in enumerate(words):
        n = _norm(w.text)
        if not n:
            continue
        if target in n:
            return w.box
        # value split over two or three neighbouring words ("INV 12345")
        if target.startswith(n):
            joined, box = n, list(w.box)
            for nxt in words[i + 1:i + 3]:
                joined += _norm(nxt.text)
                box = [min(box[0], nxt.box[0]), min(box[1], nxt.box[1]),
                       max(box[2], nxt.box[2]), max(box[3], nxt.box[3])]
                if target in joined:
                    return tuple(box)
        # digits part matches (OCR mangled the prefix)
        digits = re.sub(r"\D", "", target)
        if len(digits) >= 4 and digits in re.sub(r"\D", "", n) and best is None:
            best = w.box
    return best


def analyse(path, shapes, labels=None, page_no=None):
    try:
        info = load_page(path, page_no)
    except Exception as e:
        info = PageInfo()
        info.error = f"Could not open this file ({e.__class__.__name__}: {e})"
        traceback.print_exc()
        return info
    info.candidates = find_candidates(info.text, info.words, shapes, labels, info.source.startswith("ocr"))
    return info


# --------------------------------------------------------------------------
# File handling
# --------------------------------------------------------------------------

BAD_CHARS = r'[\\/:*?"<>|\r\n\t]'


def safe_name(s):
    s = re.sub(BAD_CHARS, "-", s).strip().strip(".")
    return re.sub(r"\s+", " ", s)


DUP_SUFFIX = re.compile(r"^(.*?) ?\((\d+)\)$")   # "X (2)" or "X(2)"


def looks_done(name, settings):
    """A file already named like an invoice number (e.g. INV839659.pdf, INV839659 (2).pdf) is finished work,
    even if this PC never saw it being renamed (renamed by hand, or done on another computer)."""
    stem = os.path.splitext(name)[0].strip()
    m = DUP_SUFFIX.match(stem)
    if m:
        stem = m.group(1)
    p = settings.data
    pre, suf = p.get("prefix", ""), p.get("suffix", "")
    if pre and stem.lower().startswith(pre.lower()):
        stem = stem[len(pre):]
    if suf and stem.lower().endswith(suf.lower()):
        stem = stem[:-len(suf)]
    stem = stem.strip()
    if re.fullmatch(r"INV[-/ ]?\d{4,}", stem, re.I):
        return True
    for shp in settings.data.get("shapes", {}):
        try:
            if re.fullmatch(shp, stem, re.I):
                return True
        except re.error:
            pass
    return False


def repair_wrong_combines(settings):
    """The previous version could mistake scans like 'ASHAK MIX.pdf' + 'ASHAK MIX (1).pdf' for one invoice
    saved twice, glue them together and hide the result. Make every such file come up again so its pages
    get done (the separate scans it was made from are still in 'Originals' as a backup)."""
    if settings.data.get("repaired_combines"):
        return []
    fixed = []
    try:
        with open(os.path.join(config_dir(), "rename-log.csv"), newline="", encoding="utf-8") as f:
            rows = list(csv.reader(f))[1:]
    except FileNotFoundError:
        rows = []
    except Exception:
        traceback.print_exc()
        return []
    for r in rows:
        if len(r) < 5 or r[1] != "combined duplicate" or looks_done(r[4], settings):
            continue
        folder, name = r[2], r[4]
        target = os.path.join(folder, name)
        settings.unmark_handled(folder, name)          # name + its current fingerprint
        if os.path.exists(target) and target not in fixed:
            fixed.append(target)
    settings.data["repaired_combines"] = True
    settings.save()
    return fixed


def list_pending(folder, settings):
    if not folder or not os.path.isdir(folder):
        return []
    fps = settings.fingerprints(folder)
    ignore_before = settings.data["ignore_before"].get(folder_key(folder), 0)
    now = time.time()
    out = []
    for name in os.listdir(folder):
        full = os.path.join(folder, name)
        if os.path.splitext(name)[1].lower() not in EXTS:
            continue
        if name.startswith("~$") or name.startswith("."):
            continue
        try:
            st = os.stat(full)
        except OSError:
            continue
        if not os.path.isfile(full) or st.st_size == 0:
            continue
        if st.st_mtime < ignore_before:
            continue
        if now - st.st_mtime < 3:      # still being copied in
            continue
        if f"{st.st_size}:{int(st.st_mtime)}" in fps or looks_done(name, settings):
            continue
        out.append((st.st_mtime, name, full))
    out.sort()
    return [f for _, _, f in out]


def unique_path(folder, base, ext, current):
    cand = os.path.join(folder, base + ext)
    n = 2
    while os.path.exists(cand) and os.path.normcase(cand) != os.path.normcase(current):
        cand = os.path.join(folder, f"{base} ({n}){ext}")
        n += 1
    return cand


def rename_with_retry(old, new):
    last = None
    for _ in range(4):
        try:
            os.rename(old, new)
            return
        except PermissionError as e:   # OneDrive or a viewer briefly has it open
            last = e
            time.sleep(0.8)
    raise last


PAGE_X_OF_Y = re.compile(r"\bpage\s*:?\s*(\d{1,3})\s*(?:of|/)\s*(\d{1,3})\b", re.I)


def continuation_hint(text):
    """True when the page says it is page 2+ of something ("Page 2 of 3")."""
    m = PAGE_X_OF_Y.search(text or "")
    return bool(m and int(m.group(1)) > 1)


def group_pages(assign):
    """[inv or None per page] -> [(invoice, [page indexes])], first-seen order.
    Pages with the same number end up in the same file; None pages are left out."""
    groups, order = {}, []
    for i, inv in enumerate(assign):
        if not inv:
            continue
        k = inv.upper()
        if k not in groups:
            groups[k] = (inv, [])
            order.append(k)
        groups[k][1].append(i)
    return [groups[k] for k in order]


def write_bytes_safely(path, data):
    tmp = path + ".podrenamer-tmp"
    with open(tmp, "wb") as f:
        f.write(data)
    last = None
    for _ in range(5):
        try:
            os.replace(tmp, path)
            return
        except PermissionError as e:   # OneDrive or a PDF viewer briefly has it open
            last = e
            time.sleep(0.8)
    try:
        os.remove(tmp)
    except OSError:
        pass
    raise last


def _append_pages(dst, src_path, pages=None, angles=None):
    """Append pages of a PDF (or a picture, as one page) to the open PDF `dst`."""
    import pymupdf
    ext = os.path.splitext(src_path)[1].lower()
    data = _read_bytes(src_path)
    if ext == ".pdf":
        src = pymupdf.open(stream=data, filetype="pdf")
    else:
        img = pymupdf.open(stream=data, filetype=ext.lstrip("."))
        src = pymupdf.open("pdf", img.convert_to_pdf())
        img.close()
    try:
        for p in (pages if pages is not None else range(len(src))):
            dst.insert_pdf(src, from_page=p, to_page=p)
            a = (angles[p] if angles and p < len(angles) else 0) or 0
            if a:   # the scan was upside down / sideways: store it the right way up
                pg = dst[len(dst) - 1]
                pg.set_rotation((pg.rotation + 360 - a) % 360)
    finally:
        src.close()


def combine_into(target, src_path, pages=None, angles=None):
    """Add pages to the end of an existing invoice PDF. Returns the target's previous bytes (for Undo)."""
    import pymupdf
    old = _read_bytes(target)
    dst = pymupdf.open(stream=old, filetype="pdf")
    try:
        _append_pages(dst, src_path, pages, angles)
        data = dst.tobytes(garbage=3, deflate=True)
    finally:
        dst.close()
    write_bytes_safely(target, data)
    return old


def invoice_pdf_path(folder, inv, prefix="", suffix=""):
    return os.path.join(folder, safe_name(f"{prefix}{inv}{suffix}") + ".pdf")


def split_pdf(src_path, assign, angles, folder, prefix="", suffix=""):
    """Save each invoice in a merged PDF. If that invoice already has a PDF in the folder, its pages are
    added to the end of it (so a multi-page invoice always ends up as ONE file).
    Returns [{"inv", "path", "pages", "old": previous bytes or None if newly created}]."""
    import pymupdf
    done = []
    try:
        for inv, pages in group_pages(assign):
            dest = invoice_pdf_path(folder, inv, prefix, suffix)
            if os.path.exists(dest):
                old = combine_into(dest, src_path, pages, angles)
                done.append({"inv": inv, "path": dest, "pages": pages, "old": old})
            else:
                out = pymupdf.open()
                _append_pages(out, src_path, pages, angles)
                write_bytes_safely(dest, out.tobytes(garbage=3, deflate=True))
                out.close()
                done.append({"inv": inv, "path": dest, "pages": pages, "old": None})
    except Exception:
        undo_saved(done)
        raise
    return done


def undo_saved(done):
    """Reverse split_pdf / combine: delete files we created, restore files we added pages to."""
    for d in reversed(done):
        try:
            if d["old"] is None:
                os.remove(d["path"])
            else:
                write_bytes_safely(d["path"], d["old"])
        except OSError:
            traceback.print_exc()


def find_duplicate_sets(folder):
    """{"INV839659": ["INV839659 (2).pdf", "INV839659 (3).pdf"]} where INV839659.pdf also exists."""
    sets = {}
    try:
        names = os.listdir(folder)
    except OSError:
        return sets
    lower = {n.lower() for n in names}
    for n in names:
        stem, ext = os.path.splitext(n)
        m = DUP_SUFFIX.match(stem)
        if ext.lower() == ".pdf" and m and (m.group(1) + ".pdf").lower() in lower:
            sets.setdefault(m.group(1), []).append((int(m.group(2)), n))
    return {k: [n for _, n in sorted(v)] for k, v in sets.items()}


def move_to_originals(path):
    folder = os.path.join(os.path.dirname(path), "Originals")
    os.makedirs(folder, exist_ok=True)
    base, ext = os.path.splitext(os.path.basename(path))
    dest = unique_path(folder, base, ext, "")
    rename_with_retry(path, dest)
    return dest


def open_file(path):
    try:
        if sys.platform == "win32":
            os.startfile(path)  # noqa
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except Exception:
        pass


# --------------------------------------------------------------------------
# Screen
# --------------------------------------------------------------------------

def run_app():
    import tkinter as tk
    from tkinter import filedialog, messagebox
    from PIL import Image, ImageTk

    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    UI = "Segoe UI" if sys.platform == "win32" else "DejaVu Sans"
    BG, PANEL, INK, MUTED = "#f4f1ec", "#ffffff", "#1f2a2e", "#5f6b6f"
    GREEN, GREEN_D, AMBER, RED, BLUE = "#1f7a4d", "#17603c", "#b7791f", "#b23b3b", "#2b5d8a"
    HILITE = "#ffcc00"

    class App(tk.Tk):
        def __init__(self):
            super().__init__()
            self.title(f"{APP}   ·   version {VERSION}" + (f"   ·   made for {MADE_FOR}" if MADE_FOR else ""))
            self.after(5000, update_started_ok)
            self.configure(bg=BG)
            self.minsize(1000, 640)
            try:
                self.state("zoomed")
            except tk.TclError:
                self.geometry("1280x800")

            self.settings = Settings()
            self.folder = self.settings.data.get("folder", "")
            self.files = []
            self.index = 0
            self.done_count = 0
            self.undo_stack = []
            self.cache = {}                 # (path, page or None) -> PageInfo
            self.cache_lock = threading.Lock()
            self.cache_gen = 0              # bumped on every "Check for new scans"
            self.work_q = queue.PriorityQueue()
            self._seq = 0
            self.page_counts = {}
            self.batch = None               # state while going through a merged PDF
            self.cur_key = None
            self._prefill = None
            self.last_done = None           # {"inv", "index"}: invoice of the last page saved (for page 2s in the next file)
            self._count_cache = {}
            self.current = None
            self.info = None
            self.sel_box = None
            self._tk_img = None
            self._tk_zoom = None
            self._scale = 1.0
            self._offset = (0, 0)

            threading.Thread(target=self._worker, daemon=True).start()
            self._build()
            self.bind("<Return>", lambda e: self.accept())
            self.bind("<KP_Enter>", lambda e: self.accept())
            self.bind("<Control-z>", lambda e: self.undo())
            self.bind("<Escape>", lambda e: self.skip())
            self.after(150, self.start)

        # ---------- layout ----------
        def _build(self):
            top = tk.Frame(self, bg=BG)
            top.pack(fill="x", padx=16, pady=(12, 6))
            tk.Label(top, text="POD Renamer", font=(UI, 18, "bold"), bg=BG, fg=INK).pack(side="left")
            self.progress = tk.Label(top, text="", font=(UI, 13), bg=BG, fg=MUTED)
            self.progress.pack(side="left", padx=20)
            self.flash_lbl = tk.Label(top, text="", font=(UI, 12, "bold"), bg=BG, fg=GREEN)
            self.flash_lbl.pack(side="left", padx=10)
            tk.Button(top, text="Change folder", font=(UI, 11), command=self.choose_folder,
                      relief="flat", bg="#e6e1d8", activebackground="#d9d2c6", padx=12, pady=4).pack(side="right")
            tk.Button(top, text="⟳  Check for new scans", font=(UI, 11), command=self.refresh,
                      relief="flat", bg="#e6e1d8", activebackground="#d9d2c6", padx=12, pady=4).pack(side="right", padx=8)
            self.folder_lbl = tk.Label(self, text="", font=(UI, 10), bg=BG, fg=MUTED, anchor="w")
            self.folder_lbl.pack(fill="x", padx=16)

            body = tk.Frame(self, bg=BG)
            body.pack(fill="both", expand=True, padx=16, pady=10)

            # right-hand panel
            side = self.side = tk.Frame(body, bg=PANEL, width=440, highlightthickness=1, highlightbackground="#ddd6ca")
            side.pack(side="right", fill="y")
            side.pack_propagate(False)
            pad = dict(padx=20)

            row = tk.Frame(side, bg=PANEL)
            row.pack(fill="x", side="bottom", pady=(4, 12), **pad)
            small = dict(font=(UI, 10), relief="flat", bg="#ece7df", activebackground="#ddd6ca", pady=6)
            tk.Button(row, text="Skip for now", command=self.skip, **small).pack(side="left", expand=True, fill="x")
            tk.Button(row, text="↶ Undo last", command=self.undo, **small).pack(side="left", expand=True, fill="x", padx=6)
            tk.Button(row, text="Open file", command=lambda: self.current and open_file(self.current), **small).pack(side="left", expand=True, fill="x")
            self.hide_btn = tk.Button(side, text="Hide this file (not a POD / already done)", command=self.hide, font=(UI, 9),
                                      relief="flat", bg=PANEL, fg=MUTED, activebackground=PANEL, cursor="hand2")
            self.hide_btn.pack(side="bottom", **pad)

            self.file_lbl = tk.Label(side, text="", font=(UI, 10), bg=PANEL, fg=MUTED, wraplength=400, justify="left", anchor="w")
            self.file_lbl.pack(fill="x", pady=(16, 2), **pad)
            self.batch_lbl = tk.Label(side, text="", font=(UI, 10, "bold"), bg=PANEL, fg=BLUE, wraplength=400,
                                      justify="left", anchor="w")
            self.batch_lbl.pack(fill="x", pady=(0, 6), **pad)

            tk.Label(side, text="Close-up", font=(UI, 10, "bold"), bg=PANEL, fg=MUTED, anchor="w").pack(fill="x", **pad)
            self.zoom = tk.Label(side, bg="#eeeae3", height=10)
            self.zoom.pack(fill="x", pady=(2, 14), **pad)

            tk.Label(side, text="Invoice number", font=(UI, 13, "bold"), bg=PANEL, fg=INK, anchor="w").pack(fill="x", **pad)
            self.var = tk.StringVar()
            self.var.trace_add("write", lambda *a: self._update_preview_name())
            self.entry = tk.Entry(side, textvariable=self.var, font=(UI, 24, "bold"), relief="solid", bd=1, fg=INK)
            self.entry.pack(fill="x", pady=(4, 4), ipady=6, **pad)
            self.conf_lbl = tk.Label(side, text="", font=(UI, 11), bg=PANEL, fg=MUTED, wraplength=400, justify="left", anchor="w")
            self.conf_lbl.pack(fill="x", **pad)
            self.newname_lbl = tk.Label(side, text="", font=(UI, 11), bg=PANEL, fg=MUTED, wraplength=400, justify="left", anchor="w")
            self.newname_lbl.pack(fill="x", pady=(2, 10), **pad)

            self.accept_btn = tk.Button(side, text="✔  Rename & next   (Enter)", font=(UI, 15, "bold"), fg="white",
                                        bg=GREEN, activebackground=GREEN_D, activeforeground="white", relief="flat",
                                        command=self.accept, pady=10, cursor="hand2")
            self.accept_btn.pack(fill="x", **pad)

            self.batch_row = tk.Frame(side, bg=PANEL)
            bsmall = dict(font=(UI, 10), relief="flat", bg="#eef3f8", fg=BLUE, activebackground="#dde8f2",
                          cursor="hand2", pady=4)
            self.same_btn = tk.Button(self.batch_row, text="↑ Same invoice as page before", command=self.same_as_prev, **bsmall)
            self.same_btn.pack(side="left", expand=True, fill="x")
            self.leave_btn = tk.Button(self.batch_row, text="Leave page out", command=self.leave_out, **bsmall)
            self.leave_btn.pack(side="left", expand=True, fill="x", padx=(6, 0))
            self._batch_row_pad = pad

            self.others_title = tk.Label(side, text="Other numbers on this page (click to use):", font=(UI, 10, "bold"),
                                         bg=PANEL, fg=MUTED, anchor="w")
            self.others_title.pack(fill="x", pady=(14, 4), **pad)
            self.others = tk.Frame(side, bg=PANEL)
            self.others.pack(fill="x", **pad)

            tk.Label(side, text="Wrong number? Click the right one on the page,\nor just type it in the box.",
                     font=(UI, 11), bg=PANEL, fg=BLUE, justify="left", anchor="w").pack(fill="x", pady=(14, 8), **pad)


            # page preview
            self.canvas = tk.Canvas(body, bg="#d9d4cb", highlightthickness=0, cursor="hand2")
            self.canvas.pack(side="left", fill="both", expand=True, padx=(0, 14))
            self.canvas.bind("<Configure>", lambda e: self._draw_page())
            self.canvas.bind("<Button-1>", self._on_click)

            self.message = tk.Frame(body, bg=BG)  # used for empty / welcome states

        # ---------- background reading ----------
        def _worker(self):
            while True:
                _, _, key = self.work_q.get()
                with self.cache_lock:
                    if key in self.cache:
                        continue
                    self.cache[key] = None   # in progress
                    gen = self.cache_gen
                shapes = dict(self.settings.data.get("shapes", {}))
                labels = dict(self.settings.data.get("labels", {}))
                path, page = key
                info = analyse(path, shapes, labels, page)
                with self.cache_lock:
                    if gen != self.cache_gen:
                        # "Check for new scans" was pressed while this page was being read: the file
                        # with this name may be a different scan now, so throw this reading away
                        if self.cache.get(key) is None:
                            self.cache.pop(key, None)
                        continue
                    self.cache[key] = info

        def _request(self, key, prio=5):
            with self.cache_lock:
                if key in self.cache:
                    return
            self._seq += 1
            self.work_q.put((prio, self._seq, key))

        def _pages(self, path):
            if path not in self.page_counts:
                self.page_counts[path] = pdf_page_count(path)
            return self.page_counts[path]

        def _key_for_first_page(self, path):
            return (path, 0) if self._pages(path) > 1 else (path, None)

        def flash(self, text, ms=7000):
            self.flash_lbl.config(text=text)
            if getattr(self, "_flash_job", None):
                self.after_cancel(self._flash_job)
            self._flash_job = self.after(ms, lambda: self.flash_lbl.config(text=""))

        # ---------- flow ----------
        def start(self):
            if not self.folder or not os.path.isdir(self.folder):
                self.show_message("Welcome!",
                                  "First, choose the folder where the scanned PODs arrive\n(usually a folder in OneDrive).",
                                  "Choose folder", self.choose_folder)
                return
            st = _load_update_state()
            if st.get("needs_setup") and st.get("told_setup") != st["needs_setup"]:
                st["told_setup"] = st["needs_setup"]
                _save_update_state(st)
                messagebox.showinfo(APP, "There is a new version of POD Renamer, but whoever set it up\n"
                                         "needs to run SETUP on this computer once.\n\nEverything still works as before.")
            fixed = repair_wrong_combines(self.settings)
            if fixed:
                messagebox.showinfo(APP, "Some scans were joined together by mistake in the previous version:\n\n"
                                    + "\n".join("   " + os.path.basename(f) for f in fixed[:8]) +
                                    "\n\nThey will come up again now so every page gets its invoice number.")
            self.refresh()

        def choose_folder(self):
            d = filedialog.askdirectory(title="Choose the folder where the scans arrive",
                                        initialdir=self.folder or os.path.expanduser("~"))
            if not d:
                return
            d = os.path.normpath(d)
            self.folder = d
            self.settings.data["folder"] = d
            key = folder_key(d)
            if key not in self.settings.data["ignore_before"]:
                midnight = datetime.datetime.combine(datetime.date.today(), datetime.time()).timestamp()
                old = [f for f in os.listdir(d) if os.path.splitext(f)[1].lower() in EXTS
                       and os.path.getmtime(os.path.join(d, f)) < midnight]
                ib = 0
                if old:
                    if messagebox.askyesno(APP, f"This folder already has {len(old)} older files (from before today).\n\n"
                                                f"Are those already done?\n\nYes = only show today's and new scans\n"
                                                f"No = go through all of them"):
                        ib = midnight
                self.settings.data["ignore_before"][key] = ib
            self.settings.save()
            self.refresh()

        def _remember_last(self, inv):
            """Remember the invoice of the last page saved, even after closing the app, so that a
            page 2 at the start of the next scan batch still joins the right invoice."""
            self.last_done = {"inv": inv, "index": self.index} if inv else None
            self.settings.data["last_invoice"] = {"inv": inv, "at": time.time()} if inv else None
            self.settings.save()

        def refresh(self):
            self._offer_tidy()
            li = self.settings.data.get("last_invoice")
            recent = li and time.time() - li.get("at", 0) < 16 * 3600
            self.last_done = {"inv": li["inv"], "index": -1} if recent else None
            # Forget every page read before. Scans are often named after the company (e.g. "ASHAK MIX.pdf"),
            # so the next batch can arrive under the same name as one already done.
            with self.cache_lock:
                self.cache.clear()
                self.cache_gen += 1
            self._count_cache = {}
            self.files = list_pending(self.folder, self.settings)
            self.index = 0
            self.done_count = 0
            self.batch = None
            self.page_counts = {}
            self.folder_lbl.config(text=f"Folder:  {self.folder}" if self.folder else "")
            for f in self.files:          # read the first page of everything in the background
                self._request(self._key_for_first_page(f), prio=5)
            self.show_current()

        def _offer_tidy(self):
            """Invoices saved earlier as 'X.pdf' + 'X (2).pdf' + 'X (3).pdf': offer to make them one PDF."""
            if not self.folder or not os.path.isdir(self.folder):
                return
            declined = set(self.settings.data.setdefault("tidy_declined", []))
            sets = {b: n for b, n in find_duplicate_sets(self.folder).items()
                    if looks_done(b + ".pdf", self.settings)
                    and f"{b}|{'|'.join(n)}".lower() not in declined}
            if not sets:
                return
            lines = [f"   {b}.pdf  +  {', '.join(n)}" for b, n in list(sets.items())[:8]]
            if len(sets) > 8:
                lines.append(f"   …and {len(sets) - 8} more")
            ok = messagebox.askyesno(APP, "Some invoices were saved as separate files instead of one PDF:\n\n"
                                          + "\n".join(lines) +
                                          "\n\nCombine each one into a single PDF now?\n"
                                          "(The extra files are moved into the 'Originals' folder, not deleted.)")
            if not ok:
                for b, n in sets.items():
                    self.settings.data["tidy_declined"].append(f"{b}|{'|'.join(n)}".lower())
                self.settings.save()
                return
            fixed, problems = 0, []
            for b, names in sets.items():
                target = os.path.join(self.folder, b + ".pdf")
                for n in names:
                    extra = os.path.join(self.folder, n)
                    try:
                        combine_into(target, extra)
                        move_to_originals(extra)
                        write_log(self.folder, n, os.path.basename(target), "combined duplicate")
                    except Exception as e:
                        problems.append(f"{n}: {e}")
                        break
                self.settings.mark_handled(self.folder, os.path.basename(target))
                fixed += 1
            self.settings.save()
            msg = f"Done: {fixed} invoice{'s are' if fixed != 1 else ' is'} now one PDF each."
            if problems:
                msg += "\n\nThese couldn't be combined (maybe open in another program):\n" + "\n".join(problems)
            messagebox.showinfo(APP, msg)

        def show_message(self, title, text, button=None, command=None):
            self.canvas.pack_forget()
            self.side.pack_forget()
            for w in self.message.winfo_children():
                w.destroy()
            self.message.pack(side="left", fill="both", expand=True)
            inner = tk.Frame(self.message, bg=BG)
            inner.place(relx=0.5, rely=0.42, anchor="center")
            tk.Label(inner, text=title, font=(UI, 26, "bold"), bg=BG, fg=INK).pack(pady=(0, 10))
            tk.Label(inner, text=text, font=(UI, 14), bg=BG, fg=MUTED, justify="center").pack()
            if button:
                tk.Button(inner, text=button, font=(UI, 14, "bold"), bg=GREEN, fg="white", relief="flat",
                          activebackground=GREEN_D, activeforeground="white", padx=24, pady=10,
                          command=command, cursor="hand2").pack(pady=24)
            self.current = None
            self.info = None
            self.var.set("")
            self.file_lbl.config(text="")
            self.conf_lbl.config(text="")
            self.zoom.config(image="", text="")
            for w in self.others.winfo_children():
                w.destroy()
            self.others_title.config(text="")
            self.accept_btn.config(state="disabled")
            self.progress.config(text="")

        def _show_canvas(self):
            if not self.canvas.winfo_ismapped():
                self.message.pack_forget()
                self.side.pack(side="right", fill="y")
                self.canvas.pack(side="left", fill="both", expand=True, padx=(0, 14))

        def show_current(self):
            if self.index >= len(self.files):
                self.batch = None
                if self.done_count:
                    self.show_message("All done!  🎉", f"{self.done_count} file{'s' if self.done_count != 1 else ''} saved.\n\n"
                                      "Scan the next batch, then press the button.", "⟳  Check for new scans", self.refresh)
                else:
                    self.show_message("No new scans", "There are no new scanned files in this folder.\n\n"
                                      "Scan the PODs, wait for them to show up in OneDrive,\nthen press the button.",
                                      "⟳  Check for new scans", self.refresh)
                return
            self._show_canvas()
            path = self.files[self.index]
            self.current = path
            n = self._pages(path)
            if n > 1:
                if not self.batch or self.batch["path"] != path:
                    self.batch = {"path": path, "n": n, "page": 0, "assign": [None] * n,
                                  "angles": [0] * n, "learned": [None] * n}
                    for p in range(n):
                        self._request((path, p), prio=1 if p < 3 else 2)
                b = self.batch
                key = (path, b["page"])
                self.progress.config(text=f"File {self.index + 1} of {len(self.files)}   ·   Page {b['page'] + 1} of {n}")
                self.file_lbl.config(text=f"Scanned file:  {os.path.basename(path)}")
                self.batch_row.pack(side="bottom", fill="x", pady=(0, 6), after=self.hide_btn, **self._batch_row_pad)
                self.leave_btn.pack(side="left", expand=True, fill="x", padx=(6, 0))
                self.same_btn.config(state="normal" if self._prev_invoice() else "disabled")
                last = b["page"] == n - 1
                self.accept_btn.config(text="✔  Save the invoices   (Enter)" if last else "✔  Next page   (Enter)")
                self._update_batch_summary()
            else:
                self.batch = None
                key = (path, None)
                self.progress.config(text=f"File {self.index + 1} of {len(self.files)}")
                self.file_lbl.config(text=f"Scanned file:  {os.path.basename(path)}")
                self.batch_lbl.config(text="")
                self.leave_btn.pack_forget()
                if self._prev_invoice():
                    self.batch_row.pack(side="bottom", fill="x", pady=(0, 6), after=self.hide_btn, **self._batch_row_pad)
                    self.same_btn.config(state="normal")
                else:
                    self.batch_row.pack_forget()
                self.accept_btn.config(text="✔  Rename & next   (Enter)")
            self.cur_key = key
            self._request(key, prio=0)
            for f in self.files[self.index + 1:self.index + 3]:
                self._request(self._key_for_first_page(f), prio=3)
            self._wait_for(key)

        def _prev_invoice(self):
            """The invoice the page before this one went into: earlier in this scan, or the
            last page of the file just before (an invoice can run over into the next scan)."""
            b = self.batch
            if b:
                for p in range(b["page"] - 1, -1, -1):
                    if b["assign"][p]:
                        return b["assign"][p]
            if self.last_done and self.last_done["index"] == self.index - 1:
                return self.last_done["inv"]
            return None

        def _update_batch_summary(self):
            b = self.batch
            if not b:
                self.batch_lbl.config(text="")
                return
            groups = group_pages(b["assign"][:b["page"]])
            txt = f"Merged scan, {b['n']} pages: each invoice is saved as its own PDF."
            if groups:
                parts = []
                for inv, pages in groups:
                    pg = f"p{pages[0] + 1}" if len(pages) == 1 else f"p{pages[0] + 1}–{pages[-1] + 1}"
                    parts.append(f"{inv} ({pg})")
                more = "…, " if len(parts) > 3 else ""
                txt += f"\nSo far: {more}{', '.join(parts[-3:])}"
            self.batch_lbl.config(text=txt)

        def _wait_for(self, key):
            with self.cache_lock:
                info = self.cache.get(key)
            if key != self.cur_key:
                return
            if info is None:
                self.info = None
                self.canvas.delete("all")
                self.canvas.create_text(self.canvas.winfo_width() // 2, self.canvas.winfo_height() // 2,
                                        text="Reading the page…", font=(UI, 18), fill=MUTED)
                self.var.set("")
                self.conf_lbl.config(text="")
                self.zoom.config(image="", text="")
                self._tk_zoom = None
                for w in self.others.winfo_children():
                    w.destroy()
                self.others_title.config(text="")
                self.accept_btn.config(state="disabled")
                self.after(150, lambda: self._wait_for(key))
                return
            self.accept_btn.config(state="normal")
            self.info = info
            # re-score with everything learned so far (cheap: the page is already read)
            if info.text or info.words:
                info.candidates = find_candidates(info.text, info.words,
                                                  self.settings.data.get("shapes", {}),
                                                  self.settings.data.get("labels", {}),
                                                  info.source.startswith("ocr"))
            self._populate()

        def _populate(self):
            info = self.info
            cands = info.candidates
            best = cands[0] if cands else None
            self.sel_box = best.box if best else None
            self.var.set(best.value if best else "")
            if info.error and not cands:
                self.conf_lbl.config(text=info.error + "\nPlease type the invoice number.", fg=RED)
            elif best is None:
                self.conf_lbl.config(text="Couldn't find a number — click it on the page or type it.", fg=RED)
            elif best.score >= 90:
                self.conf_lbl.config(text=f"✔  Confident: {best.why}.", fg=GREEN)
            elif best.score >= 50:
                self.conf_lbl.config(text=f"Probably right ({best.why}). Please check.", fg=AMBER)
            else:
                self.conf_lbl.config(text=f"Not sure ({best.why}). Please check it.", fg=RED)

            # merged PDF: is this page the next page of the invoice before it?
            prev = self._prev_invoice()
            if prev:
                if best and best.value.upper() == prev.upper():
                    self.conf_lbl.config(text=f"✔  Same invoice as the page before ({prev}) — "
                                              "they'll be saved together in one PDF.", fg=GREEN)
                elif continuation_hint(info.text) or best is None or best.score <= 10:
                    self.var.set(prev)
                    self.sel_box = locate(prev, info.words) if info.words else None
                    self.conf_lbl.config(text=f"Looks like the next page of invoice {prev} — it will go in the same PDF.\n"
                                              "If this is a new invoice, click or type its number.", fg=BLUE)
            if self._prefill is not None:      # coming back with Undo: show what was chosen before
                self.var.set(self._prefill)
                self.sel_box = locate(self._prefill, info.words) if info.words else self.sel_box
                self.conf_lbl.config(text="This is what you chose before.", fg=BLUE)
                self._prefill = None

            for w in self.others.winfo_children():
                w.destroy()
            others = [c for c in cands if c.value != self.var.get()][:3]
            self.others_title.config(text="Other numbers on this page (click to use):" if others else "")
            for n, c in enumerate(others):
                b = tk.Button(self.others, text=c.value, font=(UI, 11), relief="flat", bg="#eef3f8", fg=BLUE,
                              activebackground="#dde8f2", cursor="hand2", padx=8, pady=2,
                              command=lambda c=c: self._use(c.value, c.box))
                b.grid(row=n // 3, column=n % 3, padx=(0, 6), pady=2, sticky="we")
            for col in range(3):
                self.others.grid_columnconfigure(col, weight=1, uniform="o")
            self._draw_page()
            self.entry.focus_set()
            self.entry.icursor("end")
            self.entry.select_range(0, "end")

        def _use(self, value, box):
            self.var.set(value)
            self.sel_box = box
            self.conf_lbl.config(text="Using the number you picked.", fg=BLUE)
            self._draw_page()
            self.entry.focus_set()

        def _update_preview_name(self):
            if not self.current:
                self.newname_lbl.config(text="")
                return
            inv = safe_name(self.var.get())
            if not inv:
                self.newname_lbl.config(text="")
                return
            p = self.settings.data
            folder = os.path.dirname(self.current)
            target = invoice_pdf_path(folder, inv, p.get("prefix", ""), p.get("suffix", ""))
            exists = os.path.exists(target) and os.path.normcase(target) != os.path.normcase(self.current)
            name = os.path.basename(target)
            if exists:
                n = self._count(target)
                self.newname_lbl.config(text=f"Will be added to:  {name}  (it already has {n} page{'s' if n != 1 else ''})")
            elif self.batch:
                self.newname_lbl.config(text=f"This page goes in:  {name}")
            else:
                ext = os.path.splitext(self.current)[1].lower()
                self.newname_lbl.config(text=f"Will be saved as:  {os.path.splitext(name)[0]}{ext}")

        def _count(self, path):
            fp = fingerprint(path)
            if self._count_cache.get(path, (None,))[0] != fp:
                self._count_cache[path] = (fp, pdf_page_count(path))
            return self._count_cache[path][1]

        # ---------- drawing ----------
        def _draw_page(self):
            self.canvas.delete("all")
            info = self.info
            if not info or info.image is None:
                return
            cw, ch = max(self.canvas.winfo_width(), 50), max(self.canvas.winfo_height(), 50)
            img = info.image
            s = min((cw - 20) / img.width, (ch - 20) / img.height)
            w, h = max(int(img.width * s), 1), max(int(img.height * s), 1)
            ox, oy = (cw - w) // 2, (ch - h) // 2
            self._scale, self._offset = s, (ox, oy)
            self._tk_img = ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))
            self.canvas.create_image(ox, oy, anchor="nw", image=self._tk_img)
            if self.sel_box:
                x0, y0, x1, y1 = self.sel_box
                self.canvas.create_rectangle(ox + x0 * s - 5, oy + y0 * s - 4, ox + x1 * s + 5, oy + y1 * s + 4,
                                             outline=HILITE, width=4)
            self._draw_zoom()

        def _draw_zoom(self):
            info = self.info
            if not info or info.image is None:
                self.zoom.config(image="", text="")
                return
            img = info.image
            if self.sel_box:
                x0, y0, x1, y1 = self.sel_box
                bw, bh = x1 - x0, y1 - y0
                mx, my = max(bw * 1.2, 160), max(bh * 2.5, 50)
                crop = (max(0, x0 - mx), max(0, y0 - my), min(img.width, x1 + mx), min(img.height, y1 + my))
            else:
                crop = (img.width * 0.45, 0, img.width, img.height * 0.3)   # top-right, where invoice no. usually is
            part = img.crop(tuple(int(v) for v in crop))
            maxw, maxh = 400, (120 if self.winfo_height() >= 780 else 80)
            s = min(maxw / part.width, maxh / part.height, 3.0)
            part = part.resize((max(int(part.width * s), 1), max(int(part.height * s), 1)), Image.LANCZOS)
            if self.sel_box:
                from PIL import ImageDraw
                d = ImageDraw.Draw(part)
                x0, y0, x1, y1 = self.sel_box
                d.rectangle([(x0 - crop[0]) * s - 4, (y0 - crop[1]) * s - 3, (x1 - crop[0]) * s + 4, (y1 - crop[1]) * s + 3],
                            outline=HILITE, width=3)
            self._tk_zoom = ImageTk.PhotoImage(part)
            self.zoom.config(image=self._tk_zoom, height=maxh)

        def _on_click(self, event):
            info = self.info
            if not info or not info.words:
                return
            s, (ox, oy) = self._scale, self._offset
            x, y = (event.x - ox) / s, (event.y - oy) / s
            best, bestd = None, 1e9
            for w in info.words:
                x0, y0, x1, y1 = w.box
                pad = 6 / s
                if x0 - pad <= x <= x1 + pad and y0 - pad <= y <= y1 + pad:
                    d = abs((x0 + x1) / 2 - x) + abs((y0 + y1) / 2 - y)
                    if d < bestd:
                        best, bestd = w, d
            if best:
                val = best.text.strip().strip(".,:;#()[]")
                self._use(_clean_value(val) if re.search(r"\d", val) else val, best.box)

        # ---------- actions ----------
        def _learn_current(self, inv):
            """Learn the number's format and the label printed beside it. Returns the label learned."""
            self.settings.learn(inv)
            phrase = ""
            if self.info and self.info.words:
                box = self.sel_box or locate(inv, self.info.words)
                phrase = label_for(box, self.info.words)
                # only learn labels that aren't already built in
                if phrase and not any(re.fullmatch(lbl + r"[ \t#:]*", phrase, re.I) for lbl, _, _ in LABELS):
                    self.settings.learn_label(phrase)
                else:
                    phrase = ""
            return phrase

        def accept(self):
            if not self.current or self.info is None:   # still reading the page
                return
            inv = safe_name(self.var.get())
            if not inv:
                self.conf_lbl.config(text="Please type the invoice number first.", fg=RED)
                self.entry.focus_set()
                return
            if self.batch:
                b = self.batch
                p = b["page"]
                b["assign"][p] = inv
                b["angles"][p] = self.info.angle
                # learn only from pages with their own number, not continuation pages
                own = locate(inv, self.info.words) is not None if self.info.words else False
                if own and not (p and b["assign"][p - 1] and b["assign"][p - 1].upper() == inv.upper()):
                    b["learned"][p] = (inv, self._learn_current(inv))
                    self.settings.save()
                self._next_page()
                return
            self._rename_single(inv)

        def _next_page(self):
            b = self.batch
            if b["page"] < b["n"] - 1:
                b["page"] += 1
                self.show_current()
            else:
                self._finish_batch()

        def same_as_prev(self):
            prev = self._prev_invoice()
            if not prev:
                return
            self.var.set(prev)
            self.sel_box = locate(prev, self.info.words) if self.info and self.info.words else None
            self.conf_lbl.config(text=f"This page will go in the same file as {prev}. Press Enter.", fg=BLUE)
            self._draw_page()
            self.entry.focus_set()

        def leave_out(self):
            if not self.batch or self.info is None:
                return
            self.batch["assign"][self.batch["page"]] = None
            self._next_page()

        def _finish_batch(self):
            b = self.batch
            path = b["path"]
            folder = os.path.dirname(path)
            groups = group_pages(b["assign"])
            if not groups:
                messagebox.showinfo(APP, "Every page was left out, so nothing was saved from this file.")
                self.batch = None
                self.index += 1
                self.show_current()
                return
            p = self.settings.data
            try:
                saved = split_pdf(path, b["assign"], b["angles"], folder, p.get("prefix", ""), p.get("suffix", ""))
            except FileNotFoundError:
                messagebox.showwarning(APP, "This file isn't there any more (maybe it was moved). Skipping it.")
                self.batch = None
                self.index += 1
                self.show_current()
                return
            except Exception as e:
                messagebox.showerror(APP, f"Couldn't save the invoices from this file.\n\n({e})")
                return
            moved = None
            try:
                moved = move_to_originals(path)
            except Exception as e:
                self.settings.mark_handled(folder, os.path.basename(path))
                messagebox.showwarning(APP, "The invoices were saved, but the original file couldn't be moved to "
                                            f"the Originals folder (it may be open somewhere). It won't come up again.\n\n({e})")
            for d in saved:
                self.settings.mark_handled(folder, os.path.basename(d["path"]))
                write_log(folder, f"{os.path.basename(path)} (page {', '.join(str(x + 1) for x in d['pages'])})",
                          os.path.basename(d["path"]), "split" if d["old"] is None else "added pages")
            self.settings.save()
            self.undo_stack.append({"kind": "split", "path": path, "moved": moved, "saved": saved,
                                    "batch": b, "index": self.index, "last_done": self.last_done})
            self.done_count += len(saved)
            added = [d for d in saved if d["old"] is not None]
            self.flash(f"✔  Saved {len(saved)} invoice{'s' if len(saved) != 1 else ''}"
                       + (f" ({len(added)} added to an existing PDF)" if added else "")
                       + ("  ·  original is in 'Originals'" if moved else ""))
            last = [a for a in b["assign"] if a]
            self._remember_last(last[-1] if last else None)
            self.batch = None
            self.index += 1
            self.show_current()

        def _rename_single(self, inv):
            old = self.current
            folder = os.path.dirname(old)
            ext = os.path.splitext(old)[1].lower()
            p = self.settings.data
            target = invoice_pdf_path(folder, inv, p.get("prefix", ""), p.get("suffix", ""))
            if os.path.exists(target) and os.path.normcase(target) != os.path.normcase(old):
                self._add_to_existing(inv, old, target)
                return
            base = safe_name(f"{p.get('prefix', '')}{inv}{p.get('suffix', '')}")
            new = unique_path(folder, base, ext, old)
            try:
                if os.path.normcase(new) != os.path.normcase(old):
                    rename_with_retry(old, new)
            except FileNotFoundError:
                messagebox.showwarning(APP, "This file isn't there any more (maybe it was moved or renamed already). Skipping it.")
                self.index += 1
                self.show_current()
                return
            except Exception as e:
                messagebox.showerror(APP, "Couldn't rename this file.\n\nIt might be open in another program — "
                                          f"close it and press Enter again.\n\n({e})")
                return
            write_log(folder, os.path.basename(old), os.path.basename(new))
            self.settings.mark_handled(folder, os.path.basename(new))
            phrase = self._learn_current(inv)
            self.settings.save()
            self.undo_stack.append((old, new, inv, self.index, phrase, self.last_done))
            self._remember_last(inv)
            self.done_count += 1
            note = ""
            if os.path.basename(new).lower() != (base + ext).lower():
                note = f"Note: {base}{ext} already existed, so this one was saved as {os.path.basename(new)}"
            self.index += 1
            self.show_current()
            if note:
                messagebox.showinfo(APP, note)

        def _add_to_existing(self, inv, src, target):
            """This scan is another page of an invoice that already has a PDF: add it to that PDF."""
            folder = os.path.dirname(src)
            try:
                old_bytes = combine_into(target, src, None, [self.info.angle] if self.info else None)
            except FileNotFoundError:
                messagebox.showwarning(APP, "This file isn't there any more (maybe it was moved). Skipping it.")
                self.index += 1
                self.show_current()
                return
            except Exception as e:
                messagebox.showerror(APP, f"Couldn't add this page to {os.path.basename(target)}.\n\n"
                                          f"It might be open in another program — close it and press Enter again.\n\n({e})")
                return
            moved = None
            try:
                moved = move_to_originals(src)
            except Exception:
                self.settings.mark_handled(folder, os.path.basename(src))
            self.settings.mark_handled(folder, os.path.basename(target))
            learned = bool(self.info and self.info.words and locate(inv, self.info.words))
            phrase = self._learn_current(inv) if learned else ""
            self.settings.save()
            write_log(folder, os.path.basename(src), os.path.basename(target), "added pages")
            self.undo_stack.append({"kind": "combine", "src": src, "moved": moved, "target": target, "old": old_bytes,
                                    "inv": inv, "phrase": phrase, "learned": learned, "index": self.index,
                                    "last_done": self.last_done})
            self._remember_last(inv)
            self.done_count += 1
            n = self._count(target)
            self.flash(f"✔  Added to {os.path.basename(target)} — now {n} pages")
            self.index += 1
            self.show_current()

        def skip(self):
            if not self.current:
                return
            self.batch = None
            self.index += 1
            self.show_current()

        def hide(self):
            if not self.current:
                return
            name = os.path.basename(self.current)
            self.settings.mark_handled(os.path.dirname(self.current), name)
            self.settings.save()
            write_log(os.path.dirname(self.current), name, name, "hidden")
            self.undo_stack.append((self.current, self.current, None, self.index, "", self.last_done))
            self.batch = None
            self.index += 1
            self.show_current()

        def _unlearn_page(self, b, p):
            if b["learned"][p]:
                inv, phrase = b["learned"][p]
                self.settings.unlearn(inv)
                self.settings.unlearn_label(phrase)
                b["learned"][p] = None

        def undo(self):
            # inside a merged PDF: step back one page (nothing has been saved yet)
            if self.batch and self.batch["page"] > 0:
                b = self.batch
                b["page"] -= 1
                p = b["page"]
                self._unlearn_page(b, p)
                self.settings.save()
                self._prefill = b["assign"][p] or ""
                b["assign"][p] = None
                self.show_current()
                return
            if not self.undo_stack:
                return
            entry = self.undo_stack.pop()
            if isinstance(entry, dict) and entry.get("kind") == "split":
                self._undo_split(entry)
                return
            if isinstance(entry, dict) and entry.get("kind") == "combine":
                self._undo_combine(entry)
                return
            old, new, inv, idx, phrase, last_done = entry
            folder = os.path.dirname(old)
            self.settings.unmark_handled(folder, os.path.basename(new))   # while it still has that name
            try:
                if os.path.normcase(old) != os.path.normcase(new):
                    rename_with_retry(new, old)
            except Exception as e:
                self.settings.mark_handled(folder, os.path.basename(new))
                self.undo_stack.append(entry)
                messagebox.showerror(APP, f"Couldn't undo that rename.\n\n({e})")
                return
            self._remember_last(last_done["inv"] if last_done else None)
            if last_done:
                self.last_done = last_done
            if inv:
                self.settings.unlearn(inv)
                self.settings.unlearn_label(phrase)
                self.done_count = max(0, self.done_count - 1)
            self.settings.save()
            write_log(folder, os.path.basename(new), os.path.basename(old), "undone")
            with self.cache_lock:
                if (new, None) in self.cache and (old, None) not in self.cache:
                    self.cache[(old, None)] = self.cache[(new, None)]
            self.batch = None
            if old not in self.files:
                self.files.insert(min(idx, len(self.files)), old)
            self.index = self.files.index(old)
            self.show_current()

        def _undo_combine(self, e):
            src, moved, target = e["src"], e["moved"], e["target"]
            folder = os.path.dirname(src)
            try:
                write_bytes_safely(target, e["old"])        # the invoice PDF as it was before
                if moved:
                    rename_with_retry(moved, src)
            except Exception as ex:
                self.undo_stack.append(e)
                messagebox.showerror(APP, f"Couldn't undo that.\n\n({ex})")
                return
            self.settings.unmark_handled(folder, os.path.basename(src))
            self.settings.mark_handled(folder, os.path.basename(target))
            if e["learned"]:
                self.settings.unlearn(e["inv"])
                self.settings.unlearn_label(e["phrase"])
            self.settings.save()
            write_log(folder, os.path.basename(target), os.path.basename(src), "undone")
            self.done_count = max(0, self.done_count - 1)
            self._remember_last(e["last_done"]["inv"] if e["last_done"] else None)
            self.last_done = e["last_done"]
            self.batch = None
            if src not in self.files:
                self.files.insert(min(e["index"], len(self.files)), src)
            self.index = self.files.index(src)
            self.show_current()

        def _undo_split(self, e):
            path, moved = e["path"], e["moved"]
            folder = os.path.dirname(path)
            try:
                if moved:
                    rename_with_retry(moved, path)
            except Exception as ex:
                self.undo_stack.append(e)
                messagebox.showerror(APP, f"Couldn't bring the original file back from the Originals folder.\n\n({ex})")
                return
            for d in e["saved"]:
                if d["old"] is None:
                    self.settings.unmark_handled(folder, os.path.basename(d["path"]))
            undo_saved(e["saved"])     # delete the PDFs it made, restore the ones it added pages to
            for d in e["saved"]:
                if d["old"] is not None:
                    self.settings.mark_handled(folder, os.path.basename(d["path"]))
                write_log(folder, os.path.basename(d["path"]), os.path.basename(path), "split undone")
            self.settings.unmark_handled(folder, os.path.basename(path))
            self._remember_last(e["last_done"]["inv"] if e.get("last_done") else None)
            self.last_done = e.get("last_done")
            self.done_count = max(0, self.done_count - len(e["saved"]))
            b = e["batch"]
            p = b["n"] - 1
            self._unlearn_page(b, p)
            self._prefill = b["assign"][p] or ""
            b["assign"][p] = None
            b["page"] = p
            self.settings.save()
            self.batch = b
            if path not in self.files:
                self.files.insert(min(e["index"], len(self.files)), path)
            self.index = self.files.index(path)
            self.show_current()

    lock = acquire_single_instance()
    if lock is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showinfo(APP, "POD Renamer is already open.\n\nLook for it on the taskbar at the bottom of the screen.")
        root.destroy()
        return
    App().mainloop()
    lock.close()


# --------------------------------------------------------------------------
# Command line: `python pod_renamer.py --check FOLDER` prints what it finds
# without renaming anything (handy for testing on real scans).
# --------------------------------------------------------------------------

def check_folder(folder):
    s = Settings()
    shapes = s.data.get("shapes", {})
    files = sorted(f for f in os.listdir(folder) if os.path.splitext(f)[1].lower() in EXTS)
    for f in files:
        full = os.path.join(folder, f)
        n = pdf_page_count(full)
        for page in ([None] if n == 1 else range(n)):
            t = time.time()
            info = analyse(full, shapes, s.data.get("labels", {}), page)
            best = info.candidates[0] if info.candidates else None
            name = f if page is None else f"{f} p{page + 1}"
            cont = "  (says page 2+)" if continuation_hint(info.text) else ""
            print(f"{name:40s} -> {best.value if best else '(nothing)':18s} score={best.score if best else 0:<4} "
                  f"via {info.source:22s} {time.time() - t:4.1f}s  others={[c.value for c in info.candidates[1:4]]}"
                  + cont + (f"  ERROR: {info.error}" if info.error else ""))


if __name__ == "__main__":
    if len(sys.argv) >= 3 and sys.argv[1] == "--check":
        check_folder(sys.argv[2])
    else:
        check_for_update()
        try:
            run_app()
        except Exception:
            msg = traceback.format_exc()
            if roll_back_update("it crashed: " + msg.strip().splitlines()[-1][:150]):
                try:
                    import tkinter.messagebox as mb
                    mb.showinfo(APP, "The latest update had a problem, so the previous version\n"
                                     "has been put back. Please let whoever set up POD Renamer know.")
                except Exception:
                    pass
                _restart()
            # pythonw has no console, so make sure a crash is visible
            with open(os.path.join(config_dir(), "error-log.txt"), "a", encoding="utf-8") as f:
                f.write(f"\n--- {datetime.datetime.now()} ---\n{msg}")
            try:
                import tkinter.messagebox as mb
                mb.showerror(APP, "Something went wrong:\n\n" + msg[-1500:])
            except Exception:
                pass
            raise

# ---- end of POD Renamer ----
