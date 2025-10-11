# homography_yaw_xyz_mm.py
import cv2
import numpy as np
import math
import os
import sys

# -----------------------
# USER CONFIG
# -----------------------
CAMERA_URL = "rtsp://admin:idt12345@192.168.0.160:554/cam/realmonitor?channel=1&subtype=0"
MAX_DISPLAY_W = 1280
MAX_DISPLAY_H = 720
SAVE_DIR = "homography_pose_outputs"
os.makedirs(SAVE_DIR, exist_ok=True)

# camera-to-plane height (mm) — change interactively with 'h'
camera_height_mm = 266.0

# -----------------------
# HELPERS
# -----------------------
def image_to_world(pt, H):
    x, y = float(pt[0]), float(pt[1])
    vec = np.array([x, y, 1.0], dtype=np.float64)
    w = H.dot(vec)
    if abs(w[2]) < 1e-8:
        return None
    X = w[0] / w[2]
    Y = w[1] / w[2]
    return float(X), float(Y)

def world_distance_mm(pw1, pw2):
    dx = pw1[0] - pw2[0]
    dy = pw1[1] - pw2[1]
    return math.hypot(dx, dy)

def world_yaw_deg(pw1, pw2):
    dx = pw2[0] - pw1[0]
    dy = pw2[1] - pw1[1]
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0.0
    return math.degrees(math.atan2(dy, dx))

def tilt_angle_deg_by_height(dist_mm, height_mm):
    if dist_mm <= 1e-9:
        return 0.0
    return math.degrees(math.atan2(height_mm, dist_mm))

def angles_between_vector_and_axes(vec):
    vx, vy, vz = float(vec[0]), float(vec[1]), float(vec[2])
    norm = math.hypot(vx, vy, vz)
    if norm < 1e-9:
        return 0.0, 0.0, 0.0
    ax = math.degrees(math.atan2(math.hypot(vy, vz), vx))
    ay = math.degrees(math.atan2(math.hypot(vx, vz), vy))
    az = math.degrees(math.atan2(math.hypot(vx, vy), vz))
    return ax, ay, az

# -----------------------
# GLOBAL STATE
# -----------------------
ref_pts_img = []      
measure_pts_img = []  
homography = None
display_scale = 1.0
orig_frame_shape = None
snapshot_idx = 0
pose_available = False
rvec, tvec, R_mat = None, None, None

# -----------------------
# CAMERA CALIBRATION
# -----------------------
calib_file = "camera_calibration_data.npz"
cam_mtx = None
cam_dist = None
if os.path.exists(calib_file):
    try:
        data = np.load(calib_file)
        cam_mtx = data.get("mtx", None)
        cam_dist = data.get("dist", None)
        print("Loaded camera calibration from", calib_file)
    except Exception as e:
        print("Warning: failed to load calibration file:", e)

# -----------------------
# MOUSE CALLBACK
# -----------------------
def on_mouse(event, x, y, flags, param):
    global ref_pts_img, measure_pts_img, display_scale, orig_frame_shape, homography
    if orig_frame_shape is None:
        return
    ix = int(round(x / display_scale))
    iy = int(round(y / display_scale))
    if event == cv2.EVENT_LBUTTONDOWN:
        if homography is None:
            if len(ref_pts_img) < 4:
                ref_pts_img.append((ix, iy))
                print(f"Ref corner {len(ref_pts_img)}: {ix, iy}")
            else:
                print("Reference already has 4 points. Press 'r' to reset.")
        else:
            measure_pts_img.append((ix, iy))
            if len(measure_pts_img) > 2:
                measure_pts_img = measure_pts_img[-2:]
            print(f"Measure point #{len(measure_pts_img)}: {(ix, iy)}")

