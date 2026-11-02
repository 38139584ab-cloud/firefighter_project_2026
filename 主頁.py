# -*- coding: utf-8 -*-
import os
import time
import cv2
import gradio as gr
import torch
import xml.etree.ElementTree as ET
from ultralytics import YOLO
import pygame
import openpyxl
from openpyxl.drawing.image import Image as ExcelImage
import base64
import numpy as np
from PIL import Image, ImageDraw, ImageFont
import json
from sync_manager import SyncManager, COLLECT_DIR, atomic_imwrite


device = "cuda" if torch.cuda.is_available() else "cpu"

# 🛠️ 修正 1：移除 "project/" 前綴
model = YOLO("models/最新模型.pt")

pygame.mixer.init()

# 🛠️ 修正 2：移除 "project/" 前綴
LOG_IMG_DIR = "log_images"
os.makedirs(LOG_IMG_DIR, exist_ok=True)
EXCEL_EXPORT_DIR = "exports"
os.makedirs(EXCEL_EXPORT_DIR, exist_ok=True)

# 新增：控制本機攝影機中斷的全域變數
stop_flag = False

# 資料同步（Syncthing 內嵌，使用者端單向送出到開發者電腦）
sync = SyncManager()
COLLECT_MIN_INTERVAL = 2.0  # 最短每幾秒才收集一張，避免大量影像塞爆同步

def stop_webcam_fn():
    global stop_flag
    stop_flag = True

# ==========================================
# 📜 服務條款內容與 CSS 樣式 (彈窗設計)
# ==========================================
TOS_TEXT = """### 🛡️ 資料蒐集與服務條款
歡迎使用「消防人員裝備影像偵測系統」。

為持續提升 AI 辨識模型的準確度，本系統內建「資料收集」功能。當偵測到人員出現時，系統可自動擷取當前影像，作為未來模型訓練與優化的素材，並透過加密連線自動傳送給系統開發者。影像中可能包含人員面貌。

**您的隱私與權益：**
1. **自由開關**：您可以隨時在「設定」頁面中，手動開啟或關閉資料收集功能。
2. **功能不受限**：若您不同意，系統仍可正常執行即時偵測與警報功能，但不會自動存下任何訓練用影像。
3. **隨時停止**：關閉資料收集後，系統會立即停止傳送。

請問您是否同意啟用「資料收集」功能？（進入系統後仍可至「設定」隨時更改）"""

css = """
#modal-overlay {
    position: fixed;
    top: 0;
    left: 0;
    width: 100vw;
    height: 100vh;
    background-color: rgba(0, 0, 0, 0.75);
    z-index: 9999;
    backdrop-filter: blur(3px);
}
#tos-modal {
    position: absolute;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    background-color: var(--background-fill-primary, #ffffff);
    padding: 30px;
    border-radius: 12px;
    box-shadow: 0 8px 32px rgba(0,0,0,0.8);
    max-width: 600px;
    width: 90%;
    border: 1px solid var(--border-color-primary, #ddd);
}
"""

# ==========================================
# 📊 報表與介面輔助函數
# ==========================================
def generate_html_table(records, headers=["時間", "訊息內容", "截圖"]):
    html = "<table style='width:100%; border-collapse: collapse;'>"
    html += "<tr style='background-color: #f2f2f2;'>"
    for h in headers:
        html += f"<th style='border: 1px solid #ddd; padding: 8px; text-align: left;'>{h}</th>"
    html += "</tr>"
    
    for row in records:
        html += "<tr>"
        for i, cell in enumerate(row):
            if i == 2 and cell: 
                if os.path.exists(cell):
                    with open(cell, "rb") as f:
                        b64_img = base64.b64encode(f.read()).decode('utf-8')
                    html += f"<td style='border: 1px solid #ddd; padding: 8px;'><img src='data:image/jpeg;base64,{b64_img}' width='150' style='border-radius:5px;'></td>"
                else:
                    html += f"<td style='border: 1px solid #ddd; padding: 8px;'>找不到圖片</td>"
            else:
                html += f"<td style='border: 1px solid #ddd; padding: 8px;'>{cell}</td>"
        html += "</tr>"
    html += "</table>"
    return html

