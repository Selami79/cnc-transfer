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

class CNCTransferApp:
    def __init__(self, root):
        self.root = root

        # CustomTkinter ayarları
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")
        # Chrome/Material tarzı tipografi
        try:
            ctk.ThemeManager.theme["CTkFont"]["family"] = "Segoe UI"
        except Exception:
            pass

        self.root.title("CNC Transfer")
        self.root.geometry("1000x700")
        self.root.minsize(860, 580)

        self.app_dir = os.path.dirname(os.path.abspath(__file__))
        self.config_file = os.path.join(self.app_dir, "machines.json")
        self.log_file = os.path.join(self.app_dir, "transfer_history.txt")
        self.machines = self.load_machines()

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

        self.notebook.add("Transfer")
        self.notebook.add("Makineler")
        self.notebook.add("Yeni Makine")
        self.notebook.add("Ayarlar")
        self.notebook.set("Transfer")

        self.create_transfer_tab()
        self.create_machines_tab()
        self.create_add_machine_tab()
        self.create_settings_tab()

        self.check_machine_status()

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
                messagebox.showerror("Veri Hatası", f"Yapılandırma dosyası ({self.config_file}) bozuk veya okunamaz durumda!\n\nVeri kaybını önlemek için uygulama kapatılacak.")
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
        tab = self.notebook.tab("Transfer")
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

        ctk.CTkButton(file_inner, text="Dosya Sec", width=120, height=36,
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
            self.file_label.configure(text="Dosya secilmedi", text_color=self.colors['text_dim'])
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

        self.progress_label = ctk.CTkLabel(progress_frame, text="Hazir",
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

        proto_map = {'ftp': 'FTP', 'focas': 'FOCAS  CF_TEXT', 'focas_mem': 'FOCAS  CNC BELLEK'}

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

            status_text = "ONLINE" if online else "OFFLINE"
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
            ctk.CTkButton(card, text="GONDER", height=48,
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
                    ctk.CTkButton(sub, text="Sil + Gonder", height=32,
                                 corner_radius=6,
                                 font=ctk.CTkFont(size=12, weight="bold"),
                                 fg_color=self.colors['danger'] if online else self.colors['secondary'],
                                 hover_color=self.colors['danger_hover'] if online else self.colors['secondary'],
                                 text_color="#ffffff",
                                 text_color_disabled=self.colors['text_dim'],
                                 state="normal" if online else "disabled",
                                 command=lambda m=machine: self.focas_mem_delete_dialog(m)
                                 ).pack(side='left', fill='x', expand=True, padx=(0, 3))

                ctk.CTkButton(sub, text="Yedekle", height=32,
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
        tab = self.notebook.tab("Makineler")
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
        self.machines_tree.heading('#0', text='Makine')
        self.machines_tree.column('#0', width=160, minwidth=100)
        widths = {'IP': 140, 'Port': 70, 'Protokol': 130, 'Durum': 90}
        for c in columns:
            self.machines_tree.heading(c, text=c)
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
            ("Yenile", self.colors['primary'], self.colors['primary_hover'], "#ffffff", self.refresh_machines_list),
            ("Duzenle", self.colors['warning'], self.colors['warning_hover'], self.colors['text'], self.edit_machine),
            ("Sil", self.colors['danger'], self.colors['danger_hover'], "#ffffff", self.delete_machine),
        ]:
            ctk.CTkButton(btn_inner, text=text, width=100, height=32,
                         corner_radius=6, fg_color=color, hover_color=hover,
                         text_color=text_color, text_color_disabled=self.colors['text_dim'],
                         font=ctk.CTkFont(size=12, weight="bold"),
                         command=cmd).pack(side='left', padx=(0, 6))

        self.refresh_machines_list()
    
    def create_add_machine_tab(self):
        """Makine ekleme sekmesi"""
        tab = self.notebook.tab("Yeni Makine")
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
            ("Makine Adi", "name_entry", ""),
            ("IP Adresi", "host_entry", ""),
            ("Port", "port_entry", "21"),
            ("Kullanici", "user_entry", "anonymous"),
            ("Sifre", "password_entry", "")
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
        ctk.CTkLabel(form_inner, text="Protokol",
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

        ctk.CTkButton(form_inner, text="Baglan", height=38, corner_radius=6,
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

        self.test_button = ctk.CTkButton(btn_row, text="Test", width=90, height=32,
                                        corner_radius=6,
                                        fg_color=self.colors['secondary'], hover_color=self.colors['secondary'],
                                        text_color=self.colors['text'],
                                        text_color_disabled=self.colors['text_dim'],
                                        font=ctk.CTkFont(size=12, weight="bold"),
                                        state="disabled",
                                        command=self.test_connection)
        self.test_button.pack(side='left', padx=(0, 6))

        self.save_button = ctk.CTkButton(btn_row, text="Kaydet", width=90, height=32,
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
        tab = self.notebook.tab("Ayarlar")
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

        ctk.CTkLabel(card_inner, text="Genel Ayarlar",
                    font=ctk.CTkFont(size=14, weight="bold")).pack(anchor='w', pady=(0, 10))

        self.auto_check_cb = ctk.CTkCheckBox(card_inner,
                                            text="Otomatik durum kontrolu (30 sn)",
                                            variable=self.auto_check_var,
                                            font=ctk.CTkFont(size=12),
                                            corner_radius=4)
        self.auto_check_cb.pack(anchor='w')
        self.theme_var = tk.StringVar(value="dark")

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
        # CTkTabview: sekmeleri sil ve yeniden ekle
        for tab_name in ["Transfer", "Makineler", "Yeni Makine", "Ayarlar"]:
            try:
                self.notebook.delete(tab_name)
            except:
                pass

        self.notebook.add("Transfer")
        self.notebook.add("Makineler")
        self.notebook.add("Yeni Makine")
        self.notebook.add("Ayarlar")

        self.create_transfer_tab()
        self.create_machines_tab()
        self.create_add_machine_tab()
        self.create_settings_tab()

        self.notebook.set("Ayarlar")
        self.refresh_machines_list()
    
    def select_file(self):
        """Dosya seçim dialogu"""
        filename = filedialog.askopenfilename(
            title="NC Dosyası Seç",
            filetypes=[("NC Dosyaları", "*.nc"), ("Tüm Dosyalar", "*.*")]
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
            return ["(CF kart erişimi aktif)"]
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
            raise Exception("FOCAS DLL yüklenemedi!")

        if target_path is None:
            target_path = b'//CNC_MEM/USER/PATH1/'

        handle = ctypes.c_ushort(0)
        ip = machine['host'].encode('ascii')
        port = ctypes.c_ushort(machine['port'])

        ret = FOCAS_DLL.cnc_allclibhndl3(ip, port, ctypes.c_long(10), ctypes.byref(handle))
        if ret != 0:
            raise Exception(f"FOCAS bağlantı hatası (kod: {ret})")

        try:
            # Path-based transfer başlat (cnc_dwnstart4)
            cnc_path = ctypes.create_string_buffer(target_path, 256)
            ret = FOCAS_DLL.cnc_dwnstart4(handle, ctypes.c_short(0), cnc_path)
            if ret != 0:
                raise Exception(f"FOCAS transfer başlatma hatası (kod: {ret})")

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
                    raise Exception("Dosya içeriği boş!")

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
                        raise Exception(f"FOCAS veri gönderme hatası (kod: {ret})")

                    sent += buf_len.value
                    if progress_callback:
                        progress_callback(sent, file_size)

            finally:
                time.sleep(0.5)
                ret = FOCAS_DLL.cnc_dwnend4(handle)
                if ret == 15:
                    raise Exception("CNC belleği dolu! Eski programları silip tekrar deneyin.")
                elif ret == 5:
                    raise Exception("CNC programı kaydedemedi (aynı isim veya O numarası çakışması).")
                elif ret != 0:
                    raise Exception(f"FOCAS transfer sonlandırma hatası (kod: {ret})")
        finally:
            FOCAS_DLL.cnc_freelibhndl(handle)

    def focas_mem_upload_file(self, machine, filepath, filename, progress_callback=None, force_o_number=None):
        """FOCAS ile dosyayı CNC dahili belleğine transfer et (dwnstart3/download3 API)
        Torna için - O numarası formatında çalışır.
        force_o_number: Belirtilirse dosya içeriğindeki O numarasını bu değerle değiştirir.
        """
        if FOCAS_DLL is None:
            raise Exception("FOCAS DLL yüklenemedi!")

        handle = ctypes.c_ushort(0)
        ip = machine['host'].encode('ascii')
        port = ctypes.c_ushort(machine['port'])

        ret = FOCAS_DLL.cnc_allclibhndl3(ip, port, ctypes.c_long(10), ctypes.byref(handle))
        if ret != 0:
            raise Exception(f"FOCAS bağlantı hatası (kod: {ret})")

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
                raise Exception("Dosya içeriği boş!")

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
                raise Exception(f"CNC bellek transfer başlatma hatası (kod: {ret})")

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
                        raise Exception(f"CNC bellek veri gönderme hatası (kod: {ret})")

                    sent += buf_len.value
                    if progress_callback:
                        progress_callback(sent, file_size)

            finally:
                time.sleep(0.5)
                ret = FOCAS_DLL.cnc_dwnend3(handle)
                if ret == 15:
                    raise Exception("CNC belleği dolu! Eski programları silip tekrar deneyin.")
                elif ret == 5:
                    raise Exception("CNC programı kaydedemedi (O numarası çakışması).")
                elif ret != 0:
                    raise Exception(f"CNC bellek transfer sonlandırma hatası (kod: {ret})")
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
            messagebox.showerror("Hata", f"{machine['name']} makinesine bağlanılamıyor!")
            return

        dialog = ctk.CTkToplevel(self.root, fg_color=self.colors['bg'])
        dialog.title(f"{machine['name']} - Program Sil")
        dialog.geometry("380x220")
        dialog.transient(self.root)
        dialog.grab_set()

        ctk.CTkLabel(dialog, text="Silinecek O Numarası:",
                    font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(24, 4))

        ctk.CTkLabel(dialog, text="Örn: 11, 100, 1234",
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
                messagebox.showerror("Hata", "Geçerli bir O numarası girin!", parent=dialog)
                return
            o_num = int(o_text)
            if o_num >= 500:
                if not messagebox.askyesno("Uyarı",
                    f"O{o_num:04d} numaralı programı silmek istediğinize emin misiniz?\n\n"
                    f"Bu numara 500 ve üzerinde!", parent=dialog):
                    return
            else:
                if not messagebox.askyesno("Onay",
                    f"O{o_num:04d} numaralı programı silmek istiyor musunuz?", parent=dialog):
                    return

            if FOCAS_DLL is None:
                messagebox.showerror("Hata", "FOCAS DLL yüklenemedi!", parent=dialog)
                return

            handle = ctypes.c_ushort(0)
            ip = machine['host'].encode('ascii')
            ret = FOCAS_DLL.cnc_allclibhndl3(ip, ctypes.c_ushort(machine['port']),
                                              ctypes.c_long(10), ctypes.byref(handle))
            if ret != 0:
                messagebox.showerror("Hata", f"FOCAS bağlantı hatası (kod: {ret})", parent=dialog)
                return

            ret = FOCAS_DLL.cnc_delete(handle, ctypes.c_short(o_num))
            FOCAS_DLL.cnc_freelibhndl(handle)

            if ret == 0:
                if not self.current_file:
                    messagebox.showinfo("Başarılı", f"O{o_num:04d} silindi.\nTransfer için dosya seçili değil.", parent=dialog)
                    dialog.destroy()
                    return
                dialog.destroy()
                # Silinen numaraya dosyayı transfer et
                self.focas_mem_delete_and_transfer(machine, o_num)
            else:
                messagebox.showerror("Hata", f"O{o_num:04d} silinemedi (kod: {ret})", parent=dialog)

        o_entry.bind('<Return>', lambda e: do_delete())

        btn_frame = ctk.CTkFrame(dialog, fg_color="transparent")
        btn_frame.pack(pady=15)

        ctk.CTkButton(btn_frame, text="Sil + Gonder", command=do_delete,
                     width=120, height=36, corner_radius=6,
                     fg_color=self.colors['danger'], hover_color=self.colors['danger_hover'],
                     text_color="#ffffff",
                     font=ctk.CTkFont(size=13, weight="bold")).pack(side='left', padx=5)

        ctk.CTkButton(btn_frame, text="Iptal", command=dialog.destroy,
                     width=100, height=36, corner_radius=6,
                     fg_color=self.colors['secondary'], hover_color=self.colors['secondary_hover'],
                     text_color=self.colors['text'],
                     font=ctk.CTkFont(size=12)).pack(side='left', padx=5)

    def focas_mem_delete_and_transfer(self, machine, o_num):
        """Silinen O numarasına seçili dosyayı transfer et"""
        filename = os.path.basename(self.current_file)
        if filename.startswith('~') and filename.lower().endswith('.tmx') and self.cimco_display_name:
            filename = self.cimco_display_name

        self.progress_label.configure(text=f"O{o_num:04d} olarak transfer ediliyor...")
        self.progress_bar.set(0)

        def transfer():
            try:
                def progress_callback(sent, total):
                    progress = (sent / total) * 100
                    self.root.after(0, lambda: self.progress_bar.set(progress / 100.0))
                    self.root.after(0, lambda: self.progress_label.configure(
                        text=f"O{o_num:04d} olarak gönderiliyor: {progress:.1f}%"))

                self.focas_mem_upload_file(machine, self.current_file, filename,
                                          progress_callback, force_o_number=o_num)

                self.root.after(0, lambda: self.progress_bar.set(1.0))
                self.root.after(0, lambda: self.progress_label.configure(text="Transfer tamamlandı! (CNC Bellek)"))
                self.root.after(0, lambda: messagebox.showinfo("Başarılı",
                    f"{filename} → O{o_num:04d} olarak {machine['name']} makinesine gönderildi!"))
                self.log_transfer(machine['name'], filename, "BAŞARILI", f"O{o_num:04d} olarak CNC Bellek")

            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda: messagebox.showerror("Hata", f"Transfer hatası: {error_msg}"))
                self.root.after(0, lambda: self.progress_label.configure(text=""))
                self.log_transfer(machine['name'], filename, "HATA", error_msg)

        threading.Thread(target=transfer, daemon=True).start()

    def focas_backup(self, machine):
        """FOCAS ile CNC yedekleme (parametre, PLC, makro, offset, programlar)"""
        if machine['status'] != 'online':
            messagebox.showerror("Hata", f"{machine['name']} makinesine bağlanılamıyor!")
            return

        backup_dir = filedialog.askdirectory(title="Yedek Klasörü Seçin")
        if not backup_dir:
            return

        self.progress_label.configure(text="CNC yedekleme başlatılıyor...")
        self.progress_bar.set(0)

        def do_backup():
            try:
                handle = ctypes.c_ushort(0)
                ip = machine['host'].encode('ascii')
                port = ctypes.c_ushort(machine['port'])
                ret = FOCAS_DLL.cnc_allclibhndl3(ip, port, ctypes.c_long(10), ctypes.byref(handle))
                if ret != 0:
                    raise Exception(f"FOCAS bağlantı hatası (kod: {ret})")

                try:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    save_dir = os.path.join(backup_dir, f"{machine['name']}_yedek_{timestamp}")
                    os.makedirs(save_dir, exist_ok=True)

                    backup_types = [
                        (1, "PARAMETRE", "parametre.prm"),
                        (2, "PITCH_ERROR", "pitch_error.pit"),
                        (3, "MAKRO_DEGISKEN", "makro_degisken.mac"),
                        (4, "WORK_OFFSET", "work_offset.wof"),
                    ]

                    total_steps = len(backup_types) + 1
                    done = 0

                    for data_type, label, bkp_filename in backup_types:
                        self.root.after(0, lambda l=label: self.progress_label.configure(
                            text=f"Yedekleniyor: {l}..."))
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
                        text="Yedekleniyor: NC PROGRAMLAR..."))
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
                    self.root.after(0, lambda: self.progress_label.configure(text="Yedekleme tamamlandı!"))
                    self.root.after(0, lambda c=prog_count: messagebox.showinfo("Başarılı",
                        f"{machine['name']} yedekleme tamamlandı!\n\nKonum: {save_dir}\n\n"
                        f"Parametre, Pitch Error, Makro Değişken,\n"
                        f"Work Offset ve {c} NC program yedeklendi."))

                    self.log_transfer(machine['name'], "CNC_YEDEK", "BAŞARILI", save_dir)

                finally:
                    FOCAS_DLL.cnc_freelibhndl(handle)

            except Exception as e:
                error_msg = str(e)
                self.root.after(0, lambda: messagebox.showerror("Hata", f"Yedekleme hatası: {error_msg}"))
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

        proto_map = {'ftp': 'FTP', 'focas': 'CF_TEXT', 'focas_mem': 'CNC BELLEK'}
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
            messagebox.showwarning("Uyarı", "Lütfen bir makine seçin!")
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
        self.selected_dir_label.configure(text=f"Seçili dizin: {machine['directory']}")
        
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
            text="Güncelle",
            state='normal',
            command=lambda: self.update_machine(old_name),
            fg_color=self.colors['success'],
            hover_color=self.colors['success_hover'],
            text_color="#ffffff",
        )
        
        # Sekmeyi değiştir
        self.notebook.set("Yeni Makine")
        
    def delete_machine(self):
        """Seçili makineyi sil"""
        selection = self.machines_tree.selection()
        if not selection:
            messagebox.showwarning("Uyarı", "Lütfen bir makine seçin!")
            return
        
        # Onay al
        item = self.machines_tree.item(selection[0])
        machine_name = item['text']
        
        if messagebox.askyesno("Onay", f"{machine_name} makinesini silmek istediğinize emin misiniz?"):
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
                messagebox.showerror("Hata", "IP adresi boş olamaz!")
                return

            if protocol in ('focas', 'focas_mem'):
                test_machine = {'host': host, 'port': port}
                if self.focas_check_status(test_machine):
                    self.dir_listbox.delete(0, tk.END)
                    # CNC dahili bellek programları
                    programs = self.focas_get_program_list(test_machine)
                    self.dir_listbox.insert(tk.END, f"--- CNC Bellek ({len(programs)} program) ---")
                    for prog in programs[:30]:
                        self.dir_listbox.insert(tk.END, f"  {prog}")
                    if protocol == 'focas':
                        # CF_TEXT programları (raw TCP ile) - sadece CF_TEXT modu için
                        cf_programs = self.focas_list_cf_programs(test_machine)
                        self.dir_listbox.insert(tk.END, f"--- CF_TEXT ({len(cf_programs)} program) ---")
                        for prog in cf_programs[:30]:
                            self.dir_listbox.insert(tk.END, f"  {prog}")
                    self.dir_listbox.select_set(0)
                    target = "CF_TEXT" if protocol == 'focas' else "CNC Bellek"
                    self.selected_dir_label.configure(text=f"Hedef: {target}")
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
                    messagebox.showinfo("Başarılı",
                        f"FOCAS bağlantısı başarılı!\n"
                        f"CNC Bellek: {len(programs)} program\n"
                        f"Hedef: {target}")
                else:
                    messagebox.showerror("Hata", "FOCAS bağlantısı kurulamadı!")
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

            messagebox.showinfo("Başarılı", "Bağlantı başarılı! Lütfen bir dizin seçin.")

        except Exception as e:
            messagebox.showerror("Hata", f"Bağlantı hatası: {str(e)}")

    def on_dir_select(self, event=None):
        """Dizin seçildiğinde"""
        selection = self.dir_listbox.curselection()
        if selection:
            selected_dir = self.dir_listbox.get(selection[0])
            self.selected_dir_label.configure(text=f"Seçili dizin: {selected_dir}")
            self.test_button.configure(
                state='normal',
                fg_color=self.colors['warning'],
                hover_color=self.colors['warning_hover'],
                text_color=self.colors['text'],
            )
            # Eğer güncelleme modundaysa güncelle butonunu aktif tut, değilse kaydet butonunu aktif et
            if self.save_button['text'] == "Güncelle":
                 self.save_button.configure(
                     state='normal',
                     fg_color=self.colors['success'],
                     hover_color=self.colors['success_hover'],
                     text_color="#ffffff",
                 )
            else:
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
                    messagebox.showinfo("Başarılı", "FOCAS bağlantı testi başarılı!")
                    self.save_button.configure(
                        state='normal',
                        fg_color=self.colors['success'],
                        hover_color=self.colors['success_hover'],
                        text_color="#ffffff",
                    )
                else:
                    messagebox.showerror("Hata", "FOCAS bağlantı testi başarısız!")
                return

            selection = self.dir_listbox.curselection()
            if not selection:
                # Seçili değilse ama label doluysa (düzenleme modu)
                current_text = self.selected_dir_label.cget("text")
                if "Seçili dizin: " in current_text and len(current_text) > 14:
                    selected_dir = current_text.replace("Seçili dizin: ", "")
                else:
                    messagebox.showwarning("Uyarı", "Lütfen bir dizin seçin!")
                    return
            else:
                selected_dir = self.dir_listbox.get(selection[0])

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

            messagebox.showinfo("Başarılı", "Test başarılı!")
            self.save_button.configure(
                state='normal',
                fg_color=self.colors['success'],
                hover_color=self.colors['success_hover'],
                text_color="#ffffff",
            )

        except Exception as e:
            messagebox.showerror("Hata", f"Test başarısız: {str(e)}")

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
                 current_text = self.selected_dir_label.cget("text")
                 directory = current_text.replace("Seçili dizin: ", "")

            if not name:
                messagebox.showerror("Hata", "Makine adı boş olamaz!")
                return

            # İsim değiştiyse ve yeni isim başka makinede varsa uyar
            if name != old_name and any(m['name'] == name for m in self.machines['machines']):
                messagebox.showerror("Hata", "Bu isimde başka bir makine zaten var!")
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
            
            messagebox.showinfo("Başarılı", f"{name} başarıyla güncellendi!")
            
            # Transfer sekmesine dön
            self.notebook.set("Transfer")
            
        except Exception as e:
            messagebox.showerror("Hata", f"Güncelleme hatası: {str(e)}")

    def reset_form(self):
        """Formu temizle ve varsayılan ayarlara dön"""
        for entry in self.add_form_vars.values():
            if isinstance(entry, (tk.Entry, ctk.CTkEntry)):
                entry.delete(0, tk.END)
        
        self.add_form_vars['port_entry'].insert(0, "21")
        self.add_form_vars['user_entry'].insert(0, "anonymous")
        self.add_form_vars['protocol_combo'].set("FTP")

        self.dir_listbox.delete(0, tk.END)
        self.selected_dir_label.configure(text="Seçili dizin: ")
        self.test_button.configure(
            state='disabled',
            fg_color=self.colors['secondary'],
            hover_color=self.colors['secondary'],
            text_color=self.colors['text'],
        )
        
        # Kaydet butonunu eski haline getir
        self.save_button.configure(
            text="Kaydet",
            command=self.save_new_machine,
            state='disabled',
            fg_color=self.colors['secondary'],
            hover_color=self.colors['secondary'],
            text_color=self.colors['text'],
        )
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
                messagebox.showwarning("Uyarı", "Lütfen bir dizin seçin!")
                return
            
            directory = self.dir_listbox.get(selection[0])
            
            if not name:
                messagebox.showerror("Hata", "Makine adı boş olamaz!")
                return
            
            # Makine zaten var mı kontrol et
            if any(m['name'] == name for m in self.machines['machines']):
                messagebox.showerror("Hata", "Bu isimde bir makine zaten var!")
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
            
            messagebox.showinfo("Başarılı", f"{name} başarıyla eklendi!")
            
            # Transfer sekmesine geç
            self.notebook.set("Transfer")
            
        except Exception as e:
            messagebox.showerror("Hata", f"Kaydetme hatası: {str(e)}")
    
    def transfer_file(self, machine):
        """Dosyayı seçilen makineye transfer et"""
        if not self.current_file:
            messagebox.showerror("Hata", "Lütfen bir dosya seçin!")
            return
        
        if machine['status'] != 'online':
            messagebox.showerror("Hata", f"{machine['name']} makinesine bağlanılamıyor!")
            return
        
        # Dosya adını al (CIMCO geçici dosya ise gerçek adı kullan)
        original_filename = os.path.basename(self.current_file)
        if original_filename.startswith('~') and original_filename.lower().endswith('.tmx') and self.cimco_display_name:
            original_filename = self.cimco_display_name
        protocol = machine.get('protocol', 'ftp')

        if protocol in ('focas', 'focas_mem'):
            self.start_transfer(machine, original_filename)
            return

        # FTP: dosya adı kontrolü
        normalized_filename = self.normalize_filename(original_filename)

        if original_filename != normalized_filename:
            dialog = ctk.CTkToplevel(self.root, fg_color=self.colors['bg'])
            dialog.title("Dosya Adi Duzeltme")
            dialog.geometry("440x230")
            dialog.transient(self.root)
            dialog.grab_set()

            ctk.CTkLabel(dialog, text="Dosya adi CNC uyumlu degil!",
                        font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(16, 6))

            ctk.CTkLabel(dialog, text=f"Orjinal: {original_filename}",
                        font=ctk.CTkFont(family="Consolas", size=12),
                        text_color=self.colors['text_muted']).pack(pady=4)

            ctk.CTkLabel(dialog, text="Duzeltilmis:",
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

            ctk.CTkButton(button_frame, text="Onayla", command=confirm,
                         width=110, height=36, corner_radius=6,
                         fg_color=self.colors['primary'], hover_color=self.colors['primary_hover'],
                         text_color="#ffffff",
                         font=ctk.CTkFont(size=13, weight="bold")).pack(side='left', padx=5)
            ctk.CTkButton(button_frame, text="Iptal", command=cancel,
                         width=110, height=36, corner_radius=6,
                         fg_color=self.colors['secondary'], hover_color=self.colors['secondary_hover'],
                         text_color=self.colors['text'],
                         font=ctk.CTkFont(size=12)).pack(side='left', padx=5)

            dialog.wait_window()

            if not result['confirmed']:
                return

            normalized_filename = result['filename']

        self.start_transfer(machine, normalized_filename)
    
    def start_transfer(self, machine, filename):
        """Transfer işlemini başlat"""
        self.progress_label.configure(text=f"{machine['name']} makinesine bağlanılıyor...")
        self.progress_bar.set(0)
        protocol = machine.get('protocol', 'ftp')

        if protocol == 'focas_mem':
            # CNC dahili belleğe DLL ile transfer (Torna)
            def focas_mem_transfer():
                try:
                    self.root.after(0, lambda: self.progress_label.configure(
                        text="CNC belleğine bağlanılıyor..."))

                    def progress_callback(sent, total):
                        progress = (sent / total) * 100
                        self.root.after(0, lambda: self.progress_bar.set(progress / 100.0))
                        self.root.after(0, lambda: self.progress_label.configure(
                            text=f"CNC belleğine gönderiliyor: {progress:.1f}% ({self.format_size(sent)}/{self.format_size(total)})"))

                    self.focas_mem_upload_file(machine, self.current_file, filename, progress_callback)

                    self.root.after(0, lambda: self.progress_bar.set(1.0))
                    self.root.after(0, lambda: self.progress_label.configure(text="Transfer tamamlandı! (CNC Bellek)"))
                    self.root.after(0, lambda: messagebox.showinfo("Başarılı",
                        f"{filename} dosyası {machine['name']} makinesine (CNC Bellek) başarıyla gönderildi!"))
                    self.log_transfer(machine['name'], filename, "BAŞARILI", "Hedef: CNC Bellek")

                except Exception as e:
                    error_msg = str(e)
                    self.root.after(0, lambda: messagebox.showerror("Hata", f"FOCAS transfer hatası: {error_msg}"))
                    self.root.after(0, lambda: self.progress_label.configure(text=""))
                    self.log_transfer(machine['name'], filename, "HATA", error_msg)

            threading.Thread(target=focas_mem_transfer, daemon=True).start()
        elif protocol == 'focas':
            def focas_transfer():
                try:
                    self.root.after(0, lambda: self.progress_label.configure(
                        text="CF_TEXT'e bağlanılıyor..."))

                    with open(self.current_file, 'rb') as f:
                        file_data = f.read()

                    def progress_callback(sent, total):
                        progress = (sent / total) * 100
                        self.root.after(0, lambda: self.progress_bar.set(progress / 100.0))
                        self.root.after(0, lambda: self.progress_label.configure(
                            text=f"CF_TEXT'e gönderiliyor: {progress:.1f}% ({self.format_size(sent)}/{self.format_size(total)})"))

                    sock = focas_raw_connect(machine['host'], machine['port'])
                    if not sock:
                        raise Exception("FOCAS bağlantısı kurulamadı!")
                    try:
                        try:
                            focas_raw_write_file(sock, filename, file_data, progress_callback)
                        except FileExistsError:
                            # Dosya var - kullanıcıya sor
                            result = {'confirmed': None}
                            def ask():
                                result['confirmed'] = messagebox.askyesno(
                                    "Dosya Mevcut",
                                    f"'{filename}' CF_TEXT'te zaten var!\n\nÜzerine yazmak istiyor musunuz?")
                            self.root.after(0, ask)
                            while result['confirmed'] is None:
                                time.sleep(0.1)
                            if not result['confirmed']:
                                self.root.after(0, lambda: self.progress_label.configure(text="Transfer iptal edildi."))
                                return
                            # Overwrite modu ile yaz
                            focas_raw_write_file(sock, filename, file_data, progress_callback, overwrite=True)
                    finally:
                        focas_raw_disconnect(sock)

                    self.root.after(0, lambda: self.progress_bar.set(1.0))
                    self.root.after(0, lambda: self.progress_label.configure(text="Transfer tamamlandı! (CF_TEXT)"))
                    self.root.after(0, lambda: messagebox.showinfo("Başarılı",
                        f"{filename} dosyası {machine['name']} makinesine (CF_TEXT) başarıyla gönderildi!"))
                    self.log_transfer(machine['name'], filename, "BAŞARILI", "Hedef: CF_TEXT")

                except Exception as e:
                    error_msg = str(e)
                    self.root.after(0, lambda: messagebox.showerror("Hata", f"FOCAS transfer hatası: {error_msg}"))
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
                    self.root.after(0, lambda: messagebox.showerror("Hata", f"Transfer hatası: {str(e)}"))
                    self.root.after(0, lambda: self.progress_label.configure(text=""))

            # Arka planda çalıştır
            threading.Thread(target=transfer, daemon=True).start()
    
    def confirm_overwrite(self, ftp, machine, filename):
        """Üzerine yazma onayı"""
        if messagebox.askyesno("Dosya Zaten Var", 
                              f"{filename} dosyası sunucuda zaten mevcut.\n\nÜzerine yazmak istiyor musunuz?"):
            # Dosyayı gönder
            threading.Thread(target=lambda: self.upload_file(ftp, machine, filename), 
                           daemon=True).start()
        else:
            ftp.quit()
            self.progress_label.configure(text="İptal edildi")
    
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
                    text=f"Gönderiliyor: {progress:.1f}% ({self.format_size(uploaded)}/{self.format_size(file_size)})"))
            
            # Dosyayı gönder
            with open(self.current_file, 'rb') as f:
                ftp.storbinary(f'STOR {filename}', f, callback=callback)
            
            ftp.quit()
            
            self.root.after(0, lambda: self.progress_bar.set(1.0))
            self.root.after(0, lambda: self.progress_label.configure(text="Transfer tamamlandı!"))
            self.root.after(0, lambda: messagebox.showinfo("Başarılı", 
                                                          f"{filename} dosyası {machine['name']} makinesine başarıyla gönderildi!"))
            
            # Başarılı logu
            self.log_transfer(machine['name'], filename, "BAŞARILI")
            
        except Exception as e:
            error_msg = str(e)
            self.root.after(0, lambda: messagebox.showerror("Hata", f"Yükleme hatası: {error_msg}"))
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