# -----------------------
# MAIN
# -----------------------
def main():
    global ref_pts_img, measure_pts_img, homography, display_scale, orig_frame_shape
    global snapshot_idx, cam_mtx, cam_dist, camera_height_mm
    global pose_available, rvec, tvec, R_mat

    print("Instructions:")
    print("  Click 4 corners (TL,TR,BR,BL), enter real dims (mm). Then click two points to measure distance and angles.")
    print("Keys: r=reset, s=save, h=change camera height, q/Esc=quit")
    print("Current camera-to-plane height (mm):", camera_height_mm)

    cap = cv2.VideoCapture(CAMERA_URL)
    if not cap.isOpened():
        print("ERROR: cannot open camera URL:", CAMERA_URL)
        sys.exit(1)

    orig = None
    for _ in range(60):
        ret, frame = cap.read()
        if ret and frame is not None:
            orig = frame
            break
    if orig is None:
        print("ERROR: could not read frames from camera.")
        cap.release()
        sys.exit(1)

    orig_h, orig_w = orig.shape[:2]
    orig_frame_shape = orig.shape
    display_scale = min(1.0, MAX_DISPLAY_W / orig_w, MAX_DISPLAY_H / orig_h)

    cv2.namedWindow("Homography+Yaw+XYZ", cv2.WINDOW_NORMAL)
    cv2.setMouseCallback("Homography+Yaw+XYZ", on_mouse)

    while True:
        ret, frame = cap.read()
        if not ret:
            continue
        img = frame.copy()
        disp = cv2.resize(img, (int(orig_w*display_scale), int(orig_h*display_scale)))

        # draw reference corners
        for i, p in enumerate(ref_pts_img):
            px, py = int(p[0]*display_scale), int(p[1]*display_scale)
            cv2.circle(disp, (px, py), 6, (0,255,255), -1)
            cv2.putText(disp, f"R{i+1}", (px+6, py-6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,255), 2)
        if len(ref_pts_img) == 4:
            pts = np.array([ (int(x*display_scale), int(y*display_scale)) for (x,y) in ref_pts_img ], np.int32)
            cv2.polylines(disp, [pts], True, (0,255,255), 2)

        # draw measurement points
        for i, p in enumerate(measure_pts_img):
            px, py = int(p[0]*display_scale), int(p[1]*display_scale)
            cv2.circle(disp, (px, py), 6, (0,255,0), -1)
            cv2.putText(disp, f"P{i+1}", (px+6, py-6), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (0,255,0), 2)

        # measurement
        if homography is not None and len(measure_pts_img) >= 2:
            w1 = image_to_world(measure_pts_img[-2], homography)
            w2 = image_to_world(measure_pts_img[-1], homography)
            if w1 and w2:
                dist_mm = world_distance_mm(w1, w2)
                yaw_deg = world_yaw_deg(w1, w2)
                tilt_deg = tilt_angle_deg_by_height(dist_mm, camera_height_mm)

                # draw line
                p1_disp = int(measure_pts_img[-2][0]*display_scale), int(measure_pts_img[-2][1]*display_scale)
                p2_disp = int(measure_pts_img[-1][0]*display_scale), int(measure_pts_img[-1][1]*display_scale)
                cv2.line(disp, p1_disp, p2_disp, (255,0,0), 2)
                mid = ((p1_disp[0]+p2_disp[0])//2, (p1_disp[1]+p2_disp[1])//2)
                cv2.putText(disp, f"{dist_mm:.2f} mm", (mid[0]+8, mid[1]-8), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255,0,0), 2)

                # if pose is available compute XYZ angles
                if pose_available and R_mat is not None and tvec is not None:
                    P1_world = np.array([w1[0], w1[1], 0.0]).reshape(3,1)
                    P2_world = np.array([w2[0], w2[1], 0.0]).reshape(3,1)
                    P1_cam = R_mat.dot(P1_world) + tvec
                    P2_cam = R_mat.dot(P2_world) + tvec
                    vec_cam = (P2_cam - P1_cam).reshape(3,)
                    ax, ay, az = angles_between_vector_and_axes(vec_cam)

                    cv2.putText(disp, f"Angle_X: {ax:.2f}", (10, int(orig_h*display_scale)-120), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,255), 2)
                    cv2.putText(disp, f"Angle_Y: {ay:.2f}", (10, int(orig_h*display_scale)-90), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,255), 2)
                    cv2.putText(disp, f"Angle_Z: {az:.2f}", (10, int(orig_h*display_scale)-60), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,255), 2)

                # always show yaw and tilt
                cv2.putText(disp, f"Yaw (deg): {yaw_deg:.2f}", (10, int(orig_h*display_scale)-30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0,255,255), 2)
                cv2.putText(disp, f"Tilt(est,deg): {tilt_deg:.2f}", (200, int(orig_h*display_scale)-30), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (200,200,200), 2)

        # instructions
        cv2.putText(disp, "r=reset  s=save  h=change camera height  q/Esc=quit", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255,255,255), 1)

        cv2.imshow("Homography+Yaw+XYZ", disp)
        key = cv2.waitKey(1) & 0xFF

        if key in (27, ord('q')):
            break
        elif key == ord('r'):
            ref_pts_img = []
            measure_pts_img = []
            homography = None
            pose_available = False
            rvec = tvec = R_mat = None
            print("Reset everything.")
        elif key == ord('s'):
            snapshot_idx += 1
            save_img = img.copy()
            for i,p in enumerate(ref_pts_img):
                cv2.circle(save_img, (int(p[0]), int(p[1])), 6, (0,255,255), -1)
            for i,p in enumerate(measure_pts_img):
                cv2.circle(save_img, (int(p[0]), int(p[1])), 6, (0,255,0), -1)
            fname = os.path.join(SAVE_DIR, f"annotated_{snapshot_idx}.png")
            cv2.imwrite(fname, save_img)
            print("Saved:", fname)
        elif key == ord('h'):
            try:
                camera_height_mm = float(input("Enter camera-to-plane height (mm): ").strip())
                print("Camera height updated:", camera_height_mm)
            except Exception:
                print("Invalid input; unchanged.")

        # compute homography after 4 points clicked
        if len(ref_pts_img) == 4 and homography is None:
            try:
                w_mm = float(input("Enter reference width (mm): ").strip())
                h_mm = float(input("Enter reference height (mm): ").strip())
            except:
                print("Invalid input; reset and try again.")
                ref_pts_img = []
                continue
            src = np.array(ref_pts_img, dtype=np.float64)
            dst = np.array([[0,0],[w_mm,0],[w_mm,h_mm],[0,h_mm]], dtype=np.float64)
            H, _ = cv2.findHomography(src, dst)
            if H is None:
                print("Homography failed; reset.")
                ref_pts_img = []
                continue
            homography = H
            # compute pose for XYZ angles if camera calibrated
            if cam_mtx is not None and cam_dist is not None:
                obj_pts_3d = np.array([[0,0,0],[w_mm,0,0],[w_mm,h_mm,0],[0,h_mm,0]], dtype=np.float32)
                img_pts = np.array(ref_pts_img, dtype=np.float32)
                flags = cv2.SOLVEPNP_IPPE if hasattr(cv2, 'SOLVEPNP_IPPE') else cv2.SOLVEPNP_ITERATIVE
                ok, rvec_tmp, tvec_tmp = cv2.solvePnP(obj_pts_3d, img_pts, cam_mtx, cam_dist, flags=flags)
                if ok:
                    rvec = rvec_tmp
                    tvec = tvec_tmp.reshape(3,1)
                    R_mat, _ = cv2.Rodrigues(rvec)
                    pose_available = True
                    print("Pose computed; XYZ angles will be visible.")

    cap.release()
    cv2.destroyAllWindows()

if __name__ == "__main__":
    main()
