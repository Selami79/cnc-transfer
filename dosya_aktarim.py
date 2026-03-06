import tkinter as tk
from tkinter import ttk, filedialog, messagebox
import customtkinter as ctk
import ftplib
import os
import shutil
import sys
import json
import threading
import time
from pathlib import Path
import unicodedata
import re
from datetime import datetime
import ctypes
import socket
import struct

# FOCAS DLL yükleme (CNC_MEM transferleri için)
FOCAS_DLL = None
try:
    _focas_dll_dir = os.path.join(
        os.path.dirname(sys.executable),
        "Lib", "site-packages", "chattertools", "lib", "Fwlib64"
    )
    _focas_dll_path = os.path.join(_focas_dll_dir, "fwlibe64.dll")
    if os.path.exists(_focas_dll_path):
        os.environ['PATH'] = _focas_dll_dir + ';' + os.environ.get('PATH', '')
        os.add_dll_directory(_focas_dll_dir)
        FOCAS_DLL = ctypes.windll.LoadLibrary(_focas_dll_path)
except Exception as e:
    print(f"FOCAS DLL yüklenemedi: {e}")

# ---- Raw FOCAS TCP Protokolü (CF Kart erişimi için) ----
FOCAS_MAGIC = b'\xa0\xa0\xa0\xa0'

def focas_raw_connect(ip, port, timeout=10):
    """Raw TCP ile FOCAS bağlantısı aç"""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect((ip, port))
    # FOCAS handshake (0x0101)
    pkt = FOCAS_MAGIC + b'\x00\x01\x01\x01\x00\x02\x00\x01'
    sock.sendall(pkt)
    resp = sock.recv(65536)
    if resp and len(resp) >= 8 and resp[6:8] == b'\x01\x02':
        return sock
    sock.close()
    return None

def focas_raw_disconnect(sock):
    """Raw TCP FOCAS bağlantısını kapat"""
    try:
        pkt = FOCAS_MAGIC + b'\x00\x01\x02\x01\x00\x00'
        sock.sendall(pkt)
        sock.settimeout(2)
        sock.recv(1024)
    except:
        pass
    finally:
        try:
            sock.close()
        except:
            pass

def focas_raw_write_file(sock, filename, file_data, progress_callback=None, overwrite=False):
    """CF karta dosya yaz (raw FOCAS TCP)

    Protokol: 0x1101 (start) -> 0x1204 (data) -> 0x1301 (end)
    Drive prefix: "0:" = yeni dosya, "10000:" = üzerine yazma (overwrite)
    overwrite=False ise dosya varsa FileExistsError fırlatır.
    """
    # Write Start (0x1101)
    prefix = "10000:" if overwrite else "0:"
    drive_name = f"{prefix}{filename}".encode('ascii')
    name_padded = drive_name + b'\x00' * (512 - len(drive_name))
    body = b'\x80\x09' + b'\x00\x01' + name_padded
    pkt = FOCAS_MAGIC + b'\x00\x01' + b'\x11\x01' + struct.pack('>H', len(body)) + body

    sock.sendall(pkt)
    sock.settimeout(10)
    resp = sock.recv(65536)

    if resp and len(resp) >= 8 and resp[6:8] == b'\x11\x03':
        # Dosya var - FILE_EXISTS hatası fırlat, çağıran taraf soracak
        raise FileExistsError(filename)

    if not resp or len(resp) < 8 or resp[6:8] != b'\x11\x02':
        func_code = resp[6:8].hex() if resp and len(resp) >= 8 else 'yok'
        raise Exception(f"CF_TEXT yazma başlatılamadı (cevap: {func_code})")

    # Write Data (0x1204) - parça parça gönder
    CHUNK_SIZE = 1400
    sent = 0
    total = len(file_data)
    while sent < total:
        chunk = file_data[sent:sent + CHUNK_SIZE]
        pkt = FOCAS_MAGIC + b'\x00\x01' + b'\x12\x04' + struct.pack('>H', len(chunk)) + chunk
        sock.sendall(pkt)
        sent += len(chunk)
        if progress_callback:
            progress_callback(sent, total)
        time.sleep(0.001)

    time.sleep(0.3)

    # Write End (0x1301)
    pkt = FOCAS_MAGIC + b'\x00\x01' + b'\x13\x01' + b'\x00\x00'
    sock.sendall(pkt)
    sock.settimeout(10)
    resp = sock.recv(65536)
    if not resp or len(resp) < 8 or resp[6:8] != b'\x13\x02':
        raise Exception(f"CF kart yazma sonlandırılamadı (cevap: {resp[6:8].hex() if resp and len(resp) >= 8 else 'yok'})")

def focas_raw_read_file(sock, filename, progress_callback=None):
    """CF karttan dosya oku (raw FOCAS TCP)

    Protokol: 0x1501 (start) -> 0x1604 (data chunks) -> 0x1701 (complete)
    """
    # Read Start (0x1501)
    drive_name = f"0:{filename}".encode('ascii')
    name_padded = drive_name + b'\x00' * (512 - len(drive_name))
    body = b'\x80\x09' + b'\x00\x01' + name_padded
    pkt = FOCAS_MAGIC + b'\x00\x01' + b'\x15\x01' + struct.pack('>H', len(body)) + body
    sock.sendall(pkt)

    # Tüm TCP verisini topla
    all_data = b''
    while True:
        sock.settimeout(10)
        try:
            data = sock.recv(65536)
        except socket.timeout:
            break
        if not data:
            break
        all_data += data

    # FOCAS paketlerini parse et
    file_data = b''
    offset = 0
    # İlk 0x1502 response'u atla
    if len(all_data) >= 10 and all_data[6:8] == b'\x15\x02':
        body_len = struct.unpack('>H', all_data[8:10])[0]
        offset = 10 + body_len

    while offset < len(all_data):
        if offset + 10 <= len(all_data) and all_data[offset:offset+4] == FOCAS_MAGIC:
            func = all_data[offset+6:offset+8]
            body_len = struct.unpack('>H', all_data[offset+8:offset+10])[0]
            if func == b'\x16\x04':
                chunk = all_data[offset+10:offset+10+body_len]
                file_data += chunk
                offset += 10 + body_len
                if progress_callback:
                    progress_callback(len(file_data), 0)
            elif func == b'\x17\x01':
                # Transfer complete - ACK gönder
                ack = FOCAS_MAGIC + b'\x00\x01' + b'\x17\x02' + b'\x00\x00'
                sock.sendall(ack)
                break
            else:
                offset += 1
        else:
            offset += 1

    return file_data


def focas_raw_list_files(ip, port, timeout=10):
    """CF karttaki dosya listesini al (DLL ile upload API kullanarak)

    //CNC_MEM/USB_PRG/ path'inden dosya isimlerini okur.
    DLL yoksa veya hata olursa boş liste döner.
    """
    if FOCAS_DLL is None:
        return []

    handle = ctypes.c_ushort(0)
    ip_bytes = ip.encode('ascii')
    ret = FOCAS_DLL.cnc_allclibhndl3(ip_bytes, ctypes.c_ushort(port), ctypes.c_long(timeout), ctypes.byref(handle))
    if ret != 0:
        return []

    try:
        path = ctypes.create_string_buffer(b'//CNC_MEM/USB_PRG/', 256)
        ret = FOCAS_DLL.cnc_upstart4(handle, ctypes.c_short(0), path)
        if ret != 0:
            return []

        total_data = b''
        for _ in range(500):
            buf = ctypes.create_string_buffer(65536)
            buf_size = ctypes.c_long(65536)
            ret = FOCAS_DLL.cnc_upload4(handle, ctypes.byref(buf_size), buf)
            if ret == 10:  # EW_BUFFER
                time.sleep(0.05)
                continue
            if ret == 0 and buf_size.value > 0:
                total_data += buf.raw[:buf_size.value]
            if ret == -2 or buf_size.value <= 0:
                break
            if ret != 0 and ret != 10:
                break
        FOCAS_DLL.cnc_upend4(handle)

        # Dosya isimlerini parse et
        files = []
        text = total_data.decode('ascii', errors='ignore')
        for line in text.split('\n'):
            line = line.strip()
            if line.startswith('%') or not line:
                continue
            # <dosya_adı> formatı
            if line.startswith('<') and '>' in line:
                name = line[1:line.index('>')]
                if name:
                    files.append(name)
            # O numarası formatı
            elif line.startswith('O') and len(line) >= 2:
                match = re.match(r'O(\d+)', line)
                if match:
                    files.append(f'O{match.group(1)}')
        return files
    except:
        return []
    finally:
        FOCAS_DLL.cnc_freelibhndl(handle)

def focas_raw_list_dir(sock, path):
    """CF karttaki dosya ve klasörleri listele (raw FOCAS TCP)

    Protokol: 0x01F4 (cnc_rdpdf_alldir)
    """
    path_bytes = path.encode('ascii')
    body = path_bytes + b'\x00' * (256 - len(path_bytes))
    pkt = FOCAS_MAGIC + b'\x00\x01' + b'\x01\xF4' + struct.pack('>H', len(body)) + body
    sock.sendall(pkt)
    sock.settimeout(10)
    resp = sock.recv(65536)

    if not resp or len(resp) < 10 or resp[6:8] != b'\x01\xF5':
        return [], []

    # Yanıtı parse et
    files = []
    folders = []
    
    # 24 byte'lık bloklar halinde oku
    offset = 10 
    while offset + 24 <= len(resp):
        chunk = resp[offset:offset+24]
        item_type = struct.unpack('<H', chunk[0:2])[0]
        name_bytes = chunk[2:22]
        
        try:
            name = name_bytes.split(b'\x00', 1)[0].decode('ascii')
        except UnicodeDecodeError:
            offset += 24
            continue

        if item_type == 0: # Klasör
            if name not in ('.', '..'):
                folders.append(name)
        elif item_type == 1: # Dosya
            files.append(name)

        offset += 24

    return sorted(folders), sorted(files)

def focas_raw_delete_file(sock, file_path):
    """CF karttan dosya sil (raw FOCAS TCP)

    Protokol: 0x01F6 (cnc_pdf_del)
    """
    path_bytes = file_path.encode('ascii')
    body = path_bytes + b'\x00' * (256 - len(path_bytes))
    pkt = FOCAS_MAGIC + b'\x00\x01' + b'\x01\xF6' + struct.pack('>H', len(body)) + body
    sock.sendall(pkt)
    sock.settimeout(10)
    resp = sock.recv(65536)
    # 0x01F7 silme işleminden sonraki yanıt kodudur
    if resp and len(resp) >= 8 and resp[6:8] == b'\x01\xF7':
        return True
    return False


