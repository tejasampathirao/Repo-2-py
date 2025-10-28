# pin_full_merge_with_db_v3_split_datetime_no_tb_export.py
# Full merged system with:
#  - Separate Date / Time columns in DB & exports
#  - GUI keeps separate Top / Bottom buttons
#  - DB still stores is_top/is_bottom (for future use)
#  - Excel/PDF exports DO NOT include Top/Bottom columns
#  - Backward-compatible DB migration + robust Excel/PDF writes
#  - BIG custom Date-range dialog with live preview table + Download/Cancel

import sys
import os
import math
import cv2
import numpy as np
import sqlite3
from datetime import datetime

# Reporting dependencies
try:
    import pandas as pd
    from reportlab.lib.pagesizes import A4, landscape
    from reportlab.lib import colors
    from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
    from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer
    from reportlab.lib.units import mm
except Exception:
    pd = None
    SimpleDocTemplate = None

from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFileDialog, QMessageBox, QDialog, QGridLayout, QDateEdit,
    QTableWidget, QTableWidgetItem, QHeaderView, QSpacerItem, QSizePolicy
)
from PySide6.QtGui import QPixmap, QImage, QFont
from PySide6.QtCore import Qt, QDate

DB_FILENAME = "pin_detection.db"
EXCEL_FILENAME = "pin_report.xlsx"
PDF_FILENAME = "pin_report.pdf"

# ----------------------------
# ---- Small path helpers ----
# ----------------------------
def _ensure_dir_writable(path_dir: str) -> str:
    """Return a writable directory path; fallback to ~/Documents if needed."""
    try:
        if not path_dir:
            path_dir = os.getcwd()
        os.makedirs(path_dir, exist_ok=True)
        test_file = os.path.join(path_dir, f"__writetest_{os.getpid()}.tmp")
        with open(test_file, "wb") as f:
            f.write(b"ok")
        os.remove(test_file)
        return path_dir
    except Exception:
        docs = os.path.expanduser("~/Documents")
        try:
            os.makedirs(docs, exist_ok=True)
            test_file = os.path.join(docs, f"__writetest_{os.getpid()}.tmp")
            with open(test_file, "wb") as f:
                f.write(b"ok")
            os.remove(test_file)
            return docs
        except Exception:
            return os.getcwd()

def _timestamped(path: str) -> str:
    base, ext = os.path.splitext(path)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{base}_{ts}{ext}"

def _atomic_write_dataframe_to_excel(df, target_path: str):
    """Write Excel via temp + atomic replace; fall back to timestamp if locked."""
    target_dir = os.path.dirname(target_path)
    target_dir = _ensure_dir_writable(target_dir)
    tmp_path = os.path.join(target_dir, f".tmp_{os.getpid()}_{os.path.basename(target_path)}")
    try:
        df.to_excel(tmp_path, index=False)
        os.replace(tmp_path, target_path)
        return target_path
    except PermissionError:
        ts_path = _timestamped(target_path)
        df.to_excel(ts_path, index=False)
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        return ts_path
    except Exception:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass
        raise

def _atomic_build_pdf(build_fn, target_path: str):
    """
    Run a callable that writes a PDF to 'tmp', then atomically replace target.
    If the target is locked, write to a timestamped file instead.
    """
    target_dir = os.path.dirname(target_path)
    target_dir = _ensure_dir_writable(target_dir)
    tmp_path = os.path.join(target_dir, f".tmp_{os.getpid()}_{os.path.basename(target_path)}")
    try:
        build_fn(tmp_path)
        try:
            os.replace(tmp_path, target_path)
            return target_path
        except PermissionError:
            ts_path = _timestamped(target_path)
            os.replace(tmp_path, ts_path)
            return ts_path
    except PermissionError:
        ts_path = _timestamped(target_path)
        build_fn(ts_path)
        return ts_path
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except Exception:
            pass