def export_to_excel(records, filepath, headers=["時間", "訊息內容", "截圖"]):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(headers)
    
    ws.column_dimensions['A'].width = 15
    ws.column_dimensions['B'].width = 40
    ws.column_dimensions['C'].width = 30

    for r_idx, row_data in enumerate(records, start=2):
        time_str, msg, img_path = row_data
        ws.cell(row=r_idx, column=1, value=time_str)
        ws.cell(row=r_idx, column=2, value=msg)
        
        if img_path and os.path.exists(img_path):
            img = ExcelImage(img_path)
            img.width = 160
            img.height = 120
            ws.row_dimensions[r_idx].height = 95 
            ws.add_image(img, f"C{r_idx}")
        else:
            ws.cell(row=r_idx, column=3, value="無圖片")
            ws.row_dimensions[r_idx].height = 20
            
    wb.save(filepath)

# ==========================================
# 🎨 UI 輔助繪圖函數 (🛠️ 修正為 PIL 支援中文)
# ==========================================
def draw_warning_text(img, text="不完備"):
    # 將 OpenCV 圖片 (BGR) 轉換為 PIL 圖片 (RGB)
    img_pil = Image.fromarray(cv2.cvtColor(img, cv2.COLOR_BGR2RGB))
    draw = ImageDraw.Draw(img_pil)
    
    # 嘗試載入支援中文的字體 (微軟正黑體)
    try:
        font = ImageFont.truetype("msjh.ttc", 40)
    except IOError:
        try:
            font = ImageFont.truetype("simhei.ttf", 40)
        except IOError:
            font = ImageFont.load_default()

    # 計算文字寬高
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    text_height = bbox[3] - bbox[1]
    
    x, y = 50, 50
    pad = 10
    
    # 畫白色底框
    draw.rectangle(
        [(x - pad, y - pad), (x + text_width + pad, y + text_height + pad)],
        fill=(255, 255, 255)
    )
    # 畫紅色中文字 (PIL 使用 RGB)
    draw.text((x, y), text, font=font, fill=(255, 0, 0))
    
    # 將畫好的 PIL 圖片轉回 OpenCV 圖片 (BGR) 並原地覆寫
    img[:] = cv2.cvtColor(np.array(img_pil), cv2.COLOR_RGB2BGR)