class CNCTransferApp:
    TRANSLATIONS = {
        # App / Tabs
        "app_title": {"tr": "CNC Transfer", "en": "CNC Transfer"},
        "tab_transfer": {"tr": "Transfer", "en": "Transfer"},
        "tab_machines": {"tr": "Makineler", "en": "Machines"},
        "tab_new_machine": {"tr": "Yeni Makine", "en": "New Machine"},
        "tab_settings": {"tr": "Ayarlar", "en": "Settings"},

        # Common buttons / labels
        "btn_select_file": {"tr": "Dosya Sec", "en": "Select File"},
        "lbl_no_file_selected": {"tr": "Dosya secilmedi", "en": "No file selected"},
        "status_ready": {"tr": "Hazir", "en": "Ready"},
        "btn_send": {"tr": "GONDER", "en": "SEND"},
        "btn_delete_send": {"tr": "Sil + Gonder", "en": "Delete + Send"},
        "btn_backup": {"tr": "Yedekle", "en": "Backup"},
        "btn_refresh": {"tr": "Yenile", "en": "Refresh"},
        "btn_edit": {"tr": "Duzenle", "en": "Edit"},
        "btn_delete": {"tr": "Sil", "en": "Delete"},
        "btn_save": {"tr": "Kaydet", "en": "Save"},
        "btn_update": {"tr": "Güncelle", "en": "Update"},
        "btn_test": {"tr": "Test", "en": "Test"},
        "btn_connect": {"tr": "Baglan", "en": "Connect"},
        "btn_cancel": {"tr": "Iptal", "en": "Cancel"},
        "btn_confirm": {"tr": "Onayla", "en": "Confirm"},

        # Settings
        "settings_general": {"tr": "Genel Ayarlar", "en": "General Settings"},
        "settings_language": {"tr": "Dil", "en": "Language"},
        "lang_option_turkce": {"tr": "Turkce", "en": "Turkce"},
        "lang_option_english": {"tr": "English", "en": "English"},
        "settings_auto_status_check": {
            "tr": "Otomatik durum kontrolu (30 sn)",
            "en": "Auto status check (30 sec)",
        },

        # Add/Edit Machine form
        "field_machine_name": {"tr": "Makine Adi", "en": "Machine Name"},
        "field_ip_address": {"tr": "IP Adresi", "en": "IP Address"},
        "field_port": {"tr": "Port", "en": "Port"},
        "field_username": {"tr": "Kullanici", "en": "Username"},
        "field_password": {"tr": "Sifre", "en": "Password"},
        "field_protocol": {"tr": "Protokol", "en": "Protocol"},
        "label_selected_dir": {"tr": "Seçili dizin: {dir}", "en": "Selected directory: {dir}"},
        "label_selected_dir_empty": {"tr": "Seçili dizin: ", "en": "Selected directory: "},

        # Machine list columns
        "col_machine": {"tr": "Makine", "en": "Machine"},
        "col_ip": {"tr": "IP", "en": "IP"},
        "col_port": {"tr": "Port", "en": "Port"},
        "col_protocol": {"tr": "Protokol", "en": "Protocol"},
        "col_status": {"tr": "Durum", "en": "Status"},

        # Protocol labels (UI)
        "proto_focas_cf_text_long": {"tr": "FOCAS  CF_TEXT", "en": "FOCAS  CF_TEXT"},
        "proto_focas_cnc_memory_long": {"tr": "FOCAS  CNC BELLEK", "en": "FOCAS  CNC MEMORY"},
        "proto_cf_text_short": {"tr": "CF_TEXT", "en": "CF_TEXT"},
        "proto_cnc_memory_short": {"tr": "CNC BELLEK", "en": "CNC MEMORY"},

        # File dialogs
        "dialog_select_nc_title": {"tr": "NC Dosyası Seç", "en": "Select NC File"},
        "filetype_nc_files": {"tr": "NC Dosyaları", "en": "NC Files"},
        "filetype_all_files": {"tr": "Tüm Dosyalar", "en": "All Files"},
        "dialog_select_backup_folder": {"tr": "Yedek Klasörü Seçin", "en": "Select Backup Folder"},

        # Messagebox titles
        "title_error": {"tr": "Hata", "en": "Error"},
        "title_warning": {"tr": "Uyarı", "en": "Warning"},
        "title_success": {"tr": "Başarılı", "en": "Success"},
        "title_confirm": {"tr": "Onay", "en": "Confirm"},
        "title_data_error": {"tr": "Veri Hatası", "en": "Data Error"},
        "title_file_exists": {"tr": "Dosya Zaten Var", "en": "File Already Exists"},
        "title_file_exists_short": {"tr": "Dosya Mevcut", "en": "File Exists"},

        # Generic / common messages
        "msg_select_machine": {"tr": "Lütfen bir makine seçin!", "en": "Please select a machine!"},
        "msg_select_directory": {"tr": "Lütfen bir dizin seçin!", "en": "Please select a directory!"},
        "msg_select_file": {"tr": "Lütfen bir dosya seçin!", "en": "Please select a file!"},
        "msg_ip_empty": {"tr": "IP adresi boş olamaz!", "en": "IP address cannot be empty!"},
        "msg_machine_name_empty": {"tr": "Makine adı boş olamaz!", "en": "Machine name cannot be empty!"},
        "msg_machine_name_exists": {
            "tr": "Bu isimde bir makine zaten var!",
            "en": "A machine with this name already exists!",
        },
        "msg_machine_name_exists_other": {
            "tr": "Bu isimde başka bir makine zaten var!",
            "en": "Another machine with this name already exists!",
        },
        "msg_machine_added": {"tr": "{name} başarıyla eklendi!", "en": "{name} added successfully!"},
        "msg_machine_updated": {"tr": "{name} başarıyla güncellendi!", "en": "{name} updated successfully!"},
        "msg_save_error": {"tr": "Kaydetme hatası: {error}", "en": "Save error: {error}"},
        "msg_update_error": {"tr": "Güncelleme hatası: {error}", "en": "Update error: {error}"},
        "msg_connection_ok_select_dir": {
            "tr": "Bağlantı başarılı! Lütfen bir dizin seçin.",
            "en": "Connection successful! Please select a directory.",
        },
        "msg_connection_error": {"tr": "Bağlantı hatası: {error}", "en": "Connection error: {error}"},
        "msg_test_ok": {"tr": "Test başarılı!", "en": "Test successful!"},
        "msg_test_fail": {"tr": "Test başarısız: {error}", "en": "Test failed: {error}"},
        "msg_cannot_connect_machine": {
            "tr": "{machine_name} makinesine bağlanılamıyor!",
            "en": "Cannot connect to machine {machine_name}!",
        },

        # Corrupted config
        "msg_config_corrupt_exit": {
            "tr": "Yapılandırma dosyası ({config_file}) bozuk veya okunamaz durumda!\n\nVeri kaybını önlemek için uygulama kapatılacak.",
            "en": "Configuration file ({config_file}) is corrupted or unreadable!\n\nThe app will close to prevent data loss.",
        },

        # Status / progress texts
        "status_online": {"tr": "ONLINE", "en": "ONLINE"},
        "status_offline": {"tr": "OFFLINE", "en": "OFFLINE"},
        "status_connecting_machine": {
            "tr": "{machine_name} makinesine bağlanılıyor...",
            "en": "Connecting to {machine_name}...",
        },
        "status_connecting_cnc_memory": {"tr": "CNC belleğine bağlanılıyor...", "en": "Connecting to CNC memory..."},
        "status_connecting_cf_text": {"tr": "CF_TEXT'e bağlanılıyor...", "en": "Connecting to CF_TEXT..."},
        "status_sending_cnc_memory_progress": {
            "tr": "CNC belleğine gönderiliyor: {progress:.1f}% ({sent}/{total})",
            "en": "Sending to CNC memory: {progress:.1f}% ({sent}/{total})",
        },
        "status_sending_cf_text_progress": {
            "tr": "CF_TEXT'e gönderiliyor: {progress:.1f}% ({sent}/{total})",
            "en": "Sending to CF_TEXT: {progress:.1f}% ({sent}/{total})",
        },
        "status_sending_ftp_progress": {
            "tr": "Gönderiliyor: {progress:.1f}% ({sent}/{total})",
            "en": "Sending: {progress:.1f}% ({sent}/{total})",
        },
        "status_transferring_as_o": {"tr": "O{o_num:04d} olarak transfer ediliyor...", "en": "Transferring as O{o_num:04d}..."},
        "status_sending_as_o_progress": {"tr": "O{o_num:04d} olarak gönderiliyor: {progress:.1f}%", "en": "Sending as O{o_num:04d}: {progress:.1f}%"},
        "status_transfer_complete": {"tr": "Transfer tamamlandı!", "en": "Transfer complete!"},
        "status_transfer_complete_cnc_memory": {"tr": "Transfer tamamlandı! (CNC Bellek)", "en": "Transfer complete! (CNC Memory)"},
        "status_transfer_complete_cf_text": {"tr": "Transfer tamamlandı! (CF_TEXT)", "en": "Transfer complete! (CF_TEXT)"},
        "status_transfer_canceled": {"tr": "Transfer iptal edildi.", "en": "Transfer canceled."},
        "status_canceled_short": {"tr": "İptal edildi", "en": "Canceled"},

        # Backup
        "status_backup_starting": {"tr": "CNC yedekleme başlatılıyor...", "en": "Starting CNC backup..."},
        "status_backing_up_item": {"tr": "Yedekleniyor: {item}...", "en": "Backing up: {item}..."},
        "status_backup_complete": {"tr": "Yedekleme tamamlandı!", "en": "Backup complete!"},
        "backup_item_parameters": {"tr": "PARAMETRE", "en": "PARAMETERS"},
        "backup_item_pitch_error": {"tr": "PITCH_ERROR", "en": "PITCH ERROR"},
        "backup_item_macro_variables": {"tr": "MAKRO_DEGISKEN", "en": "MACRO VARIABLES"},
        "backup_item_work_offset": {"tr": "WORK_OFFSET", "en": "WORK OFFSET"},
        "backup_item_nc_programs": {"tr": "NC PROGRAMLAR", "en": "NC PROGRAMS"},
        "msg_backup_complete": {
            "tr": "{machine_name} yedekleme tamamlandı!\n\nKonum: {save_dir}\n\nParametre, Pitch Error, Makro Değişken,\nWork Offset ve {count} NC program yedeklendi.",
            "en": "Backup completed for {machine_name}!\n\nLocation: {save_dir}\n\nParameters, Pitch Error, Macro Variables,\nWork Offset and {count} NC programs were backed up.",
        },
        "msg_backup_error": {"tr": "Yedekleme hatası: {error}", "en": "Backup error: {error}"},

        # FOCAS / directory listing UI
        "msg_cf_card_access_active": {"tr": "(CF kart erişimi aktif)", "en": "(CF card access active)"},
        "list_header_cnc_memory": {"tr": "--- CNC Bellek ({count} program) ---", "en": "--- CNC Memory ({count} programs) ---"},
        "list_header_cf_text": {"tr": "--- CF_TEXT ({count} program) ---", "en": "--- CF_TEXT ({count} programs) ---"},
        "label_target": {"tr": "Hedef: {target}", "en": "Target: {target}"},
        "msg_focas_connection_success": {
            "tr": "FOCAS bağlantısı başarılı!\nCNC Bellek: {cnc_count} program\nHedef: {target}",
            "en": "FOCAS connection successful!\nCNC Memory: {cnc_count} programs\nTarget: {target}",
        },
        "msg_focas_connection_failed": {"tr": "FOCAS bağlantısı kurulamadı!", "en": "FOCAS connection could not be established!"},
        "msg_focas_test_success": {"tr": "FOCAS bağlantı testi başarılı!", "en": "FOCAS connection test successful!"},
        "msg_focas_test_failed": {"tr": "FOCAS bağlantı testi başarısız!", "en": "FOCAS connection test failed!"},
        "msg_focas_transfer_error": {"tr": "FOCAS transfer hatası: {error}", "en": "FOCAS transfer error: {error}"},

        # Delete program dialog
        "dlg_delete_program_title": {"tr": "{machine_name} - Program Sil", "en": "{machine_name} - Delete Program"},
        "dlg_delete_program_label": {"tr": "Silinecek O Numarası:", "en": "Program number to delete:"},
        "dlg_delete_program_hint": {"tr": "Örn: 11, 100, 1234", "en": "e.g.: 11, 100, 1234"},
        "msg_enter_valid_o_number": {"tr": "Geçerli bir O numarası girin!", "en": "Enter a valid O number!"},
        "msg_confirm_delete_program_500plus": {
            "tr": "O{o_num:04d} numaralı programı silmek istediğinize emin misiniz?\n\nBu numara 500 ve üzerinde!",
            "en": "Are you sure you want to delete program O{o_num:04d}?\n\nThis number is 500 or above!",
        },
        "msg_confirm_delete_program": {
            "tr": "O{o_num:04d} numaralı programı silmek istiyor musunuz?",
            "en": "Do you want to delete program O{o_num:04d}?",
        },
        "msg_program_deleted_no_file": {
            "tr": "O{o_num:04d} silindi.\nTransfer için dosya seçili değil.",
            "en": "O{o_num:04d} deleted.\nNo file selected for transfer.",
        },
        "msg_program_delete_failed_code": {
            "tr": "O{o_num:04d} silinemedi (kod: {code})",
            "en": "O{o_num:04d} could not be deleted (code: {code})",
        },

        # Filename fix dialog
        "dlg_fix_filename_title": {"tr": "Dosya Adi Duzeltme", "en": "Filename Fix"},
        "dlg_fix_filename_header": {"tr": "Dosya adi CNC uyumlu degil!", "en": "Filename is not CNC-compatible!"},
        "dlg_fix_filename_original": {"tr": "Orjinal: {filename}", "en": "Original: {filename}"},
        "dlg_fix_filename_suggested": {"tr": "Duzeltilmis:", "en": "Fixed:"},

        # Confirmations / overwrite
        "msg_confirm_delete_machine": {
            "tr": "{machine_name} makinesini silmek istediğinize emin misiniz?",
            "en": "Are you sure you want to delete machine {machine_name}?",
        },
        "msg_cf_text_file_exists_overwrite": {
            "tr": "'{filename}' CF_TEXT'te zaten var!\n\nÜzerine yazmak istiyor musunuz?",
            "en": "'{filename}' already exists in CF_TEXT.\n\nDo you want to overwrite it?",
        },
        "msg_ftp_file_exists_overwrite": {
            "tr": "{filename} dosyası sunucuda zaten mevcut.\n\nÜzerine yazmak istiyor musunuz?",
            "en": "{filename} already exists on the server.\n\nDo you want to overwrite it?",
        },

        # Transfer messages
        "msg_transfer_error": {"tr": "Transfer hatası: {error}", "en": "Transfer error: {error}"},
        "msg_upload_error": {"tr": "Yükleme hatası: {error}", "en": "Upload error: {error}"},
        "msg_sent_to_machine_as_o": {
            "tr": "{filename} → O{o_num:04d} olarak {machine_name} makinesine gönderildi!",
            "en": "{filename} → sent to {machine_name} as O{o_num:04d}!",
        },
        "msg_transfer_complete_ftp": {
            "tr": "{filename} dosyası {machine_name} makinesine başarıyla gönderildi!",
            "en": "{filename} sent to {machine_name} successfully!",
        },
        "msg_transfer_complete_cnc_memory": {
            "tr": "{filename} dosyası {machine_name} makinesine (CNC Bellek) başarıyla gönderildi!",
            "en": "{filename} sent to {machine_name} (CNC Memory) successfully!",
        },
        "msg_transfer_complete_cf_text": {
            "tr": "{filename} dosyası {machine_name} makinesine (CF_TEXT) başarıyla gönderildi!",
            "en": "{filename} sent to {machine_name} (CF_TEXT) successfully!",
        },

        # Low-level (raised) errors
        "err_focas_dll_not_loaded": {"tr": "FOCAS DLL yüklenemedi!", "en": "FOCAS DLL could not be loaded!"},
        "err_focas_connection_error_code": {"tr": "FOCAS bağlantı hatası (kod: {code})", "en": "FOCAS connection error (code: {code})"},
        "err_focas_transfer_start_error_code": {"tr": "FOCAS transfer başlatma hatası (kod: {code})", "en": "FOCAS transfer start error (code: {code})"},
        "err_focas_data_send_error_code": {"tr": "FOCAS veri gönderme hatası (kod: {code})", "en": "FOCAS data send error (code: {code})"},
        "err_focas_transfer_end_error_code": {"tr": "FOCAS transfer sonlandırma hatası (kod: {code})", "en": "FOCAS transfer end error (code: {code})"},
        "err_file_content_empty": {"tr": "Dosya içeriği boş!", "en": "File content is empty!"},
        "err_cnc_memory_full": {"tr": "CNC belleği dolu! Eski programları silip tekrar deneyin.", "en": "CNC memory is full! Delete old programs and try again."},
        "err_cnc_program_save_failed_conflict": {
            "tr": "CNC programı kaydedemedi (aynı isim veya O numarası çakışması).",
            "en": "CNC program could not be saved (name or O-number conflict).",
        },
        "err_cnc_program_save_failed_o_conflict": {"tr": "CNC programı kaydedemedi (O numarası çakışması).", "en": "CNC program could not be saved (O-number conflict)."},
        "err_cnc_mem_transfer_start_error_code": {"tr": "CNC bellek transfer başlatma hatası (kod: {code})", "en": "CNC memory transfer start error (code: {code})"},
        "err_cnc_mem_data_send_error_code": {"tr": "CNC bellek veri gönderme hatası (kod: {code})", "en": "CNC memory data send error (code: {code})"},
        "err_cnc_mem_transfer_end_error_code": {"tr": "CNC bellek transfer sonlandırma hatası (kod: {code})", "en": "CNC memory transfer end error (code: {code})"},
    }

    def __init__(self, root):
        self.root = root
        self.lang = "tr"

        # CustomTkinter ayarları
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        # Chrome/Material tarzı tipografi
        try:
            ctk.ThemeManager.theme["CTkFont"]["family"] = "Segoe UI"
        except Exception:
            pass

        self.root.title(self.t("app_title"))
        self.root.geometry("1000x700")
        self.root.minsize(860, 580)

        self.app_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_file = os.path.join(self.app_dir, "machines.json")
        self.log_file = os.path.join(self.app_dir, "transfer_history.log")
        self.machines = self.load_machines()
        self.lang = self._load_language_from_config()
        self.root.title(self.t("app_title"))

        # CIMCO'dan gelen dosya
        self.cimco_file = " ".join(sys.argv[1:]).strip() if len(sys.argv) > 1 else ""
        if not os.path.isfile(self.cimco_file):
            self.cimco_file = ""
        self.cimco_display_name = ""
        if self.cimco_file:
            basename = os.path.basename(self.cimco_file)
            if basename.startswith('~') and basename.lower().endswith('.tmx'):
                try:
                    with open(self.cimco_file, 'r', errors='ignore') as f:
                        for line in f:
                            line = line.strip()
                            if not line or line == '%':
                                continue
                            if line.startswith('O') and len(line) >= 2:
                                self.cimco_display_name = line.split('(')[0].strip()
                                if '(' in line:
                                    comment = line[line.index('(')+1:line.index(')')] if ')' in line else ''
                                    if comment:
                                        self.cimco_display_name = comment.strip() + ".NC"
                                break
                            elif line.startswith('<') and '>' in line:
                                self.cimco_display_name = line[1:line.index('>')] + ".NC"
                                break
                            else:
                                break
                except:
                    pass
                if not self.cimco_display_name:
                    self.cimco_display_name = basename

        self.auto_check_var = tk.BooleanVar(value=True)
        self._save_button_mode = "save"
        self.selected_directory = ""

        # Uyumluluk icin eski renk referanslari
        self.colors = {
            # Chrome açık tema + Material paleti
            'bg': '#f1f3f4', 'card_bg': '#ffffff', 'card_border': '#dadce0',
            'primary': '#4285f4', 'primary_hover': '#3367d6',
            'success': '#34a853', 'success_hover': '#2d9a47',
            'danger': '#ea4335', 'danger_hover': '#d33426',
            'warning': '#fbbc04', 'warning_hover': '#f0a900',
            'info': '#1a73e8', 'info_hover': '#185abc',
            'secondary': '#dadce0', 'secondary_hover': '#c7cace',
            'text': '#202124', 'text_muted': '#5f6368', 'text_dim': '#9aa0a6',
            'border': '#dadce0', 'border_focus': '#4285f4',
            'accent': '#4285f4', 'online': '#34a853', 'offline': '#ea4335',
            'tab_bg': '#e8eaed',
        }
        self.light_colors = self.colors
        self.dark_colors = self.colors
        self.root.configure(fg_color=self.colors['bg'])

        # Ana sekmeler
        self.notebook = ctk.CTkTabview(
            self.root,
            corner_radius=12,
            fg_color=self.colors['bg'],
            border_width=1,
            border_color=self.colors['card_border'],
            segmented_button_fg_color=self.colors['tab_bg'],
            segmented_button_selected_color=self.colors['primary'],
            segmented_button_selected_hover_color=self.colors['primary_hover'],
            segmented_button_unselected_color=self.colors['secondary'],
            segmented_button_unselected_hover_color=self.colors['secondary_hover'],
            text_color=self.colors['text'],
        )
        self.notebook.pack(fill='both', expand=True, padx=12, pady=12)

        self.notebook.add(self.t("tab_transfer"))
        self.notebook.add(self.t("tab_machines"))
        self.notebook.add(self.t("tab_new_machine"))
        self.notebook.add(self.t("tab_settings"))
        self.notebook.set(self.t("tab_transfer"))

        self.create_transfer_tab()
        self.create_machines_tab()
        self.create_add_machine_tab()
        self.create_settings_tab()

        self.check_machine_status()

    def t(self, key):
        entry = self.TRANSLATIONS.get(key)
        if not entry:
            return key
        return entry.get(self.lang, entry.get("tr", next(iter(entry.values()), key)))

    def _load_language_from_config(self):
        try:
            settings = self.machines.get("settings", {}) if isinstance(self.machines, dict) else {}
            lang = settings.get("language", "tr")
        except Exception:
            lang = "tr"
        return lang if lang in ("tr", "en") else "tr"

    def _save_language_to_config(self):
        self.machines.setdefault("settings", {})["language"] = self.lang
        self.save_machines()

    def _lang_to_choice(self, lang):
        return self.t("lang_option_english") if lang == "en" else self.t("lang_option_turkce")

    def _choice_to_lang(self, choice):
        return "en" if choice == self.t("lang_option_english") else "tr"

    def configure_styles(self):
        """Uyumluluk - ctk kendi stillerini yonetiyor"""
        pass
    
    def load_machines(self):
        """JSON dosyasından makineleri yükle"""
        bak_file = self.config_file + ".bak"
        if os.path.exists(self.config_file):
            try:
                with open(self.config_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except Exception as e:
                if os.path.exists(bak_file):
                    try:
                        with open(bak_file, 'r', encoding='utf-8') as f:
                            return json.load(f)
                    except: pass
                
                # Do not return empty list if file exists but is corrupted
                from tkinter import messagebox
                messagebox.showerror(
                    self.t("title_data_error"),
                    self.t("msg_config_corrupt_exit").format(config_file=self.config_file),
                )
                sys.exit(1)
        else:
            # Varsayılan makineler
            default = {
                "machines": [
                    {
                        "name": "Torna",
                        "host": "192.168.1.22",
                        "port": 8193,
                        "user": "",
                        "password": "",
                        "directory": "",
                        "protocol": "focas_mem",
                        "status": "offline"
                    },
                    {
                        "name": "Freze",
                        "host": "192.168.1.49",
                        "port": 8193,
                        "user": "",
                        "password": "",
                        "directory": "",
                        "protocol": "focas",
                        "status": "offline"
                    },
                    {
                        "name": "Genel Makine",
                        "host": "192.168.1.13",
                        "port": 21,
                        "user": "anonymous",
                        "password": "user@example.com",
                        "directory": "/POST/",
                        "protocol": "ftp",
                        "status": "offline"
                    }
                ]
            }
            self.save_machines(default)
            return default
    
    def save_machines(self, machines=None):
        """Makineleri JSON dosyasına kaydet"""
        if machines is None:
            machines = self.machines
        try:
            # Sadece 1 adet .bak dosyası oluştur/güncelle
            if os.path.exists(self.config_file):
                shutil.copy2(self.config_file, self.config_file + ".bak")
            
            with open(self.config_file, 'w', encoding='utf-8') as f:
                json.dump(machines, f, indent=4, ensure_ascii=False)
        except Exception as e:
            print(f"Dosya kaydetme hatası: {e}")
    
    def normalize_filename(self, filename):
        """Dosya adını CNC uyumlu hale getir"""
        # Türkçe karakterleri değiştir
        tr_chars = {'ç': 'c', 'ğ': 'g', 'ı': 'i', 'ö': 'o', 'ş': 's', 'ü': 'u',
                   'Ç': 'C', 'Ğ': 'G', 'İ': 'I', 'Ö': 'O', 'Ş': 'S', 'Ü': 'U'}
        
        for tr, en in tr_chars.items():
            filename = filename.replace(tr, en)
        
        # Boşlukları alt çizgi yap
        filename = filename.replace(' ', '_')
        
        # Sadece alfanumerik, alt çizgi, tire ve nokta bırak
        filename = re.sub(r'[^a-zA-Z0-9._-]', '', filename)
        
        return filename
    
    def create_transfer_tab(self):
        """Transfer sekmesi - CustomTkinter modern tasarim"""
        tab = self.notebook.tab(self.t("tab_transfer"))
        tab.configure(fg_color=self.colors['bg'])

        # ── Dosya Secimi ──
        file_frame = ctk.CTkFrame(
            tab,
            corner_radius=12,
            fg_color=self.colors['card_bg'],
            border_width=1,
            border_color=self.colors['card_border'],
        )
        file_frame.pack(fill='x', padx=8, pady=(4, 8))

        file_inner = ctk.CTkFrame(file_frame, fg_color="transparent")
        file_inner.pack(fill='x', padx=12, pady=10)

        ctk.CTkButton(file_inner, text=self.t("btn_select_file"), width=120, height=36,
                      corner_radius=6, font=ctk.CTkFont(size=13, weight="bold"),
                      fg_color=self.colors['primary'], hover_color=self.colors['primary_hover'],
                      command=self.select_file).pack(side='left', padx=(0, 12))

        self.file_label = ctk.CTkLabel(file_inner, text="",
                                       font=ctk.CTkFont(family="Consolas", size=14, weight="bold"),
                                       text_color=self.colors['info'], anchor='w')
        self.file_label.pack(side='left', fill='x', expand=True)

        if self.cimco_file:
            display = self.cimco_display_name if self.cimco_display_name else os.path.basename(self.cimco_file)
            self.file_label.configure(text=display)
            self.current_file = self.cimco_file
        else:
            self.file_label.configure(text=self.t("lbl_no_file_selected"), text_color=self.colors['text_dim'])
            self.current_file = ""

        # ── Makine Kartlari ──
        self.machine_buttons_frame = ctk.CTkFrame(tab, fg_color="transparent")
        self.machine_buttons_frame.pack(fill='both', expand=True, padx=4, pady=(0, 8))

        self.create_machine_buttons()

        # ── Progress ──
        progress_frame = ctk.CTkFrame(
            tab,
            corner_radius=12,
            height=60,
            fg_color=self.colors['card_bg'],
            border_width=1,
            border_color=self.colors['card_border'],
        )
        progress_frame.pack(fill='x', padx=8, pady=(0, 4))

        self.progress_label = ctk.CTkLabel(progress_frame, text=self.t("status_ready"),
                                           font=ctk.CTkFont(size=12),
                                           text_color=self.colors['text_muted'], anchor='w')
        self.progress_label.pack(fill='x', padx=14, pady=(8, 2))

        self.progress_bar = ctk.CTkProgressBar(
            progress_frame,
            height=6,
            corner_radius=3,
            progress_color=self.colors['primary'],
            fg_color=self.colors['tab_bg'],
        )
        self.progress_bar.pack(fill='x', padx=14, pady=(0, 10))
        self.progress_bar.set(0)
    
    def create_machine_buttons(self):
        """Makine kartlarini olustur - modern CTk tasarim"""
        for w in self.machine_buttons_frame.winfo_children():
            w.destroy()

        proto_map = {
            'ftp': 'FTP',
            'focas': self.t("proto_focas_cf_text_long"),
            'focas_mem': self.t("proto_focas_cnc_memory_long"),
        }

        col = 0
        for machine in self.machines['machines']:
            online = machine['status'] == 'online'
            protocol = machine.get('protocol', 'ftp')
            proto_text = proto_map.get(protocol, protocol.upper())

            # Kart
            card = ctk.CTkFrame(
                self.machine_buttons_frame,
                corner_radius=12,
                fg_color=self.colors['card_bg'],
                border_width=1,
                border_color=self.colors['card_border'],
            )
            card.grid(row=0, column=col, padx=6, pady=4, sticky='nsew')

            # Durum cubugu (ust)
            status_color = self.colors['online'] if online else self.colors['offline']
            status_bar = ctk.CTkFrame(card, height=4, corner_radius=0,
                                      fg_color=status_color)
            status_bar.pack(fill='x', padx=12, pady=(10, 0))

            # Makine adi
            name_frame = ctk.CTkFrame(card, fg_color="transparent")
            name_frame.pack(fill='x', padx=16, pady=(8, 0))

            status_text = self.t("status_online") if online else self.t("status_offline")
            ctk.CTkLabel(name_frame, text=machine['name'],
                        font=ctk.CTkFont(size=20, weight="bold"),
                        text_color=self.colors['text']).pack(side='left')
            ctk.CTkLabel(name_frame, text=status_text,
                        font=ctk.CTkFont(size=11, weight="bold"),
                        text_color=status_color).pack(side='right')

            # IP + Protokol
            ctk.CTkLabel(card, text=f"{machine['host']}:{machine['port']}",
                        font=ctk.CTkFont(family="Consolas", size=11),
                        text_color=self.colors['text_muted']).pack(anchor='w', padx=16, pady=(2, 0))

            ctk.CTkLabel(card, text=proto_text,
                        font=ctk.CTkFont(size=10, weight="bold"),
                        text_color=self.colors['primary'] if protocol != 'ftp' else self.colors['success'],
                        ).pack(anchor='w', padx=16, pady=(0, 12))

            # GONDER butonu
            ctk.CTkButton(card, text=self.t("btn_send"), height=48,
                         corner_radius=8,
                         font=ctk.CTkFont(size=16, weight="bold"),
                         fg_color=self.colors['primary'] if online else self.colors['secondary'],
                         hover_color=self.colors['primary_hover'] if online else self.colors['secondary'],
                         text_color="#ffffff",
                         text_color_disabled=self.colors['text_dim'],
                         state="normal" if online else "disabled",
                         command=lambda m=machine: self.transfer_file(m)
                         ).pack(fill='x', padx=12, pady=(0, 6))

            # Alt butonlar
            if protocol in ('focas', 'focas_mem'):
                sub = ctk.CTkFrame(card, fg_color="transparent")
                sub.pack(fill='x', padx=12, pady=(0, 10))

                if protocol == 'focas_mem':
                    ctk.CTkButton(sub, text=self.t("btn_delete_send"), height=32,
                                  corner_radius=6,
                                  font=ctk.CTkFont(size=12, weight="bold"),
                                  fg_color=self.colors['danger'] if online else self.colors['secondary'],
                                  hover_color=self.colors['danger_hover'] if online else self.colors['secondary'],
                                 text_color="#ffffff",
                                 text_color_disabled=self.colors['text_dim'],
                                 state="normal" if online else "disabled",
                                 command=lambda m=machine: self.focas_mem_delete_dialog(m)
                                 ).pack(side='left', fill='x', expand=True, padx=(0, 3))

                ctk.CTkButton(sub, text=self.t("btn_backup"), height=32,
                              corner_radius=6,
                              font=ctk.CTkFont(size=12, weight="bold"),
                              fg_color=self.colors['warning'] if online else self.colors['secondary'],
                              hover_color=self.colors['warning_hover'] if online else self.colors['secondary'],
                             text_color=self.colors['text'],
                             text_color_disabled=self.colors['text_dim'],
                             state="normal" if online else "disabled",
                             command=lambda m=machine: self.focas_backup(m)
                             ).pack(side='left', fill='x', expand=True, padx=(3, 0))

            self.machine_buttons_frame.grid_columnconfigure(col, weight=1)
            self.machine_buttons_frame.grid_rowconfigure(0, weight=1)
            col += 1
    
    def create_machines_tab(self):
        """Makineler sekmesi"""
        tab = self.notebook.tab(self.t("tab_machines"))
        tab.configure(fg_color=self.colors['bg'])

        # Treeview (tkinter - ctk'da yok)
        tree_frame = ctk.CTkFrame(
            tab,
            corner_radius=12,
            fg_color=self.colors['card_bg'],
            border_width=1,
            border_color=self.colors['card_border'],
        )
        tree_frame.pack(fill='both', expand=True, padx=8, pady=(4, 8))

        style = ttk.Style()
        style.theme_use('clam')
        style.configure(
            'Chrome.Treeview',
            background=self.colors['card_bg'],
            foreground=self.colors['text'],
            fieldbackground=self.colors['card_bg'],
            borderwidth=0,
            font=('Segoe UI', 10),
            rowheight=28,
        )
        style.configure(
            'Chrome.Treeview.Heading',
            background=self.colors['tab_bg'],
            foreground=self.colors['text'],
            font=('Segoe UI', 10, 'bold'),
            relief='flat',
        )
        style.map(
            'Chrome.Treeview',
            background=[('selected', self.colors['primary'])],
            foreground=[('selected', '#ffffff')],
        )

        columns = ('IP', 'Port', 'Protokol', 'Durum')
        self.machines_tree = ttk.Treeview(tree_frame, columns=columns,
                                         show='tree headings',
                                         style='Chrome.Treeview', height=10)
        self.machines_tree.heading('#0', text=self.t("col_machine"))
        self.machines_tree.column('#0', width=160, minwidth=100)
        widths = {'IP': 140, 'Port': 70, 'Protokol': 130, 'Durum': 90}
        header_map = {
            "IP": self.t("col_ip"),
            "Port": self.t("col_port"),
            "Protokol": self.t("col_protocol"),
            "Durum": self.t("col_status"),
        }
        for c in columns:
            self.machines_tree.heading(c, text=header_map.get(c, c))
            self.machines_tree.column(c, width=widths.get(c, 100), minwidth=60)
        self.machines_tree.pack(fill='both', expand=True, padx=8, pady=8)

        # Butonlar
        btn_frame = ctk.CTkFrame(
            tab,
            corner_radius=12,
            height=50,
            fg_color=self.colors['card_bg'],
            border_width=1,
            border_color=self.colors['card_border'],
        )
        btn_frame.pack(fill='x', padx=8, pady=(0, 4))

        btn_inner = ctk.CTkFrame(btn_frame, fg_color="transparent")
        btn_inner.pack(fill='x', padx=8, pady=8)

        for text, color, hover, text_color, cmd in [
            (self.t("btn_refresh"), self.colors['primary'], self.colors['primary_hover'], "#ffffff", self.refresh_machines_list),
            (self.t("btn_edit"), self.colors['warning'], self.colors['warning_hover'], self.colors['text'], self.edit_machine),
            (self.t("btn_delete"), self.colors['danger'], self.colors['danger_hover'], "#ffffff", self.delete_machine),
        ]:
            ctk.CTkButton(btn_inner, text=text, width=100, height=32,
                         corner_radius=6, fg_color=color, hover_color=hover,
                         text_color=text_color, text_color_disabled=self.colors['text_dim'],
                         font=ctk.CTkFont(size=12, weight="bold"),
                         command=cmd).pack(side='left', padx=(0, 6))

        self.refresh_machines_list()
    
    def create_add_machine_tab(self):
        """Makine ekleme sekmesi"""
        tab = self.notebook.tab(self.t("tab_new_machine"))
        tab.configure(fg_color=self.colors['bg'])

        # Scrollable form
        form_frame = ctk.CTkFrame(
            tab,
            corner_radius=12,
            fg_color=self.colors['card_bg'],
            border_width=1,
            border_color=self.colors['card_border'],
        )
        form_frame.pack(fill='x', padx=8, pady=(4, 8))

        form_inner = ctk.CTkFrame(form_frame, fg_color="transparent")
        form_inner.pack(fill='x', padx=16, pady=14)

        fields = [
            (self.t("field_machine_name"), "name_entry", ""),
            (self.t("field_ip_address"), "host_entry", ""),
            (self.t("field_port"), "port_entry", "21"),
            (self.t("field_username"), "user_entry", "anonymous"),
            (self.t("field_password"), "password_entry", ""),
        ]

        self.add_form_vars = {}
        for i, (label_text, var_name, default) in enumerate(fields):
            ctk.CTkLabel(form_inner, text=label_text,
                        font=ctk.CTkFont(size=12),
                        text_color=self.colors['text_muted']).grid(row=i, column=0, sticky='w', padx=(0, 12), pady=6)

            entry = ctk.CTkEntry(form_inner, width=300, height=34, corner_radius=6,
                                font=ctk.CTkFont(family="Consolas", size=12),
                                fg_color=self.colors['card_bg'],
                                text_color=self.colors['text'],
                                border_color=self.colors['card_border'])
            entry.grid(row=i, column=1, sticky='ew', pady=6)
            if default:
                entry.insert(0, default)
            self.add_form_vars[var_name] = entry

        # Protokol
        protocol_row = len(fields)
        ctk.CTkLabel(form_inner, text=self.t("field_protocol"),
                    font=ctk.CTkFont(size=12),
                    text_color=self.colors['text_muted']).grid(row=protocol_row, column=0, sticky='w', padx=(0, 12), pady=6)

        protocol_combo = ctk.CTkComboBox(form_inner, values=["FTP", "FOCAS", "FOCAS_MEM"],
                                         width=300, height=34, corner_radius=6,
                                         font=ctk.CTkFont(size=12), state='readonly',
                                         fg_color=self.colors['card_bg'],
                                         border_color=self.colors['card_border'],
                                         button_color=self.colors['tab_bg'],
                                         button_hover_color=self.colors['secondary'],
                                         dropdown_fg_color=self.colors['card_bg'],
                                         dropdown_hover_color=self.colors['tab_bg'],
                                         dropdown_text_color=self.colors['text'],
                                         text_color=self.colors['text'])
        protocol_combo.set("FTP")
        protocol_combo.grid(row=protocol_row, column=1, sticky='ew', pady=6)
        self.add_form_vars['protocol_combo'] = protocol_combo

        form_inner.grid_columnconfigure(1, weight=1)

        ctk.CTkButton(form_inner, text=self.t("btn_connect"), height=38, corner_radius=6,
                      fg_color=self.colors['primary'], hover_color=self.colors['primary_hover'],
                      font=ctk.CTkFont(size=13, weight="bold"),
                      command=self.connect_and_list_dirs
                      ).grid(row=len(fields)+1, column=0, columnspan=2, pady=(12, 0), sticky='ew')

        # Dizin secimi
        self.dir_frame = ctk.CTkFrame(
            tab,
            corner_radius=12,
            fg_color=self.colors['card_bg'],
            border_width=1,
            border_color=self.colors['card_border'],
        )
        self.dir_frame.pack(fill='both', expand=True, padx=8, pady=(0, 4))

        self.dir_listbox = tk.Listbox(self.dir_frame, height=6,
                                     bg=self.colors['card_bg'], fg=self.colors['text'],
                                     font=('Segoe UI', 10),
                                     selectbackground=self.colors['primary'],
                                     selectforeground='#ffffff',
                                     relief='flat', bd=0, highlightthickness=0)
        self.dir_listbox.pack(fill='both', expand=True, padx=10, pady=(10, 4))

        self.selected_dir_label = ctk.CTkLabel(self.dir_frame, text="",
                                               font=ctk.CTkFont(size=11),
                                               text_color=self.colors['text_muted'])
        self.selected_dir_label.pack(padx=10, pady=(0, 4))

        btn_row = ctk.CTkFrame(self.dir_frame, fg_color="transparent")
        btn_row.pack(pady=(0, 10))

        self.test_button = ctk.CTkButton(btn_row, text=self.t("btn_test"), width=90, height=32,
                                        corner_radius=6,
                                        fg_color=self.colors['secondary'], hover_color=self.colors['secondary'],
                                        text_color=self.colors['text'],
                                        text_color_disabled=self.colors['text_dim'],
                                        font=ctk.CTkFont(size=12, weight="bold"),
                                        state="disabled",
                                        command=self.test_connection)
        self.test_button.pack(side='left', padx=(0, 6))

        self.save_button = ctk.CTkButton(btn_row, text=self.t("btn_save"), width=90, height=32,
                                        corner_radius=6,
                                        fg_color=self.colors['secondary'], hover_color=self.colors['secondary'],
                                        text_color=self.colors['text'],
                                        text_color_disabled=self.colors['text_dim'],
                                        font=ctk.CTkFont(size=12, weight="bold"),
                                        state="disabled",
                                        command=self.save_new_machine)
        self.save_button.pack(side='left')

        self.dir_listbox.bind('<<ListboxSelect>>', self.on_dir_select)
        self.dir_frame.pack_forget()
    
    def create_settings_tab(self):
        """Ayarlar sekmesi"""
        tab = self.notebook.tab(self.t("tab_settings"))
        tab.configure(fg_color=self.colors['bg'])

        card = ctk.CTkFrame(
            tab,
            corner_radius=12,
            fg_color=self.colors['card_bg'],
            border_width=1,
            border_color=self.colors['card_border'],
        )
        card.pack(fill='x', padx=8, pady=(4, 8))

        card_inner = ctk.CTkFrame(card, fg_color="transparent")
        card_inner.pack(fill='x', padx=16, pady=14)

        ctk.CTkLabel(card_inner, text=self.t("settings_general"),
                    font=ctk.CTkFont(size=14, weight="bold")).pack(anchor='w', pady=(0, 10))

        lang_row = ctk.CTkFrame(card_inner, fg_color="transparent")
        lang_row.pack(fill='x', pady=(0, 10))
        ctk.CTkLabel(lang_row, text=self.t("settings_language"),
                    font=ctk.CTkFont(size=12),
                    text_color=self.colors['text_muted']).pack(side='left')

        self.language_var = tk.StringVar(value=self._lang_to_choice(self.lang))
        self.language_combo = ctk.CTkComboBox(
            lang_row,
            values=[self.t("lang_option_turkce"), self.t("lang_option_english")],
            variable=self.language_var,
            width=140,
            height=34,
            corner_radius=6,
            font=ctk.CTkFont(size=12),
            state="readonly",
            fg_color=self.colors['card_bg'],
            border_color=self.colors['card_border'],
            button_color=self.colors['tab_bg'],
            button_hover_color=self.colors['secondary'],
            dropdown_fg_color=self.colors['card_bg'],
            dropdown_hover_color=self.colors['tab_bg'],
            dropdown_text_color=self.colors['text'],
            text_color=self.colors['text'],
            command=self.on_language_changed,
        )
        self.language_combo.pack(side='right')

        self.auto_check_cb = ctk.CTkCheckBox(card_inner,
                                            text=self.t("settings_auto_status_check"),
                                            variable=self.auto_check_var,
                                            font=ctk.CTkFont(size=12),
                                            corner_radius=4)
        self.auto_check_cb.pack(anchor='w')
        self.theme_var = tk.StringVar(value="dark")

    def on_language_changed(self, choice):
        self.set_language(self._choice_to_lang(choice))

    def set_language(self, lang):
        lang = lang if lang in ("tr", "en") else "tr"
        if lang == self.lang:
            return
        self.lang = lang
        self._save_language_to_config()
        self.redraw_ui()

    def change_theme(self):
        """Temayı değiştir"""
        theme = self.theme_var.get()
        
        if theme == "dark":
            self.colors = self.dark_colors
            # Style temasını güncellemek gerekebilir ama 'clam' theme renkleri manual alıyor
        else:
            self.colors = self.light_colors
            
        # Root rengini güncelle
        self.root.configure(fg_color=self.colors['bg'])
        
        # Stilleri güncelle
        self.configure_styles()
        
        # Arayüzü yeniden çiz
        self.redraw_ui()

    def redraw_ui(self):
        """Arayüzü yeniden oluştur"""
        self.root.title(self.t("app_title"))
        # CTkTabview: sekmeleri sil ve yeniden ekle (dil/tema değişimi dahil)
        existing_tabs = []
        try:
            existing_tabs = list(self.notebook._tab_dict.keys())
        except Exception:
            existing_tabs = []

        for tab_name in existing_tabs:
            try:
                self.notebook.delete(tab_name)
            except:
                pass

        self.notebook.add(self.t("tab_transfer"))
        self.notebook.add(self.t("tab_machines"))
        self.notebook.add(self.t("tab_new_machine"))
        self.notebook.add(self.t("tab_settings"))

        self.create_transfer_tab()
        self.create_machines_tab()
        self.create_add_machine_tab()
        self.create_settings_tab()

        self.notebook.set(self.t("tab_settings"))
        self.refresh_machines_list()
    
    def select_file(self):
        """Dosya seçim dialogu"""
        filename = filedialog.askopenfilename(
            title=self.t("dialog_select_nc_title"),
            filetypes=[(self.t("filetype_nc_files"), "*.nc"), (self.t("filetype_all_files"), "*.*")]
        )
        if filename:
            self.current_file = filename
            self.file_label.configure(text=os.path.basename(filename))
    
    def focas_check_status(self, machine):
        """FOCAS ile makine bağlantısını test et (raw TCP)"""
        try:
            sock = focas_raw_connect(machine['host'], machine['port'], timeout=5)
            if sock:
                focas_raw_disconnect(sock)
                return True
        except:
            pass
        return False

    def focas_list_cf_programs(self, machine):
        """CF karttaki dosya listesini al (bağlantı testi)"""
        # CF kart dosya listesi için dizin navigasyonu gerekli
        # Şimdilik bağlantı testi ile yetiniyoruz
        if self.focas_check_status(machine):
            return [self.t("msg_cf_card_access_active")]
        return []

    def focas_get_program_list(self, machine, target_path=None):
        """FOCAS ile CNC'deki program listesini oku (isim tabanlı)"""
        if FOCAS_DLL is None:
            return []

        if target_path is None:
            target_path = b'//CNC_MEM/USER/PATH1/'

        handle = ctypes.c_ushort(0)
        ip = machine['host'].encode('ascii')
        port = ctypes.c_ushort(machine['port'])
        ret = FOCAS_DLL.cnc_allclibhndl3(ip, port, ctypes.c_long(5), ctypes.byref(handle))
        if ret != 0:
            return []

        try:
            path = ctypes.create_string_buffer(target_path, 256)
            ret = FOCAS_DLL.cnc_upstart4(handle, ctypes.c_short(0), path)
            if ret != 0:
                return []

            total_data = b''
            for _ in range(500):
                buf = ctypes.create_string_buffer(65536)
                buf_size = ctypes.c_long(65536)
                ret = FOCAS_DLL.cnc_upload4(handle, ctypes.byref(buf_size), buf)
                if ret == 10:
                    time.sleep(0.05)
                    continue
                if ret == 0 and buf_size.value > 0:
                    total_data += buf.raw[:buf_size.value]
                if ret == -2 or buf_size.value <= 0:
                    break
                if ret != 0 and ret != 10:
                    break
            FOCAS_DLL.cnc_upend4(handle)

            # Program isimlerini parse et
            programs = []
            text = total_data.decode('ascii', errors='ignore')
            for line in text.split('\n'):
                line = line.strip()
                # <dosya_adı> formatı
                if line.startswith('<') and '>' in line:
                    name = line[1:line.index('>')]
                    if name:
                        programs.append(name)
                # O numarası formatı
                elif line.startswith('O') and len(line) >= 2:
                    match = re.match(r'O(\d+)', line)
                    if match:
                        programs.append(f'O{match.group(1)}')
            return programs
        finally:
            FOCAS_DLL.cnc_freelibhndl(handle)

    def focas_upload_file(self, machine, filepath, filename, progress_callback=None, target_path=None):
        """FOCAS ile dosyayı CNC'ye transfer et (path-based API)
        target_path: b'//CNC_MEM/USER/PATH1/' (dahili bellek) veya b'//CNC_MEM/USB_PRG/' (CF kart)
        """
        if FOCAS_DLL is None:
            raise Exception(self.t("err_focas_dll_not_loaded"))

        if target_path is None:
            target_path = b'//CNC_MEM/USER/PATH1/'

        handle = ctypes.c_ushort(0)
        ip = machine['host'].encode('ascii')
        port = ctypes.c_ushort(machine['port'])

        ret = FOCAS_DLL.cnc_allclibhndl3(ip, port, ctypes.c_long(10), ctypes.byref(handle))
        if ret != 0:
            raise Exception(self.t("err_focas_connection_error_code").format(code=ret))

        try:
            # Path-based transfer başlat (cnc_dwnstart4)
            cnc_path = ctypes.create_string_buffer(target_path, 256)
            ret = FOCAS_DLL.cnc_dwnstart4(handle, ctypes.c_short(0), cnc_path)
            if ret != 0:
                raise Exception(self.t("err_focas_transfer_start_error_code").format(code=ret))

            try:
                with open(filepath, 'r', encoding='ascii', errors='ignore') as f:
                    text_data = f.read()

                # CRLF -> LF dönüşümü
                text_data = text_data.replace('\r\n', '\n').replace('\r', '\n')

                # Dosya adından uzantıyı kaldır (CNC'de isim olarak kullanılacak)
                prog_name = os.path.splitext(filename)[0] if '.' in filename else filename

                # Satırlara böl ve temizle
                lines = text_data.split('\n')

                # Baştaki ve sondaki boş satırları ve % satırlarını kaldır
                while lines and lines[0].strip() in ('', '%'):
                    lines.pop(0)
                while lines and lines[-1].strip() in ('', '%'):
                    lines.pop()

                # İlk satır <...> header ise kaldır (yeniden oluşturacağız)
                if lines and lines[0].strip().startswith('<') and '>' in lines[0]:
                    lines.pop(0)

                # İlk satır O numarası ise kaldır (path-based API'de <filename> kullanılır)
                if lines and re.match(r'^O\d+', lines[0].strip()):
                    lines.pop(0)

                # İçerik boşsa hata ver
                if not lines:
                    raise Exception(self.t("err_file_content_empty"))

                # Program gövdesini birleştir
                body = '\n'.join(lines)

                # Doğru formatı oluştur: %\n<dosya_adı>\n...gövde...\n%\n
                file_content = f'%\n<{prog_name}>\n{body}\n%\n'

                file_data = file_content.encode('ascii')
                file_size = len(file_data)
                chunk_size = 1280
                sent = 0

                while sent < file_size:
                    chunk = file_data[sent:sent + chunk_size]
                    buf_len = ctypes.c_long(len(chunk))
                    buf = ctypes.create_string_buffer(chunk)

                    for retry in range(20):
                        buf_len.value = len(chunk)
                        ret = FOCAS_DLL.cnc_download4(handle, ctypes.byref(buf_len), buf)
                        if ret == 10:  # EW_BUFFER - buffer dolu, tekrar dene
                            time.sleep(0.1)
                            continue
                        break

                    if ret != 0:
                        raise Exception(self.t("err_focas_data_send_error_code").format(code=ret))

                    sent += buf_len.value
                    if progress_callback:
                        progress_callback(sent, file_size)

            finally:
                time.sleep(0.5)
                ret = FOCAS_DLL.cnc_dwnend4(handle)
                if ret == 15:
                    raise Exception(self.t("err_cnc_memory_full"))
                elif ret == 5:
                    raise Exception(self.t("err_cnc_program_save_failed_conflict"))
                elif ret != 0:
                    raise Exception(self.t("err_focas_transfer_end_error_code").format(code=ret))
        finally:
            FOCAS_DLL.cnc_freelibhndl(handle)

    def focas_mem_upload_file(self, machine, filepath, filename, progress_callback=None, force_o_number=None):
        """FOCAS ile dosyayı CNC dahili belleğine transfer et (dwnstart3/download3 API)
        Torna için - O numarası formatında çalışır.
        force_o_number: Belirtilirse dosya içeriğindeki O numarasını bu değerle değiştirir.
        """
        if FOCAS_DLL is None:
            raise Exception(self.t("err_focas_dll_not_loaded"))

        handle = ctypes.c_ushort(0)
        ip = machine['host'].encode('ascii')
        port = ctypes.c_ushort(machine['port'])

        ret = FOCAS_DLL.cnc_allclibhndl3(ip, port, ctypes.c_long(10), ctypes.byref(handle))
        if ret != 0:
            raise Exception(self.t("err_focas_connection_error_code").format(code=ret))

        try:
            with open(filepath, 'r', encoding='ascii', errors='ignore') as f:
                text_data = f.read()

            # CRLF -> LF dönüşümü
            text_data = text_data.replace('\r\n', '\n').replace('\r', '\n')

            # Satırlara böl ve temizle
            lines = text_data.split('\n')

            # Baştaki ve sondaki boş satırları ve % satırlarını kaldır
            while lines and lines[0].strip() in ('', '%', '%%'):
                lines.pop(0)
            while lines and lines[-1].strip() in ('', '%', '%%'):
                lines.pop()

            if not lines:
                raise Exception(self.t("err_file_content_empty"))

            # O numarasını değiştir (force_o_number verilmişse)
            if force_o_number is not None:
                first_line = lines[0].strip()
                o_match = re.match(r'^O\d+(.*)', first_line)
                if o_match:
                    # Mevcut O numarasını değiştir, yorum kısmını koru
                    lines[0] = f'O{force_o_number:04d}{o_match.group(1)}'
                else:
                    # O numarası yoksa başa ekle
                    lines.insert(0, f'O{force_o_number:04d}')

            # Program gövdesini birleştir
            body = '\n'.join(lines)

            # %%\n...içerik...\n%%\n formatında oluştur
            file_content = f'%%\n{body}\n%%\n'
            file_data = file_content.encode('ascii')

            # Transfer başlat (cnc_dwnstart3, type=0 = NC program)
            ret = FOCAS_DLL.cnc_dwnstart3(handle, ctypes.c_short(0))
            if ret != 0:
                raise Exception(self.t("err_cnc_mem_transfer_start_error_code").format(code=ret))

            try:
                file_size = len(file_data)
                chunk_size = 1280
                sent = 0

                while sent < file_size:
                    chunk = file_data[sent:sent + chunk_size]
                    buf_len = ctypes.c_long(len(chunk))
                    buf = ctypes.create_string_buffer(chunk)

                    for retry in range(20):
                        buf_len.value = len(chunk)
                        ret = FOCAS_DLL.cnc_download3(handle, ctypes.byref(buf_len), buf)
                        if ret == 10:  # EW_BUFFER
                            time.sleep(0.1)
                            continue
                        break

                    if ret != 0:
                        raise Exception(self.t("err_cnc_mem_data_send_error_code").format(code=ret))

                    sent += buf_len.value
                    if progress_callback:
                        progress_callback(sent, file_size)

            finally:
                time.sleep(0.5)
                ret = FOCAS_DLL.cnc_dwnend3(handle)
                if ret == 15:
                    raise Exception(self.t("err_cnc_memory_full"))
                elif ret == 5:
                    raise Exception(self.t("err_cnc_program_save_failed_o_conflict"))
                elif ret != 0:
                    raise Exception(self.t("err_cnc_mem_transfer_end_error_code").format(code=ret))
        finally:
            FOCAS_DLL.cnc_freelibhndl(handle)

    def focas_delete_program(self, machine, prog_name, target_path=None):
        """FOCAS ile CNC'den program sil"""
        if FOCAS_DLL is None:
            return False
        if target_path is None:
            target_path = '//CNC_MEM/USER/PATH1/'
        else:
            target_path = target_path.decode('ascii') if isinstance(target_path, bytes) else target_path
        handle = ctypes.c_ushort(0)
        ip = machine['host'].encode('ascii')
        port = ctypes.c_ushort(machine['port'])
        ret = FOCAS_DLL.cnc_allclibhndl3(ip, port, ctypes.c_long(10), ctypes.byref(handle))
        if ret != 0:
            return False
        try:
            full_path = f'{target_path.rstrip("/")}/{prog_name}'.encode('ascii')
            path = ctypes.create_string_buffer(full_path, 256)
            ret = FOCAS_DLL.cnc_pdf_del(handle, path)
            return ret == 0
        finally:
            FOCAS_DLL.cnc_freelibhndl(handle)

    def focas_read_data(self, handle, data_type):
        """FOCAS ile belirli tip veriyi oku (CNC -> PC) - upstart4/upload4 ile"""
        path = ctypes.create_string_buffer(b'//CNC_MEM/USER/PATH1/', 256)
        ret = FOCAS_DLL.cnc_upstart4(handle, ctypes.c_short(data_type), path)
        if ret != 0:
            # Path4 desteklemiyorsa eski API dene
            ret = FOCAS_DLL.cnc_upstart3(handle, ctypes.c_short(data_type),
                                         ctypes.c_long(0), ctypes.c_long(0))
            if ret != 0:
                return None
            data = b''
            for _ in range(5000):
                buf = ctypes.create_string_buffer(4096)
                buf_len = ctypes.c_long(4096)
                ret = FOCAS_DLL.cnc_upload3(handle, ctypes.byref(buf_len), buf)
                if ret == 10:
                    time.sleep(0.05)
                    continue
                if ret != 0 or buf_len.value == 0:
                    break
                data += buf.raw[:buf_len.value]
            FOCAS_DLL.cnc_upend3(handle)
            return data

        data = b''
        for _ in range(5000):
            buf = ctypes.create_string_buffer(4096)
            buf_len = ctypes.c_long(4096)
            ret = FOCAS_DLL.cnc_upload4(handle, ctypes.byref(buf_len), buf)
            if ret == 10:
                time.sleep(0.05)
                continue
            if ret != 0 or buf_len.value == 0:
                break
            data += buf.raw[:buf_len.value]
        FOCAS_DLL.cnc_upend4(handle)
        return data

    def focas_mem_delete_dialog(self, machine):
        """CNC dahili bellekten program silme dialogu (O numarası ile)"""
        if machine['status'] != 'online':
            messagebox.showerror(
                self.t("title_error"),
                self.t("msg_cannot_connect_machine").format(machine_name=machine["name"]),
            )
            return

        dialog = ctk.CTkToplevel(self.root, fg_color=self.colors['bg'])
        dialog.title(self.t("dlg_delete_program_title").format(machine_name=machine["name"]))
        dialog.geometry("380x220")
        dialog.transient(self.root)
        dialog.grab_set()

        ctk.CTkLabel(dialog, text=self.t("dlg_delete_program_label"),
                    font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(24, 4))

        ctk.CTkLabel(dialog, text=self.t("dlg_delete_program_hint"),
                    font=ctk.CTkFont(size=11),
                    text_color=self.colors['text_muted']).pack(pady=(0, 10))

        o_var = tk.StringVar()
        o_entry = ctk.CTkEntry(dialog, textvariable=o_var, width=200, height=38,
                              corner_radius=6, font=ctk.CTkFont(size=14),
                              fg_color=self.colors['card_bg'],
                              text_color=self.colors['text'],
                              border_color=self.colors['card_border'],
                              justify='center')
        o_entry.pack(pady=5)
        o_entry.focus_set()

        def do_delete():
            o_text = o_var.get().strip()
            # O harfini kaldır
            o_text = o_text.lstrip('Oo')
            if not o_text.isdigit():
                messagebox.showerror(self.t("title_error"), self.t("msg_enter_valid_o_number"), parent=dialog)
                return
            o_num = int(o_text)
            if o_num >= 500:
                if not messagebox.askyesno(
                    self.t("title_warning"),
                    self.t("msg_confirm_delete_program_500plus").format(o_num=o_num),
                    parent=dialog,
                ):
                    return
            else:
                if not messagebox.askyesno(
                    self.t("title_confirm"),
                    self.t("msg_confirm_delete_program").format(o_num=o_num),
                    parent=dialog,
                ):
                    return

            if FOCAS_DLL is None:
                messagebox.showerror(self.t("title_error"), self.t("err_focas_dll_not_loaded"), parent=dialog)
                return

            handle = ctypes.c_ushort(0)
            ip = machine['host'].encode('ascii')
            ret = FOCAS_DLL.cnc_allclibhndl3(ip, ctypes.c_ushort(machine['port']),
                                              ctypes.c_long(10), ctypes.byref(handle))
            if ret != 0:
                messagebox.showerror(
                    self.t("title_error"),
                    self.t("err_focas_connection_error_code").format(code=ret),
                    parent=dialog,
                )
                return

            ret = FOCAS_DLL.cnc_delete(handle, ctypes.c_short(o_num))
            FOCAS_DLL.cnc_freelibhndl(handle)

            if ret == 0:
                if not self.current_file:
                    messagebox.showinfo(
                        self.t("title_success"),
                        self.t("msg_program_deleted_no_file").format(o_num=o_num),
                        parent=dialog,
                    )
                    dialog.destroy()
                    return
                dialog.destroy()
                # Silinen numaraya dosyayı transfer et
                self.focas_mem_delete_and_transfer(machine, o_num)
            else:
                messagebox.showerror(
                    self.t("title_error"),
                    self.t("msg_program_delete_failed_code").format(o_num=o_num, code=ret),
                    parent=dialog,
                )

        o_entry.bind('<Return>', lambda e: do_delete())

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=15)

        ctk.CTkButton(btn_frame, text=self.t("btn_delete_send"), command=do_delete,
                     width=120, height=36, corner_radius=6,
                     fg_color=self.colors['danger'], hover_color=self.colors['danger_hover'],
                     text_color="#ffffff",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side='left', padx=5)

        ctk.CTkButton(btn_frame, text=self.t("btn_cancel"), command=dialog.destroy,
                     width=100, height=36, corner_radius=6,
                     fg_color=self.colors['secondary'], hover_color=self.colors['secondary_hover'],
                     text_color=self.colors['text'],
                     font=ctk.CTkFont(size=12)).pack(side='left', padx=5)

    def focas_mem_delete_and_transfer(self, machine, o_num):
        """Silinen O numarasına seçili dosyayı transfer et"""
        filename = os.path.basename(self.current_file)
        if filename.startswith('~') and filename.lower().endswith('.tmx') and self.cimco_display_name:
            filename = self.cimco_display_name

        self.progress_label.configure(text=self.t("status_transferring_as_o").format(o_num=o_num))
        self.progress_bar.set(0)

        def transfer():
            try:
                def progress_callback(sent, total):
                    progress = (sent / total) * 100
                    self.root.after(0, lambda: self.progress_bar.set(progress / 100.0))
                    self.root.after(0, lambda: self.progress_label.configure(
                        text=self.t("status_sending_as_o_progress").format(o_num=o_num, progress=progress)))

                self.focas_mem_upload_file(machine, self.current_file, filename,
                                          progress_callback, force_o_number=o_num)

                self.root.after(0, lambda: self.progress_bar.set(1.0))
                self.root.after(0, lambda: self.progress_label.configure(text=self.t("status_transfer_complete_cnc_memory")))
                self.root.after(0, lambda: messagebox.showinfo(
                    self.t("title_success"),
                    self.t("msg_sent_to_machine_as_o").format(filename=filename, o_num=o_num, machine_name=machine["name"]),
                ))
                self.log_transfer(machine['name'], filename, "BAŞARILI", f"O{o_num:04d} olarak CNC Bellek")

            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda: messagebox.showerror(
                    self.t("title_error"),
                    self.t("msg_transfer_error").format(error=error_msg),
                ))
                self.root.after(0, lambda: self.progress_label.configure(text=""))
                self.log_transfer(machine['name'], filename, "HATA", error_msg)

        threading.Thread(target=transfer, daemon=True).start()

    def focas_backup(self, machine):
        """FOCAS ile CNC yedekleme (parametre, PLC, makro, offset, programlar)"""
        if machine['status'] != 'online':
            messagebox.showerror(
                self.t("title_error"),
                self.t("msg_cannot_connect_machine").format(machine_name=machine["name"]),
            )
            return

        backup_dir = filedialog.askdirectory(title=self.t("dialog_select_backup_folder"))
        if not backup_dir:
            return

        self.progress_label.configure(text=self.t("status_backup_starting"))
        self.progress_bar.set(0)

        def do_backup():
            try:
                handle = ctypes.c_ushort(0)
                ip = machine['host'].encode('ascii')
                port = ctypes.c_ushort(machine['port'])
                ret = FOCAS_DLL.cnc_allclibhndl3(ip, port, ctypes.c_long(10), ctypes.byref(handle))
                if ret != 0:
                    raise Exception(self.t("err_focas_connection_error_code").format(code=ret))

                try:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    save_dir = os.path.join(backup_dir, f"{machine['name']}_yedek_{timestamp}")
                    os.makedirs(save_dir, exist_ok=True)

                    backup_types = [
                        (1, self.t("backup_item_parameters"), "parametre.prm"),
                        (2, self.t("backup_item_pitch_error"), "pitch_error.pit"),
                        (3, self.t("backup_item_macro_variables"), "makro_degisken.mac"),
                        (4, self.t("backup_item_work_offset"), "work_offset.wof"),
                    ]

                    total_steps = len(backup_types) + 1
                    done = 0

                    for data_type, label, bkp_filename in backup_types:
                        self.root.after(0, lambda l=label: self.progress_label.configure(
                            text=self.t("status_backing_up_item").format(item=l)))
                        data = self.focas_read_data(handle, data_type)
                        if data:
                            fpath = os.path.join(save_dir, bkp_filename)
                            with open(fpath, 'wb') as f:
                                f.write(data)
                        done += 1
                        progress = (done / total_steps) * 100
                        self.root.after(0, lambda p=progress: self.progress_bar.set(p / 100.0))

                    # NC Programları - path-based API ile tümünü oku
                    self.root.after(0, lambda: self.progress_label.configure(
                        text=self.t("status_backing_up_item").format(item=self.t("backup_item_nc_programs"))))
                    prog_dir = os.path.join(save_dir, "programlar")
                    os.makedirs(prog_dir, exist_ok=True)

                    path = ctypes.create_string_buffer(b'//CNC_MEM/USER/PATH1/', 256)
                    ret = FOCAS_DLL.cnc_upstart4(handle, ctypes.c_short(0), path)
                    if ret == 0:
                        total_data = b''
                        for _ in range(500):
                            buf = ctypes.create_string_buffer(65536)
                            buf_size = ctypes.c_long(65536)
                            ret = FOCAS_DLL.cnc_upload4(handle, ctypes.byref(buf_size), buf)
                            if ret == 10:
                                time.sleep(0.05)
                                continue
                            if ret == 0 and buf_size.value > 0:
                                total_data += buf.raw[:buf_size.value]
                            if ret == -2 or buf_size.value <= 0:
                                break
                            if ret != 0 and ret != 10:
                                break
                        FOCAS_DLL.cnc_upend4(handle)

                        # Programları dosya dosya ayır ve kaydet
                        text = total_data.decode('ascii', errors='ignore')
                        programs = text.split('%')
                        prog_count = 0
                        for prog in programs:
                            prog = prog.strip()
                            if not prog:
                                continue
                            # Program ismini belirle
                            first_line = prog.split('\n')[0].strip()
                            if first_line.startswith('<') and '>' in first_line:
                                name = first_line[1:first_line.index('>')]
                            elif first_line.startswith('O'):
                                match = re.match(r'O(\d+)', first_line)
                                name = f'O{match.group(1)}' if match else f'prog_{prog_count}'
                            else:
                                continue
                            # O9000+ makro programlarını atla
                            o_match = re.match(r'O(\d+)', name)
                            if o_match and int(o_match.group(1)) >= 9000:
                                continue
                            safe_name = re.sub(r'[<>:"/\\|?*]', '_', name)
                            prog_file = os.path.join(prog_dir, f"{safe_name}.nc")
                            with open(prog_file, 'w', encoding='ascii') as f:
                                f.write(f'%\n{prog}\n%\n')
                            prog_count += 1

                    done += 1
                    self.root.after(0, lambda: self.progress_bar.set(1.0))
                    self.root.after(0, lambda: self.progress_label.configure(text=self.t("status_backup_complete")))
                    self.root.after(0, lambda c=prog_count: messagebox.showinfo(
                        self.t("title_success"),
                        self.t("msg_backup_complete").format(machine_name=machine["name"], save_dir=save_dir, count=c),
                    ))

                    self.log_transfer(machine['name'], "CNC_YEDEK", "BAŞARILI", save_dir)

                finally:
                    FOCAS_DLL.cnc_freelibhndl(handle)

            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda: messagebox.showerror(
                    self.t("title_error"),
                    self.t("msg_backup_error").format(error=error_msg),
                ))
                self.root.after(0, lambda: self.progress_label.configure(text=""))
                self.log_transfer(machine['name'], "CNC_YEDEK", "HATA", error_msg)

        threading.Thread(target=do_backup, daemon=True).start()

    def check_machine_status(self):
        """Tüm makinelerin durumunu kontrol et"""
        def check():
            changed = False
            for machine in self.machines['machines']:
                protocol = machine.get('protocol', 'ftp')
                old_status = machine['status']
                try:
                    if protocol in ('focas', 'focas_mem'):
                        if self.focas_check_status(machine):
                            machine['status'] = 'online'
                        else:
                            machine['status'] = 'offline'
                    else:
                        ftp = ftplib.FTP()
                        ftp.connect(machine['host'], machine['port'], timeout=5)
                        ftp.login(machine['user'], machine['password'])
                        ftp.cwd(machine['directory'])
                        ftp.quit()
                        machine['status'] = 'online'
                except Exception as e:
                    machine['status'] = 'offline'
                if machine['status'] != old_status:
                    changed = True

            # Sadece durum degistiyse GUI'yi guncelle
            if changed:
                self.root.after(0, self.create_machine_buttons)
                self.root.after(0, self.refresh_machines_list)
                self.root.after(0, self.save_machines)

        threading.Thread(target=check, daemon=True).start()
    
    def refresh_machines_list(self):
        """Makine listesini yenile"""
        for item in self.machines_tree.get_children():
            self.machines_tree.delete(item)

        proto_map = {
            'ftp': 'FTP',
            'focas': self.t("proto_cf_text_short"),
            'focas_mem': self.t("proto_cnc_memory_short"),
        }
        for machine in self.machines['machines']:
            protocol = machine.get('protocol', 'ftp')
            status = machine['status'].upper()
            values = (
                machine['host'],
                machine['port'],
                proto_map.get(protocol, protocol.upper()),
                status
            )
            self.machines_tree.insert('', 'end', text=machine['name'], values=values)
    
    def edit_machine(self):
        """Seçili makineyi düzenle"""
        selection = self.machines_tree.selection()
        if not selection:
            messagebox.showwarning(self.t("title_warning"), self.t("msg_select_machine"))
            return
        
        # Seçili makineyi bul
        item = self.machines_tree.item(selection[0])
        old_name = item['text']
        machine = next((m for m in self.machines['machines'] if m['name'] == old_name), None)
        
        if not machine:
            return
            
        # Formu doldur
        self.add_form_vars['name_entry'].delete(0, tk.END)
        self.add_form_vars['name_entry'].insert(0, machine['name'])
        
        self.add_form_vars['host_entry'].delete(0, tk.END)
        self.add_form_vars['host_entry'].insert(0, machine['host'])
        
        self.add_form_vars['port_entry'].delete(0, tk.END)
        self.add_form_vars['port_entry'].insert(0, str(machine['port']))
        
        self.add_form_vars['user_entry'].delete(0, tk.END)
        self.add_form_vars['user_entry'].insert(0, machine['user'])
        
        self.add_form_vars['password_entry'].delete(0, tk.END)
        self.add_form_vars['password_entry'].insert(0, machine['password'])

        # Protokol ayarla
        protocol = machine.get('protocol', 'ftp').upper()
        self.add_form_vars['protocol_combo'].set(protocol)

        # Dizin listesini temizle ve mevcut dizini göster
        self.dir_listbox.delete(0, tk.END)
        self.dir_listbox.insert(tk.END, machine['directory'])
        self.selected_directory = machine["directory"]
        self.selected_dir_label.configure(text=self.t("label_selected_dir").format(dir=machine["directory"]))
        
        # Dizin seçimi alanını göster
        self.dir_frame.pack(padx=20, pady=(0, 20), fill='both', expand=True)
        # Listbox'ta seçili hale getir
        self.dir_listbox.select_set(0)
        
        # Butonları güncelle
        self.test_button.configure(
            state='normal',
            fg_color=self.colors['warning'],
            hover_color=self.colors['warning_hover'],
            text_color=self.colors['text'],
        )
        self.save_button.configure(
            text=self.t("btn_update"),
            state='normal',
            command=lambda: self.update_machine(old_name),
            fg_color=self.colors['success'],
            hover_color=self.colors['success_hover'],
            text_color="#ffffff",
        )
        self._save_button_mode = "update"
        
        # Sekmeyi değiştir
        self.notebook.set(self.t("tab_new_machine"))
        
    def delete_machine(self):
        """Seçili makineyi sil"""
        selection = self.machines_tree.selection()
        if not selection:
            messagebox.showwarning(self.t("title_warning"), self.t("msg_select_machine"))
            return
        
        # Onay al
        item = self.machines_tree.item(selection[0])
        machine_name = item['text']
        
        if messagebox.askyesno(
            self.t("title_confirm"),
            self.t("msg_confirm_delete_machine").format(machine_name=machine_name),
        ):
            # Makineyi bul ve sil
            self.machines['machines'] = [m for m in self.machines['machines'] if m['name'] != machine_name]
            self.save_machines()
            self.refresh_machines_list()
            self.create_machine_buttons()

    def connect_and_list_dirs(self):
        """Bağlantı kur ve dizinleri listele"""
        try:
            # Form verilerini al
            host = self.add_form_vars['host_entry'].get()
            port = int(self.add_form_vars['port_entry'].get())
            user = self.add_form_vars['user_entry'].get()
            password = self.add_form_vars['password_entry'].get()
            protocol = self.add_form_vars['protocol_combo'].get().lower()

            if not host:
                messagebox.showerror(self.t("title_error"), self.t("msg_ip_empty"))
                return

            if protocol in ('focas', 'focas_mem'):
                test_machine = {'host': host, 'port': port}
                if self.focas_check_status(test_machine):
                    self.dir_listbox.delete(0, tk.END)
                    # CNC dahili bellek programları
                    programs = self.focas_get_program_list(test_machine)
                    self.dir_listbox.insert(
                        tk.END,
                        self.t("list_header_cnc_memory").format(count=len(programs)),
                    )
                    for prog in programs[:30]:
                        self.dir_listbox.insert(tk.END, f"  {prog}")
                    if protocol == 'focas':
                        # CF_TEXT programları (raw TCP ile) - sadece CF_TEXT modu için
                        cf_programs = self.focas_list_cf_programs(test_machine)
                        self.dir_listbox.insert(
                            tk.END,
                            self.t("list_header_cf_text").format(count=len(cf_programs)),
                        )
                        for prog in cf_programs[:30]:
                            self.dir_listbox.insert(tk.END, f"  {prog}")
                    self.dir_listbox.select_set(0)
                    target = self.t("proto_cf_text_short") if protocol == 'focas' else self.t("proto_cnc_memory_short")
                    self.selected_directory = ""
                    self.selected_dir_label.configure(text=self.t("label_target").format(target=target))
                    self.dir_frame.pack(padx=20, pady=(0, 20), fill='both', expand=True)
                    self.test_button.configure(
                        state='normal',
                        fg_color=self.colors['warning'],
                        hover_color=self.colors['warning_hover'],
                        text_color=self.colors['text'],
                    )
                    self.save_button.configure(
                        state='normal',
                        fg_color=self.colors['success'],
                        hover_color=self.colors['success_hover'],
                        text_color="#ffffff",
                    )
                    messagebox.showinfo(
                        self.t("title_success"),
                        self.t("msg_focas_connection_success").format(
                            cnc_count=len(programs),
                            target=target,
                        ),
                    )
                else:
                    messagebox.showerror(self.t("title_error"), self.t("msg_focas_connection_failed"))
                return

            # FTP bağlantısı
            self.ftp_connection = ftplib.FTP()
            self.ftp_connection.connect(host, port, timeout=5)
            self.ftp_connection.login(user, password)

            # Dizin listesini al
            self.dir_listbox.delete(0, tk.END)

            # Ana dizini ekle
            self.dir_listbox.insert(tk.END, "/")

            # Alt dizinleri ekle
            dirs = []
            try:
                self.ftp_connection.retrlines('LIST', lambda x: dirs.append(x))
            except:
                pass # LIST komutu hata verirse sadece root kalsın

            for line in dirs:
                parts = line.split()
                # Basit bir parsing, sunucuya göre değişebilir
                if len(parts) >= 9 and parts[0].startswith('d'):
                    dir_name = ' '.join(parts[8:])
                    self.dir_listbox.insert(tk.END, f"/{dir_name}/")

            # Dizin seçim alanını göster
            self.dir_frame.pack(padx=20, pady=(0, 20), fill='both', expand=True)

            self.selected_directory = ""
            self.selected_dir_label.configure(text=self.t("label_selected_dir_empty"))
            messagebox.showinfo(self.t("title_success"), self.t("msg_connection_ok_select_dir"))

        except Exception as e:
            messagebox.showerror(self.t("title_error"), self.t("msg_connection_error").format(error=str(e)))

    def on_dir_select(self, event=None):
        """Dizin seçildiğinde"""
        selection = self.dir_listbox.curselection()
        if selection:
            selected_dir = self.dir_listbox.get(selection[0])
            self.selected_directory = selected_dir
            self.selected_dir_label.configure(text=self.t("label_selected_dir").format(dir=selected_dir))
            self.test_button.configure(
                state='normal',
                fg_color=self.colors['warning'],
                hover_color=self.colors['warning_hover'],
                text_color=self.colors['text'],
            )
            self.save_button.configure(
                state='normal',
                fg_color=self.colors['success'],
                hover_color=self.colors['success_hover'],
                text_color="#ffffff",
            )

    def test_connection(self):
        """Bağlantıyı test et"""
        try:
            protocol = self.add_form_vars['protocol_combo'].get().lower()

            if protocol in ('focas', 'focas_mem'):
                host = self.add_form_vars['host_entry'].get()
                port = int(self.add_form_vars['port_entry'].get())
                test_machine = {'host': host, 'port': port}
                if self.focas_check_status(test_machine):
                    messagebox.showinfo(self.t("title_success"), self.t("msg_focas_test_success"))
                    self.save_button.configure(
                        state='normal',
                        fg_color=self.colors['success'],
                        hover_color=self.colors['success_hover'],
                        text_color="#ffffff",
                    )
                else:
                    messagebox.showerror(self.t("title_error"), self.t("msg_focas_test_failed"))
                return

            selection = self.dir_listbox.curselection()
            if not selection:
                selected_dir = self.selected_directory
                if not selected_dir:
                    messagebox.showwarning(self.t("title_warning"), self.t("msg_select_directory"))
                    return
            else:
                selected_dir = self.dir_listbox.get(selection[0])
                self.selected_directory = selected_dir

            # Bağlantı yoksa kur (form verilerinden)
            if not hasattr(self, 'ftp_connection'):
                 host = self.add_form_vars['host_entry'].get()
                 port = int(self.add_form_vars['port_entry'].get())
                 user = self.add_form_vars['user_entry'].get()
                 password = self.add_form_vars['password_entry'].get()
                 self.ftp_connection = ftplib.FTP()
                 self.ftp_connection.connect(host, port, timeout=5)
                 self.ftp_connection.login(user, password)

            # Dizine geçmeyi dene
            self.ftp_connection.cwd(selected_dir)

            # Test dosyası oluştur
            test_filename = f"test_{int(time.time())}.txt"
            test_content = b"CNC Transfer System Test"

            from io import BytesIO
            self.ftp_connection.storbinary(f'STOR {test_filename}', BytesIO(test_content))

            # Test dosyasını sil
            self.ftp_connection.delete(test_filename)

            messagebox.showinfo(self.t("title_success"), self.t("msg_test_ok"))
            self.save_button.configure(
                state='normal',
                fg_color=self.colors['success'],
                hover_color=self.colors['success_hover'],
                text_color="#ffffff",
            )

        except Exception as e:
            messagebox.showerror(self.t("title_error"), self.t("msg_test_fail").format(error=str(e)))

    def update_machine(self, old_name):
        """Mevcut makineyi güncelle"""
        try:
            # Form verilerini al
            name = self.add_form_vars['name_entry'].get()
            host = self.add_form_vars['host_entry'].get()
            port = int(self.add_form_vars['port_entry'].get())
            user = self.add_form_vars['user_entry'].get()
            password = self.add_form_vars['password_entry'].get()
            
            # Dizin alma (listbox veya labeldan)
            selection = self.dir_listbox.curselection()
            if selection:
                directory = self.dir_listbox.get(selection[0])
            else:
                directory = self.selected_directory

            if not name:
                messagebox.showerror(self.t("title_error"), self.t("msg_machine_name_empty"))
                return

            # İsim değiştiyse ve yeni isim başka makinede varsa uyar
            if name != old_name and any(m['name'] == name for m in self.machines['machines']):
                messagebox.showerror(self.t("title_error"), self.t("msg_machine_name_exists_other"))
                return

            # Makineyi güncelle
            for m in self.machines['machines']:
                if m['name'] == old_name:
                    m['name'] = name
                    m['host'] = host
                    m['port'] = port
                    m['user'] = user
                    m['password'] = password
                    m['directory'] = directory
                    m['protocol'] = self.add_form_vars['protocol_combo'].get().lower()
                    m['status'] = "online" # Güncellendiyse varsayılan online kabul et
                    break
            
            self.save_machines()
            
            # Formu temizle ve resetle
            self.reset_form()
            
            # GUI'yi güncelle
            self.create_machine_buttons()
            self.refresh_machines_list()
              
            messagebox.showinfo(self.t("title_success"), self.t("msg_machine_updated").format(name=name))
               
            # Transfer sekmesine dön
            self.notebook.set(self.t("tab_transfer"))
             
        except Exception as e:
            messagebox.showerror(self.t("title_error"), self.t("msg_update_error").format(error=str(e)))

    def reset_form(self):
        """Formu temizle ve varsayılan ayarlara dön"""
        for entry in self.add_form_vars.values():
            if isinstance(entry, (tk.Entry, ctk.CTkEntry)):
                entry.delete(0, tk.END)
        
        self.add_form_vars['port_entry'].insert(0, "21")
        self.add_form_vars['user_entry'].insert(0, "anonymous")
        self.add_form_vars['protocol_combo'].set("FTP")

        self.dir_listbox.delete(0, tk.END)
        self.selected_directory = ""
        self.selected_dir_label.configure(text=self.t("label_selected_dir_empty"))
        self.test_button.configure(
            state='disabled',
            fg_color=self.colors['secondary'],
            hover_color=self.colors['secondary'],
            text_color=self.colors['text'],
        )
        
        # Kaydet butonunu eski haline getir
        self.save_button.configure(
            text=self.t("btn_save"),
            command=self.save_new_machine,
            state='disabled',
            fg_color=self.colors['secondary'],
            hover_color=self.colors['secondary'],
            text_color=self.colors['text'],
        )
        self._save_button_mode = "save"
        self.dir_frame.pack_forget()

    def save_new_machine(self):
        """Yeni makineyi kaydet"""
        try:
            # Form verilerini al
            name = self.add_form_vars['name_entry'].get()
            host = self.add_form_vars['host_entry'].get()
            port = int(self.add_form_vars['port_entry'].get())
            user = self.add_form_vars['user_entry'].get()
            password = self.add_form_vars['password_entry'].get()
            
            selection = self.dir_listbox.curselection()
            if not selection:
                messagebox.showwarning(self.t("title_warning"), self.t("msg_select_directory"))
                return
            
            directory = self.dir_listbox.get(selection[0])
            
            if not name:
                messagebox.showerror(self.t("title_error"), self.t("msg_machine_name_empty"))
                return
            
            # Makine zaten var mı kontrol et
            if any(m['name'] == name for m in self.machines['machines']):
                messagebox.showerror(self.t("title_error"), self.t("msg_machine_name_exists"))
                return
            
            # Protokol bilgisini al
            protocol = self.add_form_vars['protocol_combo'].get().lower()

            # Yeni makineyi ekle
            new_machine = {
                "name": name,
                "host": host,
                "port": port,
                "user": user,
                "password": password,
                "directory": directory,
                "protocol": protocol,
                "status": "online"
            }
            
            self.machines['machines'].append(new_machine)
            self.save_machines()
            
            # Formu resetle
            self.reset_form()
            
            # GUI'yi güncelle
            self.create_machine_buttons()
            self.refresh_machines_list()
            
            messagebox.showinfo(self.t("title_success"), self.t("msg_machine_added").format(name=name))
            
            # Transfer sekmesine geç
            self.notebook.set(self.t("tab_transfer"))
            
        except Exception as e:
            messagebox.showerror(self.t("title_error"), self.t("msg_save_error").format(error=str(e)))
    
    def transfer_file(self, machine):
        """Dosyayı seçilen makineye transfer et"""
        if not self.current_file:
            messagebox.showerror(self.t("title_error"), self.t("msg_select_file"))
            return
        
        if machine['status'] != 'online':
            messagebox.showerror(
                self.t("title_error"),
                self.t("msg_cannot_connect_machine").format(machine_name=machine["name"]),
            )
            return
        
        # Dosya adını al (CIMCO geçici dosya ise gerçek adı kullan)
        original_filename = os.path.basename(self.current_file)
        if original_filename.startswith('~') and original_filename.lower().endswith('.tmx') and self.cimco_display_name:
            original_filename = self.cimco_display_name
        protocol = machine.get('protocol', 'ftp')

        if protocol == 'focas':
            self.start_transfer(machine, original_filename)
            return

        if protocol in ('focas_mem'):
            self.start_transfer(machine, original_filename)
            return

        # FTP: dosya adı kontrolü
        normalized_filename = self.normalize_filename(original_filename)

        if original_filename != normalized_filename:
            dialog = ctk.CTkToplevel(self.root, fg_color=self.colors['bg'])
            dialog.title(self.t("dlg_fix_filename_title"))
            dialog.geometry("440x230")
            dialog.transient(self.root)
            dialog.grab_set()

            ctk.CTkLabel(dialog, text=self.t("dlg_fix_filename_header"),
                        font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(16, 6))

            ctk.CTkLabel(dialog, text=self.t("dlg_fix_filename_original").format(filename=original_filename),
                        font=ctk.CTkFont(family="Consolas", size=12),
                        text_color=self.colors['text_muted']).pack(pady=4)

            ctk.CTkLabel(dialog, text=self.t("dlg_fix_filename_suggested"),
                        font=ctk.CTkFont(size=12)).pack(pady=(4, 2))
            filename_var = tk.StringVar(value=normalized_filename)
            filename_entry = ctk.CTkEntry(dialog, textvariable=filename_var, width=340,
                                         height=36, corner_radius=6,
                                         font=ctk.CTkFont(family="Consolas", size=12),
                                         fg_color=self.colors['card_bg'],
                                         text_color=self.colors['text'],
                                         border_color=self.colors['card_border'])
            filename_entry.pack(pady=4)

            result = {'confirmed': False, 'filename': ''}

            def confirm():
                result['confirmed'] = True
                result['filename'] = filename_var.get()
                dialog.destroy()

            def cancel():
                dialog.destroy()

            button_frame = ctk.CTkFrame(dialog, fg_color="transparent")
            button_frame.pack(pady=16)

            ctk.CTkButton(button_frame, text=self.t("btn_confirm"), command=confirm,
                         width=110, height=36, corner_radius=6,
                         fg_color=self.colors['primary'], hover_color=self.colors['primary_hover'],
                         text_color="#ffffff",
                         font=ctk.CTkFont(size=13, weight="bold")).pack(side='left', padx=5)
            ctk.CTkButton(button_frame, text=self.t("btn_cancel"), command=cancel,
                         width=110, height=36, corner_radius=6,
                         fg_color=self.colors['secondary'], hover_color=self.colors['secondary_hover'],
                         text_color=self.colors['text'],
                         font=ctk.CTkFont(size=12)).pack(side='left', padx=5)

            dialog.wait_window()

            if not result['confirmed']:
                return

            normalized_filename = result['filename']

        self.start_transfer(machine, normalized_filename)
    
    def start_transfer(self, machine, filename, dest_folder=""):
        """Transfer işlemini başlat"""
        self.progress_label.configure(
            text=self.t("status_connecting_machine").format(machine_name=machine["name"])
        )
        self.progress_bar.set(0)
        protocol = machine.get('protocol', 'ftp')

        if protocol == 'focas_mem':
            # CNC dahili belleğe DLL ile transfer (Torna)
            def focas_mem_transfer():
                try:
                    self.root.after(0, lambda: self.progress_label.configure(
                        text=self.t("status_connecting_cnc_memory")))

                    def progress_callback(sent, total):
                        progress = (sent / total) * 100
                        self.root.after(0, lambda: self.progress_bar.set(progress / 100.0))
                        self.root.after(0, lambda: self.progress_label.configure(
                            text=self.t("status_sending_cnc_memory_progress").format(
                                progress=progress,
                                sent=self.format_size(sent),
                                total=self.format_size(total),
                            )))

                    self.focas_mem_upload_file(machine, self.current_file, filename, progress_callback)

                    self.root.after(0, lambda: self.progress_bar.set(1.0))
                    self.root.after(0, lambda: self.progress_label.configure(text=self.t("status_transfer_complete_cnc_memory")))
                    self.root.after(0, lambda: messagebox.showinfo(
                        self.t("title_success"),
                        self.t("msg_transfer_complete_cnc_memory").format(filename=filename, machine_name=machine["name"]),
                    ))
                    self.log_transfer(machine['name'], filename, "BAŞARILI", "Hedef: CNC Bellek")

                except Exception as e:
                    error_msg = str(e)
                    self.root.after(0, lambda: messagebox.showerror(
                        self.t("title_error"),
                        self.t("msg_focas_transfer_error").format(error=error_msg),
                    ))
                    self.root.after(0, lambda: self.progress_label.configure(text=""))
                    self.log_transfer(machine['name'], filename, "HATA", error_msg)

            threading.Thread(target=focas_mem_transfer, daemon=True).start()
        elif protocol == 'focas':
            # Klasör seçimini thread öncesi yap (GUI thread'inde)
            if not dest_folder:
                try:
                    sock = focas_raw_connect(machine['host'], machine['port'])
                    if sock:
                        folders, _ = focas_raw_list_dir(sock, "/")
                        focas_raw_disconnect(sock)
                        if folders:
                            browser = CFCardBrowserDialog(self.root, self, machine)
                            self.root.wait_window(browser.top)
                            if browser.selected_path:
                                dest_folder = browser.selected_path
                            else:
                                self.progress_label.configure(text=self.t("status_transfer_canceled"))
                                return
                except Exception:
                    pass

            selected_dest = dest_folder

            def focas_transfer():
                try:
                    self.root.after(0, lambda: self.progress_label.configure(
                        text=self.t("status_connecting_cf_text")))

                    with open(self.current_file, 'rb') as f:
                        file_data = f.read()

                    def progress_callback(sent, total):
                        if total > 0:
                            progress = (sent / total) * 100
                            self.root.after(0, lambda: self.progress_bar.set(progress / 100.0))
                            self.root.after(0, lambda: self.progress_label.configure(
                                text=self.t("status_sending_cf_text_progress").format(
                                    progress=progress,
                                    sent=self.format_size(sent),
                                    total=self.format_size(total),
                                )))

                    sock = focas_raw_connect(machine['host'], machine['port'])
                    if not sock:
                        raise Exception(self.t("msg_focas_connection_failed"))

                    try:
                        # Tam dosya yolunu oluştur
                        if selected_dest and selected_dest != "/":
                            full_path = f"{selected_dest}/{filename}".lstrip("/")
                        else:
                            full_path = filename

                        try:
                            focas_raw_write_file(sock, full_path, file_data, progress_callback)
                        except FileExistsError:
                            # Dosya var - kullanıcıya sor
                            result = {'confirmed': None}
                            def ask():
                                result['confirmed'] = messagebox.askyesno(
                                    self.t("title_file_exists_short"),
                                    self.t("msg_cf_text_file_exists_overwrite").format(filename=full_path),
                                )
                            self.root.after(0, ask)
                            while result['confirmed'] is None:
                                time.sleep(0.1)
                            if not result['confirmed']:
                                self.root.after(0, lambda: self.progress_label.configure(text=self.t("status_transfer_canceled")))
                                return
                            # Overwrite modu ile yaz
                            focas_raw_write_file(sock, full_path, file_data, progress_callback, overwrite=True)
                    finally:
                        focas_raw_disconnect(sock)

                    self.root.after(0, lambda: self.progress_bar.set(1.0))
                    self.root.after(0, lambda: self.progress_label.configure(text=self.t("status_transfer_complete_cf_text")))
                    self.root.after(0, lambda: messagebox.showinfo(
                        self.t("title_success"),
                        self.t("msg_transfer_complete_cf_text").format(filename=filename, machine_name=machine["name"]),
                    ))
                    self.log_transfer(machine['name'], filename, "BAŞARILI", f"Hedef: CF_TEXT ({selected_dest})")

                except Exception as e:
                    error_msg = str(e)
                    self.root.after(0, lambda: messagebox.showerror(
                        self.t("title_error"),
                        self.t("msg_focas_transfer_error").format(error=error_msg),
                    ))
                    self.root.after(0, lambda: self.progress_label.configure(text=""))
                    self.log_transfer(machine['name'], filename, "HATA", error_msg)

            threading.Thread(target=focas_transfer, daemon=True).start()
        else:
            def transfer():
                try:
                    # FTP bağlantısı
                    ftp = ftplib.FTP()
                    ftp.connect(machine['host'], machine['port'], timeout=10)
                    ftp.login(machine['user'], machine['password'])

                    # Dizine geç
                    ftp.cwd(machine['directory'])

                    # Dosya var mı kontrol et
                    files = []
                    ftp.retrlines('NLST', files.append)

                    if filename in files:
                        # GUI thread'inde dialog göster
                        self.root.after(0, lambda: self.confirm_overwrite(ftp, machine, filename))
                    else:
                        # Dosyayı gönder
                        self.upload_file(ftp, machine, filename)

                except Exception as e:
                    self.root.after(0, lambda: messagebox.showerror(
                        self.t("title_error"),
                        self.t("msg_transfer_error").format(error=str(e)),
                    ))
                    self.root.after(0, lambda: self.progress_label.configure(text=""))

            # Arka planda çalıştır
            threading.Thread(target=transfer, daemon=True).start()
    
    def confirm_overwrite(self, ftp, machine, filename):
        """Üzerine yazma onayı"""
        if messagebox.askyesno(
            self.t("title_file_exists"),
            self.t("msg_ftp_file_exists_overwrite").format(filename=filename),
        ):
            # Dosyayı gönder
            threading.Thread(target=lambda: self.upload_file(ftp, machine, filename), 
                           daemon=True).start()
        else:
            ftp.quit()
            self.progress_label.configure(text=self.t("status_canceled_short"))
    
    def log_transfer(self, machine_name, filename, status, message=""):
        """Transfer işlemini logla"""
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            log_entry = f"[{timestamp}] {status}: {filename} -> {machine_name} | {message}\n"
            
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(log_entry)
        except Exception as e:
            print(f"Loglama hatası: {e}")

    def upload_file(self, ftp, machine, filename):
        """Dosyayı yükle"""
        try:
            file_size = os.path.getsize(self.current_file)
            uploaded = 0
            
            def callback(data):
                nonlocal uploaded
                uploaded += len(data)
                progress = (uploaded / file_size) * 100
                self.root.after(0, lambda: self.progress_bar.set(progress / 100.0))
                self.root.after(0, lambda: self.progress_label.configure(
                    text=self.t("status_sending_ftp_progress").format(
                        progress=progress,
                        sent=self.format_size(uploaded),
                        total=self.format_size(file_size),
                    )))
            
            # Dosyayı gönder
            with open(self.current_file, 'rb') as f:
                ftp.storbinary(f'STOR {filename}', f, callback=callback)
            
            ftp.quit()
            
            self.root.after(0, lambda: self.progress_bar.set(1.0))
            self.root.after(0, lambda: self.progress_label.configure(text=self.t("status_transfer_complete")))
            self.root.after(0, lambda: messagebox.showinfo(
                self.t("title_success"),
                self.t("msg_transfer_complete_ftp").format(filename=filename, machine_name=machine["name"]),
            ))
            
            # Başarılı logu
            self.log_transfer(machine['name'], filename, "BAŞARILI")
            
        except Exception as e:
            error_msg = str(e)
            self.root.after(0, lambda: messagebox.showerror(
                self.t("title_error"),
                self.t("msg_upload_error").format(error=error_msg),
            ))
            self.root.after(0, lambda: self.progress_label.configure(text=""))
            
            # Hata logu
            self.log_transfer(machine['name'], filename, "HATA", error_msg)
    
    def format_size(self, size):
        """Dosya boyutunu formatla"""
        for unit in ['B', 'KB', 'MB', 'GB']:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"