# ----------------------------
# ---- Database Manager ------
# ----------------------------
class DatabaseManager:
    def __init__(self, db_path=DB_FILENAME):
        self.db_path = db_path
        self._conn = None
        self._ensure_db()

    def _connect(self):
        if self._conn is None:
            self._conn = sqlite3.connect(self.db_path, timeout=10)
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _has_column(self, table: str, column: str) -> bool:
        conn = self._connect()
        cur = conn.cursor()
        cur.execute(f"PRAGMA table_info({table});")
        cols = [r[1] for r in cur.fetchall()]
        return column in cols

    def _ensure_db(self):
        conn = self._connect()
        cur = conn.cursor()
        # Base table (original)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS pin_inspection_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            image_name TEXT,
            timestamp TEXT,        -- legacy combined
            view TEXT,             -- "Top" | "Bottom" | "Side"
            pin1_to_pin2_px REAL,
            pin2_to_pin3_px REAL,
            pin3_to_pin1_px REAL,
            pin1_to_pin2_mm REAL,
            pin2_to_pin3_mm REAL,
            pin3_to_pin1_mm REAL,
            centroid_distance_mm REAL,
            result TEXT,
            heights_text TEXT,
            accuracy REAL
            -- New columns via migration:
            -- date TEXT (YYYY-MM-DD)
            -- time TEXT (HH:MM:SS)
            -- is_top INTEGER (0/1)
            -- is_bottom INTEGER (0/1)
        )
        """)
        conn.commit()
        self._migrate_schema()

    def _migrate_schema(self):
        """Add new columns if missing: date, time, is_top, is_bottom."""
        conn = self._connect()
        cur = conn.cursor()
        if not self._has_column('pin_inspection_data', 'date'):
            cur.execute("ALTER TABLE pin_inspection_data ADD COLUMN date TEXT")
        if not self._has_column('pin_inspection_data', 'time'):
            cur.execute("ALTER TABLE pin_inspection_data ADD COLUMN time TEXT")
        if not self._has_column('pin_inspection_data', 'is_top'):
            cur.execute("ALTER TABLE pin_inspection_data ADD COLUMN is_top INTEGER DEFAULT 0")
        if not self._has_column('pin_inspection_data', 'is_bottom'):
            cur.execute("ALTER TABLE pin_inspection_data ADD COLUMN is_bottom INTEGER DEFAULT 0")
        conn.commit()

    def insert_record(self,
                      image_name,
                      view,
                      pin1_to_pin2_px=None,
                      pin2_to_pin3_px=None,
                      pin3_to_pin1_px=None,
                      pin1_to_pin2_mm=None,
                      pin2_to_pin3_mm=None,
                      pin3_to_pin1_mm=None,
                      centroid_distance_mm=None,
                      result=None,
                      heights_text=None,
                      accuracy=None,
                      date_str=None,
                      time_str=None,
                      is_top=False,
                      is_bottom=False):
        conn = self._connect()
        cur = conn.cursor()

        now = datetime.now()
        ts = now.strftime("%Y-%m-%d %H:%M:%S")
        if date_str is None:
            date_str = now.strftime("%Y-%m-%d")
        if time_str is None:
            time_str = now.strftime("%H:%M:%S")

        cur.execute("""
            INSERT INTO pin_inspection_data
            (image_name, timestamp, view,
             pin1_to_pin2_px, pin2_to_pin3_px, pin3_to_pin1_px,
             pin1_to_pin2_mm, pin2_to_pin3_mm, pin3_to_pin1_mm,
             centroid_distance_mm, result, heights_text, accuracy,
             date, time, is_top, is_bottom)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            image_name, ts, view,
            _none_or_float(pin1_to_pin2_px),
            _none_or_float(pin2_to_pin3_px),
            _none_or_float(pin3_to_pin1_px),
            _none_or_float(pin1_to_pin2_mm),
            _none_or_float(pin2_to_pin3_mm),
            _none_or_float(pin3_to_pin1_mm),
            _none_or_float(centroid_distance_mm),
            result, heights_text, _none_or_float(accuracy),
            date_str, time_str, 1 if is_top else 0, 1 if is_bottom else 0
        ))
        conn.commit()
        return cur.lastrowid

    def fetch_all(self):
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("SELECT * FROM pin_inspection_data ORDER BY id ASC")
        rows = cur.fetchall()
        return rows

    def fetch_between(self, start_date: str, end_date: str):
        """Fetch rows where date is between start_date and end_date (inclusive)."""
        conn = self._connect()
        cur = conn.cursor()
        cur.execute("""
            SELECT * FROM pin_inspection_data
            WHERE date IS NOT NULL AND date <> ''
              AND date >= ? AND date <= ?
            ORDER BY id ASC
        """, (start_date, end_date))
        return cur.fetchall()

    def export_reports(self, excel_path=None, pdf_path=None, start_date: str=None, end_date: str=None):
        """Exports DB to Excel and multi-page PDF. If start/end dates are provided,
           export only rows within [start_date, end_date] inclusive.
           Atomic & lock-safe on Windows."""
        if pd is None or SimpleDocTemplate is None:
            raise RuntimeError("pandas and reportlab are required for exporting. Install via: pip install pandas reportlab")

        base_dir = os.path.dirname(os.path.abspath(self.db_path))
        base_dir = _ensure_dir_writable(base_dir)

        excel_path = (excel_path or os.path.join(base_dir, EXCEL_FILENAME))
        pdf_path   = (pdf_path   or os.path.join(base_dir, PDF_FILENAME))

        if start_date and end_date:
            rows = self.fetch_between(start_date, end_date)
        else:
            rows = self.fetch_all()

        # NOTE: Top/Bottom columns intentionally excluded from export
        cols = [
            'id','image_name','date','time','timestamp','view',
            'pin1_to_pin2_px','pin2_to_pin3_px','pin3_to_pin1_px',
            'pin1_to_pin2_mm','pin2_to_pin3_mm','pin3_to_pin1_mm',
            'centroid_distance_mm','result','heights_text','accuracy'
        ]

        if not rows:
            df = pd.DataFrame(columns=cols)
        else:
            df_all = pd.DataFrame([dict(r) for r in rows])
            for c in cols:
                if c not in df_all.columns:
                    df_all[c] = None
            df = df_all[cols]

        # Append range tag to filenames to avoid overwriting all-data files
        if start_date and end_date:
            tag = f"_{start_date}_to_{end_date}"
            base, ext = os.path.splitext(excel_path)
            excel_path = f"{base}{tag}{ext}"
            base, ext = os.path.splitext(pdf_path)
            pdf_path   = f"{base}{tag}{ext}"

        excel_path_final = _atomic_write_dataframe_to_excel(df, excel_path)

        def _build_pdf(out_path: str):
            self._create_pdf([dict(r) for r in rows] if rows else [], out_path,
                             date_range=(start_date, end_date) if start_date and end_date else None)

        pdf_path_final = _atomic_build_pdf(_build_pdf, pdf_path)
        return excel_path_final, pdf_path_final

    def _create_pdf(self, records, pdf_path, date_range=None):
        """Create a multi-page PDF in LANDSCAPE A4 with wrapping & repeated header."""
        doc = SimpleDocTemplate(
            pdf_path,
            pagesize=landscape(A4),
            rightMargin=12*mm, leftMargin=12*mm,
            topMargin=18*mm,  bottomMargin=12*mm
        )
        styles = getSampleStyleSheet()
        cell_style = ParagraphStyle(
            'Cell8', parent=styles['Normal'],
            fontName='Helvetica', fontSize=8, leading=10,
            spaceAfter=0, spaceBefore=0
        )

        story = []
        title_style = styles['Title']
        title_style.alignment = 1  # center
        story.append(Paragraph("ROBOWORKS AUTOMATION", title_style))
        story.append(Spacer(1, 6))

        # Optional date range subtitle
        if date_range:
            s, e = date_range
            sub = Paragraph(f"Date range: <b>{s}</b> to <b>{e}</b>", styles['Heading4'])
            story.append(sub)
            story.append(Spacer(1, 6))

        # Header (Top/Bottom columns intentionally removed)
        header = [
            'S.No', 'Image Name', 'Date', 'Time', 'View',
            'P1→P2 (px)', 'P2→P3 (px)', 'P3→P1 (px)',
            'P1→P2 (mm)', 'P2→P3 (mm)', 'P3→P1 (mm)',
            'Centroid (mm)', 'Result', 'Heights', 'Accuracy'
        ]
        data = [header]

        def P(x):
            return Paragraph(str("" if x is None else x), cell_style)

        for r in records:
            data.append([
                P(r.get('id')),
                P(r.get('image_name')),
                P(r.get('date')),
                P(r.get('time')),
                P(r.get('view')),
                P(_fmt(r.get('pin1_to_pin2_px'))),
                P(_fmt(r.get('pin2_to_pin3_px'))),
                P(_fmt(r.get('pin3_to_pin1_px'))),
                P(_fmt(r.get('pin1_to_pin2_mm'))),
                P(_fmt(r.get('pin2_to_pin3_mm'))),
                P(_fmt(r.get('pin3_to_pin1_mm'))),
                P(_fmt(r.get('centroid_distance_mm'))),
                P(r.get('result')),
                P(r.get('heights_text')),
                P(_fmt(r.get('accuracy')))
            ])

        avail_w = doc.width
        # 15 columns; percentages sum to 100
        col_pct = [4, 14, 7, 7, 6, 6, 6, 6, 6, 6, 6, 7, 6, 8, 5]
        col_widths = [(pct/100.0)*avail_w for pct in col_pct]

        table = Table(data, colWidths=col_widths, repeatRows=1, hAlign='LEFT')
        table.setStyle(TableStyle([
            ('GRID', (0,0), (-1,-1), 0.4, colors.black),
            ('BACKGROUND', (0,0), (-1,0), colors.HexColor('#dfe7f3')),
            ('ALIGN', (0,0), (-1,0), 'CENTER'),
            ('VALIGN', (0,0), (-1,-1), 'MIDDLE'),
            ('FONTSIZE', (0,0), (-1,-1), 8),
            ('LEFTPADDING', (0,0), (-1,-1), 3),
            ('RIGHTPADDING', (0,0), (-1,-1), 3),
            ('TOPPADDING', (0,0), (-1,-1), 2),
            ('BOTTOMPADDING', (0,0), (-1,-1), 2),
        ]))
        story.append(table)

        def _draw_header(canvas, doc_):
            canvas.saveState()
            w, h = landscape(A4)
            canvas.setFont("Helvetica-Bold", 14)
            canvas.drawCentredString(w/2.0, h - 12*mm, "ROBOWORKS AUTOMATION")
            canvas.setLineWidth(0.6)
            canvas.line(12*mm, h - 14*mm, w - 12*mm, h - 14*mm)
            canvas.restoreState()

        doc.build(story, onFirstPage=_draw_header, onLaterPages=_draw_header)