def draw_cached_boxes(img, boxes_info):
    # 類別標籤通常是英文 (如 person, no_helmet)，所以保留使用速度較快的 OpenCV putText
    for box in boxes_info:
        cls_name, conf, x1, y1, x2, y2 = box
        if "no_" in cls_name.lower():
            color = (0, 0, 255)  
            txt_color = (255, 255, 255)
        else:
            color = (0, 255, 0)  
            txt_color = (0, 0, 0)

        cv2.rectangle(img, (int(x1), int(y1)), (int(x2), int(y2)), color, 2)
        label = f"{cls_name} {conf:.2f}"
        (tw, th), _ = cv2.getTextSize(label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        cv2.rectangle(img, (int(x1), int(y1) - th - 5), (int(x1) + tw, int(y1)), color, -1)
        cv2.putText(img, label, (int(x1), int(y1) - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.5, txt_color, 1, cv2.LINE_AA)

# ==========================================
# 📁 資料儲存邏輯
# ==========================================
def save_voc_annotation(img_name, img_width, img_height, boxes_info, save_dir=COLLECT_DIR):
    os.makedirs(save_dir, exist_ok=True)
    
    annotation = ET.Element("annotation")
    ET.SubElement(annotation, "folder").text = "data"  # 不寫入使用者本機完整路徑
    ET.SubElement(annotation, "filename").text = img_name
    
    size = ET.SubElement(annotation, "size")
    ET.SubElement(size, "width").text = str(img_width)
    ET.SubElement(size, "height").text = str(img_height)
    ET.SubElement(size, "depth").text = "3"
    
    for box in boxes_info:
        cls_name = box[0]
        xmin, ymin, xmax, ymax = box[2], box[3], box[4], box[5]
        
        obj = ET.SubElement(annotation, "object")
        ET.SubElement(obj, "name").text = cls_name
        ET.SubElement(obj, "pose").text = "Unspecified"
        ET.SubElement(obj, "truncated").text = "0"
        ET.SubElement(obj, "difficult").text = "0"
        
        bndbox = ET.SubElement(obj, "bndbox")
        ET.SubElement(bndbox, "xmin").text = str(int(xmin))
        ET.SubElement(bndbox, "ymin").text = str(int(ymin))
        ET.SubElement(bndbox, "xmax").text = str(int(xmax))
        ET.SubElement(bndbox, "ymax").text = str(int(ymax))
        
    tree = ET.ElementTree(annotation)
    xml_filename = os.path.join(save_dir, img_name.rsplit(".", 1)[0] + ".xml")
    tmp_xml = xml_filename + ".tmp"  # 先寫暫存再改名，避免同步到半成品
    tree.write(tmp_xml, encoding="utf-8", xml_declaration=True)
    os.replace(tmp_xml, xml_filename)

# ==========================================
# 🚀 核心：本機端 OpenCV 攝影機迴圈
# ==========================================
def run_local_webcam(interval, confidence_level, alarm_enabled, dataOff_set, voice_class, voice_vol, history_logs):
    global stop_flag
    stop_flag = False  

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        err_logs = history_logs.copy() if history_logs else []
        err_logs.append([time.strftime('%H:%M:%S'), "❌ 無法開啟本機攝影機", ""])
        yield None, generate_html_table(err_logs), None, err_logs
        return

    log_records = history_logs.copy() if history_logs else []
    webcam_counter = 0
    sin_token = 0
    is_alerting = False
    cached_boxes = [] 
    last_collect_ts = 0.0
    
    interval = int(interval) if interval and interval > 0 else 4
    conf_threshold = float(confidence_level) / 100.0 if confidence_level else 0.50

    audio_path = f"voice/{voice_class}.mp3"
    alarm_sound = None
    if os.path.exists(audio_path):
        alarm_sound = pygame.mixer.Sound(audio_path)
        alarm_sound.set_volume(voice_vol / 100.0)

    excel_filename = f"webcam_log_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
    excel_filepath = os.path.join(EXCEL_EXPORT_DIR, excel_filename)

    def push_update():
        export_to_excel(log_records, excel_filepath)
        return generate_html_table(log_records), excel_filepath, log_records

    log_records.append([time.strftime('%H:%M:%S'), f"📷 攝影機已啟動 (門檻: {conf_threshold*100:.0f}%)", ""])
    yield None, *push_update()

    while cap.isOpened():
        if stop_flag:
            log_records.append([time.strftime('%H:%M:%S'), "⏹️ 攝影機已手動關閉。", ""])
            yield None, *push_update()
            break

        ret, frame = cap.read()
        if not ret:
            break

        webcam_counter += 1
        output_frame = frame.copy()
        log_updated = False 

        if (webcam_counter - 1) % interval == 0:
            results = model(frame, conf=conf_threshold)
            detected_classes = [model.names[int(cls)].lower() for cls in results[0].boxes.cls]
            
            cached_boxes = []
            for box in results[0].boxes:
                cls_id = int(box.cls[0])
                cls_name = model.names[cls_id]
                conf = float(box.conf[0])
                xyxy = box.xyxy[0].tolist()
                cached_boxes.append((cls_name, conf, xyxy[0], xyxy[1], xyxy[2], xyxy[3]))

            # 檢查 dataOff_set 是否啟用
            if (dataOff_set and len(results[0].boxes) > 0 and "person" in detected_classes
                    and time.time() - last_collect_ts >= COLLECT_MIN_INTERVAL):
                last_collect_ts = time.time()
                h, w = frame.shape[:2]
                timestamp = time.strftime("%Y%m%d_%H%M%S") + f"_{int(time.time()*1000)%1000}"
                img_filename = f"train_{timestamp}.jpg"
                
                os.makedirs(COLLECT_DIR, exist_ok=True)
                img_path = os.path.join(COLLECT_DIR, img_filename)

                atomic_imwrite(img_path, frame)  # 先寫圖片
                save_voc_annotation(img_filename, w, h, cached_boxes)  # 最後才寫 xml（代表這組資料完整）

            if "no_helmet" in detected_classes or "no_gloves" in detected_classes or "no_glove" in detected_classes:
                sin_token += 1
                if sin_token == 4 and not is_alerting:
                    is_alerting = True
                    
                    temp_frame = output_frame.copy()
                    draw_cached_boxes(temp_frame, cached_boxes)
                    img_name = f"alert_{time.strftime('%Y%m%d_%H%M%S')}.jpg"
                    img_path = os.path.join(LOG_IMG_DIR, img_name)
                    cv2.imwrite(img_path, temp_frame)
                    
                    log_records.append([time.strftime('%H:%M:%S'), "⚠️ 偵測到消防員未配戴完整裝備！", img_path])
                    log_updated = True
                    
                    if alarm_enabled and alarm_sound:
                        alarm_sound.play(-1)
            else:
                sin_token = 0 
                if is_alerting:
                    is_alerting = False
                    log_records.append([time.strftime('%H:%M:%S'), "✅ 裝備已恢復完整，警報解除。", ""])
                    log_updated = True
                    if alarm_sound:
                        alarm_sound.stop()

        draw_cached_boxes(output_frame, cached_boxes)

        if is_alerting and alarm_enabled:
            draw_warning_text(output_frame)

        rgb_frame = cv2.cvtColor(output_frame, cv2.COLOR_BGR2RGB)

        if log_updated:
            html_log, excel_log, history_log = push_update()
            yield rgb_frame, html_log, excel_log, history_log
        else:
            yield rgb_frame, gr.skip(), gr.skip(), gr.skip()

    if alarm_sound:
        alarm_sound.stop()
    cap.release()

# ==========================================
# 🎬 影片偵測邏輯
# ==========================================
def detect_video_fn(video_frame, interval, confidence_level, alarm_enabled):
    if video_frame is None:
        return None, generate_html_table([["錯誤", "請先上傳影片", ""]]), None
    
    video_counter = 0
    log_records = []
    cached_boxes = [] 
    is_alerting = False
    sin_token = 0

    get_video = cv2.VideoCapture(video_frame)
    fps = get_video.get(cv2.CAP_PROP_FPS)
    if fps <= 0 or fps is None:
        fps = 24.0

    interval = int(interval) if interval and interval > 0 else 4
    conf_threshold = float(confidence_level) / 100.0 if confidence_level else 0.50

    ret, first_frame = get_video.read()
    if not ret:
        return None, generate_html_table([["錯誤", "無法讀取影片", ""]]), None
        
    h, w = first_frame.shape[:2]
    width = (w // 2) * 2
    height = (h // 2) * 2

    output_dir = "videos"
    os.makedirs(output_dir, exist_ok=True)
    output_name = os.path.join(output_dir, "output_processed.webm")
    output_format = cv2.VideoWriter_fourcc(*"vp80")
    out = cv2.VideoWriter(output_name, output_format, fps, (width, height))
    
    get_video.set(cv2.CAP_PROP_POS_FRAMES, 0)

    while get_video.isOpened():
        end_frame, frame = get_video.read()
        if not end_frame:
            break   

        frame = cv2.resize(frame, (width, height))
        video_counter += 1
        output_frame = frame.copy()

        if (video_counter - 1) % interval == 0:
            results = model(frame, conf=conf_threshold)  
            detected_classes = [model.names[int(cls)].lower() for cls in results[0].boxes.cls]
            
            cached_boxes = []
            for box in results[0].boxes:
                cls_id = int(box.cls[0])
                cls_name = model.names[cls_id]
                conf = float(box.conf[0])
                xyxy = box.xyxy[0].tolist()
                cached_boxes.append((cls_name, conf, xyxy[0], xyxy[1], xyxy[2], xyxy[3]))
            
            if "no_helmet" in detected_classes or "no_gloves" in detected_classes or "no_glove" in detected_classes:
                sin_token += 1
            else:
                if is_alerting:
                    total_seconds = video_counter / fps
                    m = int(total_seconds // 60)
                    s = int(total_seconds % 60)
                    ms = int((total_seconds % 1) * 1000)
                    time_str = f"{m:02d}:{s:02d}.{ms:03d}"
                    log_records.append([time_str, "✅ 裝備已恢復完整，警報解除。", ""])
                    is_alerting = False
                sin_token = 0 
            
            if sin_token == 4:
                is_alerting = True
                total_seconds = video_counter / fps
                m = int(total_seconds // 60)
                s = int(total_seconds % 60)
                ms = int((total_seconds % 1) * 1000)
                time_str = f"{m:02d}:{s:02d}.{ms:03d}"
                
                temp_frame = output_frame.copy()
                draw_cached_boxes(temp_frame, cached_boxes)
                img_name = f"video_alert_{time.strftime('%Y%m%d_%H%M%S')}_{video_counter}.jpg"
                img_path = os.path.join(LOG_IMG_DIR, img_name)
                cv2.imwrite(img_path, temp_frame)
                
                log_records.append([time_str, "⚠️ 偵測到消防員未配戴完整裝備！", img_path])

        draw_cached_boxes(output_frame, cached_boxes)

        if is_alerting and alarm_enabled:
            draw_warning_text(output_frame)

        out.write(output_frame)

    get_video.release()
    out.release()
    
    if not log_records:
        log_records.append(["-", "✅ 影片檢測完畢，未發現違規狀況。", ""])
    
    excel_filename = f"video_log_{time.strftime('%Y%m%d_%H%M%S')}.xlsx"
    excel_filepath = os.path.join(EXCEL_EXPORT_DIR, excel_filename)
    export_to_excel(log_records, excel_filepath)

    return output_name, generate_html_table(log_records), excel_filepath

# ==========================================
# ⚙️ 設定檔讀寫邏輯 (記住使用者的同意狀態)
# ==========================================
CONFIG_FILE = "config.json"

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    # 預設：尚未回答過 (tos_answered: False)、資料收集關閉 (data_collection: False)
    return {"tos_answered": False, "data_collection": False}

def save_config(tos_answered, data_collection):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump({"tos_answered": tos_answered, "data_collection": data_collection}, f)

sys_config = load_config()
# 只有在使用者已同意資料收集時，才啟動背景同步
if sys_config["tos_answered"] and sys_config["data_collection"]:
    sync.start()
# ==========================================
# 網頁 UI 介面
# ==========================================
with gr.Blocks(css=css) as demo:
    
    # 新增一個隱藏狀態：用來分辨 Checkbox 是「手動點擊」還是「程式自動切換」
    programmatic_change = gr.State(False)
    
    # 這裡的 visible 預設先關閉，由下方的 demo.load 決定是否顯示
    with gr.Column(elem_id="modal-overlay", visible=False) as tos_overlay:
        with gr.Column(elem_id="tos-modal"):
            gr.Markdown(TOS_TEXT)
            with gr.Row():
                agree_btn = gr.Button("✅ 我同意資料蒐集", variant="primary")
                disagree_btn = gr.Button("❌ 我不同意")

    with gr.Column() as main_screen:
        gr.Markdown('# 消防人員裝備影像偵測系統')
        webcam_history_state = gr.State([])

        with gr.Tab("及時偵測"):
            gr.Markdown("<h3 style='text-align: center;'>📷 本機端攝影機即時偵測</h3>")
            
            with gr.Row():
                start_webcam_btn = gr.Button("🚀 啟動本機攝影機", variant="primary")
                stop_webcam_btn = gr.Button("⏹️ 關閉攝影機")
                
            live_view = gr.Image(label="即時影像")

        with gr.Tab("影片偵測"):
            video_input = gr.Video(sources=["upload"])
            start_button = gr.Button("開始偵測")
            video_output = gr.Video()

        with gr.Tab("工作日誌"):
            with gr.Tab("及時偵測紀錄"):
                webcam_log_html = gr.HTML(label="檢測紀錄")
                webcam_excel_file = gr.File(label="下載 Excel 日誌檔 (內含圖片)")
                
            with gr.Tab("影像偵測紀錄"):
                video_log_html = gr.HTML(label="檢測紀錄")
                video_excel_file = gr.File(label="下載 Excel 日誌檔 (內含圖片)")

        with gr.Tab("設定"):
            fps_set = gr.Number(label="抽幀間隔 (1 即為每幀皆辨識)", value=1)
            confidence_level = gr.Number(label="信心程度過濾 (0~100)", value=50, interactive=True)
            voiceOff_set = gr.Checkbox(label="啟用警報", value=True)
            
            # 這裡的預設 value 先給 False，載入時 demo.load 會自動去讀檔覆寫
            dataOff_set = gr.Checkbox(label="啟用資料收集", value=False, interactive=True)
            
            voiceclass_set = gr.Dropdown(["警報", "吼叫"], label="自訂警報聲", value="警報", interactive=True)
            voice_slider = gr.Slider(minimum=0, maximum=100, value=50, step=1, label="警報聲大小", interactive=True)
            sync_status = gr.Textbox(label="資料同步狀態", interactive=False)
            sync_refresh_btn = gr.Button("🔄 更新同步狀態")

    # ==========================================
    # 🎛️ 事件綁定與狀態更新
    # ==========================================

    def init_app():
        """每次網頁刷新(F5)時都會執行，確保讀取到最新設定檔"""
        config = load_config()
        # 回傳：彈窗是否顯示 (未回答就顯示)、資料收集開關狀態、設定標記為「程式自動」
        return gr.update(visible=not config["tos_answered"]), gr.update(value=config["data_collection"]), True

    # 網頁載入時觸發
    demo.load(fn=init_app, outputs=[tos_overlay, dataOff_set, programmatic_change])

    def agree_action():
        save_config(tos_answered=True, data_collection=True)
        sync.start()
        # 隱藏彈窗、將 Checkbox 打勾，並標記這次改變是「程式自動」的
        return gr.update(visible=False), gr.update(value=True), True
        
    def disagree_action():
        save_config(tos_answered=True, data_collection=False)
        sync.stop()
        # 隱藏彈窗、將 Checkbox 取消打勾，並標記這次改變是「程式自動」的
        return gr.update(visible=False), gr.update(value=False), True

    def checkbox_changed(is_checked, is_prog):
        if is_prog:
            # 如果是程式自動改的 (例如剛載入、或點了同意按鈕)，就忽略這次事件，並把狀態歸零
            return gr.skip(), False
            
        if is_checked:
            # 玩家手動「從關切換到開」 -> 跳出條款彈窗再次詢問
            return gr.update(visible=True), False
        else:
            # 玩家手動「從開切換到關」 -> 隱藏彈窗(以防萬一)，並直接儲存設定為 False
            save_config(tos_answered=True, data_collection=False)
            sync.stop()
            return gr.update(visible=False), False

    # 綁定彈窗按鈕
    agree_btn.click(fn=agree_action, outputs=[tos_overlay, dataOff_set, programmatic_change])
    disagree_btn.click(fn=disagree_action, outputs=[tos_overlay, dataOff_set, programmatic_change])

    # 綁定設定頁面的開關切換
    dataOff_set.change(
        fn=checkbox_changed,
        inputs=[dataOff_set, programmatic_change],
        outputs=[tos_overlay, programmatic_change]
    )

    sync_refresh_btn.click(fn=sync.get_status, outputs=sync_status)

    start_webcam_btn.click(
        fn=run_local_webcam,
        inputs=[
            fps_set,
            confidence_level,
            voiceOff_set,
            dataOff_set,
            voiceclass_set,
            voice_slider,
            webcam_history_state
        ],
        outputs=[
            live_view,           
            webcam_log_html, 
            webcam_excel_file,
            webcam_history_state 
        ]
    )

    stop_webcam_btn.click(fn=stop_webcam_fn)

    start_button.click(
        fn=detect_video_fn,
        inputs=[
            video_input,
            fps_set,
            confidence_level,
            voiceOff_set,
        ],
        outputs=[
            video_output, 
            video_log_html,
            video_excel_file
        ],
    )

demo.launch(allowed_paths=["voice", "data", "log_images"])