class CFCardBrowserDialog:
    def __init__(self, parent, app, machine):
        self.parent = parent
        self.app = app
        self.machine = machine
        self.current_path = "/"
        self.selected_path = ""

        self.top = ctk.CTkToplevel(parent, fg_color=app.colors['bg'])
        self.top.title(f"CF Card Browser - {machine['name']}")
        self.top.geometry("500x400")
        self.top.transient(parent)
        self.top.grab_set()

        self.path_label = ctk.CTkLabel(self.top, text=self.current_path,
                                       font=ctk.CTkFont(family="Consolas", size=12))
        self.path_label.pack(pady=5)

        self.listbox = tk.Listbox(self.top, height=15,
                                     bg=app.colors['card_bg'], fg=app.colors['text'],
                                     font=('Segoe UI', 10),
                                     selectbackground=app.colors['primary'],
                                     selectforeground='#ffffff',
                                     relief='flat', bd=0, highlightthickness=0)
        self.listbox.pack(fill='both', expand=True, padx=10)
        self.listbox.bind('<Double-Button-1>', self.on_double_click)

        button_frame = ctk.CTkFrame(self.top, fg_color="transparent")
        button_frame.pack(pady=10)
        
        ctk.CTkButton(button_frame, text="Up", command=self.go_up).pack(side='left', padx=5)
        ctk.CTkButton(button_frame, text="Delete", command=self.delete_selected).pack(side='left', padx=5)
        ctk.CTkButton(button_frame, text="Select Folder", command=self.select_folder).pack(side='left', padx=5)
        ctk.CTkButton(button_frame, text="Cancel", command=self.top.destroy).pack(side='left', padx=5)

        self.refresh_list()

    def refresh_list(self):
        self.listbox.delete(0, tk.END)
        self.path_label.configure(text=self.current_path)

        try:
            sock = focas_raw_connect(self.machine['host'], self.machine['port'])
            if not sock:
                messagebox.showerror("Error", "Could not connect to machine.")
                self.top.destroy()
                return

            try:
                folders, files = focas_raw_list_dir(sock, self.current_path)
                for folder in folders:
                    self.listbox.insert(tk.END, f"📁 {folder}")
                for file in files:
                    self.listbox.insert(tk.END, f"📄 {file}")
            finally:
                focas_raw_disconnect(sock)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to list directory: {e}")
            self.top.destroy()
    
    def go_up(self):
        if self.current_path != "/":
            self.current_path = "/".join(self.current_path.split('/')[:-2]) + "/"
            if self.current_path == "//":
                self.current_path = "/"
            self.refresh_list()
            
    def on_double_click(self, event):
        selection = self.listbox.curselection()
        if not selection:
            return
        
        item = self.listbox.get(selection[0])
        if item.startswith("📁"):
            folder_name = item.split(" ", 1)[1]
            if self.current_path == "/":
                self.current_path += folder_name + "/"
            else:
                self.current_path += folder_name + "/"
            self.refresh_list()

    def delete_selected(self):
        selection = self.listbox.curselection()
        if not selection:
            return

        item = self.listbox.get(selection[0])
        if not item.startswith("📄"):
            messagebox.showinfo("Info", "Only files can be deleted.")
            return

        file_name = item.split(" ", 1)[1]
        full_path = f"{self.current_path}{file_name}".replace("//", "/")
        
        if not messagebox.askyesno("Confirm", f"Are you sure you want to delete {full_path}?"):
            return

        try:
            sock = focas_raw_connect(self.machine['host'], self.machine['port'])
            if not sock:
                messagebox.showerror("Error", "Could not connect to machine.")
                return

            try:
                if focas_raw_delete_file(sock, full_path):
                    messagebox.showinfo("Success", f"{full_path} deleted successfully.")
                    self.refresh_list()
                else:
                    messagebox.showerror("Error", f"Failed to delete {full_path}.")
            finally:
                focas_raw_disconnect(sock)
        except Exception as e:
            messagebox.showerror("Error", f"Failed to delete file: {e}")

    def select_folder(self):
        self.selected_path = self.current_path
        self.top.destroy()


def main():
    root = ctk.CTk()
    app = CNCTransferApp(root)

    def periodic_check():
        if app.auto_check_var.get():
            app.check_machine_status()
        root.after(30000, periodic_check)

    periodic_check()
    root.mainloop()

if __name__ == "__main__":
    main()