def _fmt(v):
    if v is None:
        return ""
    try:
        if isinstance(v, float):
            s = ("{:.3f}".format(v)).rstrip('0').rstrip('.')
            return s
        return str(v)
    except Exception:
        return str(v)

def _none_or_float(v):
    if v is None: return None
    try:
        return float(v)
    except Exception:
        return None

# ----------------------------
# ---- S398 (top/bottom) -----
# ----------------------------
class PerfectS398Detector:
    def __init__(self):
        self.target_pcd = 11.66
        self.tolerance = 0.13
        self.scale_mm_per_px = 0.01  # <<--- set your calibrated mm/px
        self.mm_tolerance = 0.18
        self.px_tolerance = self.mm_tolerance / self.scale_mm_per_px

    def preprocess_image(self, img):
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        filtered = cv2.medianBlur(gray, 5)
        enhanced = cv2.equalizeHist(filtered)
        return enhanced

    def detect_pins(self, img):
        processed = self.preprocess_image(img)
        circles = cv2.HoughCircles(
            processed, cv2.HOUGH_GRADIENT, dp=1, minDist=70,
            param1=120, param2=50, minRadius=10, maxRadius=60
        )
        if circles is not None:
            circles = np.round(circles[0, :]).astype("int")
            if len(circles) > 3:
                scores = []
                for (x, y, r) in circles:
                    y0 = max(0, y - r); y1 = min(processed.shape[0], y + r)
                    x0 = max(0, x - r); x1 = min(processed.shape[1], x + r)
                    roi = processed[y0:y1, x0:x1]
                    edges = cv2.Canny(roi, 50, 150) if roi.size > 0 else None
                    scores.append(np.sum(edges) if edges is not None else 0)
                top3 = np.argsort(scores)[-3:]
                circles = circles[top3]
            return circles
        return None

    def calculate_distances(self, pins):
        distances = []
        for i in range(len(pins)):
            for j in range(i + 1, len(pins)):
                x1, y1 = pins[i][:2]
                x2, y2 = pins[j][:2]
                dist = np.sqrt((x2 - x1) ** 2 + (y2 - y1) ** 2)
                distances.append(dist)
        return distances

    def find_outer_circle(self, img_bgr):
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (7,7), 0)
        _, thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        white_area = np.sum(thr == 255)
        black_area = np.sum(thr == 0)
        cleaned = thr if black_area < white_area else cv2.bitwise_not(thr)
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7,7))
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_CLOSE, kernel, iterations=2)
        cleaned = cv2.morphologyEx(cleaned, cv2.MORPH_OPEN, kernel, iterations=1)
        contours, _ = cv2.findContours(cleaned, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if not contours:
            return None, cleaned
        cnt = max(contours, key=cv2.contourArea)
        area = cv2.contourArea(cnt)
        h,w = img_bgr.shape[:2]
        if area < (0.01 * w * h):
            return None, cleaned
        (cx, cy), radius = cv2.minEnclosingCircle(cnt)
        return (int(round(cx)), int(round(cy)), int(round(radius))), cleaned

    def process_image(self, image_path):
        print(f"\n🔍 Processing: {os.path.basename(image_path)}")
        img = cv2.imread(image_path)
        if img is None:
            return None, "❌ Could not load image!", None
        result_img = img.copy()
        pins = self.detect_pins(img)
        if pins is None or len(pins) < 3:
            return result_img, "❌ Could not detect 3 pins!", None

        for i, (x, y, r) in enumerate(pins):
            cv2.circle(result_img, (int(x), int(y)), int(r), (0, 255, 0), 3)
            cv2.circle(result_img, (int(x), int(y)), 3, (0, 0, 255), -1)
            cv2.putText(result_img, f"Pin{i + 1}", (int(x) - 25, int(y) - int(r) - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

        xs = [int(p[0]) for p in pins[:3]]
        ys = [int(p[1]) for p in pins[:3]]
        cx = int(round(sum(xs) / 3.0))
        cy = int(round(sum(ys) / 3.0))

        outer_circle, _ = self.find_outer_circle(img)
        if outer_circle is None:
            cv2.circle(result_img, (cx, cy), 6, (0, 0, 255), -1)
            cv2.putText(result_img, "Outer circle: NOT found", (20, 160),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,0,255), 2)
            return result_img, "❌ Outer circle not found — cannot compute centroid distance", None

        ox, oy, orad = outer_circle
        dist_px = float(np.hypot(float(ox - cx), float(oy - cy)))
        dist_mm = dist_px * float(self.scale_mm_per_px)
        status = "OK" if dist_mm <= self.mm_tolerance else "NG"
        color = (0, 255, 0) if status == "OK" else (0, 0, 255)

        cv2.circle(result_img, (cx, cy), 6, (0, 0, 255), -1)
        cv2.putText(result_img, "Centroid", (cx + 8, cy - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0,0,255), 2)
        cv2.circle(result_img, (ox, oy), 6, (255, 0, 0), -1)
        cv2.putText(result_img, "CircleCtr", (ox + 8, oy - 8),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,0,0), 2)
        cv2.circle(result_img, (ox, oy), int(orad), (200,200,0), 2)
        cv2.line(result_img, (cx, cy), (ox, oy), (0, 255, 255), 2)
        cv2.putText(result_img, f"Dist: {dist_px:.2f}px / {dist_mm:.3f}mm",
                    (20, 100), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,255,0), 2)
        cv2.putText(result_img, f"Tol: {self.mm_tolerance:.3f}mm -> {status}",
                    (20, 140), cv2.FONT_HERSHEY_SIMPLEX, 0.9, color, 3)

        p0 = (float(pins[0][0]), float(pins[0][1]))
        p1 = (float(pins[1][0]), float(pins[1][1]))
        p2 = (float(pins[2][0]), float(pins[2][1]))
        d01_px = float(np.hypot(p1[0]-p0[0], p1[1]-p0[1]))
        d12_px = float(np.hypot(p2[0]-p1[0], p2[1]-p1[1]))
        d20_px = float(np.hypot(p0[0]-p2[0], p0[1]-p2[1]))

        d01_mm = d01_px * float(self.scale_mm_per_px)
        d12_mm = d12_px * float(self.scale_mm_per_px)
        d20_mm = d20_px * float(self.scale_mm_per_px)

        distances = self.calculate_distances(pins)
        avg_distance = np.mean(distances)
        pin_pairs = [(0,1),(1,2),(0,2)]
        for i,(p1i,p2i) in enumerate(pin_pairs):
            x1,y1 = int(pins[p1i][0]), int(pins[p1i][1])
            x2,y2 = int(pins[p2i][0]), int(pins[p2i][1])
            mid_x, mid_y = (x1 + x2)//2, (y1 + y2)//2
            cv2.line(result_img,(x1,y1),(x2,y2),(255,0,255),2)
            cv2.putText(result_img, f"{distances[i]:.1f}px", (mid_x-20, mid_y),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255,0,255), 2)

        cv2.putText(result_img, f"Pins: {len(pins)}/3", (20, 40),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,255,255), 2)
        cv2.putText(result_img, f"Avg Distance: {avg_distance:.2f}px", (20, 60),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255,255,255), 2)

        msg = f"CentroidDist: {dist_mm:.3f} mm ({dist_px:.2f} px) -> {status}"
        print("📏", msg)

        meta = {
            'pin1_to_pin2_px': d01_px,
            'pin2_to_pin3_px': d12_px,
            'pin3_to_pin1_px': d20_px,
            'pin1_to_pin2_mm': d01_mm,
            'pin2_to_pin3_mm': d12_mm,
            'pin3_to_pin1_mm': d20_mm,
            'centroid_distance_mm': dist_mm,
            'result': "Pass" if status == "OK" else "Fail"
        }
        return result_img, f"✅ {msg}", meta

