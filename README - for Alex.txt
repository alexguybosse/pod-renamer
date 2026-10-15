POD RENAMER - setup notes for Alex
==================================

WHAT IT DOES
  Opens each new scanned POD in the folder, finds the invoice number on the page
  (PDF text if the copier made a searchable PDF, otherwise Windows' built-in OCR),
  highlights it, and renames the file to it when she presses Enter.
  e.g.  SKM_C25826100212301.pdf  ->  IN004512.pdf

  - Wrong guess: she clicks the right number on the page (or types it) and presses Enter.
  - It learns: every confirmed rename teaches it that supplier's number format and the
    label printed next to it (e.g. "Our Ref"), so it gets more accurate each week.
  - Upside-down / sideways scans are turned the right way automatically.
  - On scans, 1 and I (or 0 and O) can look alike, e.g. SI625126 read as S1625126. The first time a
    number format like that is seen it shows ORANGE (please check). Once she has corrected it a few
    times it fixes it automatically and shows green.
  - ONE PDF PER INVOICE: if an invoice already has a PDF in the folder, new pages with that
    number are ADDED to it (never a separate "(2)" file). Works for one-file-per-page scans,
    merged scans, and invoices that run over from one scan batch into the next one.
    The screen says "Will be added to: INV123.pdf (it already has 2 pages)" before she presses Enter.
  - A page with no number of its own right after an invoice (e.g. "Page 3 of 3") is assumed to
    belong to that invoice, even across scan batches and app restarts.
  - "Undo last" puts the original name back. Every rename is also logged.
  - Nothing leaves her computer: no internet, no cloud OCR.

WHAT COUNTS AS "ALREADY DONE" (so it never shows finished work again)
  - Files it renamed, saved or hid (remembered by size+date, NOT by the scan's name, because scans
    named after the company, e.g. "ASHAK MIX.pdf", reuse the same name every batch).
  - Any file already named like an invoice number (INV839659.pdf, INV839659 (2).pdf, or any
    format it has learned), even if it was renamed by hand or on another computer.
  - Files in the Originals subfolder.
  - Only ONE POD Renamer window can be open; a second double-click just says it's already open.
  - "Skip for now" files DO come back on the next check (that's what skip means);
    use "Hide this file" for anything that should never come up.
  - Scans that reuse a name (e.g. every batch is called "ASHAK MIX.pdf"): "Check for new scans"
    reads every file fresh, so no need to close and reopen the app.
  - Old "INV123 (2).pdf / INV123 (3).pdf" files: when it finds them it offers to combine each set into
    one PDF. Only files named like an invoice number; scans like "ASHAK MIX (1).pdf" are never touched (extras go to Originals, nothing is deleted). Say No and it won't ask about them again.

MERGED SCANS (several invoices in one PDF)
  A PDF with more than one page is treated as a merged scan. She checks each page's
  number and presses Enter, like normal. At the last page it saves one PDF per invoice
  in the same folder and moves the merged original into an "Originals" subfolder.
  - Pages with the same invoice number are kept together in one file.
  - A page with no invoice number of its own (e.g. "Page 2 of 2" of a big order) is
    assumed to belong to the invoice before it; the screen says so in blue.
  - "Same invoice as page before" / "Leave page out" buttons cover the odd cases
    (a blank back page, a cover sheet).
  - Undo steps back one page; after saving, Undo removes the split files and puts
    the original back.
  - Upside-down pages are saved the right way up.

SETUP ON HER PC (about 5 minutes, do it yourself the first time)
  1. Copy this whole folder onto her PC (e.g. Downloads) and unzip it.
  2. Double-click  "SETUP - double-click me first.bat".
     It installs Python if needed (via winget), installs the helpers
     (pymupdf, pillow, winocr), and puts a "Rename PODs" icon on the desktop.
     If Windows SmartScreen warns you: More info -> Run anyway.
  3. Double-click "Rename PODs" on the desktop -> "Choose folder" -> pick the
     OneDrive folder the copier scans into.
     If the folder already has older files it asks whether those are already done.
     Answer Yes so she only sees today's and new scans.
  4. Do the first batch WITH her: the first few guesses for each supplier
     can be orange/red until it has learned their layout.

WORTH CHECKING ON THE COPIER
  If the copier's scan settings have "Searchable PDF" / "OCR" / "PDF (text)",
  turn it on. Then the tool reads the copier's own text instantly and more
  accurately. It still works without it, just a second or so slower per page.

BEFORE YOU HAND IT OVER (optional sanity test)
  Open Command Prompt in the install folder and run:
      py "%LOCALAPPDATA%\POD Renamer\pod_renamer.py" --check "C:\path\to\scan\folder"
  It prints what it would name each file, without renaming anything.

WHERE THINGS LIVE
  Program:            %LOCALAPPDATA%\POD Renamer\pod_renamer.py
  Settings + learned: %APPDATA%\POD Renamer\settings.json
  Rename log:         %APPDATA%\POD Renamer\rename-log.csv   (opens in Excel)
  Crash log:          %APPDATA%\POD Renamer\error-log.txt

OPTIONS (edit settings.json with Notepad while the app is closed)
  "prefix": ""   text in front of the number, e.g. "POD "   -> POD IN004512.pdf
  "suffix": ""   text after the number,       e.g. " POD"   -> IN004512 POD.pdf

TROUBLESHOOTING
  - "Could not read the text on this page ... Windows OCR not available":
    the English OCR pack is missing. In PowerShell (as Administrator):
        Add-WindowsCapability -Online -Name "Language.OCR~~~en-US~0.0.1.0"
    Or install Tesseract (github.com/UB-Mannheim/tesseract/wiki) and run
        py -m pip install pytesseract
    Either works. The tool tries Windows OCR first, then Tesseract.
  - "Couldn't rename this file": it's open in another program (Adobe/Edge)
    or OneDrive is mid-sync. Close it and press Enter again.
  - A file she doesn't want to deal with: "Skip for now" (comes back next time)
    or "Hide this file" (never shown again).
  - Updating the program: it updates itself from GitHub, see UPDATES below.
    Her learned settings are kept.

UPDATES (from anywhere, through GitHub: github.com/alexguybosse/pod-renamer)
  Every time the app opens it checks version.txt on GitHub. If that number is higher than
  its own, it downloads the new pod_renamer.py, checks it, keeps the old one as a backup
  and restarts into the new one (takes a second or two). The window title shows the version.
  To send an update:
    1. Change pod_renamer.py on GitHub (test it on your own laptop first).
    2. Raise   VERSION = "1.1.0"   near the top of pod_renamer.py, e.g. to "1.1.1".
    3. Put the SAME number in version.txt.  (Numbers not matching = no update, on purpose.)
    4. Leave the last line "# ---- end of POD Renamer ----" in place (it proves the
       download is complete).
  Safety:
    - No internet / GitHub blocked: she just keeps using the version she has.
    - New version crashes on start: the previous one is put back automatically, and that
      version number is never installed again (raise the number to try again).
    - New version needs another Python package (REQUIRES near the top): it doesn't install;
      the user sees "whoever set it up needs to run SETUP". Run SETUP on that PC.
    - Log: %APPDATA%\POD Renamer\update-log.txt     Backup: pod_renamer.py.previous
    - To stop updates on a PC: add  "auto_update": false  to settings.json.

UNINSTALL
  Delete the desktop icon, %LOCALAPPDATA%\POD Renamer and %APPDATA%\POD Renamer.
