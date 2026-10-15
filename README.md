# POD Renamer

Renames scanned Proofs of Delivery (PODs) to their invoice numbers. It reads each page, suggests the invoice number, and saves **one PDF per invoice**. Merged scans are split up, and invoices that run over several pages or scans are joined. Windows only.

## Install on a new PC
1. Click the green **Code** button → **Download ZIP**, and unzip it (right-click → *Extract all*).
2. In the unzipped folder, double-click **`SETUP - double-click me first.bat`**. If Windows warns you, click *More info → Run anyway*.
3. Double-click **Rename PODs** on the desktop and choose the folder the scans arrive in.

After setup, the downloaded folder can be deleted. `How to use POD Renamer.pdf` is a one-page guide to print for the people using it.

## Updates
Every installed copy checks `version.txt` here each time it opens. If the number is higher than its own, it downloads the new `pod_renamer.py` and restarts into it.

To release an update:
1. Edit `pod_renamer.py` (test it on your own PC first).
2. Raise `VERSION = "…"` near the top of `pod_renamer.py`.
3. Put **the same number** in `version.txt`.

Nothing is sent out until both numbers match and are higher than before, so half-finished edits never reach anyone. Keep the last line `# ---- end of POD Renamer ----`.

If an update fails to start, the PC goes back to the previous version by itself. See `README - for Alex.txt` for details and logs.