class S398App(QWidget):
    """Top/Bottom specific launcher; 'view_label' must be 'Top' or 'Bottom'."""
    def __init__(self, db_manager: DatabaseManager, view_label: str):
        super().__init__()
        self.setWindowTitle(f"Perfect S398 Pin Detector – {view_label}")
        self.detector = PerfectS398Detector()
        self.db = db_manager
        self.view_label = "Top" if str(view_label).lower().startswith("top") else "Bottom"
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()
        self.upload_btn = QPushButton(f"Upload {self.view_label} Image")
        self.quit_btn = QPushButton("Close")
        self.upload_btn.clicked.connect(self.upload_image)
        self.quit_btn.clicked.connect(self.close)
        layout.addWidget(self.upload_btn)
        layout.addWidget(self.quit_btn)
        layout.setAlignment(Qt.AlignTop)
        self.setLayout(layout)
        self.setFixedSize(900, 700)

    def upload_image(self):
        file_dialog = QFileDialog(self, f"Select S398 Terminal Image - {self.view_label}")
        file_dialog.setNameFilters(["Images (*.jpg *.jpeg *.png *.bmp *.tiff)", "All files (*)"])
        if file_dialog.exec():
            file_path = file_dialog.selectedFiles()[0]
            self.process_image(file_path)

    def process_image(self, file_path):
        result_img, message, meta = self.detector.process_image(file_path)
        if result_img is not None:
            max_w, max_h = 800, 600
            h, w = result_img.shape[:2]
            scale = min(float(max_w) / w, float(max_h) / h, 1.0)
            if scale < 1.0:
                new_w = int(round(w * scale))
                new_h = int(round(h * scale))
                result_img = cv2.resize(result_img, (new_w, new_h), interpolation=cv2.INTER_AREA)

            window_name = f"S398 Detection ({self.view_label}) - {os.path.basename(file_path)}"
            cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
            cv2.imshow(window_name, result_img)
            cv2.waitKey(0)
            cv2.destroyAllWindows()

            try:
                image_name = os.path.basename(file_path)
                rowid = self.db.insert_record(
                    image_name=image_name,
                    view=self.view_label,
                    pin1_to_pin2_px=meta.get('pin1_to_pin2_px'),
                    pin2_to_pin3_px=meta.get('pin2_to_pin3_px'),
                    pin3_to_pin1_px=meta.get('pin3_to_pin1_px'),
                    pin1_to_pin2_mm=meta.get('pin1_to_pin2_mm'),
                    pin2_to_pin3_mm=meta.get('pin2_to_pin3_mm'),
                    pin3_to_pin1_mm=meta.get('pin3_to_pin1_mm'),
                    centroid_distance_mm=meta.get('centroid_distance_mm'),
                    result=meta.get('result'),
                    heights_text=None,
                    accuracy=None,
                    is_top=(self.view_label == "Top"),
                    is_bottom=(self.view_label == "Bottom")
                )
                QMessageBox.information(self, "Result", f"{message}\nSaved to DB (id={rowid}).")
            except Exception as e:
                QMessageBox.warning(self, "DB Error", f"{message}\nBut failed to save to DB:\n{e}")
        else:
            QMessageBox.critical(self, "Error", message)

