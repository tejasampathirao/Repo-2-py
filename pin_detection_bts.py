# pin_full_merge_gui.py
# Merged launcher for:
#  - s398_with_centroid_check.py  (Top/Bottom view detector)
#  - pin_height_gui_v11_simple.py (Side view height detector)
#
# Both pipelines are preserved; this file only wraps both GUIs in a single launcher window.
# Requires: PySide6, OpenCV (cv2), numpy

import sys
import os
import math
import cv2
import numpy as np

from PySide6.QtWidgets import (
    QApplication, QWidget, QLabel, QPushButton, QVBoxLayout, QHBoxLayout,
    QFileDialog, QMessageBox, QVBoxLayout, QDialog
)
from PySide6.QtGui import QPixmap, QImage
from PySide6.QtCore import Qt

# ----------------------------
# ---- S398 (top/bottom) -----
# ----------------------------
class PerfectS398Detector:
    def __init__(self):
        # NOTE: target_pcd originally provided in your code (11.66)
        # kept for compatibility but not used for centroid-vs-circle check.
        self.target_pcd = 11.66
        self.tolerance = 0.13

        # IMPORTANT: set this to your calibrated mm-per-pixel scale.
        # Example: if 1 pixel = 0.01 mm then scale_mm_per_px = 0.01
        # You MUST set this to your real calibration value for mm checks to be meaningful.
        self.scale_mm_per_px = 0.01  # <<--- CHANGE THIS to actual mm/px

        # pixel tolerance computed from mm tolerance (0.18 mm)
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
                    if roi.size > 0:
                        edges = cv2.Canny(roi, 50, 150)
                        scores.append(np.sum(edges))
                    else:
                        scores.append(0)

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
        """Find largest contour and return minEnclosingCircle (cx,cy,r)."""
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        blur = cv2.GaussianBlur(gray, (7,7), 0)

        # adaptive threshold + morphological clean to get object silhouette robustly
        _, thr = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
        white_area = np.sum(thr == 255)
        black_area = np.sum(thr == 0)
        if black_area < white_area:
            cleaned = thr
        else:
            cleaned = cv2.bitwise_not(thr)

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
            return None, "❌ Could not load image!"

        result_img = img.copy()
        pins = self.detect_pins(img)

        if pins is None or len(pins) < 3:
            return result_img, "❌ Could not detect 3 pins!"

        # draw pins
        for i, (x, y, r) in enumerate(pins):
            cv2.circle(result_img, (int(x), int(y)), int(r), (0, 255, 0), 3)
            cv2.circle(result_img, (int(x), int(y)), 3, (0, 0, 255), -1)
            cv2.putText(result_img, f"Pin{i + 1}", (int(x) - 25, int(y) - int(r) - 10),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 0), 2)

        # compute triangle centroid (average of centers)
        xs = [int(p[0]) for p in pins[:3]]
        ys = [int(p[1]) for p in pins[:3]]
        cx = int(round(sum(xs) / 3.0))
        cy = int(round(sum(ys) / 3.0))

        # find outer circle center
        outer_circle, cleaned_mask = self.find_outer_circle(img)
        if outer_circle is None:
            cv2.circle(result_img, (cx, cy), 6, (0, 0, 255), -1)
            cv2.putText(result_img, "Outer circle: NOT found", (20, 160),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0,0,255), 2)
            return result_img, "❌ Outer circle not found — cannot compute centroid distance"

        ox, oy, orad = outer_circle

        # compute distance in pixels
        dist_px = float(np.hypot(float(ox - cx), float(oy - cy)))
        # compute in mm using scale; be sure user set correct scale_mm_per_px
        dist_mm = dist_px * float(self.scale_mm_per_px)

        # status check against 0.18 mm
        status = "OK" if dist_mm <= self.mm_tolerance else "NG"
        color = (0, 255, 0) if status == "OK" else (0, 0, 255)

        # draw visuals
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

        distances = self.calculate_distances(pins)
        avg_distance = np.mean(distances)
        pin_pairs = [(0,1),(1,2),(0,2)]
        for i,(p1,p2) in enumerate(pin_pairs):
            x1,y1 = int(pins[p1][0]), int(pins[p1][1])
            x2,y2 = int(pins[p2][0]), int(pins[p2][1])
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
        return result_img, f"✅ {msg}"

class S398App(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Perfect S398 Pin Detector")
        self.detector = PerfectS398Detector()
        self.setup_ui()

    def setup_ui(self):
        layout = QVBoxLayout()

        self.upload_btn = QPushButton("Upload Image")
        self.quit_btn = QPushButton("Close")

        self.upload_btn.clicked.connect(self.upload_image)
        self.quit_btn.clicked.connect(self.close)

        layout.addWidget(self.upload_btn)
        layout.addWidget(self.quit_btn)
        layout.setAlignment(Qt.AlignTop)

        self.setLayout(layout)
        self.setFixedSize(900, 700)

    def upload_image(self):
        file_dialog = QFileDialog(self, "Select S398 Terminal Image")
        file_dialog.setNameFilters(["Images (*.jpg *.jpeg *.png *.bmp *.tiff)", "All files (*)"])
        if file_dialog.exec():
            file_path = file_dialog.selectedFiles()[0]
            self.process_image(file_path)

    def process_image(self, file_path):
        result_img, message = self.detector.process_image(file_path)
        if result_img is not None:
            # scale to fit window while keeping aspect ratio
            max_w, max_h = 800, 600
            h, w = result_img.shape[:2]
            scale = min(float(max_w) / w, float(max_h) / h, 1.0)
            if scale < 1.0:
                new_w = int(round(w * scale))
                new_h = int(round(h * scale))
                result_img = cv2.resize(result_img, (new_w, new_h), interpolation=cv2.INTER_AREA)

            window_name = f"S398 Detection - {os.path.basename(file_path)}"
            cv2.namedWindow(window_name, cv2.WINDOW_NORMAL)
            cv2.imshow(window_name, result_img)
            cv2.waitKey(0)
            cv2.destroyAllWindows()

            QMessageBox.information(self, "Result", message)
        else:
            QMessageBox.critical(self, "Error", message)


# ---------------------------------------
# ---- Pin height (side view) pipeline ---
# ---------------------------------------
# Config (kept as original)
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
    min_r_lo = max(2, int(0.15*max_r))

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
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Pin Height Measurement (v11 simple – 3 lines)")
        self.setMinimumSize(1200, 600)

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

# ----------------------------
# ---- Launcher Window  ------
# ----------------------------
class LauncherWindow(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("PIN DETECTION - Launcher")
        self.setMinimumSize(420, 160)
        self._open_windows = []  # keep references to child windows

        btn_top = QPushButton("Open Top/Bottom View Detector (S398)")
        btn_side = QPushButton("Open Side View Height Detector (v11)")

        btn_top.clicked.connect(self.open_s398)
        btn_side.clicked.connect(self.open_pin_height)

        qbtn = QPushButton("Quit")
        qbtn.clicked.connect(self.close)

        v = QVBoxLayout()
        v.addWidget(btn_top)
        v.addWidget(btn_side)
        v.addStretch(1)
        v.addWidget(qbtn)
        self.setLayout(v)

    def open_s398(self):
        w = S398App()
        w.show()
        self._open_windows.append(w)

    def open_pin_height(self):
        w = PinHeightApp()
        w.show()
        self._open_windows.append(w)

# ----------------------------
# ---- Main entrypoint ------
# ----------------------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    launcher = LauncherWindow()
    launcher.show()
    sys.exit(app.exec())