# ---------------------------------------
# ---- Pin height (side view) pipeline ---
# ---------------------------------------
SAMPLE_PATH = "/mnt/data/IMG_9489.jpg"
EXPECTED_COUNT = 3
ANGLE_TOL_DEG = 6
CANNY1, CANNY2 = 40, 120
HOUGH_TH, MIN_LINE, MAX_GAP = 48, 36, 12
X_BORDER_FRAC = 0.01
X_CLUSTER_GAP_FRAC = 0.014
X_SUPPRESS_FRAC = 0.075
MIN_HEIGHT_PX = 240
HEAD_BAND_UP = 0.20
HEAD_BAND_DN = 0.25
HEAD_R_MAX   = 0.10

def cv_to_qpixmap(img):
    if img is None: return QPixmap()
    if img.ndim == 2:
        h, w = img.shape; q = QImage(img.data, w, h, w, QImage.Format_Grayscale8)
        return QPixmap.fromImage(q)
    rgb = cv2.cvtColor(img, cv2.COLOR_BGR2RGB)
    h, w, c = rgb.shape; q = QImage(rgb.data, w, h, w*c, QImage.Format_RGB888)
    return QPixmap.fromImage(q)

def compute_distance_cv(vals):
    if not vals: return 999.0
    a = np.asarray(vals, np.float32); m = float(a.mean())
    return 999.0 if m == 0 else float(a.std(ddof=0)/m*100.0)

def compute_accuracy_from_cv(cv_percent, n, expected):
    if expected <= 0: return 0.0
    match = 1.0 if n == expected else min(1.0, n/expected)
    return round(max(0.0, 100.0*match*(1.0 - min(1.0, cv_percent/100.0))), 2)

def preprocess(gray):
    bg = cv2.GaussianBlur(gray, (101,101), 0)
    norm = cv2.subtract(gray, bg)
    norm = cv2.normalize(norm, None, 0, 255, cv2.NORM_MINMAX)
    clahe = cv2.createCLAHE(clipLimit=3.0, tileGridSize=(8,8))
    return clahe.apply(norm)

def hough_vertical_clusters(gray_clean):
    h, w = gray_clean.shape
    edges = cv2.Canny(gray_clean, CANNY1, CANNY2)
    lines = cv2.HoughLinesP(edges, 1, np.pi/180, HOUGH_TH, minLineLength=MIN_LINE, maxLineGap=MAX_GAP)
    if lines is None: return []
    x_margin = int(max(6, round(w*X_BORDER_FRAC)))
    segs = []
    for l in lines:
        x1,y1,x2,y2 = map(int, l[0])
        if (x1<x_margin and x2<x_margin) or (x1>w-x_margin and x2>w-x_margin): continue
        dx, dy = x2-x1, y2-y1
        ang = 90.0 if dx==0 else abs(math.degrees(math.atan2(dy, dx)))
        if ang>90: ang = 180-ang
        if abs(90-ang) <= ANGLE_TOL_DEG:
            t, b = max(0,min(y1,y2)), min(h-1,max(y1,y2))
            if b>t: segs.append((0.5*(x1+x2), t, b))
    if not segs: return []
    segs.sort(key=lambda s: s[0])
    gap = int(max(6, round(w*X_CLUSTER_GAP_FRAC)))
    clusters, cur = [], [segs[0]]
    for xm,t,b in segs[1:]:
        if abs(xm - cur[-1][0]) <= gap: cur.append((xm,t,b))
        else: clusters.append(cur); cur=[(xm,t,b)]
    clusters.append(cur)
    info=[]
    for cl in clusters:
        xs=[c[0] for c in cl]; ts=[c[1] for c in cl]; bs=[c[2] for c in cl]
        cx=int(round(sum(xs)/len(xs))); top=int(min(ts)); bot=int(max(bs))
        info.append({'x':cx,'top':top,'bottom':bot,'height':max(0,bot-top)})
    info=[c for c in info if c['height']>=MIN_HEIGHT_PX]
    return info

def suppress_by_x(clusters, w, want=EXPECTED_COUNT):
    if not clusters: return []
    clusters = sorted(clusters, key=lambda c: c['height'], reverse=True)
    out=[]; min_dx = max(8, int(X_SUPPRESS_FRAC*w))
    for c in clusters:
        if all(abs(c['x']-o['x'])>=min_dx for o in out):
            out.append(c)
            if len(out)==want: break
    if len(out)<want:
        rest=[c for c in clusters if c not in out]
        rest.sort(key=lambda c:c['x'])
        for c in rest:
            if all(abs(c['x']-o['x'])>=min_dx for o in out):
                out.append(c)
                if len(out)==want: break
    return sorted(out[:want], key=lambda c:c['x'])

def _pick_best_circle(circles, left, top, target_x):
    if circles is None: return None
    cs = np.round(circles[0,:]).astype(int)
    best=None; bestdx=1e9
    for cx,cy,r in cs:
        fx, fy = left+cx, top+cy
        d = abs(fx-int(target_x))
        if d<bestdx: bestdx=d; best=(fx,fy,int(r))
    return best

def find_head(gray, cx, y_top, y_bot, max_r):
    h,w = gray.shape
    left = max(0, int(cx-2*max_r)); right=min(w, int(cx+2*max_r))
    top  = max(0, int(y_top));      bot  = min(h-1, int(y_bot))
    if right-left<8 or bot-top<8: return None
    roi = gray[top:bot, left:right]
    min_r = max(3, int(0.25*max_r))
    best = _pick_best_circle(
        cv2.HoughCircles(cv2.medianBlur(roi,5), cv2.HOUGH_GRADIENT,1.2,8,
                         param1=85,param2=10,minRadius=min_r,maxRadius=int(max_r*0.9)),
        left, top, cx)
    edges = cv2.Canny(cv2.GaussianBlur(roi,(3,3),0),70,150)
    cand = _pick_best_circle(
        cv2.HoughCircles(edges, cv2.HOUGH_GRADIENT,1.1,8,
                         param1=80,param2=7,minRadius=min_r,maxRadius=int(max_r*0.9)),
        left, top, cx)
    if cand is not None: best = cand if best is None else best
    if best is None: return None
    bx, by, br = best
    by = max(0, by - int(0.30 * br))
    br = int(max(3, br * 0.7))
    return (bx, by, br)

def find_trapezoid_bottom(gray_clean, cx, head_y, head_r, cluster_bottom):
    h,w = gray_clean.shape
    half_w = max(36, int(0.12*(cluster_bottom - head_y + 1)))
    left=max(0, cx-half_w); right=min(w, cx+half_w)
    start = min(h-2, max(0, int(head_y + max(4, 0.4*head_r))))
    end   = min(h-1, int(cluster_bottom))
    if right-left<8 or end-start<8: return None
    roi = gray_clean[start:end+1, left:right]
    gy = cv2.Sobel(roi, cv2.CV_32F, 0, 1, ksize=3)
    row_energy = np.mean(np.abs(gy), axis=1).astype(np.float32)
    if row_energy.size==0 or float(np.max(row_energy))==0: return None
    sm = cv2.GaussianBlur(row_energy.reshape(-1,1),(1,9),0).ravel()
    thr = max(6.0, 0.40*float(np.max(sm)))
    best=None
    for i in range(len(sm)-2, 1, -1):
        if sm[i]>=thr and sm[i]>=sm[i-1] and sm[i]>=sm[i+1]:
            best = i; break
    if best is None:
        best = int(np.argmax(sm))
    y = start + best
    return min(y, int(cluster_bottom))

def detect_and_measure(img_bgr):
    out = img_bgr.copy()
    h, w = out.shape[:2]
    gray = cv2.cvtColor(out, cv2.COLOR_BGR2GRAY)
    clean = preprocess(gray)
    clusters = hough_vertical_clusters(clean)
    clusters = suppress_by_x(clusters, w, EXPECTED_COUNT)
    if len(clusters) < EXPECTED_COUNT:
        xs = [int(w*0.3), int(w*0.5), int(w*0.7)]
        clusters = [{'x':x,'top':int(0.08*h),'bottom':int(0.75*h),'height':int(0.67*h)} for x in xs]
    heights=[]
    for c in clusters:
        cx, top, bot = int(c['x']), int(c['top']), int(c['bottom'])
        height = max(1, bot-top)
        y_top = max(0, top - int(HEAD_BAND_UP*height))
        y_bot = min(h-1, top + int(HEAD_BAND_DN*height))
        max_r = int(max(6, round(HEAD_R_MAX*height)))
        circ = find_head(gray, cx, y_top, y_bot, max_r)
        if circ is None:
            circ_x, circ_y, circ_r = cx, top, int(max_r*0.8)
        else:
            circ_x, circ_y, circ_r = circ
        trap_y = find_trapezoid_bottom(clean, cx, circ_y, circ_r, bot)
        if trap_y is not None and trap_y > circ_y:
            hpx = int(trap_y - circ_y)
            heights.append(hpx)
            cv2.circle(out,(circ_x,circ_y),max(3,int(circ_r)),(0,255,0),2)
            cv2.circle(out,(circ_x,circ_y),2,(0,255,0),-1)
            cv2.line(out,(cx,circ_y),(cx,trap_y),(255,255,0),2)
            cv2.putText(out,f"{hpx}px",(cx+8,(circ_y+trap_y)//2),
                        cv2.FONT_HERSHEY_SIMPLEX,0.5,(0,255,255),2)
        else:
            hpx = int(height)
            heights.append(hpx)
            cv2.line(out,(cx,top),(cx,bot),(0,0,255),2)
            cv2.putText(out,f"{hpx}px (fb)",(cx+8,(top+bot)//2),
                        cv2.FONT_HERSHEY_SIMPLEX,0.45,(0,200,255),2)
    cvp = compute_distance_cv(heights)
    acc = compute_accuracy_from_cv(cvp, len(heights), EXPECTED_COUNT)
    return clean, out, heights, acc

class PinHeightApp(QWidget):
    def __init__(self, db_manager: DatabaseManager):
        super().__init__()
        self.setWindowTitle("Pin Height Measurement (v11 simple – 3 lines)")
        self.setMinimumSize(1200, 600)
        self.db = db_manager
        self.open_btn = QPushButton("Open Image")
        self.sample_btn = QPushButton("Use Sample")
        self.status = QLabel("Heights: – | Accuracy: –")
        self.orig = QLabel(); self.mid = QLabel(); self.out = QLabel()
        for L in (self.orig, self.mid, self.out):
            L.setAlignment(Qt.AlignCenter)
            L.setStyleSheet("border:1px solid #555; background:#111;")
            L.setScaledContents(True)
            L.setMinimumSize(350, 400)
        top = QHBoxLayout()
        top.addWidget(self.open_btn); top.addWidget(self.sample_btn)
        top.addStretch(1); top.addWidget(self.status)
        imgs = QHBoxLayout()
        imgs.addWidget(self.orig); imgs.addWidget(self.mid); imgs.addWidget(self.out)
        lay = QVBoxLayout(); lay.addLayout(top); lay.addLayout(imgs)
        self.setLayout(lay)
        self.open_btn.clicked.connect(self.open_image)
        self.sample_btn.clicked.connect(self.load_sample)
        if os.path.exists(SAMPLE_PATH):
            self.load_and_process(SAMPLE_PATH)

    def open_image(self):
        p,_ = QFileDialog.getOpenFileName(self,"Open Image","","Images (*.png *.jpg *.jpeg *.bmp)")
        if p: self.load_and_process(p)

    def load_sample(self):
        if os.path.exists(SAMPLE_PATH): self.load_and_process(SAMPLE_PATH)
        else: QMessageBox.warning(self,"Error","Sample not found.")

    def load_and_process(self, path):
        data = np.fromfile(path, np.uint8)
        img = cv2.imdecode(data, cv2.IMREAD_COLOR)
        if img is None:
            QMessageBox.critical(self,"Error",f"Cannot load {path}")
            return
        h,w = img.shape[:2]; max_dim = 1200
        if max(h,w)>max_dim:
            s = max_dim/max(h,w); img = cv2.resize(img,(int(w*s), int(h*s)))
        clean, overlay, heights, acc = detect_and_measure(img)
        self.orig.setPixmap(cv_to_qpixmap(img))
        self.mid.setPixmap(cv_to_qpixmap(clean))
        self.out.setPixmap(cv_to_qpixmap(overlay))
        self.status.setText(
            f"Heights: {', '.join(map(str,heights))} px | Accuracy: {acc}%"
            if heights else "No vertical pins detected"
        )
        try:
            image_name = os.path.basename(path)
            heights_text = (f"min/mean/max={min(heights)}/{int(sum(heights)/len(heights))}/{max(heights)} px (n={len(heights)})") if heights else None
            result = "Pass" if (acc is not None and acc >= 80.0) else "Fail"
            rowid = self.db.insert_record(
                image_name=image_name,
                view="Side",
                pin1_to_pin2_px=None,
                pin2_to_pin3_px=None,
                pin3_to_pin1_px=None,
                pin1_to_pin2_mm=None,
                pin2_to_pin3_mm=None,
                pin3_to_pin1_mm=None,
                centroid_distance_mm=None,
                result=result,
                heights_text=heights_text,
                accuracy=acc,
                is_top=False,
                is_bottom=False
            )
            QMessageBox.information(self, "Result", f"Processed. Saved to DB (id={rowid}).")
        except Exception as e:
            QMessageBox.warning(self, "DB Error", f"Processed but failed to save to DB:\n{e}")

# ----------------------------
# ---- BIG Date Dialog (Preview) ----
# ----------------------------
class DateRangeDialog(QDialog):
    """
    Big custom dialog with:
      [Title: Generate Excel & PDF Report]
      [From date: __] [To date: __]
      [LIVE PREVIEW TABLE of rows that will be exported]
      [ Download ]   [ Cancel ]
    - Changing the dates updates the table instantly (today shows today's rows).
    - Download writes Excel/PDF directly and closes after success.
    """
    COLS = [
        'id','image_name','date','time','timestamp','view',
        'pin1_to_pin2_px','pin2_to_pin3_px','pin3_to_pin1_px',
        'pin1_to_pin2_mm','pin2_to_pin3_mm','pin3_to_pin1_mm',
        'centroid_distance_mm','result','heights_text','accuracy'
    ]

    def __init__(self, parent, db_manager: DatabaseManager):
        super().__init__(parent)
        self.db = db_manager
        self.setWindowTitle("Generate Report")
        self.setModal(True)
        # Make it big as requested
        self.resize(980, 640)
        self.setMinimumSize(900, 560)

        # --- Title at top ---
        title = QLabel("Generate Excel & PDF Report")
        title_font = QFont()
        title_font.setPointSize(14)
        title_font.setBold(True)
        title.setFont(title_font)
        title.setAlignment(Qt.AlignCenter)

        # --- Dates row ---
        today = QDate.currentDate()
        self.from_edit = QDateEdit(self); self.from_edit.setCalendarPopup(True)
        self.from_edit.setDisplayFormat("yyyy-MM-dd"); self.from_edit.setDate(today)
        self.to_edit   = QDateEdit(self); self.to_edit.setCalendarPopup(True)
        self.to_edit.setDisplayFormat("yyyy-MM-dd");   self.to_edit.setDate(today)

        grid = QGridLayout()
        grid.addWidget(QLabel("From date:"), 0, 0)
        grid.addWidget(self.from_edit,          0, 1)
        grid.addWidget(QLabel("To date:"),   0, 2)
        grid.addWidget(self.to_edit,           0, 3)

        # --- Preview header (count info) ---
        self.count_label = QLabel("0 rows")
        self.count_label.setAlignment(Qt.AlignRight | Qt.AlignVCenter)

        # --- Preview table ---
        self.table = QTableWidget(0, len(self.COLS), self)
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.setAlternatingRowColors(True)
        self.table.horizontalHeader().setStretchLastSection(False)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeToContents)
        self.table.verticalHeader().setVisible(False)

        # --- Buttons row ---
        self.btn_download = QPushButton("Download")
        self.btn_cancel   = QPushButton("Cancel")
        btn_row = QHBoxLayout()
        btn_row.addItem(QSpacerItem(10, 10, QSizePolicy.Expanding, QSizePolicy.Minimum))
        btn_row.addWidget(self.btn_download)
        btn_row.addWidget(self.btn_cancel)

        # --- Compose layout ---
        root = QVBoxLayout()
        root.addWidget(title)
        root.addSpacing(6)
        root.addLayout(grid)
        root.addSpacing(4)
        root.addWidget(self.count_label)
        root.addWidget(self.table)
        root.addLayout(btn_row)
        self.setLayout(root)

        # --- Signals ---
        self.from_edit.dateChanged.connect(self._refresh_preview)
        self.to_edit.dateChanged.connect(self._refresh_preview)
        self.btn_download.clicked.connect(self._on_download)
        self.btn_cancel.clicked.connect(self.reject)

        # Initial preview (today)
        self._refresh_preview()

    def _refresh_preview(self):
        """Load rows for selected range and populate the table (live preview)."""
        start = self.from_edit.date()
        end   = self.to_edit.date()
        if end < start:
            self.count_label.setText("Invalid range")
            self.table.setRowCount(0)
            return

        start_str = start.toString("yyyy-MM-dd")
        end_str   = end.toString("yyyy-MM-dd")
        rows = self.db.fetch_between(start_str, end_str)

        self.table.setRowCount(len(rows))
        for r_i, r in enumerate(rows):
            d = dict(r)
            for c_i, col in enumerate(self.COLS):
                val = d.get(col, "")
                if isinstance(val, float):
                    val = _fmt(val)
                if val is None:
                    val = ""
                item = QTableWidgetItem(str(val))
                item.setFlags(item.flags() ^ Qt.ItemIsEditable)
                self.table.setItem(r_i, c_i, item)

        self.count_label.setText(f"{len(rows)} row(s) • {start_str} → {end_str}")

        # Resize columns to fit content (without lagging too often)
        self.table.resizeColumnsToContents()

    def _on_download(self):
        # Validate range
        start = self.from_edit.date()
        end   = self.to_edit.date()
        if end < start:
            QMessageBox.warning(self, "Invalid Range", "‘To date’ must be the same as or after ‘From date’.")
            return

        start_str = start.toString("yyyy-MM-dd")
        end_str   = end.toString("yyyy-MM-dd")

        # Check dependencies once here for a clean UX
        if pd is None or SimpleDocTemplate is None:
            QMessageBox.critical(self, "Missing Packages",
                                 "pandas and reportlab are required for report export.\nInstall via: pip install pandas reportlab")
            return

        try:
            excel_path, pdf_path = self.db.export_reports(start_date=start_str, end_date=end_str)
            QMessageBox.information(
                self, "Export Complete",
                f"Filtered by: {start_str} → {end_str}\n\n"
                f"Excel saved to:\n{excel_path}\n\n"
                f"PDF saved to:\n{pdf_path}\n\n"
                "Note: If a viewer keeps the old PDF open, a timestamped file is created."
            )
            self.accept()  # close after success
        except Exception as e:
            QMessageBox.critical(self, "Export Error", f"Failed to export reports:\n{e}")

# ----------------------------
# ---- Launcher Window  ------
# ----------------------------
class LauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PIN DETECTION - ROBOWORKS AUTOMATION")
        self.setMinimumSize(640, 260)
        self._open_windows = []
        self.db = DatabaseManager(DB_FILENAME)
        self.setup_ui()

    def setup_ui(self):
        main_layout = QVBoxLayout()
        header = QLabel("ROBOWORKS AUTOMATION")
        header.setAlignment(Qt.AlignCenter)
        font = QFont(); font.setPointSize(16); font.setBold(True)
        header.setFont(font)
        main_layout.addWidget(header)

        # Row 1: Top / Bottom separate buttons
        row1 = QHBoxLayout()
        btn_top = QPushButton("Top View Detector (S398 – TOP)")
        btn_bottom = QPushButton("Bottom View Detector (S398 – BOTTOM)")
        row1.addWidget(btn_top)
        row1.addWidget(btn_bottom)
        main_layout.addLayout(row1)

        # Row 2: Side + Reports
        row2 = QHBoxLayout()
        btn_side = QPushButton("Side View Height Detector (v11)")
        btn_report = QPushButton("Generate Excel & PDF Report")
        row2.addWidget(btn_side)
        row2.addWidget(btn_report)
        main_layout.addLayout(row2)

        qbtn = QPushButton("Quit")
        qbtn.clicked.connect(self.close)
        main_layout.addStretch(1)
        main_layout.addWidget(qbtn)
        self.setLayout(main_layout)

        btn_top.clicked.connect(self.open_s398_top)
        btn_bottom.clicked.connect(self.open_s398_bottom)
        btn_side.clicked.connect(self.open_pin_height)
        btn_report.clicked.connect(self.open_report_dialog)

    def open_s398_top(self):
        w = S398App(self.db, "Top"); w.show(); self._open_windows.append(w)

    def open_s398_bottom(self):
        w = S398App(self.db, "Bottom"); w.show(); self._open_windows.append(w)

    def open_pin_height(self):
        w = PinHeightApp(self.db); w.show(); self._open_windows.append(w)

    def open_report_dialog(self):
        dlg = DateRangeDialog(self, self.db)
        dlg.exec()  # dialog handles preview + download internally

# ----------------------------
# ---- Main entrypoint ------
# ----------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    launcher = LauncherWindow()
    launcher.show()
    sys.exit(app.exec())
